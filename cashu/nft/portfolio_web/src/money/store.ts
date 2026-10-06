// Encrypted persistence for the ordinary ecash wallet.
//
// Coco's IndexedDB repositories store proof secrets and key material in
// plaintext. Instead, Coco runs on its in-memory repositories and this module
// persists them as one AES-256-GCM snapshot: locally in IndexedDB and, as a
// backup, at the portfolio server. Every mutating repository call awaits the
// encrypted write before returning, so state reaches storage before Coco makes
// its next remote request. Decrypted spend material exists only in memory of
// the unlocked wallet. This does not protect against malicious same-origin
// JavaScript; it protects data at rest and server backups.
import { Amount } from '@cashu/cashu-ts';
import { MemoryRepositories } from '@cashu/coco-core';
import { hexToBytes, bytesToHex } from '@noble/hashes/utils.js';
import { local, openRecordStore, type RecordStore } from '../storage.ts';

const enc = new TextEncoder();
const copy = (b: Uint8Array) => Uint8Array.from(b);

// --- typed codec ---------------------------------------------------------------

export type Encoded = null | boolean | number | string | Encoded[] | { [k: string]: Encoded };

export function encode(value: unknown): Encoded {
  if (value === null || value === undefined) return null;
  if (value instanceof Amount) return { $amount: value.toString() };
  if (value instanceof Uint8Array) return { $u8: bytesToHex(value) };
  if (typeof value === 'bigint') return { $bigint: value.toString() };
  if (value instanceof Map) return { $map: Array.from(value.entries(), ([k, v]) => [encode(k), encode(v)]) as Encoded[] };
  if (value instanceof Set) return { $set: Array.from(value, encode) };
  if (Array.isArray(value)) return value.map(encode);
  if (typeof value === 'object') {
    const out: { [k: string]: Encoded } = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (typeof v === 'function') continue;
      out[k.startsWith('$') ? '$' + k : k] = encode(v);
    }
    return out;
  }
  return value as Encoded;
}

export function decode(value: Encoded): unknown {
  if (value === null) return undefined;
  if (Array.isArray(value)) return value.map(decode);
  if (typeof value !== 'object') return value;
  if ('$amount' in value) return Amount.from(value.$amount as string);
  if ('$u8' in value) return hexToBytes(value.$u8 as string);
  if ('$bigint' in value) return BigInt(value.$bigint as string);
  if ('$map' in value) return new Map((value.$map as Encoded[][]).map(([k, v]) => [decode(k), decode(v)]));
  if ('$set' in value) return new Set((value.$set as Encoded[]).map(decode));
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value)) {
    const d = decode(v);
    if (d !== undefined) out[k.startsWith('$$') ? k.slice(1) : k] = d;
  }
  return out;
}

// The persistable state of MemoryRepositories: each repository's own data
// fields (Maps/arrays of plain records). Cross-repository references (the
// history projection) are rebuilt by the constructor, not serialized.
const SKIP_FIELDS = new Set(['sendOperationRepository', 'meltOperationRepository', 'mintOperationRepository', 'mintQuoteRepository', 'receiveOperationRepository']);

export function snapshotOf(repos: MemoryRepositories): Encoded {
  const out: Record<string, Encoded> = {};
  for (const [name, repo] of Object.entries(repos)) {
    if (!repo || typeof repo !== 'object' || !name.endsWith('Repository') || name === 'historyRepository') continue;
    const fields: Record<string, Encoded> = {};
    for (const [field, value] of Object.entries(repo as Record<string, unknown>)) {
      if (SKIP_FIELDS.has(field) || typeof value === 'function') continue;
      fields[field] = encode(value);
    }
    out[name] = fields;
  }
  return out;
}

export function restoreInto(repos: MemoryRepositories, snapshot: Encoded): void {
  const data = snapshot as Record<string, Record<string, Encoded>>;
  for (const [name, fields] of Object.entries(data)) {
    const repo = (repos as unknown as Record<string, Record<string, unknown>>)[name];
    if (!repo) continue;
    for (const [field, value] of Object.entries(fields)) {
      if (field in repo) repo[field] = decode(value);
    }
  }
}

// --- keys -------------------------------------------------------------------------

// `root` is the profile's HKDF root (signer.ts `Profile.root`).
async function hkdfMaterial(root: string) {
  return crypto.subtle.importKey('raw', copy(hexToBytes(root)), 'HKDF', false, ['deriveBits', 'deriveKey']);
}
const SALT = () => copy(enc.encode('Cashu_Money_v1'));

