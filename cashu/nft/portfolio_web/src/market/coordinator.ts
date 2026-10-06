// HTLC coordinator for funded marketplace offers (Coco plugin `market`).
//
// Coco's send handlers cover default and P2PK sends only, so offers use this
// coordinator on Coco's supported service seams: proofs are selected and
// reserved through ProofService, the swap runs on WalletService's cashu-ts
// wallet, and change returns through ProofService.saveProofs. Every spend
// follows the same journal discipline:
//
//   1. write the complete operation (inputs, every output's secret and
//      blinding factor, keys, preimage, signed terms) to the encrypted
//      journal, locally and as a server recovery record;
//   2. make the remote call;
//   3. reconcile from mint evidence: proof states, NUT-09 restore of the
//      saved outputs, or the job result the executor stored.
//
// A reply lost in step 2 is recovered in step 3 without creating new
// outputs. Experimental: requires human review before production use.
import { Amount, OutputData, hasValidDleq, verifyProofsForReceive, type Proof, type Wallet } from '@cashu/cashu-ts';
import type { Plugin } from '@cashu/coco-core/plugin';
import { secp256k1 } from '@noble/curves/secp256k1.js';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';
import { parseShowing } from '../crypto.mjs';
import { authorizationKey, LocalRecords, moneyKey, open, seal, type Sealed } from '../money/store.ts';
import { isDeferred, type SignOptions, type Signer } from '../signer.ts';
import { ORDER, integer, type Card, type Credential } from '../wallet/ps.ts';
import type { Listing, MarketApi, MarketConfig, OfferView } from './api.ts';
import {
  ACCEPT_PROTOCOL, ACCEPT_WINDOW, LISTING_PROTOCOL, PROTOCOL, deliveryBinding, escrowPreimage, fundingAmount, hashlockOf,
  manifestHash, proveReceive, randomHex, receiveCommitment, sigAllDigest, signAcceptance, signListing, signOffer, signSigAll,
  verifyReceipt, type Acceptance, type BlindedOutput, type ListingManifest, type OfferManifest,
} from './protocol.ts';

declare module '@cashu/coco-core/plugin' { interface PluginExtensions { market: MarketCoordinator; } }

/** NFT wallet operations the coordinator needs (implemented by BrowserNFTWallet). */
export interface NftSide {
  rotate(card: Card): Promise<Card>;
  presentForDelivery(card: Card, binding: Uint8Array): Promise<string>;
  importPurchased(cardId: string, cred: Credential, publish: (body: { encrypted_credential: unknown; showing: string; signature: string }) => Promise<unknown>, opts?: SignOptions): Promise<void>;
  verify(cred: Credential): void;
}

interface SavedOutput { amount: number; id: string; B_: string; r: string; secret: string; }
type WireProof = { amount: number; id: string; secret: string; C: string; dleq?: { e: string; s: string; r: string } | null };

export type BuyerStage = 'intent' | 'funded' | 'registered' | 'rejected' | 'refunded' | 'purchased' | 'abandoned';
export interface BuyerRecord {
  kind: 'offer'; id: string; stage: BuyerStage; mint: string; keyset_id: string; test_value: boolean;
  manifest: OfferManifest; preimage: string; s_new: string; refund_key: string;
  inputs: WireProof[]; send: SavedOutput[]; keep: SavedOutput[];
  htlc: WireProof[]; refund: SavedOutput[]; refund_signature: string; buyer_signature?: string; body?: unknown;
  error?: string; created: number; updated: number;
}
export type SellerStage = 'prepared' | 'accepted' | 'paid' | 'failed';
export interface SellerRecord {
  kind: 'sale'; id: string; stage: SellerStage; mint: string; keyset_id: string; listing_id: string;
  claim: SavedOutput[]; error?: string; created: number; updated: number;
}
type JournalRecord = BuyerRecord | SellerRecord;

