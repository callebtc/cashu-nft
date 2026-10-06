// Who a collection is (its public key and signer) and what its wallets derive
// from (its root secret). A collection made with its own key uses that key
// for both. A Nostr collection signs with the Nostr key (pasted, or inside a
// signing extension) and derives its wallets from a separate vault secret
// (NOSTR_LOGIN_PLAN.md).
import { schnorr } from '@noble/curves/secp256k1.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';
import { profileKey } from './crypto.mjs';

export type Purpose = 'auth' | 'claim' | 'listing' | 'offer' | 'accept';
/** Background work (recovery, reconciliation) passes `interactive: false`:
 *  a signer that would prompt the user defers instead. `id` names the
 *  operation, so a deferred one is counted once however often it retries. */
export interface SignOptions { interactive?: boolean; id?: string }

export interface Signer {
  readonly pubkey: string;
  /** Signs an owner request's challenge message. `session` names the
   *  session key that signed in place of the profile key, if any. */
  auth(message: string): Promise<{ signature: string; session?: string }>;
  /** A public profile signature over a 32-byte digest (crypto.mjs `verifySignature`). */
  sign(purpose: Purpose, digest: Uint8Array, opts?: SignOptions): Promise<string>;
}

export interface Profile {
  readonly pubkey: string;
  readonly signer: Signer;
  /** HKDF root of every wallet, encryption and market key (64 hex). */
  readonly root: string;
}

/** Thrown instead of prompting during background work; the signer queues it. */
export class SignatureDeferred extends Error {
  constructor(readonly purpose: Purpose) { super('Waiting for your signature'); this.name = 'SignatureDeferred'; }
}
export const isDeferred = (error: unknown): error is SignatureDeferred => error instanceof SignatureDeferred;

/** Operations waiting for a signature the user hasn't been asked for yet. */
const waiting = new Set<string>(), listeners = new Set<(count: number) => void>();
export const deferred = {
  add(id: string) { if (!waiting.has(id)) { waiting.add(id); listeners.forEach((f) => f(waiting.size)); } },
  clear() { if (waiting.size) { waiting.clear(); listeners.forEach((f) => f(0)); } },
  get count() { return waiting.size; },
  subscribe(listener: (count: number) => void) { listeners.add(listener); return () => { listeners.delete(listener); }; },
};

const utf8 = (s: string) => new TextEncoder().encode(s);

/** Signs with a private key held in this page. */
export function localSigner(secret: string): Signer {
  const key = secret.trim().toLowerCase(), pubkey: string = profileKey(key), raw = hexToBytes(key);
  return {
    pubkey,
    async auth(message) { return { signature: bytesToHex(schnorr.sign(sha256(utf8(message)), raw)) }; },
    async sign(_purpose, digest) { return bytesToHex(schnorr.sign(digest, raw)); },
  };
}

/** A collection that uses its own key: it signs, and its wallets derive from it. */
export function keyProfile(secret: string): Profile {
  const signer = localSigner(secret);
  return { pubkey: signer.pubkey, signer, root: secret.trim().toLowerCase() };
}
export const asProfile = (profile: string | Profile): Profile => (typeof profile === 'string' ? keyProfile(profile) : profile);
export const asSigner = (signer: string | Signer): Signer => (typeof signer === 'string' ? localSigner(signer) : signer);
