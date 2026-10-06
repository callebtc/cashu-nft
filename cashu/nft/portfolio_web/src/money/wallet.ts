// Ordinary ecash wallet (Coco 2.0.0 + cashu-ts 5.0.0-rc.11) on the encrypted
// repository boundary. One wallet per profile, independent of the NFT keyset:
// its seed, snapshot key, journal key and authorization keys are all derived
// from the profile's root secret under separate, versioned domains (store.ts).
import { OperationInProgressError, getTokenMetadata, initializeCoco, type Manager } from '@cashu/coco-core';
import { asProfile, type Profile, type SignOptions } from '../signer.ts';
import { MarketApi, remoteBackup } from '../market/api.ts';
import { marketPlugin, type MarketCoordinator } from '../market/coordinator.ts';
import { installCocoCompat } from './compat.ts';
import { EncryptedRepositories, LocalRecords, moneySeed } from './store.ts';

installCocoCompat();

export interface MintBalance {
  mint: string; available: number; reserved: number; offerLocked: number; pendingRefund: number; pendingClaim: number; testValue: boolean;
}
export interface MintCheck { url: string; ok: boolean; reasons: string[]; name?: string; nuts: number[]; }

/** Discovery shortcuts, not endorsements (MARKETPLACE_PLAN.md). */
export const MINT_SHORTCUTS = [
  { name: 'Testnut', url: 'https://testnut.cashu.space', testValue: true },
  { name: 'Minibits', url: 'https://mint.minibits.cash/Bitcoin', testValue: false },
  { name: 'Coinos', url: 'https://mint.coinos.io', testValue: false },
  { name: 'Macadamia', url: 'https://mint.macadamia.cash', testValue: false },
];
const TEST_VALUE = new Set(['https://testnut.cashu.space']);
// Ordinary wallet use needs mint/melt/swap/state plus NUT-20 locked quotes
// (cashu-ts rc.11 locks every mint quote). Marketplace eligibility is stricter
// and checked by the server before funding.
const WALLET_NUTS = [4, 5, 7, 20];

/** One identity per mint: lowercase scheme/host, keep the path prefix, no
 *  trailing slash, no credentials, query or fragment (as normalize_mint_url). */
export function normalizeMintUrl(raw: string, allowHttp = false): string {
  let url: URL;
  try { url = new URL(raw.trim()); } catch { throw new Error('Enter a valid mint URL'); }
  if (url.protocol !== 'https:' && !(allowHttp && url.protocol === 'http:')) throw new Error('Mint URLs must use https');
  if (url.username || url.password) throw new Error('Mint URLs must not contain credentials');
  if (url.search || url.hash) throw new Error('Mint URLs must not contain a query or fragment');
  const path = url.pathname.replace(/\/+$/, '');
  if (path.includes('//') || path.split('/').some((s) => s === '.' || s === '..')) throw new Error('Mint URL path is not canonical');
  return `${url.protocol}//${url.host.toLowerCase()}${path}`;
}

export class MoneyWallet {
  readonly pubkey: string;
  private visibility?: () => void;
  private syncing?: ReturnType<typeof setInterval>;
  private constructor(readonly manager: Manager, readonly repos: EncryptedRepositories, readonly market: MarketCoordinator, readonly api: MarketApi, private local: LocalRecords, private devMints: string[]) {
    this.pubkey = api.pubkey;
  }

  /** `profile` is a signer.ts Profile, or a raw key for a collection that uses its own key. */
  static async open(profile: string | Profile, opts: { origin?: string; devMints?: string[]; remote?: boolean; takeover?: boolean } = {}): Promise<MoneyWallet> {
    const { pubkey, signer, root } = asProfile(profile);
    const api = new MarketApi(signer, opts.origin ?? '');
    const repos = await EncryptedRepositories.open(root, pubkey, opts.remote === false ? null : remoteBackup(api));
    await repos.acquireLease(opts.takeover ?? false);
    const seed = await moneySeed(root);
    const journalStore = new LocalRecords(pubkey + ':journal');
    let market: MarketCoordinator | undefined;
    const manager = await initializeCoco({
      repo: repos, seedGetter: async () => seed,
      plugins: [marketPlugin(root, signer, api, journalStore, journalStore, (c) => { market = c; })],
      // Watchers and processors on: quotes are claimed and proof states
      // tracked while the wallet is open; paused when the page is hidden.
      processors: { mintOperationProcessor: { autoClaimMintQuotes: true } },
    });
    if (!market) throw new Error('The market coordinator did not initialize');
    const wallet = new MoneyWallet(manager, repos, market, api, journalStore, opts.devMints ?? []);
    wallet.startLifecycle();
    return wallet;
  }

  get readOnly() { return this.repos.readOnly; }
  /** Take the single-writer lease from another device (after a fresh read). */
  async takeOver() { return this.repos.acquireLease(true); }

  private startLifecycle() {
    if (typeof document !== 'undefined') {
      const onChange = () => { void (document.hidden ? this.manager.pauseSubscriptions() : this.resume()); };
      document.addEventListener('visibilitychange', onChange);
      this.visibility = () => document.removeEventListener('visibilitychange', onChange);
    }
    // Push the encrypted snapshot and renew the lease while open.
    this.syncing = setInterval(() => { void this.sync().catch(() => {}); }, 30_000);
  }
  private lastResume = 0;
  private async resume() {
    await this.manager.resumeSubscriptions();
    // Tab switches can be frequent; the lease and backup only need a nudge.
    if (Date.now() - this.lastResume < 30_000) return;
    this.lastResume = Date.now();
    await this.sync().catch(() => {});
  }
  async sync() { if (!this.repos.readOnly) { await this.repos.acquireLease(false); await this.repos.sync(); } }