const now = () => Math.floor(Date.now() / 1000);
const amountSplit = (n: number) => { const out: number[] = []; for (let i = 0, x = BigInt(n); x; i++, x >>= 1n) if (x & 1n) out.push(2 ** i); return out; };
const num = (a: unknown) => (typeof a === 'number' ? a : Number(String(a)));
const wire = (p: Proof): WireProof => ({ amount: num(p.amount), id: p.id, secret: p.secret, C: p.C, dleq: p.dleq ? { e: p.dleq.e, s: p.dleq.s, r: p.dleq.r } : null });
const saveOutput = (o: OutputData): SavedOutput => ({ amount: num(o.blindedMessage.amount), id: o.blindedMessage.id, B_: o.blindedMessage.B_, r: o.blindingFactor.toString(16).padStart(64, '0'), secret: bytesToHex(o.secret) });
const outputData = (o: SavedOutput) => new OutputData({ amount: Amount.from(o.amount), id: o.id, B_: o.B_ }, BigInt('0x' + o.r), hexToBytes(o.secret));
const blinded = (o: SavedOutput): BlindedOutput => ({ amount: o.amount, id: o.id, B_: o.B_ });
const compressedPub = (priv: Uint8Array) => bytesToHex(secp256k1.getPublicKey(priv, true));
/** Proofs built from signatures someone relayed (executor results, NUT-09
 *  restores) must carry valid DLEQ proofs from the mint's keys. */
function verified(wallet: Wallet, proofs: Proof[]): Proof[] {
  verifyProofsForReceive(proofs, (id) => wallet.getKeyset(id), { requireDleq: true });
  return proofs;
}

/** Encrypted operation journal: local IndexedDB plus server recovery records. */
class Journal {
  private key: Promise<CryptoKey>;
  constructor(root: string, private pubkey: string, private local: LocalRecords, private api: MarketApi) {
    this.key = moneyKey(root, 'market-journal');
  }
  private aad = (id: string) => `Cashu_Market_Journal_v1\n${this.pubkey}\n${id}`;
  async put(record: JournalRecord): Promise<void> {
    record.updated = now();
    const envelope = await seal(await this.key, this.aad(record.kind + ':' + record.id), record);
    await this.local.put('journal:' + record.kind + ':' + record.id, envelope);
    // The remote copy is what makes recovery possible from another browser.
    await this.api.putRecovery({ id: record.kind + ':' + record.id, kind: record.kind, envelope });
  }
  async all(): Promise<JournalRecord[]> {
    const merged = new Map<string, Sealed>();
    for (const { id, value } of await this.local.all()) if (id.startsWith('journal:')) merged.set(id.slice(8), value as Sealed);
    try {
      for (const r of await this.api.listRecovery()) if (r.kind === 'offer' || r.kind === 'sale') merged.set(r.id, r.envelope);
    } catch { /* offline: local journal only */ }
    const out: JournalRecord[] = [];
    for (const [id, envelope] of merged) {
      try { out.push(await open<JournalRecord>(await this.key, this.aad(id), envelope)); } catch { /* foreign or corrupt */ }
    }
    // Prefer the newest copy of each record (local may be ahead of remote or behind it).
    const byId = new Map<string, JournalRecord>();
    for (const r of out) { const k = r.kind + ':' + r.id, prev = byId.get(k); if (!prev || prev.updated <= r.updated) byId.set(k, r); }
    return [...byId.values()];
  }
}

export interface MarketBalances { offerLocked: number; pendingRefund: number; pendingClaim: number; }

export class MarketCoordinator {
  readonly journal: Journal;
  readonly pubkey: string;
  private config?: MarketConfig;
  constructor(
    private root: string,
    private signer: Signer,
    private api: MarketApi,
    private services: { proofService: any; walletService: any; mintService: any },
    local: LocalRecords,
    private pins: LocalRecords,
  ) {
    this.pubkey = signer.pubkey;
    this.journal = new Journal(root, this.pubkey, local, api);
  }

  /** Market config with trust-on-first-use pinning of the NFT mint's escrow
   *  and receipt keys (and keyset). A changed key needs explicit review. */
  async marketConfig(): Promise<MarketConfig> {
    if (this.config) return this.config;
    const config = await this.api.config();
    const pin = { nft_keyset_id: config.nft_keyset_id, escrow: config.escrow, receipt: config.receipt };
    const saved = await this.pins.get<typeof pin>('market-pins');
    if (!saved) await this.pins.put('market-pins', pin);
    else if (JSON.stringify(saved) !== JSON.stringify(pin)) throw new Error('The marketplace settlement keys changed. Review before trading.');
    return (this.config = config);
  }

