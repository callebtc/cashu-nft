// Typed client for /api/market and the profile-signed market/money routes.
import { checked, signedRequest } from '../api.mjs';
import { asSigner, type Signer } from '../signer.ts';
import type { RemoteBackup, Sealed } from '../money/store.ts';
import type { Acceptance, BlindedOutput, DeliveryReceipt, ListingManifest, OfferManifest } from './protocol.ts';

export interface MarketConfig {
  protocol: string; nft_keyset_id: string; escrow: { version: string; public_key: string };
  receipt: { version: string; public_key: string }; accept_window: number; clock_skew: number;
  min_lifetime: number; max_lifetime: number; default_lifetime: number;
  mint_shortcuts: { name: string; url: string; test_value: boolean }[]; dev_mints: string[]; now: number;
}
export interface Eligibility { mint: string; eligible: boolean; reasons: string[]; version: string | null; keyset_id: string | null; fee_ppk: number | null; test_value: boolean; }
export interface Quote extends Eligibility { price?: number; amount: number | null; claim_fee: number | null; refund_fee?: number; refund_amount?: number; }
export interface Listing {
  id: string; card_id: string; seller: string; seller_name: string | null; title: string | null; h: string; nullifier: string;
  price: number; revision: number; claim_pubkey: string; state: string; created: number; updated: number;
}
export interface Proof { amount: number; id: string; secret: string; C: string; dleq?: { e: string; s: string; r: string } | null; }
export interface Job { kind: 'claim' | 'refund'; state: string; outcome: string | null; attempts: number; last_error: string | null; not_before: number; }
export interface OfferView {
  id: string; listing_id: string; card_id: string | null; title: string | null; h: string; seller: string; seller_name: string | null;
  buyer: string; buyer_name: string | null; role: 'buyer' | 'seller'; price: number; amount: number; claim_fee: number; mint: string;
  keyset_id: string; test_value: boolean; cash_deadline: number; accept_deadline: number; disposition: string; nft_leg: string;
  cash_leg: string; publication: string; manifest: OfferManifest; manifest_hash: string; created: number; updated: number;
  jobs: Job[]; proofs?: Proof[];
}
export interface JobResult { state: string; outcome: string | null; mint: string; outputs: BlindedOutput[]; signatures: { amount: number; id: string; C_: string; dleq?: { e: string; s: string } }[] | null; last_error: string | null; }
export interface InboxEvent { id: number; kind: string; offer_id: string | null; listing_id: string | null; payload: Record<string, unknown>; created: number; read: boolean; }
export interface RecoveryRecord { id: string; kind: string; envelope: Sealed; updated?: number; }

const json = (value: unknown) => new TextEncoder().encode(JSON.stringify(value));

export class MarketApi {
  readonly pubkey: string;
  private signer: Signer;
  constructor(signer: string | Signer, readonly origin = '') { this.signer = asSigner(signer); this.pubkey = this.signer.pubkey; }

  private async read<T>(path: string): Promise<T> {
    return (await checked(await fetch(this.origin + path, { cache: 'no-store' }))).json();
  }
  private async post<T>(path: string, body?: unknown): Promise<T> {
    const bytes = body === undefined ? new Uint8Array() : json(body);
    const response = await signedRequest(this.signer, `/api/profiles/${this.pubkey}${path}`, bytes, 'application/json', this.origin);
    return response.json();
  }

  config() { return this.read<MarketConfig>('/api/market/config'); }
  checkMint(url: string) { return this.read<Eligibility>(`/api/market/mints/check?url=${encodeURIComponent(url)}`); }
  quote(mint: string, price: number) { return this.read<Quote>(`/api/market/quote?mint=${encodeURIComponent(mint)}&price=${price}`); }
  listings(params: { sort?: string; q?: string; seller?: string; limit?: number; offset?: number } = {}) {
    const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== '').map(([k, v]) => [k, String(v)]));
    return this.read<{ items: Listing[]; more: boolean }>(`/api/market/listings?${query}`);
  }
  listing(id: string) { return this.read<Listing>(`/api/market/listings/${id}`); }
  listingForCard(cardId: string) { return this.read<{ listing: Listing | null }>(`/api/market/cards/${cardId}/listing`); }
  sales(limit = 30) { return this.read<unknown[]>(`/api/market/sales?limit=${limit}`); }

  createListing(listing: ListingManifest, signature: string) { return this.post<Listing>('/market/listings', { listing, signature }); }
  reviseListing(listing: ListingManifest, signature: string) { return this.post<Listing>(`/market/listings/${listing.listing_id}/revise`, { listing, signature }); }
  unlist(id: string) { return this.post<Listing>(`/market/listings/${id}/unlist`); }

  submitOffer(body: unknown) { return this.post<OfferView>('/market/offers', body); }
  offers() { return this.post<OfferView[]>('/market/offers/list'); }
  offer(id: string) { return this.post<OfferView>(`/market/offers/${id}`); }
  accept(id: string, acceptance: Acceptance, seller_signature: string, presentation: string) {
    return this.post<{ receipt: DeliveryReceipt; signature: string; offer: OfferView }>(`/market/offers/${id}/accept`, { acceptance, seller_signature, presentation });
  }
  decline(id: string) { return this.post<OfferView>(`/market/offers/${id}/decline`); }
  payment(id: string, kind: 'claim' | 'refund') { return this.post<JobResult>(`/market/offers/${id}/payment/${kind}`); }

  purchases() { return this.post<{ id: string; h: string; price: number; publication: string; delivered: number; title: string | null }[]>('/market/purchases'); }
  receipt(id: string) { return this.post<{ receipt: DeliveryReceipt; signature: string; receipt_key: string }>(`/market/purchases/${id}/receipt`); }
  publishPurchase(id: string, body: { encrypted_credential: unknown; showing: string; signature: string }) {
    return this.post<unknown>(`/market/purchases/${id}/publish`, body);
  }

  inbox(after: number, wait: number) { return this.post<{ events: InboxEvent[]; cursor: number; unread: number }>('/market/inbox', { after, wait }); }
  markRead(upto: number) { return this.post<{ ok: boolean }>('/market/inbox/read', { upto }); }

  putRecovery(record: RecoveryRecord) { return this.post<{ ok: boolean }>('/market/recovery/put', record); }
  listRecovery() { return this.post<RecoveryRecord[]>('/market/recovery/list'); }

  // RemoteBackup: the encrypted ordinary-wallet snapshot and its single-writer lease.
  lease(device: string, takeover = false, release = false) { return this.post<{ granted: boolean; holder: string; until: number }>('/money/lease', { device, takeover, release }); }
  get_backup() { return this.post<{ revision: number; envelope: Sealed | null }>('/money/backup/get'); }
  put(device: string, base_revision: number, revision: number, envelope: Sealed) {
    return this.post<{ revision: number }>('/money/backup/put', { device, base_revision, revision, envelope });
  }
}

/** RemoteBackup view (the interface names `get` for the backup read). */
export function remoteBackup(api: MarketApi): RemoteBackup {
  return { lease: (d, t, r) => api.lease(d, t, r), get: () => api.get_backup(), put: (d, b, r, e) => api.put(d, b, r, e) };
}