/** Domain-separated, profile-scoped and independent of the NFT keyset. */
export async function moneySeed(root: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: SALT(), info: copy(enc.encode('Cashu_Money_Seed_v1')) }, await hkdfMaterial(root), 512));
}

/** Separate signing keys for operation authorizations (HTLC refunds/claims). */
export async function authorizationKey(root: string, label: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: SALT(), info: copy(enc.encode(`Cashu_Money_Authorization_v1/${label}`)) }, await hkdfMaterial(root), 256));
}

export async function moneyKey(root: string, purpose: string): Promise<CryptoKey> {
  return crypto.subtle.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt: SALT(), info: copy(enc.encode(`Cashu_Money_Encryption_v1/${purpose}`)) },
    await hkdfMaterial(root), { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
}

export interface Sealed { version: string; nonce: string; ciphertext: string }

export async function seal(key: CryptoKey, aad: string, value: unknown): Promise<Sealed> {
  const nonce = crypto.getRandomValues(new Uint8Array(12));
  const plain = enc.encode(JSON.stringify(encode(value)));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce, additionalData: copy(enc.encode(aad)) }, key, plain));
  return { version: '1', nonce: bytesToHex(nonce), ciphertext: bytesToHex(ct) };
}

export async function open<T>(key: CryptoKey, aad: string, sealed: Sealed): Promise<T> {
  if (sealed.version !== '1') throw new Error('Unsupported wallet record version');
  const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: copy(hexToBytes(sealed.nonce)), additionalData: copy(enc.encode(aad)) }, key, copy(hexToBytes(sealed.ciphertext)));
  return decode(JSON.parse(new TextDecoder().decode(plain))) as T;
}

// --- local storage ----------------------------------------------------------------

export class LocalRecords {
  private store: Promise<RecordStore>;
  constructor(pubkey: string) { this.store = openRecordStore(`cashu-money-v1:${pubkey}`, 'records'); }
  /** False when the browser keeps nothing on this device (memory fallback). */
  async persistent() { return (await this.store).persistent; }
  async get<T>(id: string): Promise<T | undefined> { return (await (await this.store).get<{ id: string; value: T }>(id))?.value; }
  async put(id: string, value: unknown): Promise<void> { await (await this.store).put({ id, value }); }
  async remove(id: string): Promise<void> { await (await this.store).remove(id); }
  async all(): Promise<{ id: string; value: unknown }[]> { return (await this.store).all<{ id: string; value: unknown }>(); }
  async close() { (await this.store).close(); }
}

// --- encrypted snapshot repositories ----------------------------------------------

export interface RemoteBackup {
  lease(device: string, takeover?: boolean, release?: boolean): Promise<{ granted: boolean; holder: string; until: number }>;
  get(): Promise<{ revision: number; envelope: Sealed | null }>;
  put(device: string, baseRevision: number, revision: number, envelope: Sealed): Promise<{ revision: number }>;
}

const READ_PREFIXES = ['get', 'is', 'find', 'list', 'has', 'count', 'init'];
const isRead = (name: string) => READ_PREFIXES.some((p) => name.startsWith(p));

export class EncryptedRepositories extends MemoryRepositories {
  revision = 0;
  device = '';
  /** True when nothing persists on this device: every change is pushed to the server backup right away. */
  ephemeral = false;
  private syncTimer: ReturnType<typeof setTimeout> | null = null;
  readOnly = false;
  private key!: CryptoKey;
  private local!: LocalRecords;
  private aad = '';
  private remote: RemoteBackup | null = null;
  private writing: Promise<void> = Promise.resolve();
  private dirtyRemote = false;
  private leaseUntil = 0;

  static async open(root: string, pubkey: string, remote: RemoteBackup | null): Promise<EncryptedRepositories> {
    const repos = new EncryptedRepositories();
    repos.local = new LocalRecords(pubkey);
    repos.key = await moneyKey(root, 'snapshot');
    repos.aad = `Cashu_Money_Snapshot_v1\n${pubkey}`;
    repos.remote = remote;
    repos.ephemeral = !(await repos.local.persistent());
    // Without on-device storage, keep the device id in localStorage so a
    // reload is the same device (and not locked out by its own lease).
    const deviceKey = `cashu-money-device:${pubkey}`;
    let device = await repos.local.get<string>('device');
    if (!device && repos.ephemeral) { device = local.get(deviceKey) ?? undefined; }
    if (!device) {
      device = bytesToHex(crypto.getRandomValues(new Uint8Array(16)));
      await repos.local.put('device', device);
      if (repos.ephemeral) local.set(deviceKey, device);
    }
    repos.device = device;
    await repos.load();
    // Wrap only after loading so hydration doesn't count as a mutation.
    for (const [name, repo] of Object.entries(repos)) {
      if (!repo || typeof repo !== 'object' || !name.endsWith('Repository')) continue;
      (repos as unknown as Record<string, unknown>)[name] = repos.wrap(repo as Record<string, unknown>);
    }
    return repos;
  }