  /** Offers, acceptances and reconciliation run one at a time: recovery must
   *  never judge an operation whose request is still in flight. */
  private queue: Promise<unknown> = Promise.resolve();
  private exclusive<T>(task: () => Promise<T>): Promise<T> {
    const run = this.queue.then(task, task);
    this.queue = run.catch(() => {});
    return run;
  }

  private wallet(mint: string): Promise<Wallet> { return this.services.walletService.getWallet(mint, 'sat'); }
  /** Funding an offer, or accepting one after review, is the owner's decision
   *  to hold ecash from this mint: make sure Coco tracks it as trusted, or the
   *  imported proofs would not count towards any balance. */
  private async trust(mint: string) {
    if (!(await this.services.mintService.isTrustedMint(mint).catch(() => false))) {
      await this.services.mintService.addMintByUrl(mint, { trusted: true });
    }
  }
  /** Hand proofs back to Coco as ordinary ready proofs of this mint. */
  private save(mint: string, proofs: Proof[], operationId: string) {
    if (!proofs.length) return Promise.resolve();
    return this.services.proofService.saveProofs(mint, proofs.map((p) => ({ ...p, mintUrl: mint, unit: 'sat', state: 'ready', createdByOperationId: operationId })));
  }

  // --- seller: listings ------------------------------------------------------

  private async claimKey(listingId: string) { return authorizationKey(this.root, `claim:${listingId}`); }
  /** Per-offer buyer secrets derived from the profile's root secret, so the
   *  refund key and the new owner secret s' survive even if the journal is lost. */
  private async offerKeys(offerId: string) {
    const refundKey = await authorizationKey(this.root, `refund:${offerId}`);
    if (!secp256k1.utils.isValidSecretKey(refundKey)) throw new Error('Derived refund key out of range');
    const raw = BigInt('0x' + bytesToHex(await authorizationKey(this.root, `receive:${offerId}`)));
    return { refundKey, sNew: (raw % (ORDER - 1n)) + 1n };
  }

  async list(nft: NftSide, card: Card, price: number): Promise<Listing> {
    const config = await this.marketConfig();
    // Listing rotates the credential first: earlier file exports and links die.
    const fresh = await nft.rotate(card);
    const listingId = randomHex(16);
    const shown = parseShowing(fresh.showing);
    const listing: ListingManifest = {
      v: LISTING_PROTOCOL, listing_id: listingId, revision: 1, card_id: fresh.id, seller: this.pubkey, h: fresh.h,
      nullifier: bytesToHex(shown.presentation.slice(209, 257)), nft_keyset: config.nft_keyset_id, price,
      claim_pubkey: compressedPub(await this.claimKey(listingId)), created: now(),
    };
    return this.api.createListing(listing, await signListing(listing, this.signer));
  }

  async revise(current: Listing, price: number): Promise<Listing> {
    const config = await this.marketConfig();
    const listing: ListingManifest = {
      v: LISTING_PROTOCOL, listing_id: current.id, revision: current.revision + 1, card_id: current.card_id, seller: this.pubkey,
      h: current.h, nullifier: current.nullifier, nft_keyset: config.nft_keyset_id, price, claim_pubkey: current.claim_pubkey, created: now(),
    };
    return this.api.reviseListing(listing, await signListing(listing, this.signer));
  }

  // --- buyer: funded offers ------------------------------------------------------

  async quote(mint: string, price: number) {
    const quote = await this.api.quote(mint, price);
    if (!quote.eligible || !quote.keyset_id || quote.fee_ppk === null) throw new Error(`This mint can't fund offers: ${quote.reasons.join(', ')}`);
    const { amount, fee } = fundingAmount(price, quote.fee_ppk);
    if (amount !== quote.amount || fee !== quote.claim_fee) throw new Error('Fee calculation differs from the marketplace');
    return { ...quote, amount, fee };
  }

  makeOffer(listing: Listing, mint: string, opts: { price?: number; lifetime?: number; deadline?: number; created?: number } = {}): Promise<BuyerRecord> {
    return this.exclusive(() => this.fundOffer(listing, mint, opts));
  }

