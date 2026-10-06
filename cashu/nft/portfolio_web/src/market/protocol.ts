// Marketplace protocol v1 (`cashu-nft-offer-1`), browser side. Byte-for-byte
// counterpart of cashu/nft/market_protocol.py; see MARKETPLACE_PLAN.md
// ("Protocol v1"). Pure helpers, no I/O. Experimental cryptography: requires
// human review before production use.
import { Amount, SigAll } from '@cashu/cashu-ts';
import { schnorr } from '@noble/curves/secp256k1.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { bytesToHex, concatBytes, hexToBytes } from '@noble/hashes/utils.js';
import { dlog, g1, integer, utf8 } from '../wallet/ps.ts';
import { sealPreimage } from './escrow.mjs';
import type { SignOptions, Signer } from '../signer.ts';

export const PROTOCOL = 'cashu-nft-offer-1';
export const LISTING_PROTOCOL = 'cashu-nft-listing-1';
export const ACCEPT_PROTOCOL = 'cashu-nft-accept-1';
const OFFER_DOMAIN = 'Cashu_NFT_Market_Offer_v1\n';
const OFFER_SIG_DOMAIN = 'Cashu_NFT_Market_Offer_Sig_v1\n';
const LISTING_DOMAIN = 'Cashu_NFT_Market_Listing_v1\n';
const ACCEPT_DOMAIN = 'Cashu_NFT_Market_Accept_v1\n';
const RECEIPT_DOMAIN = 'Cashu_NFT_Market_Receipt_v1\n';
const RECEIVE_DST = 'Cashu_NFT_Market_Receive_v1';
const DELIVER_BINDING = 'Cashu_NFT_Market_Deliver_v1';
export const ACCEPT_WINDOW = 3600;
export const MAX_PRICE = 2_100_000_000_000_000;

export interface NftRef { keyset_id: string; h: string; nullifier: string; }
export interface Payment { mint: string; unit: 'sat'; keyset_id: string; amount: number; claim_fee: number; refund_fee: number; }
export interface OfferManifest {
  v: typeof PROTOCOL; offer_id: string; listing_id: string; listing_revision: number; nft: NftRef;
  seller: string; buyer: string; price: number; payment: Payment; hashlock: string;
  claim_pubkey: string; refund_pubkey: string; cash_deadline: number; accept_deadline: number;
  nft_destination: string; escrow_key: string; created: number;
}
export interface ListingManifest {
  v: typeof LISTING_PROTOCOL; listing_id: string; revision: number; card_id: string; seller: string;
  h: string; nullifier: string; nft_keyset: string; price: number; claim_pubkey: string; created: number;
}
export interface BlindedOutput { amount: number; id: string; B_: string; }
export interface Acceptance {
  v: typeof ACCEPT_PROTOCOL; offer_id: string; manifest_hash: string; claim_outputs: BlindedOutput[];
  claim_signature: string; accepted: number;
}
export interface DeliveryReceipt {
  v: string; offer_id: string; manifest_hash: string; nft_keyset: string; h: string;
  spent_nullifier: string; destination: string; u: string; v_: string; delivered: number;
}

/** Python `json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False)`. */
export function canonical(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    if (typeof value === 'number' && !Number.isSafeInteger(value)) throw new Error('Only safe integers are canonical');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  const entries = Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== undefined);
  entries.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return '{' + entries.map(([k, v]) => JSON.stringify(k) + ':' + canonical(v)).join(',') + '}';
}
const digest = (domain: string, value: unknown) => sha256(concatBytes(utf8(domain), utf8(canonical(value))));
export const manifestHash = (m: OfferManifest) => digest(OFFER_DOMAIN, m);
export const listingHash = (l: ListingManifest) => digest(LISTING_DOMAIN, l);
export const acceptanceHash = (a: Acceptance) => digest(ACCEPT_DOMAIN, a);
export const receiptHash = (r: DeliveryReceipt) => digest(RECEIPT_DOMAIN, r);

