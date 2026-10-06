// Nostr login (NOSTR_LOGIN_PLAN.md): pasted keys, relays, the profile import
// and the wallet vault. Loaded on demand; it pulls in nostr-tools.
//
// A Nostr collection's public key is the Nostr key. Its wallets derive from
// a random vault secret, NIP-44-encrypted to that same key and stored twice:
// on this server and as a kind 30078 event on the user's relays. Any way of
// holding the key (a signing extension or a pasted nsec) can decrypt it.
import { hkdf } from '@noble/hashes/hkdf.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes, randomBytes } from '@noble/hashes/utils.js';
import { decode } from 'nostr-tools/nip19';
import * as nip44 from 'nostr-tools/nip44';
import { decrypt as decryptKey } from 'nostr-tools/nip49';
import { SimplePool } from 'nostr-tools/pool';
import { finalizeEvent, type Event } from 'nostr-tools/pure';
import { checked, getJSON, signedRequest } from './api.mjs';
import { profileKey } from './crypto.mjs';
import { extension, extensionSignature, extensionSigner, signWithExtension, waitForExtension, type Session } from './identity.ts';
import { localSigner, type Signer } from './signer.ts';
import { moneyKey, open, type Sealed } from './money/store.ts';
import { EncryptedVault, type Envelope } from './wallet/vault.ts';

/** Profile indexers asked for every profile; the user's own relays join them. */
export const RELAYS = ['wss://purplepag.es', 'wss://relay.damus.io', 'wss://nos.lol', 'wss://relay.primal.net'];
let defaults = RELAYS;
/** Tests point the module at their own relays (or none). */
export function useRelays(relays: string[]) { defaults = relays; }
const RELAY_WAIT = 3000;
const VAULT_KIND = 30078, VAULT_D = 'nonfungible.cash:wallet-vault:v1';
const SESSION_DAYS = 30;
const MAX_NAME = 40;

// --- pasted keys ---------------------------------------------------------------

export type PastedKey =
  | { kind: 'hex' | 'nsec'; secret: string }
  | { kind: 'ncryptsec'; ncryptsec: string }
  | { kind: 'npub'; pubkey: string };

export function parseKey(text: string): PastedKey {
  const value = text.trim().replace(/^nostr:/i, '');
  if (/^[0-9a-f]{64}$/i.test(value)) return { kind: 'hex', secret: value.toLowerCase() };
  const lower = value.toLowerCase();
  try {
    if (lower.startsWith('ncryptsec1')) return { kind: 'ncryptsec', ncryptsec: lower };
    if (lower.startsWith('nsec1')) {
      const decoded = decode(lower);
      if (decoded.type === 'nsec') return { kind: 'nsec', secret: bytesToHex(decoded.data) };
    }
    if (lower.startsWith('npub1')) {
      const decoded = decode(lower);
      if (decoded.type === 'npub') return { kind: 'npub', pubkey: decoded.data };
    }
  } catch { /* malformed bech32 */ }
  throw new Error('Paste a private key: 64 hex characters, an nsec or an ncryptsec.');
}

/** NIP-49: the private key inside an ncryptsec. */
export function unlockKey(ncryptsec: string, password: string): string {
  try { return bytesToHex(decryptKey(ncryptsec, password.normalize('NFKC'))); }
  catch { throw new Error('Wrong password, or this ncryptsec is damaged.'); }
}

// --- who is logging in -----------------------------------------------------------

/** Holds the Nostr key: encrypts to itself and signs events. */
interface Keyholder {
  readonly pubkey: string;
  encrypt(plaintext: string): Promise<string>;
  decrypt(ciphertext: string): Promise<string>;
  signEvent(template: { created_at: number; kind: number; tags: string[][]; content: string }): Promise<Event>;
}

function localHolder(secret: string): Keyholder {
  const key = hexToBytes(secret), pubkey: string = profileKey(secret);
  const conversation = nip44.getConversationKey(key, pubkey);
  return {
    pubkey,
    async encrypt(plaintext) { return nip44.encrypt(plaintext, conversation); },
    async decrypt(ciphertext) { return nip44.decrypt(ciphertext, conversation); },
    async signEvent(template) { return finalizeEvent(template, key); },
  };
}

