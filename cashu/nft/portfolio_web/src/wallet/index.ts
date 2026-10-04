import { MemoryRepositories, initializeCoco, type Manager } from '@cashu/coco-core';
import { onDeviceStorage } from '../storage.ts';
import { IndexedDbRepositories } from '@cashu/coco-indexeddb';
import type { Plugin } from '@cashu/coco-core/plugin';
import { hmac } from '@noble/hashes/hmac.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, concatBytes } from '@noble/hashes/utils.js';
import { profileKey, parseShowing } from '../crypto.mjs';
import { checked, signedRequest } from '../api.mjs';
import { uploadHeaders } from '../turnstile.mjs';
import { EncryptedVault, walletSeed, type Envelope } from './vault.ts';
import { ORDER, utf8, integer, boundPresentation, issueCommitment, finishIssue, finishBlindIssueV2, blindTransfer, finishBlind, hashAsset, nullifier, decodeToken, encodeToken, verifyCredential, publicCard, type MintConfig, type Credential, type Card } from './ps.ts';
import { splitImage, transferImage } from './image.ts';
import { imageUrl } from '../formats.mjs';

interface Prepared { id: string; h: string; title: string; jpg: string; card_id?: string | null; begin: { session: string; u: string; keyset_id: string } | null; legacy_token: string | null; }
interface ProofRequest { b: string; proof: string; version?: 1 | 2 | 3; session?: string; asset_tag?: string; owner_commitment?: string; presentation?: string; new_owner_commitment?: string; new_proof?: string; }
interface Job { id: string; h: string; s: string; t?: string; u?: string; cardId: string; request: ProofRequest; }
const fromBase64 = (s: string) => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const scope = (id: string, h: string) => `card:${id}:${h}`;
declare module '@cashu/coco-core/plugin' { interface PluginExtensions { nft: BrowserNFTWallet; } }

