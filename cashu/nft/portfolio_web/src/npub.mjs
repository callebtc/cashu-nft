// NIP-19 public keys (npub) for routes, lookups and display. Private keys
// and everything else Nostr live in nostr.ts, which loads on demand.
import { bech32 } from '@scure/base';

export function npubEncode(hex) {
  return bech32.encode('npub', bech32.toWords(Uint8Array.from(hex.match(/../g).map((b) => parseInt(b, 16)))), 5000);
}
/** The hex public key of an npub, or null. */
export function npubDecode(text) {
  try {
    const { prefix, words } = bech32.decode(text.trim().toLowerCase(), 5000);
    const bytes = bech32.fromWords(words);
    return prefix === 'npub' && bytes.length === 32 ? Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('') : null;
  } catch { return null; }
}
/** A hex public key, an npub, a nostr:npub URI, or a /p/ link to either. */
export function publicKeyIn(text) {
  const value = text.trim().replace(/^nostr:/i, '');
  const hex = value.match(/(?:^|\/p\/)([0-9a-f]{64})\/?$/)?.[1];
  if (hex) return hex;
  const npub = value.match(/(?:^|\/p\/)(npub1[0-9a-z]+)\/?$/i)?.[1];
  return npub ? npubDecode(npub) : null;
}
export const shortNpub = (hex) => { const n = npubEncode(hex); return `${n.slice(0, 10)}…${n.slice(-6)}`; };