function extensionHolder(pubkey: string): Keyholder {
  const crypt = () => {
    const ext = extension();
    if (!ext?.nip44) throw new Error('Your Nostr extension doesn’t support NIP-44 encryption. Update it, or paste your key instead.');
    return ext.nip44;
  };
  return {
    pubkey,
    async encrypt(plaintext) { return crypt().encrypt(pubkey, plaintext); },
    async decrypt(ciphertext) { return crypt().decrypt(pubkey, ciphertext); },
    async signEvent(template) { return signWithExtension(template, pubkey) as Promise<Event>; },
  };
}

export interface ServerProfile { pubkey: string; name: string; nostr: boolean; avatar: number | null }
async function serverProfile(pubkey: string): Promise<ServerProfile | null> {
  const response = await fetch(`/api/profiles/${pubkey}`, { cache: 'no-store' });
  if (response.status === 404) return null;
  return (await checked(response)).json();
}

export interface Login {
  readonly pubkey: string;
  readonly method: 'nip07' | 'nsec' | 'ncryptsec';
  /** Signs owner requests: the pasted key, or a fresh session key. */
  readonly signer: Signer;
  readonly holder: Keyholder;
  readonly secret?: string;
  readonly ncryptsec?: string;
  readonly session?: Session;
  /** This server's collection for the key, if there is one. */
  readonly existing: ServerProfile | null;
  /** Name, picture and relays from Nostr, looked up in the background. */
  readonly nostr: Promise<NostrProfile>;
}

/** Step one of a login: who the key is and what this server knows about it. */
export async function connectExtension(): Promise<Login> {
  const ext = await waitForExtension();
  if (!ext) throw new Error('No Nostr extension found. Paste your key instead.');
  const pubkey = (await ext.getPublicKey())?.toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(pubkey || '')) throw new Error('Your Nostr extension returned an invalid public key.');
  const existing = await serverProfile(pubkey);
  if (existing && !existing.nostr) throw new Error('This collection uses its own key. Unlock it with that key instead.');
  const nostr = lookup(pubkey);
  const session = await startSession(pubkey);
  return { pubkey, method: 'nip07', signer: extensionSigner(pubkey, session), holder: extensionHolder(pubkey), session, existing, nostr };
}
export async function connectKey(secret: string, ncryptsec?: string): Promise<Login> {
  const holder = localHolder(secret);
  const existing = await serverProfile(holder.pubkey);
  return {
    pubkey: holder.pubkey, method: ncryptsec ? 'ncryptsec' : 'nsec', signer: localSigner(secret), holder, secret, ncryptsec,
    existing, nostr: lookup(holder.pubkey),
  };
}

/** One extension signature authorizes a session key for owner requests. */
async function startSession(pubkey: string): Promise<Session> {
  const secret = bytesToHex(randomBytes(32));
  const session = { secret, expires: Math.floor(Date.now() / 1000) + SESSION_DAYS * 86400 };
  const signer: Signer = {
    pubkey,
    async auth(message) { return { signature: await extensionSignature(pubkey, 'auth', sha256(new TextEncoder().encode(message))) }; },
    async sign() { throw new Error('Not used'); },
  };
  await signedRequest(signer, `/api/profiles/${pubkey}/session`, JSON.stringify({ session_pubkey: profileKey(secret), expires: session.expires }), 'application/json');
  return session;
}
export async function endSession(signer: Signer, session: Session) {
  await signedRequest(signer, `/api/profiles/${signer.pubkey}/session/revoke`, JSON.stringify({ session_pubkey: profileKey(session.secret) }), 'application/json');
}

// --- relays and the profile import -------------------------------------------------

export interface NostrProfile { name: string | null; picture: string | null; relays: string[] }

let pool: SimplePool | null = null;
const relayPool = () => (pool ??= new SimplePool());
const timeout = <T>(promise: Promise<T>, ms: number, fallback: T) =>
  Promise.race([promise.catch(() => fallback), new Promise<T>((resolve) => setTimeout(() => resolve(fallback), ms))]);
const relayUrl = (url: unknown) => {
  try { const u = new URL(String(url)); return u.protocol === 'wss:' ? u.href.replace(/\/$/, '') : null; } catch { return null; }
};
const unique = (urls: (string | null)[]) => [...new Set(urls.filter((u): u is string => !!u))];

