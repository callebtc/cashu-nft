/** PS credentials extend cashu-ts BLS; they are not NUT-00 ecash proofs. */
import { BLS_FR_ORDER, createRandomBlsSecretKey, pointFromHexG1, pointFromHexG2, type G1Point, type G2Point } from '@cashu/cashu-ts';
import { bls12_381 as bls } from '@noble/curves/bls12-381.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, hexToBytes, concatBytes } from '@noble/hashes/utils.js';
import { expectedContext, signClaim, validateKeyset, verifyCard } from '../crypto.mjs';

export const ORDER = BLS_FR_ORDER;
export const utf8 = (s: string) => new TextEncoder().encode(s);
export const frame = (b: Uint8Array, size = 2) => concatBytes(integer(BigInt(b.length), size), b);
export const integer = (n: bigint, size = 32) => {
  if (n < 0n || n >= 1n << BigInt(size * 8)) throw new Error('Integer out of range');
  return hexToBytes(n.toString(16).padStart(size * 2, '0'));
};
const number = (b: Uint8Array) => BigInt('0x' + bytesToHex(b));
export const randomScalar = () => number(createRandomBlsSecretKey());
const challenge = (b: Uint8Array) => number(sha256(b)) % ORDER;
type Point = G1Point | G2Point;
const mul = <P extends Point>(p: P, s: bigint): P => p.multiplyUnsafe(s) as P;
const add = <P extends Point>(a: P, b: P): P => (a as G1Point).add(b as G1Point) as P;
const encoded = (p: Point) => p.toBytes(true);
export const g1 = bls.G1.Point.BASE; const g2 = bls.G2.Point.BASE;
export const G_NULL = bls.G1.hashToCurve(utf8('ps_nullifier_base'), { DST: 'CASHU_PS_GNULL_XMD:SHA-256_SSWU_RO_' });
const G_ASSET = bls.G1.hashToCurve(utf8('ps_asset_tag_base'), { DST: 'CASHU_PS_ASSET_TAG_XMD:SHA-256_SSWU_RO_' });
export interface MintConfig { keyset_id: string; public_key: string; max_image_bytes?: number; max_jpg_bytes?: number; }
export interface Credential { u: string; v: string; h: string; s: string; keyset_id: string; }
export interface Card { id: string; pubkey: string; h: string; title: string; showing: string; signature: string | null; status: string; created: number; sent: number | null; custody?: string; }
export const hashAsset = (file: Uint8Array) => challenge(concatBytes(utf8('Cashu_PS_Asset_v1'), frame(file, 4)));
export const nullifier = (cred: Credential) => bytesToHex(encoded(mul(G_NULL, scalar(cred.s, true))));
const scalar = (hex: string, nonzero = false) => {
  if (!/^[0-9a-f]{64}$/.test(hex)) throw new Error('Invalid scalar encoding');
  const n = BigInt('0x' + hex);
  if (n >= ORDER || (nonzero && !n)) throw new Error('Scalar out of range');
  return n;
};

