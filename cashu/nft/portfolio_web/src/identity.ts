// Collections stored in this browser (the keyring) and the identities they
// unlock. Kept free of nostr-tools: restoring a Nostr collection on page load
// needs only the stored session key and the extension (NOSTR_LOGIN_PLAN.md).
import { schnorr } from '@noble/curves/secp256k1.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';
import { eventHash, eventSignature, profileKey, signatureEvent } from './crypto.mjs';
import { local, tab } from './storage.ts';
import { SignatureDeferred, deferred, keyProfile, localSigner, type Profile, type Purpose, type Signer } from './signer.ts';

export const KEYRING = 'cashu-nft-keys-v1', ACTIVE = 'cashu-nft-active-v1';
const UNLOCKED = 'cashu-nft-unlocked-v1:';
const HEX64 = /^[0-9a-f]{64}$/;

/** A string is the private key of a collection that uses its own key (the
 *  only kind before Nostr login). Objects are Nostr collections; `vault` is
 *  their wallet root secret. A password-protected key keeps both per tab. */
export type NostrEntry =
  | { kind: 'nsec'; secret: string; vault: string }
  | { kind: 'ncryptsec'; ncryptsec: string }
  | { kind: 'nip07'; session: Session; vault: string };
export type Entry = string | NostrEntry;
export interface Session { secret: string; expires: number }

export interface Identity extends Profile {
  readonly kind: 'key' | 'nsec' | 'ncryptsec' | 'nip07';
  readonly nostr: boolean;
  /** Collections that use their own key: the key "Back up private key" shows. */
  readonly secret?: string;
}

export function storedKeys(): Record<string, Entry> {
  try { const keys = JSON.parse(local.get(KEYRING) || '{}'); return keys && typeof keys === 'object' && !Array.isArray(keys) ? keys : {}; }
  catch { return {}; }
}
/** Only collections that use their own key, as hex: what the origin move carries. */
export const keyCollections = (): Record<string, string> =>
  Object.fromEntries(Object.entries(storedKeys()).filter((e): e is [string, string] => typeof e[1] === 'string'));

export function saveEntry(pubkey: string, entry: Entry) {
  local.set(KEYRING, JSON.stringify({ ...storedKeys(), [pubkey]: entry }));
  local.set(ACTIVE, pubkey);
}
export function forget(pubkey: string) {
  const keys = storedKeys();
  delete keys[pubkey];
  local.set(KEYRING, JSON.stringify(keys));
  tab.remove(UNLOCKED + pubkey);
  if (local.get(ACTIVE) === pubkey) {
    const next = Object.keys(keys)[0];
    if (next) local.set(ACTIVE, next); else local.remove(ACTIVE);
  }
}
/** A password-protected key, decrypted for this tab. */
export function unlockForTab(pubkey: string, secret: string, vault: string) {
  tab.set(UNLOCKED + pubkey, JSON.stringify({ secret, vault }));
}

/** The identity a stored entry unlocks now, or null while it needs a sign-in
 *  (an expired extension session, or a password not yet entered in this tab). */
export function identityFor(pubkey: string, entry: Entry): Identity | null {
  try {
    if (typeof entry === 'string') {
      const profile = keyProfile(entry);
      return profile.pubkey === pubkey ? { ...profile, kind: 'key', nostr: false, secret: profile.root } : null;
    }
    if (entry.kind === 'nsec') return nostrIdentity(pubkey, 'nsec', localSigner(entry.secret), entry.vault);
    if (entry.kind === 'ncryptsec') {
      const unlocked = JSON.parse(tab.get(UNLOCKED + pubkey) || 'null');
      return unlocked ? nostrIdentity(pubkey, 'ncryptsec', localSigner(unlocked.secret), unlocked.vault) : null;
    }
    if (entry.kind === 'nip07' && entry.session.expires > Date.now() / 1000 + 60) {
      return nostrIdentity(pubkey, 'nip07', extensionSigner(pubkey, entry.session), entry.vault);
    }
  } catch { /* malformed entry */ }
  return null;
}
function nostrIdentity(pubkey: string, kind: Identity['kind'], signer: Signer, root: string): Identity | null {
  if (signer.pubkey !== pubkey || !HEX64.test(root)) return null;
  return { pubkey, signer, root, kind, nostr: true };
}