async function extensionRelays(): Promise<string[]> {
  try {
    const none: Record<string, { read: boolean; write: boolean }> = {};
    const relays = await timeout(extension()?.getRelays?.() ?? Promise.resolve(none), 1000, none);
    return unique(Object.entries(relays || {}).filter(([, v]) => v?.write).map(([url]) => relayUrl(url))).slice(0, 8);
  } catch { return []; }
}
async function query(relays: string[], filter: Parameters<SimplePool['querySync']>[1]): Promise<Event[]> {
  if (!relays.length) return [];
  return timeout(relayPool().querySync(relays, filter, { maxWait: RELAY_WAIT }), RELAY_WAIT + 1000, []);
}
const newest = (events: Event[], pubkey: string, kind: number) =>
  events.filter((e) => e.kind === kind && e.pubkey === pubkey).sort((a, b) => b.created_at - a.created_at)[0];

/** Strips control characters and cuts to the server's 40-character limit. */
export function cleanName(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const text = value.replace(/\s+/g, ' ').replace(/[\p{Cc}\p{Cf}]/gu, '').trim();
  return Array.from(text).slice(0, MAX_NAME).join('').trim() || null;
}
/** display_name, then name, then the NIP-05 local part. */
export function nameFrom(meta: Record<string, unknown>): string | null {
  const nip05 = typeof meta.nip05 === 'string' ? meta.nip05.split('@')[0] : null;
  for (const candidate of [meta.display_name, meta.displayName, meta.name, nip05 === '_' ? null : nip05]) {
    const name = cleanName(candidate);
    if (name) return name;
  }
  return null;
}
export function pictureFrom(meta: Record<string, unknown>): string | null {
  try { const url = new URL(String(meta.picture ?? '')); return url.protocol === 'https:' ? url.href : null; } catch { return null; }
}

/** Kind 0 metadata and the NIP-65 write relays, newest of each. */
export async function lookup(pubkey: string): Promise<NostrProfile> {
  const relays = unique([...defaults, ...await extensionRelays()]);
  const events = await query(relays, { kinds: [0, 10002], authors: [pubkey], limit: 10 });
  let meta: Record<string, unknown> = {};
  try { const parsed = JSON.parse(newest(events, pubkey, 0)?.content || '{}'); if (parsed && typeof parsed === 'object') meta = parsed; } catch { /* not JSON */ }
  const list = newest(events, pubkey, 10002);
  const write = (list?.tags || []).filter((t) => t[0] === 'r' && (!t[2] || t[2] === 'write')).map((t) => relayUrl(t[1]));
  return { name: nameFrom(meta), picture: pictureFrom(meta), relays: unique(write).slice(0, 8) };
}

/** The profile picture, if this browser may fetch it (CORS) and it isn't huge. */
export async function fetchPicture(url: string | null): Promise<Blob | null> {
  if (!url) return null;
  try {
    const response = await fetch(url, { mode: 'cors', credentials: 'omit', referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(5000) });
    if (!response.ok || Number(response.headers.get('content-length') || 0) > 5 * 1024 * 1024) return null;
    const blob = await response.blob();
    return blob.size && blob.size <= 5 * 1024 * 1024 ? blob : null;
  } catch { return null; }
}

// --- the wallet vault ----------------------------------------------------------------

export interface Vault { root: string; ciphertext: string; check: string }
export const newRoot = () => bytesToHex(randomBytes(32));
/** Public commitment to a vault secret, to recognise the right one. */
export const vaultCheck = (root: string) =>
  bytesToHex(hkdf(sha256, hexToBytes(root), undefined, new TextEncoder().encode('Cashu_NFT_Vault_Check_v1'), 16));

async function openVault(login: Login, ciphertext: string, check?: string): Promise<Vault | null> {
  let root: string;
  // A pasted key that can't decrypt is the wrong key; an extension error is
  // more likely a declined prompt, so it surfaces.
  try { root = (await login.holder.decrypt(ciphertext)).trim(); } catch (error) {
    if (login.method === 'nip07') throw error;
    return null;
  }
  if (!/^[0-9a-f]{64}$/.test(root) || (check && vaultCheck(root) !== check)) return null;
  return { root, ciphertext, check: vaultCheck(root) };
}

/** This collection's vault: the copy cached here, the server's, then the relays'.
 *  `cached` skips decryption when it matches the server's check value. */