/** Profile signatures sign SHA256(domain || hash) (market_protocol.sign_purpose). */
const purposeDigest = (domain: string, hash: Uint8Array) => sha256(concatBytes(utf8(domain), hash));
export const signListing = (l: ListingManifest, signer: Signer, opts?: SignOptions) => signer.sign('listing', purposeDigest(LISTING_DOMAIN, listingHash(l)), opts);
export const signOffer = (m: OfferManifest, signer: Signer, opts?: SignOptions) => signer.sign('offer', purposeDigest(OFFER_SIG_DOMAIN, manifestHash(m)), opts);
export const signAcceptance = (a: Acceptance, signer: Signer, opts?: SignOptions) => signer.sign('accept', purposeDigest(ACCEPT_DOMAIN, acceptanceHash(a)), opts);

export function verifyReceipt(receipt: DeliveryReceipt, signature: string, publicKey: string): boolean {
  try { return schnorr.verify(hexToBytes(signature), receiptHash(receipt), hexToBytes(publicKey)); } catch { return false; }
}

/** The buyer's new owner commitment S' = s'·G1 and a proof of knowledge of
 *  s' bound to one manifest, so it authorizes this receipt and no other. */
export function receiveCommitment(s: bigint): string {
  return bytesToHex(g1.multiply(s).toBytes(true));
}
export function proveReceive(s: bigint, mhash: Uint8Array): string {
  const S = g1.multiply(s);
  return bytesToHex(dlog([g1], [S], s, RECEIVE_DST, mhash));
}
export const deliveryBinding = (mhash: Uint8Array, destination: string) =>
  concatBytes(utf8(DELIVER_BINDING), mhash, hexToBytes(destination));

export function htlcSecretTerms(m: OfferManifest) {
  return { hashlock: m.hashlock, mainKeys: [m.claim_pubkey], refundKeys: [m.refund_pubkey], locktime: m.cash_deadline };
}

/** NUT-11 SIG_ALL v0 digest (hex) and a signature over it. Signing the
 *  digest directly is what lets the buyer authorize a refund before its
 *  locktime and the seller a claim before the preimage exists. */
export function sigAllDigest(inputs: { secret: string; C: string }[], outputs: BlindedOutput[]): string {
  return SigAll.computeDigests(inputs.map(({ secret, C }) => ({ secret, C })), outputs.map((o) => ({ ...o, amount: Amount.from(o.amount) }))).v0;
}
export const signSigAll = (digestHex: string, privkey: string) => SigAll.signDigest(digestHex, privkey);

export async function escrowPreimage(publicKey: string, version: string, preimage: Uint8Array, mhash: Uint8Array) {
  return sealPreimage(publicKey, version, preimage, mhash) as Promise<Record<string, string>>;
}

/** NUT-02 input fees, as `market.funding_amount`: the locked amount pays the
 *  claim's own input fee so the seller nets exactly `price`. */
const splitCount = (n: number) => { let c = 0; for (let x = BigInt(n); x; x >>= 1n) c += Number(x & 1n); return c; };
export const feeFor = (inputs: number, ppk: number) => Math.ceil((inputs * ppk) / 1000);
export function fundingAmount(price: number, ppk: number): { amount: number; fee: number } {
  checkedSats(price);
  let fee = 0;
  for (let i = 0; i < 16; i++) {
    const amount = checkedSats(price + fee), needed = feeFor(splitCount(amount), ppk);
    if (needed === fee) return { amount, fee };
    fee = needed;
  }
  throw new Error('Fee calculation does not converge');
}
export function checkedSats(n: number): number {
  if (!Number.isSafeInteger(n) || n <= 0 || n > MAX_PRICE) throw new Error('Amount out of range');
  return n;
}

export const randomHex = (bytes: number) => bytesToHex(crypto.getRandomValues(new Uint8Array(bytes)));
export const hashlockOf = (preimage: Uint8Array) => bytesToHex(sha256(preimage));
export { integer };