  private async fundOffer(listing: Listing, mint: string, opts: { price?: number; lifetime?: number; deadline?: number; created?: number }): Promise<BuyerRecord> {
    const config = await this.marketConfig();
    const price = opts.price ?? listing.price;
    if (price < listing.price) throw new Error('Offers must meet the asking price');
    const quote = await this.quote(mint, price);
    // `created` must be within the NFT mint's clock skew; tests pass the server's clock.
    const created = opts.created ?? now(), cash_deadline = opts.deadline ?? created + (opts.lifetime ?? config.default_lifetime);
    // Same window the NFT mint enforces at registration.
    if (cash_deadline < created + config.min_lifetime - config.clock_skew || cash_deadline > created + config.max_lifetime) throw new Error('Offer lifetime out of range');
    const preimage = crypto.getRandomValues(new Uint8Array(32));
    const offerId = randomHex(16);
    const { refundKey, sNew: s_new } = await this.offerKeys(offerId);
    const manifest: OfferManifest = {
      v: PROTOCOL, offer_id: offerId, listing_id: listing.id, listing_revision: listing.revision,
      nft: { keyset_id: config.nft_keyset_id, h: listing.h, nullifier: listing.nullifier }, seller: listing.seller, buyer: this.pubkey,
      price, payment: { mint, unit: 'sat', keyset_id: quote.keyset_id!, amount: quote.amount, claim_fee: quote.fee, refund_fee: quote.fee },
      hashlock: hashlockOf(preimage), claim_pubkey: listing.claim_pubkey, refund_pubkey: compressedPub(refundKey),
      cash_deadline, accept_deadline: cash_deadline - ACCEPT_WINDOW, nft_destination: receiveCommitment(s_new),
      escrow_key: config.escrow.version, created,
    };
    const wallet = await this.wallet(mint);
    const keysetId = quote.keyset_id!;
    // Select and reserve through Coco so nothing else spends these proofs.
    const selected: Proof[] = await this.services.proofService.selectProofsToSend(mint, { amount: quote.amount, unit: 'sat' }, true);
    await this.services.proofService.reserveProofs(mint, selected.map((p) => p.secret), 'market:' + manifest.offer_id);
    let record: BuyerRecord | undefined;
    let unused = new Set<string>();
    try {
      const preview = await wallet.prepareSwapToSend(quote.amount, selected, { keysetId, includeFees: false }, {
        send: { type: 'lock', options: { mainKeys: [manifest.claim_pubkey], hashlock: manifest.hashlock, locktime: cash_deadline, refundKeys: [manifest.refund_pubkey], sigAll: true }, denominations: amountSplit(quote.amount) },
        keep: { type: 'random' },
      });
      // Only the swap's inputs stay reserved; selected proofs it doesn't need
      // go back now (cashu-ts returns them with the change, see below).
      unused = new Set((preview.unselectedProofs ?? []).map((p) => p.secret));
      await this.services.proofService.releaseProofs(mint, [...unused]);
      record = {
        kind: 'offer', id: manifest.offer_id, stage: 'intent', mint, keyset_id: keysetId, test_value: quote.test_value, manifest,
        preimage: bytesToHex(preimage), s_new: bytesToHex(integer(s_new)), refund_key: bytesToHex(refundKey),
        inputs: preview.inputs.map(wire), send: (preview.sendOutputs ?? []).map((o) => saveOutput(o as OutputData)),
        keep: (preview.keepOutputs ?? []).map((o) => saveOutput(o as OutputData)), htlc: [], refund: [], refund_signature: '',
        created, updated: created,
      };
      await this.journal.put(record); // (1) intent is durable before the spend
      const result = await wallet.completeSwap(preview);
      // Re-saving the unused proofs could clear a reservation made since.
      await this.afterFunding(record, result.send, result.keep.filter((p) => !unused.has(p.secret)));
    } catch (error) {
      // Never assume the swap failed: resolve from mint evidence.
      if (!record) { await this.services.proofService.releaseProofs(mint, selected.map((p) => p.secret)); throw error; }
      await this.recoverFunding(record);
      if (record.stage === 'abandoned') throw record.error ? new Error(record.error) : error;
    }
    await this.signAndRegister(record);
    return record;
  }