  async dispose() {
    this.visibility?.();
    if (this.syncing) clearInterval(this.syncing);
    await this.sync().catch(() => {});
    await this.manager.dispose();
    await this.repos.releaseLease().catch(() => {});
    await this.repos.close();
    await this.local.close();
  }

  // --- mints --------------------------------------------------------------------

  normalize(url: string) {
    const devLike = this.devMints.some((m) => m === url.trim().replace(/\/+$/, ''));
    return normalizeMintUrl(url, devLike);
  }

  /** Capability and browser-connectivity check (CORS) before adding a mint. */
  async checkMint(raw: string): Promise<MintCheck> {
    const url = this.normalize(raw);
    let info: { name?: string; nuts?: Record<string, { supported?: boolean; disabled?: boolean; methods?: unknown[] }> };
    try { info = await (await fetch(url + '/v1/info', { cache: 'no-store' })).json(); }
    catch { return { url, ok: false, reasons: ['This browser cannot reach the mint (offline or CORS blocked)'], nuts: [] }; }
    const nuts = Object.entries(info.nuts ?? {}).filter(([, v]) => v && (v.supported === true || (Array.isArray(v.methods) && v.disabled !== true))).map(([k]) => Number(k));
    const reasons = WALLET_NUTS.filter((n) => !nuts.includes(n)).map((n) => `NUT-${n} not advertised`);
    return { url, ok: reasons.length === 0, reasons, name: info.name, nuts };
  }

  async addMint(raw: string): Promise<MintCheck> {
    const check = await this.checkMint(raw);
    if (!check.ok) throw new Error(check.reasons.join('. '));
    await this.manager.mint.addMint(check.url, { trusted: true });
    return check;
  }

  async mints(): Promise<string[]> {
    return (await this.manager.mint.getAllTrustedMints()).map((m: { mintUrl: string }) => m.mintUrl);
  }

  isTestValue(mint: string) { return TEST_VALUE.has(mint); }

  async balances(): Promise<MintBalance[]> {
    const coco = await this.manager.wallet.balances.byMint();
    const market = await this.market.balances();
    const mints = new Set([...await this.mints(), ...Object.keys(market)]);
    return [...mints].map((mint) => {
      const b = coco[mint], m = market[mint];
      return {
        mint, available: Number(String(b?.spendable ?? 0)), reserved: Number(String(b?.reserved ?? 0)), offerLocked: m?.offerLocked ?? 0,
        pendingRefund: m?.pendingRefund ?? 0, pendingClaim: m?.pendingClaim ?? 0, testValue: this.isTestValue(mint),
      };
    });
  }

  // --- ordinary operations -------------------------------------------------------

  /** Lightning top-up: a NUT-20 locked quote; Coco claims it once paid. */
  async topUp(mint: string, amount: number) {
    const quote = await this.manager.quotes.mint.create({ mintUrl: mint, method: 'bolt11', amount, locked: true });
    const operation = await this.manager.ops.mint.prepare({ quote, amount });
    return { invoice: quote.request as string, quoteId: quote.quoteId as string, operationId: operation.id as string };
  }
  /** Current top-up state; nudges a pending operation unless Coco's own
   *  processor is already claiming it. */
  async topUpState(operationId: string): Promise<string> {
    const op = await this.manager.ops.mint.get(operationId);
    if (!op) throw new Error('Unknown top-up');
    if (op.state !== 'pending') return op.state;
    try { return (await this.manager.ops.mint.refresh(operationId)).state; }
    catch (error) { if (error instanceof OperationInProgressError) return 'executing'; throw error; }
  }

  async receive(token: string) {
    const meta = getTokenMetadata(token.trim());
    if (meta.unit !== 'sat') throw new Error('Only sat tokens are supported');
    const mint = this.normalize(meta.mint);
    if (!(await this.mints()).includes(mint)) await this.addMint(mint);
    await this.manager.wallet.receive(token.trim());
    return { mint, amount: meta.amount.toNumber() };
  }

  async send(mint: string, amount: number): Promise<string> {
    const prepared = await this.manager.ops.send.prepare({ mintUrl: mint, amount });
    const { token } = await this.manager.ops.send.execute(prepared.id);
    return this.manager.wallet.encodeToken(token);
  }

  /** Lightning withdrawal: melt quote, then a Coco melt operation. */
  async withdrawQuote(mint: string, invoice: string) {
    const quote = await this.manager.quotes.melt.create({ mintUrl: mint, method: 'bolt11', methodData: { invoice: invoice.trim() } });
    const operation = await this.manager.ops.melt.prepare({ quote });
    return { quote, operation };
  }
  async withdraw(operationId: string) { return this.manager.ops.melt.execute(operationId); }

  /** Browser recovery path for offers and sales; run on open and on events. */
  reconcile(nft?: Parameters<MarketCoordinator['reconcile']>[0], opts?: SignOptions) { return this.market.reconcile(nft, opts); }
}