  /** Newest of the local and remote snapshots wins. */
  private async load(): Promise<void> {
    const saved = await this.local.get<{ revision: number; envelope: Sealed }>('snapshot');
    let best = saved ?? null;
    if (this.remote) {
      try {
        const remote = await this.remote.get();
        if (remote.envelope && remote.revision > (best?.revision ?? -1)) best = { revision: remote.revision, envelope: remote.envelope };
      } catch { /* offline: the local snapshot stands until reconnect */ }
    }
    if (best) {
      restoreInto(this, await open(this.key, this.aad, best.envelope));
      this.revision = best.revision;
      await this.local.put('snapshot', best);
    }
  }

  /** Acquire the single-writer lease. Without it the wallet is read-only here. */
  async acquireLease(takeover = false): Promise<boolean> {
    if (!this.remote) return true;
    // Renewing a lease we hold needs no backup re-read; only a change of
    // holder (or takeover) can mean another device wrote in between.
    const renewing = !this.readOnly && this.leaseUntil > 0 && !takeover;
    if (renewing && Date.now() / 1000 < this.leaseUntil - 60) return true;
    const lease = await this.remote.lease(this.device, takeover);
    this.leaseUntil = lease.granted ? lease.until : 0;
    if (lease.granted && !renewing) {
      // Always continue from the latest backup another device may have written.
      const remote = await this.remote.get();
      if (remote.envelope && remote.revision > this.revision) {
        restoreInto(this, await open(this.key, this.aad, remote.envelope));
        this.revision = remote.revision;
        await this.local.put('snapshot', { revision: remote.revision, envelope: remote.envelope });
      }
    }
    this.readOnly = !lease.granted;
    return lease.granted;
  }

  private wrap(repo: Record<string, unknown>) {
    return new Proxy(repo, {
      get: (target, prop, receiver) => {
        const value = Reflect.get(target, prop, receiver);
        if (typeof value !== 'function' || prop === 'constructor') return value;
        const method = value as (...a: unknown[]) => unknown;
        // Always run against the real repository: its internal helpers
        // (makeKey, quoteKey, ...) are synchronous and must not see the proxy.
        if (typeof prop !== 'string' || isRead(prop) || method.constructor.name !== 'AsyncFunction') {
          return method.bind(target);
        }
        return async (...args: unknown[]) => {
          if (this.readOnly) throw new Error('This wallet is open on another device. Take over to make changes here.');
          const result = await method.apply(target, args);
          await this.persist();
          return result;
        };
      },
    });
  }

  /** Serialize writes; each mutation is durable locally before returning. */
  persist(): Promise<void> {
    this.writing = this.writing.then(async () => {
      const envelope = await seal(this.key, this.aad, snapshotOf(this));
      this.revision += 1;
      await this.local.put('snapshot', { revision: this.revision, envelope });
      this.dirtyRemote = true;
      if (this.ephemeral && this.remote && !this.syncTimer) {
        this.syncTimer = setTimeout(() => { this.syncTimer = null; this.sync().catch(() => { this.dirtyRemote = true; }); }, 250);
      }
    });
    return this.writing;
  }

  /** Push the latest snapshot to the server backup. Revisions are numbered
   *  by the writer; the server compares-and-swaps on them. */
  async sync(): Promise<void> {
    if (!this.remote || !this.dirtyRemote || this.readOnly) return;
    await this.writing;
    const saved = await this.local.get<{ revision: number; envelope: Sealed }>('snapshot');
    if (!saved) return;
    const remote = await this.remote.get();
    if (remote.revision > saved.revision) throw new Error('Another device changed this wallet. Reload before continuing.');
    if (remote.revision < saved.revision) await this.remote.put(this.device, remote.revision, saved.revision, saved.envelope);
    this.dirtyRemote = false;
  }

  /** Give up the single-writer lease (closing the wallet on this device). */
  async releaseLease(): Promise<void> {
    if (this.remote && !this.readOnly) await this.remote.lease(this.device, false, true);
  }

  async close() {
    await this.writing;
    if (this.syncTimer) { clearTimeout(this.syncTimer); this.syncTimer = null; await this.sync().catch(() => {}); }
    await this.local.close();
  }
}