export class BrowserNFTWallet {
  readonly pubkey: string;
  private base: string;
  constructor(private secret: string, readonly config: MintConfig, private vault: EncryptedVault, private nextCounter: () => Promise<number>, private seed: Uint8Array) {
    this.pubkey = profileKey(secret);
    this.base = `/api/profiles/${this.pubkey}/wallet`;
  }
  private async post(path: string, body: object | Uint8Array = new Uint8Array(), headers: Record<string, string> = {}) {
    const binary = body instanceof Uint8Array;
    const bytes = binary ? Uint8Array.from(body as Uint8Array) : utf8(JSON.stringify(body));
    return (await signedRequest(this.secret, this.base + path, bytes, binary ? 'application/octet-stream' : 'application/json', '', headers)).json();
  }
  private async lock<T>(fn: () => Promise<T>): Promise<T> {
    if (!globalThis.navigator?.locks) throw new Error('This browser needs Web Locks to safely use the NFT wallet');
    return navigator.locks.request(`cashu-nft:${this.pubkey}:${this.config.keyset_id}`, fn);
  }
  private async ownerSecret(id: string) {
    const counter = await this.nextCounter();
    let attempt = 0, s = 0n;
    do { s = BigInt('0x' + bytesToHex(hmac(sha256, this.seed, concatBytes(utf8('Cashu_PS_Coco_Owner_v1'), utf8(id), integer(BigInt(counter)), integer(BigInt(attempt++), 4))))); } while (!s || s >= ORDER);
    return s;
  }
  private async unspent(cred: Credential) {
    const response = await checked(await fetch('/v1/nft/checkstate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nullifiers: [nullifier(cred)] }) }));
    if ((await response.json()).states[0]?.state !== 'UNSPENT') throw new Error('This NFT was already transferred or canceled');
  }
  private async prepare(kind: string, bytes: Uint8Array, title: string, cardId?: string): Promise<Prepared> {
    // Image uploads (mint, receive) carry an invisible Turnstile token.
    const headers = kind === 'mint' || kind === 'receive' ? await uploadHeaders() : {};
    return this.post(`/prepare?kind=${kind}&title=${encodeURIComponent(title)}${cardId ? '&card_id=' + cardId : ''}${kind === 'mint' ? '&issuance_version=3' : ''}`, bytes, headers);
  }
  async mint(bytes: Uint8Array, title: string): Promise<Card> {
    if (splitImage(bytes).token) throw new Error('This is a transfer file. Add it to receive the NFT inside');
    return this.lock(async () => {
      // The server returns the normalized picture under its original field name.
      const stage = await this.prepare('mint', bytes, title), image = fromBase64(stage.jpg);
      const h = hashAsset(image), s = await this.ownerSecret(stage.id);
      if (stage.h !== bytesToHex(integer(h))) throw new Error('Invalid mint preparation');
      const request = issueCommitment(this.config, h, s, stage.id);
      return this.saveAndExecute({ id: stage.id, h: stage.h, s: bytesToHex(integer(s)), cardId: stage.id, request });
    });
  }
  async receive(bytes: Uint8Array, title: string): Promise<Card> {
    // No HTTP request happens before token/picture matching and credential checks.
    const { image, token } = splitImage(bytes);
    if (!token) throw new Error('This file has no transfer token. Ask for the original transfer file');
    const cred = decodeToken(token);
    if (cred.h !== bytesToHex(integer(hashAsset(image)))) throw new Error('The embedded token does not belong to this picture. Nothing was redeemed');
    verifyCredential(cred, this.config);
    return this.lock(async () => {
      await this.unspent(cred);
      const stage = await this.prepare('receive', image, title);
      return this.swap(stage, cred);
    });
  }
  async migrate(card: Card): Promise<Card> {
    return this.lock(async () => {
      const stage = await this.prepare('migrate', new Uint8Array(), card.title, card.id);
      if (!stage.legacy_token) throw new Error('Legacy credential unavailable');
      const cred = decodeToken(stage.legacy_token);
      if (cred.h !== card.h || cred.h !== bytesToHex(integer(hashAsset(fromBase64(stage.jpg))))) throw new Error('Legacy picture identity mismatch');
      verifyCredential(cred, this.config);
      return this.swap(stage, cred);
    });
  }
  private async swap(stage: Prepared, cred: Credential): Promise<Card> {
    if (stage.h !== cred.h) throw new Error('The public picture does not match this credential');
    const begin = await (await checked(await fetch('/v1/nft/transfer/private/begin', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nullifier: nullifier(cred) }) }))).json();
    const s = await this.ownerSecret(stage.id), blind = blindTransfer(this.config, cred, s, begin.u);
    // Only proofs and the new secret's encrypted recovery material persist.
    return this.saveAndExecute({ id: stage.id, h: cred.h, s: bytesToHex(integer(s)), t: blind.t, u: begin.u, cardId: stage.card_id || stage.id, request: blind.request });
  }
  private async saveAndExecute(job: Job): Promise<Card> {
    const envelope = await this.vault.encrypt(job, 'operation:' + job.id);
    await this.vault.put('operation:' + job.id, envelope);
    await this.post(`/operations/${job.id}/backup`, envelope);
    return this.execute(job);
  }
  private async execute(job: Job): Promise<Card> {
    if (job.request.version !== 3 && (!job.t || (job.request.version !== 2 && !job.u))) throw new Error('Missing legacy unblinding material');
    const response = await this.post(`/operations/${job.id}/finish`, job.request);
    const cred = job.request.version === 3
      ? finishIssue(this.config, job.h, job.s, response)
      : job.request.version === 2
        ? finishBlindIssueV2(this.config, job.h, job.s, job.t!, response)
        : finishBlind(this.config, job.h, job.s, job.t!, job.u!, response);
    await this.unspent(cred);
    const encrypted = await this.vault.encrypt(cred, scope(job.cardId, cred.h));
    await this.vault.put('card:' + job.cardId, encrypted);
    const card = await this.post(`/operations/${job.id}/publish`, { encrypted_credential: encrypted, ...publicCard(cred, this.secret, this.pubkey) });
    await this.vault.remove('operation:' + job.id);
    return card;
  }
  async recover(): Promise<{ cards: number; operations: number; discarded: number }> {
    return this.lock(async () => {
      const backups: { cards: { id: string; h: string; encrypted_credential: Envelope }[]; operations: { id: string; backup: Envelope; state: string; kind: string; created: number }[] } = await this.post('/recover');
      let discarded = 0;
      for (const card of backups.cards) {
        const cred = await this.vault.decrypt<Credential>(card.encrypted_credential, scope(card.id, card.h));
        if (cred.h !== card.h) throw new Error('Wallet backup has the wrong picture identity');
        verifyCredential(cred, this.config);
        await this.vault.put('card:' + card.id, card.encrypted_credential);
      }
      for (const op of backups.operations) {
        const job = await this.vault.decrypt<Job>(op.backup, 'operation:' + op.id);
        if (job.id !== op.id) throw new Error('Wallet recovery operation mismatch');
        await this.vault.put('operation:' + op.id, op.backup);
        try { await this.execute(job); }
        catch (error) {
          // Never discard an issued credential. A prepared job can expire,
          // or lose a transfer race, without ever obtaining a signature.
          let abandoned = op.state === 'prepared' && op.kind === 'mint' && (job.request.version ?? 1) === 1 && Date.now() / 1000 > op.created + 300;
          if (op.state === 'prepared' && job.request.presentation) {
            const N = job.request.presentation.slice(546, 642);
            const state = await checked(await fetch('/v1/nft/checkstate', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({nullifiers:[N]}) }));
            abandoned = (await state.json()).states[0]?.state === 'SPENT';
          }
          if (!abandoned) throw error;
          await this.post(`/operations/${op.id}/discard`);
          await this.vault.remove('operation:' + op.id);
          discarded++;
        }
      }
      return { cards: backups.cards.length, operations: backups.operations.length - discarded, discarded };
    });
  }
  private async credential(card: Card) {
    if (card.pubkey !== this.pubkey || card.status === 'sent') throw new Error('This card is not in your wallet');
    const encrypted = await this.vault.get('card:' + card.id);
    if (!encrypted) throw new Error('Recover your encrypted wallet backup first');
    const cred = await this.vault.decrypt<Credential>(encrypted, scope(card.id, card.h));
    verifyCredential(cred, this.config);
    const shown = bytesToHex(parseShowing(card.showing).presentation.slice(209, 257));
    if (cred.h !== card.h || nullifier(cred) !== shown) throw new Error('The wallet backup does not match current ownership');
    await this.unspent(cred);
    return cred;
  }
  async send(card: Card): Promise<Uint8Array> {
    return this.lock(async () => {
      const cred = await this.credential(card);
      const image = new Uint8Array(await (await checked(await fetch(imageUrl(card.h)))).arrayBuffer());
      if (bytesToHex(integer(hashAsset(image))) !== cred.h) throw new Error('The public picture does not match your NFT');
      const result = transferImage(image, encodeToken(cred));
      await this.post(`/cards/${card.id}/ready`);
      return result;
    });
  }
  /** The bearer token for a transfer link; marks the card as pending like a file export. */
  async sendToken(card: Card): Promise<{ token: string; nullifier: string }> {
    return this.lock(async () => {
      const cred = await this.credential(card);
      await this.post(`/cards/${card.id}/ready`);
      return { token: encodeToken(cred), nullifier: nullifier(cred) };
    });
  }
  // --- marketplace hooks (NftSide) ---------------------------------------------
  /** Listing rotates the credential so earlier exports and links stop working. */
  rotate(card: Card): Promise<Card> {
    return this.lock(async () => {
      const cred = await this.credential(card), stage = await this.prepare('refresh', new Uint8Array(), card.title, card.id);
      return this.swap(stage, cred);
    });
  }
  /** Public presentation of the listed credential, bound to one offer and its
   *  fixed destination; the NFT mint spends it only inside that delivery. */
  async presentForDelivery(card: Card, binding: Uint8Array): Promise<string> {
    return this.lock(async () => boundPresentation(await this.credential(card), binding));
  }
  verify(cred: Credential) { verifyCredential(cred, this.config); }
  /** Save a purchased credential (recovered from the delivery receipt and the
   *  buyer's own s') encrypted, then publish the ordinary signed showing. */
  async importPurchased(cardId: string, cred: Credential, publish: (body: { encrypted_credential: Envelope; showing: string; signature: string }) => Promise<unknown>): Promise<void> {
    return this.lock(async () => {
      verifyCredential(cred, this.config);
      await this.unspent(cred);
      const encrypted = await this.vault.encrypt(cred, scope(cardId, cred.h));
      await this.vault.put('card:' + cardId, encrypted);
      await publish({ encrypted_credential: encrypted, ...publicCard(cred, this.secret, this.pubkey) });
    });
  }
  /** Delete for good: the mint burns the credential (pending links and
   *  transfer files die with it), then the server drops the card and its picture. */
  async destroy(card: Card): Promise<void> {
    return this.lock(async () => {
      const cred = await this.credential(card);
      await this.post(`/cards/${card.id}/delete`, { presentation: boundPresentation(cred, utf8('Cashu_PS_Burn_v1')) });
      await this.vault.remove('card:' + card.id);
    });
  }
  async cancel(card: Card): Promise<Card> {
    return this.lock(async () => {
      const cred = await this.credential(card), stage = await this.prepare('rotate', new Uint8Array(), card.title, card.id);
      return this.swap(stage, cred);
    });
  }
}
const opened = new Map<string, Promise<{ manager: Manager; wallet: BrowserNFTWallet }>>();
export function openWallet(secret: string, config: MintConfig) {
  const pubkey = profileKey(secret), id = pubkey + ':' + config.keyset_id;
  if (!opened.has(id)) opened.set(id, (async () => {
    // Coco's IndexedDB storage needs IndexedDB; without it (e.g. Safari
    // Lockdown Mode) the wallet runs in memory and recovers from its
    // encrypted server backups on every open.
    const repos = (await onDeviceStorage()) ? new IndexedDbRepositories({ name: 'cashu-nft-coco-v2:' + id }) : new MemoryRepositories();
    await repos.init();
    const seed = await walletSeed(secret, config.keyset_id), vault = new EncryptedVault(secret, config.keyset_id);
    let wallet: BrowserNFTWallet | undefined;
    const plugin: Plugin<['counterService']> = {
      name: 'cashu-ps-nft', required: ['counterService'],
      onReady: ({ services, registerExtension }) => {
        wallet = new BrowserNFTWallet(secret, config, vault, async () => {
          const counter = await services.counterService.incrementCounter(location.origin, 'psnft:' + config.keyset_id, 1);
          return counter.counter;
        }, seed);
        registerExtension('nft', wallet);
        return () => vault.close();
      },
    };
    const manager = await initializeCoco({ repo: repos, seedGetter: async () => seed, plugins: [plugin], watchers: { mintOperationWatcher: { disabled: true }, proofStateWatcher: { disabled: true }, meltQuoteWatcher: { disabled: true } }, processors: { mintOperationProcessor: { disabled: true }, meltSettlementProcessor: { disabled: true } } });
    if (!wallet || manager.ext.nft !== wallet) throw new Error('The coco NFT wallet did not initialize');
    return { manager, wallet };
  })().catch(error => { opened.delete(id); throw error; }));
  return opened.get(id)!;
}