  private async afterFunding(record: BuyerRecord, send: Proof[], keep: Proof[]) {
    await this.save(record.mint, keep, 'market:' + record.id);
    await this.services.proofService.setProofState(record.mint, record.inputs.map((p) => p.secret), 'spent');
    record.htlc = send.map(wire);
    record.stage = 'funded';
    await this.journal.put(record);
  }

  /** A funding swap with an unknown outcome, resolved from mint evidence.
   *  A swap is atomic: its inputs are spent exactly when its outputs are
   *  signed. So the saved outputs (NUT-09 restore) decide whether it ran;
   *  spent inputs alone only say that something spent them. */
  private async recoverFunding(record: BuyerRecord) {
    const wallet = await this.wallet(record.mint);
    const states = await wallet.checkProofsStates(record.inputs);
    if (states.some((s) => s.state === 'PENDING')) throw new Error('The mint is still processing this offer\'s funding. Try again shortly.');
    const outputs = [...record.send, ...record.keep];
    const restored = states.every((s) => s.state === 'UNSPENT')
      ? { outputs: [], signatures: [] }
      : await wallet.mint.restore({ outputs: outputs.map(blinded) as never });
    const byB = new Map(restored.outputs.map((o, i) => [o.B_, restored.signatures[i]]));
    if (byB.size === 0) {
      // Never funded. Inputs spent elsewhere (another device, a stale copy of
      // this wallet) are gone; the rest are spendable again.
      const spent = record.inputs.filter((_, i) => states[i].state === 'SPENT').map((p) => p.secret);
      await this.services.proofService.setProofState(record.mint, spent, 'spent');
      await this.services.proofService.releaseProofs(record.mint, record.inputs.filter((_, i) => states[i].state === 'UNSPENT').map((p) => p.secret));
      record.stage = 'abandoned';
      if (spent.length) record.error = 'Some of the ecash for this offer had already been spent elsewhere, so the offer was not funded.';
      await this.journal.put(record);
      return;
    }
    if (outputs.some((o) => !byB.has(o.B_))) throw new Error('The mint restored only part of this offer\'s funding. Contact the mint operator.');
    const keyset = wallet.getKeyset(record.keyset_id);
    const toProofs = (saved: SavedOutput[]) => verified(wallet, saved.map((o) => outputData(o).toProof(byB.get(o.B_)!, keyset)));
    await this.afterFunding(record, toProofs(record.send), toProofs(record.keep));
  }

  private async signAndRegister(record: BuyerRecord, opts?: SignOptions) {
    const config = await this.marketConfig();
    if (!record.refund.length) {
      const wallet = await this.wallet(record.mint);
      const refund = OutputData.createRandomData(record.manifest.payment.amount - record.manifest.payment.refund_fee, wallet.getKeyset(record.keyset_id));
      record.refund = refund.map(saveOutput);
      record.refund_signature = signSigAll(sigAllDigest(record.htlc, record.refund.map(blinded)), record.refund_key);
      await this.journal.put(record); // the refund authorization is durable before anyone sees the offer
    }
    if (!record.buyer_signature) {
      // Signed once and journaled: a retried registration reuses it.
      record.buyer_signature = await signOffer(record.manifest, this.signer, { ...opts, id: 'offer:' + record.id });
      await this.journal.put(record);
    }
    const mhash = manifestHash(record.manifest);
    const body = {
      manifest: record.manifest, buyer_signature: record.buyer_signature,
      receive_proof: proveReceive(BigInt('0x' + record.s_new), mhash),
      escrow: await escrowPreimage(config.escrow.public_key, config.escrow.version, hexToBytes(record.preimage), mhash),
      proofs: record.htlc, refund: { outputs: record.refund.map(blinded), signature: record.refund_signature },
    };
    try {
      await this.api.submitOffer(body);
      record.stage = 'registered';
      record.error = undefined;
    } catch (error) {
      // Funded but not registered: the HTLC still refunds to us at the deadline.
      record.stage = 'rejected';
      record.error = (error as Error).message;
    }
    await this.journal.put(record);
  }