export function initialIdentity(): Identity | null {
  const active = local.get(ACTIVE), entry = active ? storedKeys()[active] : undefined;
  return active && entry ? identityFor(active, entry) : null;
}
/** The active Nostr collection that needs a sign-in before it opens. */
export function lockedEntry(): { pubkey: string; entry: NostrEntry } | null {
  const active = local.get(ACTIVE), entry = active ? storedKeys()[active] : undefined;
  return active && entry && typeof entry !== 'string' && !identityFor(active, entry) ? { pubkey: active, entry } : null;
}

// --- NIP-07 signing extensions ------------------------------------------------

interface SignedEvent { id: string; pubkey: string; created_at: number; kind: number; tags: string[][]; content: string; sig: string }
export interface NostrExtension {
  getPublicKey(): Promise<string>;
  signEvent(event: { created_at: number; kind: number; tags: string[][]; content: string }): Promise<SignedEvent>;
  nip44?: { encrypt(pubkey: string, plaintext: string): Promise<string>; decrypt(pubkey: string, ciphertext: string): Promise<string> };
  getRelays?(): Promise<Record<string, { read: boolean; write: boolean }>>;
}
export const extension = (): NostrExtension | undefined => (globalThis as { nostr?: NostrExtension }).nostr;

/** Extensions inject `window.nostr` after the page loads; wait up to `ms`. */
export async function waitForExtension(ms = 1000): Promise<NostrExtension | undefined> {
  for (const end = Date.now() + ms; !extension() && Date.now() < end;) await new Promise((r) => setTimeout(r, 50));
  return extension();
}

/** Signs a Nostr event through the extension and checks what comes back. */
export async function signWithExtension(template: { created_at: number; kind: number; tags: string[][]; content: string }, pubkey: string): Promise<SignedEvent> {
  const ext = extension();
  if (!ext) throw new Error('No Nostr extension found. Paste your key instead.');
  const signed = await ext.signEvent(template);
  if (!signed || signed.pubkey !== pubkey) throw new Error('Your Nostr extension is signed in as another account.');
  if (!Number.isSafeInteger(signed.created_at) || signed.created_at < 0 || signed.created_at > 9999999999) throw new Error('Your Nostr extension returned an invalid event.');
  return signed;
}

/** A profile signature (crypto.mjs `verifySignature`) made by the extension. */
export async function extensionSignature(pubkey: string, purpose: Purpose, digest: Uint8Array): Promise<string> {
  const { pubkey: _, ...template } = signatureEvent(pubkey, purpose, digest, Math.floor(Date.now() / 1000));
  const signed = await signWithExtension(template, pubkey);
  const id = eventHash(signatureEvent(pubkey, purpose, digest, signed.created_at));
  let valid = false;
  try { valid = schnorr.verify(hexToBytes(signed.sig), id, hexToBytes(pubkey)); } catch { /* malformed */ }
  if (!valid) throw new Error('Your Nostr extension returned an invalid signature.');
  return eventSignature(signed);
}

/** Owner requests go through a session key the extension authorized once;
 *  public signatures (showings, listings, offers) still ask the extension. */
export function extensionSigner(pubkey: string, session: Session): Signer {
  const key = hexToBytes(session.secret), sessionPubkey: string = profileKey(session.secret);
  return {
    pubkey,
    async auth(message) {
      if (Date.now() / 1000 >= session.expires) throw new Error('Your Nostr sign-in has expired. Sign in again.');
      return { signature: bytesToHex(schnorr.sign(sha256(new TextEncoder().encode(message)), key)), session: sessionPubkey };
    },
    async sign(purpose, digest, opts) {
      if (opts?.interactive === false) {
        deferred.add(opts.id ?? purpose + ':' + bytesToHex(digest));
        throw new SignatureDeferred(purpose);
      }
      return extensionSignature(pubkey, purpose, digest);
    },
  };
}
