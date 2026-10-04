import { bls12_381 as bls } from '@noble/curves/bls12-381.js';
import { schnorr } from '@noble/curves/secp256k1.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes, concatBytes } from '@noble/hashes/utils.js';

export { bytesToHex, hexToBytes, sha256, schnorr };
const utf8 = new TextEncoder();
export const textBytes = (s) => utf8.encode(s);
const ORDER = 52435875175126190479447740508185965837690552500527637822603658699938581184513n;
const G_NULL = bls.G1.hashToCurve(textBytes('ps_nullifier_base'), { DST: 'CASHU_PS_GNULL_XMD:SHA-256_SSWU_RO_' });
const number = (b) => BigInt('0x' + bytesToHex(b));
const prefix = (size, length) => {
  const b = new Uint8Array(size);
  new DataView(b.buffer)[size === 2 ? 'setUint16' : 'setUint32'](0, length, false);
  return b;
};
const frame = (b, size = 2) => concatBytes(prefix(size, b.length), b);
const mul = (p, n) => n === 0n ? p.constructor.ZERO : p.multiplyUnsafe(n);
const scalar = (b) => {
  const n = number(b);
  if (n >= ORDER) throw new Error('Noncanonical scalar');
  return n;
};
const point = (group, b) => {
  const p = group.Point.fromHex(bytesToHex(b));
  p.assertValidity();
  if (p.equals(group.Point.ZERO)) throw new Error('Invalid infinity point');
  if (bytesToHex(p.toBytes(true)) !== bytesToHex(b)) throw new Error('Noncanonical point');
  return p;
};

export function profileKey(secret) {
  const raw = hexToBytes(secret.trim().toLowerCase());
  if (raw.length !== 32) throw new Error('Use a 64-character private key.');
  return bytesToHex(schnorr.getPublicKey(raw));
}
export function newPrivateKey() { return bytesToHex(schnorr.utils.randomSecretKey()); }
export function signMessage(secret, message) {
  return bytesToHex(schnorr.sign(sha256(textBytes(message)), hexToBytes(secret)));
}
export function signClaim(secret, showing) {
  return signMessage(secret, 'Cashu_NFT_Portfolio_Claim_v1\n' + showing);
}
export function expectedContext(pubkey, h, keyset) {
  return `Cashu_NFT_Portfolio_Show_v1\n${pubkey}\n${h}\n${keyset}`;
}
export function parseShowing(token) {
  if (!token.startsWith('pshow1')) throw new Error('Invalid showing prefix');
  const raw = hexToBytes(token.slice(6));
  if (raw.length < 2) throw new Error('Truncated showing');
  const len = new DataView(raw.buffer, raw.byteOffset, raw.byteLength).getUint16(0, false);
  if (raw.length !== 2 + len + 321) throw new Error('Invalid showing size');
  return { context: raw.slice(2, 2 + len), presentation: raw.slice(2 + len) };
}
export function validateKeyset(config) {
  const raw = hexToBytes(config.public_key);
  if (raw.length !== 336) throw new Error('Invalid mint parameters');
  const parts = [raw.slice(0, 96), raw.slice(96, 192), raw.slice(192, 288), raw.slice(288)];
  const derived = '03' + bytesToHex(sha256(concatBytes(...parts.map((p) => frame(p, 4)), frame(textBytes('psnft'), 4))));
  if (config.keyset_id !== derived) throw new Error('The mint parameters do not match its keyset.');
  // Decode all public points, including the G1 parameter committed by keyset ID.
  const [X2, Yh2, Ys2] = parts.slice(0, 3).map((p) => point(bls.G2, p));
  point(bls.G1, parts[3]);
  return { X2, Yh2, Ys2 };
}

export function verifyCard(card, expectedPubkey, config) {
  try {
    if (card.pubkey !== expectedPubkey) throw new Error('Wrong profile');
    const { context, presentation: p } = parseShowing(card.showing);
    const keyset = bytesToHex(p.slice(0, 33));
    if (keyset !== config.keyset_id) throw new Error('Untrusted mint keyset');
    const h = scalar(p.slice(33, 65));
    if (bytesToHex(p.slice(33, 65)) !== card.h) throw new Error('Wrong picture identity');
    const expected = textBytes(expectedContext(expectedPubkey, card.h, keyset));
    if (bytesToHex(context) !== bytesToHex(expected)) throw new Error('Wrong profile context');
    const [u, v, us, N] = [65, 113, 161, 209].map((offset) => point(bls.G1, p.slice(offset, offset + 48)));
    const c = scalar(p.slice(257, 289)), r = scalar(p.slice(289, 321));
    const commitments = [mul(G_NULL, r).subtract(mul(N, c)), mul(u, r).subtract(mul(us, c))];
    const binding = concatBytes(textBytes('Cashu_PS_Showing_v1'), frame(context));
    const transcript = concatBytes(textBytes('Cashu_PS_Present_v1'), frame(binding),
      ...[G_NULL, u, N, us, ...commitments].map((p) => frame(p.toBytes(true))));
    if (number(sha256(transcript)) % ORDER !== c) throw new Error('Invalid owner proof');
    const { X2, Yh2, Ys2 } = validateKeyset(config);
    const paired = bls.pairingBatch([
      { g1: v.negate(), g2: bls.G2.Point.BASE },
      { g1: u, g2: X2.add(mul(Yh2, h)) },
      { g1: us, g2: Ys2 },
    ]);
    if (!bls.fields.Fp12.eql(paired, bls.fields.Fp12.ONE)) throw new Error('Invalid mint signature');
    if (!card.signature) return { valid: false, pending: true, reason: 'Awaiting profile signature' };
    if (!schnorr.verify(hexToBytes(card.signature), sha256(textBytes('Cashu_NFT_Portfolio_Claim_v1\n' + card.showing)), hexToBytes(expectedPubkey))) {
      throw new Error('Invalid profile signature');
    }
    return { valid: true, nullifier: bytesToHex(N.toBytes(true)) };
  } catch (error) {
    return { valid: false, reason: error.message };
  }
}