  // --- seller: acceptance -------------------------------------------------------

  /** Everything a seller must check before delivering the NFT. */
  async review(offer: OfferView) {
    const reasons: string[] = [];
    const eligibility = await this.api.checkMint(offer.mint);
    if (!eligibility.eligible) reasons.push(...eligibility.reasons);
    const wallet = await this.services.walletService.getWallet(offer.mint, 'sat').catch(() => null) as Wallet | null;
    const proofs = offer.proofs ?? [];
    if (!wallet) reasons.push('Add this mint to your wallet to review the payment');
    else {
      const keyset = wallet.getKeyset(offer.keyset_id);
      for (const p of proofs) if (!hasValidDleq({ ...p, amount: p.amount } as never, keyset, { require: true })) reasons.push('A payment proof is not signed by this mint');
      const states = await wallet.checkProofsStates(proofs);
      if (!states.every((s) => s.state === 'UNSPENT')) reasons.push('The payment is no longer locked');
    }
    const total = proofs.reduce((n, p) => n + p.amount, 0);
    if (total !== offer.amount || offer.amount - offer.claim_fee !== offer.price) reasons.push('Payment amount differs from the offer');
    if (now() > offer.accept_deadline - 300) reasons.push('Acceptance window closed');
    return { ok: reasons.length === 0, reasons: [...new Set(reasons)], test_value: offer.test_value };
  }

  accept(nft: NftSide, offer: OfferView, card: Card) {
    return this.exclusive(() => this.acceptOffer(nft, offer, card));
  }

  private async acceptOffer(nft: NftSide, offer: OfferView, card: Card) {
    if (offer.role !== 'seller' || offer.disposition !== 'funded') throw new Error('This offer can no longer be accepted');
    const review = await this.review(offer);
    if (!review.ok) throw new Error(review.reasons.join('. '));
    await this.trust(offer.mint);
    const wallet = await this.wallet(offer.mint);
    const claim = OutputData.createRandomData(offer.price, wallet.getKeyset(offer.keyset_id)).map(saveOutput);
    const claimKey = await this.claimKey(offer.listing_id);
    if (compressedPub(claimKey) !== offer.manifest.claim_pubkey) throw new Error('This offer is for another listing key');
    const record: SellerRecord = { kind: 'sale', id: offer.id, stage: 'prepared', mint: offer.mint, keyset_id: offer.keyset_id, listing_id: offer.listing_id, claim, created: now(), updated: now() };
    await this.journal.put(record); // claim outputs are recoverable before the NFT moves
    const acceptance: Acceptance = {
      v: ACCEPT_PROTOCOL, offer_id: offer.id, manifest_hash: offer.manifest_hash, claim_outputs: claim.map(blinded),
      claim_signature: signSigAll(sigAllDigest(offer.proofs!, claim.map(blinded)), bytesToHex(claimKey)), accepted: now(),
    };
    const presentation = await nft.presentForDelivery(card, deliveryBinding(hexToBytes(offer.manifest_hash), offer.manifest.nft_destination));
    try {
      const result = await this.api.accept(offer.id, acceptance, await signAcceptance(acceptance, this.signer), presentation);
      record.stage = 'accepted';
      await this.journal.put(record);
      return result;
    } catch (error) {
      const fresh = await this.api.offer(offer.id).catch(() => null);
      record.stage = fresh?.nft_leg === 'delivered' ? 'accepted' : 'failed';
      record.error = (error as Error).message;
      await this.journal.put(record);
      if (record.stage === 'failed') throw error;
      return { offer: fresh! };
    }
  }

  // --- reconciliation (browser recovery path) -----------------------------------

  /** Bring every journaled operation forward from authoritative evidence.
   *  Background runs pass `interactive: false`: a step that needs a prompt
   *  waits for the signer instead (signer.ts `SignatureDeferred`). */
  reconcile(nft?: NftSide, opts?: SignOptions): Promise<{ changed: number; attention: string[] }> {
    return this.exclusive(() => this.reconcileAll(nft, opts));
  }