export function dlog(bases: G1Point[], points: G1Point[], s: bigint, dst: string, binding: Uint8Array = new Uint8Array()): Uint8Array {
  const w = randomScalar();
  const commitments = bases.map(p => mul(p, w));
  const c = challenge(concatBytes(utf8(dst), frame(binding), ...[...bases, ...points, ...commitments].map(p => frame(encoded(p)))));
  return concatBytes(integer(c), integer((w + c * s) % ORDER));
}
type Statement = { point: Point; terms: [Point, number][] };
function linear(statements: Statement[], witnesses: bigint[], dst: string, binding: Uint8Array): Uint8Array {
  const nonces = witnesses.map(randomScalar);
  const terms: Uint8Array[] = [utf8(dst), frame(binding)];
  for (const { point, terms: bases } of statements) {
    const commitment = bases.map(([p, index]) => mul(p, nonces[index])).reduce((a, b) => add(a, b));
    terms.push(frame(encoded(point)), frame(encoded(commitment)));
    for (const [base, index] of bases) terms.push(frame(encoded(base)), integer(BigInt(index), 2));
  }
  const c = challenge(concatBytes(...terms));
  return concatBytes(integer(c), ...witnesses.map((w, i) => integer((nonces[i] + c * w) % ORDER)));
}
export function ownerCommitment(s: bigint) {
  const S = mul(g1, s);
  return { owner_commitment: bytesToHex(encoded(S)), owner_proof: bytesToHex(dlog([g1], [S], s, 'Cashu_PS_Issue_v1')) };
}
export function blindIssue(config: MintConfig, h: bigint, s: bigint, base: string, session: string) {
  validateKeyset(config);
  if (!/^[0-9a-f]{32}$/.test(session)) throw new Error('Invalid issuance session');
  const u = pointFromHexG1(base), t = randomScalar(), S = mul(g1, s), D = mul(G_ASSET, h);
  const B = add(mul(u, h), mul(g1, t));
  const proof = linear([
    { point: D, terms: [[G_ASSET, 0]] }, { point: B, terms: [[u, 0], [g1, 1]] }, { point: S, terms: [[g1, 2]] },
  ], [h, t, s], 'Cashu_PS_BlindIssue_v1', concatBytes(hexToBytes(config.keyset_id), hexToBytes(session)));
  return { t: bytesToHex(integer(t)), request: { session, asset_tag: bytesToHex(encoded(D)), b: bytesToHex(encoded(B)), owner_commitment: bytesToHex(encoded(S)), proof: bytesToHex(proof) } };
}
export function blindIssueV2(config: MintConfig, h: bigint, s: bigint, session: string) {
  validateKeyset(config);
  if (!/^[0-9a-f]{32}$/.test(session) || h < 0n || h >= ORDER || s <= 0n || s >= ORDER) throw new Error('Invalid issuance parameters');
  const Yh1 = pointFromHexG1(config.public_key.slice(576)), t = randomScalar();
  const S = mul(g1, s), D = mul(G_ASSET, h), C = add(mul(Yh1, h), mul(g1, t));
  const proof = linear([
    { point: D, terms: [[G_ASSET, 0]] }, { point: C, terms: [[Yh1, 0], [g1, 1]] }, { point: S, terms: [[g1, 2]] },
  ], [h, t, s], 'Cashu_PS_BlindIssue_v2', concatBytes(hexToBytes(config.keyset_id), hexToBytes(session)));
  return { t: bytesToHex(integer(t)), request: { version: 2 as const, session, asset_tag: bytesToHex(encoded(D)), b: bytesToHex(encoded(C)), owner_commitment: bytesToHex(encoded(S)), proof: bytesToHex(proof) } };
}
export function issueCommitment(config: MintConfig, h: bigint, s: bigint, session: string) {
  validateKeyset(config);
  if (!/^[0-9a-f]{32}$/.test(session) || h < 0n || h >= ORDER || s <= 0n || s >= ORDER) throw new Error('Invalid issuance parameters');
  const Yh1 = pointFromHexG1(config.public_key.slice(576));
  const S = mul(g1, s), D = mul(G_ASSET, h), C = mul(Yh1, h);
  const proof = linear([
    { point: D, terms: [[G_ASSET, 0]] }, { point: C, terms: [[Yh1, 0]] }, { point: S, terms: [[g1, 1]] },
  ], [h, s], 'Cashu_PS_CommittedIssue_v3', concatBytes(hexToBytes(config.keyset_id), hexToBytes(session)));
  return { version: 3 as const, session, asset_tag: bytesToHex(encoded(D)), b: bytesToHex(encoded(C)), owner_commitment: bytesToHex(encoded(S)), proof: bytesToHex(proof) };
}
export function finishIssue(config: MintConfig, h: string, s: string, response: { u: string; v: string; keyset_id: string }): Credential {
  if (response.keyset_id !== config.keyset_id) throw new Error('Mint changed the issuance keyset');
  const cred = { u: response.u, v: response.v, h, s, keyset_id: config.keyset_id };
  verifyCredential(cred, config);
  return cred;
}
export function finishBlindIssueV2(config: MintConfig, h: string, s: string, t: string, response: { u: string; v: string; keyset_id: string }): Credential {
  if (response.keyset_id !== config.keyset_id) throw new Error('Mint changed the issuance keyset');
  const u = pointFromHexG1(response.u);
  if (u.equals(bls.G1.Point.ZERO)) throw new Error('Invalid issuance base');
  const v = pointFromHexG1(response.v).subtract(mul(u, scalar(t, true)));
  const cred = { u: response.u, v: bytesToHex(encoded(v)), h, s, keyset_id: config.keyset_id };
  verifyCredential(cred, config);
  return cred;
}
function presentation(cred: Credential, binding: Uint8Array) {
  const rho = randomScalar(), s = scalar(cred.s, true), h = scalar(cred.h);
  const u = mul(pointFromHexG1(cred.u), rho), v = mul(pointFromHexG1(cred.v), rho);
  const us = mul(u, s), N = mul(G_NULL, s);
  return { h, u, v, us, N, proof: dlog([G_NULL, u], [N, us], s, 'Cashu_PS_Present_v1', binding) };
}
/** Public presentation (Python `Presentation.to_bytes`) bound to one purpose. */
export function boundPresentation(cred: Credential, binding: Uint8Array): string {
  verifyKeysetId(cred.keyset_id);
  const p = presentation(cred, binding);
  return bytesToHex(concatBytes(hexToBytes(cred.keyset_id), integer(p.h), ...[p.u, p.v, p.us, p.N].map(encoded), p.proof));
}
const verifyKeysetId = (id: string) => { if (!/^[0-9a-f]{66}$/.test(id)) throw new Error('Invalid NFT keyset'); };
export function showing(cred: Credential, pubkey: string) {
  const context = utf8(expectedContext(pubkey, cred.h, cred.keyset_id));
  const p = presentation(cred, concatBytes(utf8('Cashu_PS_Showing_v1'), frame(context)));
  return 'pshow1' + bytesToHex(concatBytes(frame(context), hexToBytes(cred.keyset_id), integer(p.h), ...[p.u, p.v, p.us, p.N].map(encoded), p.proof));
}
export function verifyCredential(cred: Credential, config: MintConfig) {
  if (cred.keyset_id !== config.keyset_id) throw new Error('This NFT belongs to another mint');
  scalar(cred.s, true); scalar(cred.h);
  const pubkey = '00'.repeat(32);
  const result = verifyCard({ pubkey, h: cred.h, showing: showing(cred, pubkey), signature: null }, pubkey, config);
  if (!result.pending) throw new Error(result.reason || 'Invalid NFT credential');
}
export function finishBlind(config: MintConfig, h: string, s: string, t: string, base: string, response: { u: string; v: string; keyset_id: string }): Credential {
  if (response.u !== base || response.keyset_id !== config.keyset_id) throw new Error('Mint changed the issuance base or keyset');
  const Yh1 = pointFromHexG1(config.public_key.slice(576));
  const v = pointFromHexG1(response.v).subtract(mul(Yh1, scalar(t, true)));
  const cred = { u: base, v: bytesToHex(encoded(v)), h, s, keyset_id: config.keyset_id };
  verifyCredential(cred, config);
  return cred;
}
export function blindTransfer(config: MintConfig, cred: Credential, s: bigint, base: string) {
  verifyCredential(cred, config);
  const owner = ownerCommitment(s), binding = hexToBytes(owner.owner_commitment);
  const p = presentation(cred, binding), o = randomScalar(), t = randomScalar();
  const Yh2 = pointFromHexG2(config.public_key.slice(192, 384));
  const kappa = add(mul(Yh2, p.h), mul(g2, o));
  const v = add(p.v, mul(p.u, o));
  const u2 = pointFromHexG1(base), B = add(mul(u2, p.h), mul(g1, t));
  const proof = linear([
    { point: kappa, terms: [[Yh2, 0], [g2, 1]] }, { point: B, terms: [[u2, 0], [g1, 2]] },
  ], [p.h, o, t], 'Cashu_PS_CommitEq_v1', binding);
  const wire = concatBytes(hexToBytes(cred.keyset_id), ...[p.u, v, kappa, p.us, p.N].map(encoded), p.proof);
  return { t: bytesToHex(integer(t)), request: { presentation: bytesToHex(wire), b: bytesToHex(encoded(B)), proof: bytesToHex(proof), new_owner_commitment: owner.owner_commitment, new_proof: owner.owner_proof } };
}
export const encodeToken = (cred: Credential) => 'psnft1' + bytesToHex(concatBytes(hexToBytes(cred.keyset_id), hexToBytes(cred.u), hexToBytes(cred.v), hexToBytes(cred.h), hexToBytes(cred.s)));
export function decodeToken(token: string): Credential {
  if (!/^psnft1[0-9a-f]{386}$/.test(token)) throw new Error('Invalid NFT transfer token');
  const b = token.slice(6);
  const cred = { keyset_id: b.slice(0, 66), u: b.slice(66, 162), v: b.slice(162, 258), h: b.slice(258, 322), s: b.slice(322) };
  pointFromHexG1(cred.u); pointFromHexG1(cred.v); scalar(cred.h); scalar(cred.s, true);
  return cred;
}
export function publicCard(cred: Credential, secret: string, pubkey: string) {
  const proof = showing(cred, pubkey);
  return { showing: proof, signature: signClaim(secret, proof) };
}