export async function findVault(login: Login, cached?: string): Promise<(Vault & { onServer: boolean }) | null> {
  let server: { ciphertext: string; check: string } | null = null;
  try {
    server = await (await signedRequest(login.signer, `/api/profiles/${login.pubkey}/vault/get`)).json();
  } catch (error) {
    if (!/No wallet key/.test((error as Error).message)) throw error;
  }
  if (server) {
    if (cached && vaultCheck(cached) === server.check) return { root: cached, ...server, onServer: true };
    const vault = await openVault(login, server.ciphertext, server.check);
    if (!vault) throw new Error('Your wallet key on this server can’t be decrypted with this Nostr key.');
    return { ...vault, onServer: true };
  }
  const relays = unique([...defaults, ...(await login.nostr).relays]);
  const events = (await query(relays, { kinds: [VAULT_KIND], authors: [login.pubkey], '#d': [VAULT_D] }))
    .filter((e) => e.pubkey === login.pubkey && e.tags.some((t) => t[0] === 'd' && t[1] === VAULT_D))
    .sort((a, b) => b.created_at - a.created_at);
  for (const event of events) {
    const vault = await openVault(login, event.content);
    if (vault) return { ...vault, onServer: false };
  }
  return null;
}

export async function sealVault(login: Login, root: string): Promise<Vault> {
  return { root, ciphertext: await login.holder.encrypt(root), check: vaultCheck(root) };
}

/** The backup copy on the user's relays (their NIP-65 write relays, else the defaults). */
export async function publishVault(login: Login, vault: Vault): Promise<boolean> {
  const own = (await login.nostr).relays;
  const relays = own.length ? own : defaults;
  if (!relays.length) return false;
  const event = await login.holder.signEvent({ created_at: Math.floor(Date.now() / 1000), kind: VAULT_KIND, tags: [['d', VAULT_D]], content: vault.ciphertext });
  const within = (p: Promise<string>) => Promise.race([p, new Promise<never>((_, reject) => setTimeout(() => reject(new Error('timeout')), 5000))]);
  const results = await Promise.allSettled(relayPool().publish(relays, event).map(within));
  return results.some((r) => r.status === 'fulfilled');
}

/** Creates the Nostr collection with its vault (both copies). */
export async function createCollection(login: Login, name: string, vault: Vault, onRelays: boolean) {
  const response = await signedRequest(login.signer, `/api/profiles/${login.pubkey}`,
    JSON.stringify({ name: cleanName(name) || 'Untitled collection', vault: { ciphertext: vault.ciphertext, check: vault.check } }), 'application/json');
  const profile = await response.json();
  const published = onRelays || await publishVault(login, vault).catch(() => false);
  return { profile, published };
}

/** Puts a found or restored vault back on the server and the relays. */
export async function storeVault(login: Login, vault: Vault, { server = true, relays = true } = {}) {
  if (server) await signedRequest(login.signer, `/api/profiles/${login.pubkey}/vault`, JSON.stringify({ ciphertext: vault.ciphertext, check: vault.check }), 'application/json');
  if (relays) await publishVault(login, vault).catch(() => false);
}

/** Whether a wallet key (from "Back up wallet key") opens this collection's
 *  backups: the money wallet's, else an NFT credential's. True if none exist. */
export async function rootMatches(login: Login, root: string): Promise<boolean> {
  const base = `/api/profiles/${login.pubkey}`;
  const money: { envelope: Sealed | null } = await (await signedRequest(login.signer, `${base}/money/backup/get`)).json();
  if (money.envelope) {
    try { await open(await moneyKey(root, 'snapshot'), `Cashu_Money_Snapshot_v1\n${login.pubkey}`, money.envelope); return true; }
    catch { return false; }
  }
  const backups: { cards: { id: string; h: string; encrypted_credential: Envelope }[] } = await (await signedRequest(login.signer, `${base}/wallet/recover`)).json();
  const card = backups.cards[0];
  if (!card) return true;
  const config = await getJSON('/api/config');
  const vault = new EncryptedVault(root, login.pubkey, config.keyset_id);
  try { await vault.decrypt(card.encrypted_credential, `card:${card.id}:${card.h}`); return true; }
  catch { return false; }
  finally { await vault.close(); }
}