  private async reconcileAll(nft?: NftSide, opts?: SignOptions): Promise<{ changed: number; attention: string[] }> {
    let changed = 0;
    const attention: string[] = [];
    for (const record of await this.journal.all()) {
      const before = record.stage;
      try {
        if (record.kind === 'offer') await this.reconcileOffer(record, nft, opts);
        else await this.reconcileSale(record);
        // Self-heal wallets whose settled proofs were saved under an untrusted mint.
        if (['paid', 'refunded'].includes(record.stage)) await this.trust(record.mint);
      } catch (error) { if (!isDeferred(error)) attention.push(`${record.id}: ${(error as Error).message}`); }
      if (record.stage !== before) changed++;
    }
    if (nft) changed += await this.recoverOrphanPurchases(nft, attention, opts);
    return { changed, attention };
  }

  private async importOutputs(record: { id: string; mint: string; keyset_id: string }, saved: SavedOutput[], signatures: { amount: number; id: string; C_: string; dleq?: { e: string; s: string } }[]) {
    await this.trust(record.mint);
    const wallet = await this.wallet(record.mint);
    if (signatures.length !== saved.length) throw new Error('Payment result does not match the saved outputs');
    const proofs = saved.map((o, i) => {
      const sig = signatures[i];
      if (sig.amount !== o.amount) throw new Error('The mint signed a different amount');
      // Keyset may legitimately differ only within the same mint/unit; verify with its keys.
      return outputData({ ...o, id: sig.id }).toProof({ ...sig, amount: Amount.from(sig.amount) } as never, wallet.getKeyset(sig.id));
    });
    await this.save(record.mint, verified(wallet, proofs), 'market:' + record.id);
  }

  private async reconcileOffer(record: BuyerRecord, nft?: NftSide, opts?: SignOptions) {
    if (record.stage === 'intent') {
      // Coco's startup recovery releases reservations it doesn't own; hold
      // the inputs again until the outcome is known.
      for (const p of record.inputs) await this.services.proofService.reserveProofs(record.mint, [p.secret], 'market:' + record.id).catch(() => {});
      await this.recoverFunding(record);
    }
    if (record.stage === 'funded') await this.signAndRegister(record, opts);
    if (record.stage === 'registered') {
      const offer = await this.api.offer(record.id);
      if (offer.cash_leg === 'refunded') {
        const result = await this.api.payment(record.id, 'refund');
        if (result.signatures) { await this.importOutputs(record, record.refund, result.signatures); record.stage = 'refunded'; await this.journal.put(record); }
      } else if (offer.nft_leg === 'delivered' && offer.publication !== 'published' && nft) {
        await this.recoverPurchase(record, nft, true, opts);
      } else if (offer.nft_leg === 'delivered' && offer.publication === 'published') {
        record.stage = 'purchased'; await this.journal.put(record);
      }
    }
    if (record.stage === 'rejected' && now() >= record.manifest.cash_deadline) await this.refundDirectly(record);
  }

  /** Independent refund path for offers the server never took: submit the
   *  pre-signed refund after the locktime, or restore it if it already ran. */
  private async refundDirectly(record: BuyerRecord) {
    const wallet = await this.wallet(record.mint);
    const states = await wallet.checkProofsStates(record.htlc);
    if (states.every((s) => s.state === 'UNSPENT')) {
      const inputs = record.htlc.map(({ dleq: _d, ...p }, i) => (i === 0 ? { ...p, witness: JSON.stringify({ signatures: [record.refund_signature] }) } : p));
      try { await wallet.mint.swap({ inputs, outputs: record.refund.map(blinded) } as never); } catch { /* lost reply or race: restore below decides */ }
    }
    const restored = await wallet.mint.restore({ outputs: record.refund.map(blinded) as never });
    if (restored.signatures.length !== record.refund.length) throw new Error('Refund not executed yet');
    const byB = new Map(restored.outputs.map((o, i) => [o.B_, restored.signatures[i]]));
    await this.importOutputs(record, record.refund, record.refund.map((o) => {
      const s = byB.get(o.B_)!;
      return { amount: num(s.amount), id: s.id, C_: s.C_, dleq: s.dleq };
    }));
    record.stage = 'refunded';
    await this.journal.put(record);
  }

  /** Purchases without a journal record (journal lost): s' is re-derived
   *  from the root secret and the offer id; the receipt does the rest. */
  private async recoverOrphanPurchases(nft: NftSide, attention: string[], opts?: SignOptions): Promise<number> {
    let recovered = 0;
    const known = new Set((await this.journal.all()).filter((r) => r.kind === 'offer').map((r) => r.id));
    let purchases: { id: string; publication: string }[] = [];
    try { purchases = await this.api.purchases(); } catch { return 0; }
    for (const p of purchases) {
      if (known.has(p.id) || p.publication === 'published') continue;
      try {
        const offer = await this.api.offer(p.id);
        const { sNew } = await this.offerKeys(p.id);
        const record = { id: p.id, manifest: offer.manifest, s_new: bytesToHex(integer(sNew)) } as BuyerRecord;
        if (receiveCommitment(sNew) !== offer.manifest.nft_destination) throw new Error('This purchase was made with a different key');
        await this.recoverPurchase(record, nft, false, opts);
        recovered++;
      } catch (error) { if (!isDeferred(error)) attention.push(`${p.id}: ${(error as Error).message}`); }
    }
    return recovered;
  }

  async recoverPurchase(record: BuyerRecord, nft: NftSide, journaled = true, opts?: SignOptions) {
    const config = await this.marketConfig();
    const { receipt, signature, receipt_key } = await this.api.receipt(record.id);
    if (receipt_key !== config.receipt.public_key || !verifyReceipt(receipt, signature, config.receipt.public_key)) throw new Error('Delivery receipt is not signed by the pinned NFT mint key');
    if (receipt.offer_id !== record.id || receipt.manifest_hash !== bytesToHex(manifestHash(record.manifest)) || receipt.destination !== record.manifest.nft_destination || receipt.h !== record.manifest.nft.h || receipt.nft_keyset !== config.nft_keyset_id) {
      throw new Error('Delivery receipt does not match this offer');
    }
    const cred: Credential = { u: receipt.u, v: receipt.v_, h: receipt.h, s: record.s_new, keyset_id: receipt.nft_keyset };
    nft.verify(cred); // a receipt is not delivery until the credential verifies
    await nft.importPurchased(record.id, cred, (body) => this.api.publishPurchase(record.id, body), opts);
    if (!journaled) return;
    record.stage = 'purchased';
    await this.journal.put(record);
  }

  private async reconcileSale(record: SellerRecord) {
    if (record.stage !== 'accepted') return;
    const result = await this.api.payment(record.id, 'claim');
    if (result.outcome === 'claim' && result.signatures) {
      await this.importOutputs(record, record.claim, result.signatures);
      record.stage = 'paid';
      await this.journal.put(record);
    }
  }

  /** Per-mint amounts held by offers that are not ordinary spendable proofs. */
  async balances(): Promise<Record<string, MarketBalances>> {
    const out: Record<string, MarketBalances> = {};
    const add = (mint: string, k: keyof MarketBalances, n: number) => { out[mint] ??= { offerLocked: 0, pendingRefund: 0, pendingClaim: 0 }; out[mint][k] += n; };
    for (const r of await this.journal.all()) {
      if (r.kind === 'offer' && ['intent', 'funded', 'registered', 'rejected'].includes(r.stage)) {
        add(r.mint, r.stage === 'rejected' ? 'pendingRefund' : 'offerLocked', r.manifest.payment.amount);
      }
      if (r.kind === 'sale' && r.stage === 'accepted') add(r.mint, 'pendingClaim', r.claim.reduce((n, o) => n + o.amount, 0));
    }
    return out;
  }
}

export function marketPlugin(root: string, signer: Signer, api: MarketApi, local: LocalRecords, pins: LocalRecords, onReady: (c: MarketCoordinator) => void): Plugin<['proofService', 'walletService', 'mintService']> {
  return {
    name: 'cashu-nft-market', required: ['proofService', 'walletService', 'mintService'],
    onReady: ({ services, registerExtension }) => {
      const coordinator = new MarketCoordinator(root, signer, api, services, local, pins);
      registerExtension('market', coordinator);
      onReady(coordinator);
    },
  };
}
