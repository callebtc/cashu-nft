// Browser-client phases for tests/test_nft_market_browser.py. Each phase is a
// separate Node process with an empty in-memory IndexedDB, i.e. a fresh
// browser: everything it needs comes from the profile key and the server's
// encrypted backups. Usage: node --import tsx tests/market.e2e.mjs <phase> '<json>'
import 'fake-indexeddb/auto';
import { readFileSync } from 'node:fs';
import { signedRequest } from '../src/api.mjs';
import { profileKey } from '../src/crypto.mjs';
import { openWallet } from '../src/wallet/index.ts';
import { MoneyWallet } from '../src/money/wallet.ts';

const ORIGIN = process.env.PORTFOLIO_URL, MINT = process.env.MINT_URL;
if (!ORIGIN || !MINT) throw new Error('PORTFOLIO_URL and MINT_URL are required');
const [phase, raw = '{}'] = process.argv.slice(2);
const input = JSON.parse(raw);

// The app is same-origin in the browser; give relative requests the server origin.
const realFetch = globalThis.fetch;
let dropSwapReply = false;
let dropNftReply = false;
let nftIssueRequests = 0;
globalThis.fetch = async (url, init) => {
  const target = typeof url === 'string' && url.startsWith('/') ? ORIGIN + url : url;
  const response = await realFetch(target, init);
  if (String(target).includes('/wallet/operations/') && String(target).endsWith('/finish')) {
    nftIssueRequests++;
    if (dropNftReply && response.ok) {
      dropNftReply = false;
      throw new TypeError('NFT issuance reply lost');
    }
  }
  if (dropSwapReply && String(target).endsWith('/v1/swap')) {
    dropSwapReply = false;
    throw new TypeError('network connection lost'); // the mint processed it; the reply is gone
  }
  return response;
};
globalThis.location = { origin: ORIGIN };

const fixture = JSON.parse(readFileSync(new URL('./fixtures/wallet.json', import.meta.url)));
const jpg = Uint8Array.from(Buffer.from(fixture.jpg, 'base64'));
const json = (v) => new TextEncoder().encode(JSON.stringify(v));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = async (path) => (await realFetch(ORIGIN + path)).json();

async function profile(secret, name) {
  const pubkey = profileKey(secret);
  await signedRequest(secret, `/api/profiles/${pubkey}`, json({ name }), 'application/json');
  return pubkey;
}
async function nftWallet(secret) {
  const config = await get('/api/config');
  const { manager, wallet } = await openWallet(secret, config);
  return { manager, wallet };
}
async function money(secret) {
  // A new device takes over the single-writer lease after a fresh read.
  return MoneyWallet.open(secret, { devMints: [MINT], takeover: true });
}
async function fund(wallet, amount) {
  const { operationId } = await wallet.topUp(MINT, amount);
  for (let i = 0; i < 60; i++) {
    if ((await wallet.topUpState(operationId)) === 'finalized') return;
    await sleep(250);
  }
  throw new Error('top-up did not finalize');
}
const balanceOf = async (wallet) => (await wallet.balances()).find((b) => b.mint === MINT);
async function card(pubkey, cardId) {
  return (await get(`/api/profiles/${pubkey}`)).cards.find((c) => c.id === cardId);
}

const phases = {
  async nft_mint({ secret, drop_reply = false }) {
    const pubkey = await profile(secret, 'Collector');
    const { manager, wallet } = await nftWallet(secret);
    dropNftReply = drop_reply;
    try {
      const minted = await wallet.mint(jpg, 'One request');
      let duplicateRejected = false;
      try { await wallet.mint(jpg, 'Duplicate'); }
      catch (error) { duplicateRejected = /already minted/i.test(error.message); }
      return { pubkey, minted, duplicateRejected, nftIssueRequests };
    } catch (error) {
      if (!drop_reply || !/NFT issuance reply lost/.test(error.message)) throw error;
      return { pubkey, interrupted: true, nftIssueRequests };
    } finally { await manager.dispose(); }
  },

  async nft_recover({ secret }) {
    const { manager, wallet } = await nftWallet(secret);
    try {
      const recovered = await wallet.recover();
      const collection = await get(`/api/profiles/${profileKey(secret)}`);
      return { recovered, cards: collection.cards, nftIssueRequests };
    } finally { await manager.dispose(); }
  },

  /** Owner creates a transfer link, then deletes the NFT: the mint burns it. */
  async nft_delete({ secret }) {
    const pubkey = await profile(secret, 'Collector');
    const { manager, wallet } = await nftWallet(secret);
    try {
      const minted = await wallet.mint(jpg, 'Doomed');
      const { nullifier: N } = await wallet.sendToken(minted);
      await wallet.destroy(await card(pubkey, minted.id));
      const state = await (await realFetch(ORIGIN + '/v1/nft/checkstate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nullifiers: [N] }) })).json();
      let remint = '';
      try { await wallet.mint(jpg, 'Again'); } catch (error) { remint = error.message; }
      const image = (await realFetch(`${ORIGIN}/api/images/${minted.h}.jpg`)).status;
      return { cards: (await get(`/api/profiles/${pubkey}`)).cards, image, spent: state.states[0]?.state, remint };
    } finally { await manager.dispose(); }
  },

  /** A listed NFT can't be deleted (a fresh device recovers its wallet first). */
  async nft_delete_listed({ secret, card_id }) {
    const { manager, wallet } = await nftWallet(secret);
    try {
      await wallet.recover();
      await wallet.destroy(await card(profileKey(secret), card_id));
      return { deleted: true };
    } catch (error) { return { error: error.message }; }
    finally { await manager.dispose(); }
  },

  /** Seller mints an NFT in the browser wallet and lists it (rotating it first). */
  async seller_list({ secret, price, jpg_path, title = 'Sunset' }) {
    const pubkey = await profile(secret, 'Seller');
    const { manager, wallet: nft } = await nftWallet(secret);
    const minted = await nft.mint(jpg_path ? Uint8Array.from(readFileSync(jpg_path)) : jpg, title);
    // The seller doesn't add the payment mint: accepting an offer trusts it.
    const wallet = await money(secret);
    const listing = await wallet.market.list(nft, minted, price);
    const exportedBefore = minted.showing !== (await card(pubkey, minted.id)).showing; // rotated
    await wallet.dispose(); await manager.dispose();
    return { pubkey, listing, rotated: exportedBefore };
  },

  /** Buyer funds an offer (losing the swap reply once) and closes the browser. */
  async buyer_offer({ secret, name, listing_id, topup, price, deadline_in, clock_offset, drop_reply }) {
    const pubkey = await profile(secret, name);
    const wallet = await money(secret);
    await wallet.addMint(MINT);
    await fund(wallet, topup);
    const listing = await wallet.api.listing(listing_id);
    // Real-time deadline (the mint enforces it); `created` follows the
    // server's shifted market clock. Never shift Date.now: Coco's request
    // rate limiter reads it too.
    const nowS = Math.floor(Date.now() / 1000);
    const deadline = deadline_in ? nowS + deadline_in : undefined;
    const created = clock_offset ? nowS + clock_offset : undefined;
    dropSwapReply = Boolean(drop_reply);
    try {
      const record = await wallet.market.makeOffer(listing, MINT, { price, deadline, created });
      const balance = await balanceOf(wallet);
      return { pubkey, offer_id: record.id, stage: record.stage, error: record.error ?? null, amount: record.manifest.payment.amount, balance, reply_dropped: Boolean(drop_reply) && !dropSwapReply };
    } finally {
      await wallet.dispose();
    }
  },

  /** Another copy of this wallet already spent one of its proofs; this copy
   *  still lists it as ready and bids with it. */
  async stale_offer({ secret, listing_id, topup, price, spend_elsewhere }) {
    await profile(secret, 'Stale');
    const wallet = await money(secret);
    await wallet.addMint(MINT);
    await fund(wallet, topup);
    const ready = await wallet.repos.proofRepository.getAvailableProofs(MINT, { unit: 'sat' });
    const stale = ready.find((p) => Number(String(p.amount)) === spend_elsewhere);
    // Straight to the mint with cashu-ts: Coco's proof store never hears of it.
    const raw = await wallet.market['services'].walletService.getWallet(MINT, 'sat');
    await raw.completeSwap(await raw.prepareSwapToSend(spend_elsewhere, [stale], { includeFees: false }, { send: { type: 'random' }, keep: { type: 'random' } }));
    const listing = await wallet.api.listing(listing_id);
    let error = null;
    try { await wallet.market.makeOffer(listing, MINT, { price }); } catch (e) { error = e.message; }
    const journal = (await wallet.market.journal.all()).map((r) => ({ stage: r.stage, error: r.error ?? null }));
    const { attention } = await wallet.reconcile();
    const balance = await balanceOf(wallet);
    await wallet.dispose();
    return { error, journal, attention, balance };
  },

  /** Two offers and a reconciliation started at the same time. */
  async concurrent_offers({ secret, listing_id, topup, prices }) {
    await profile(secret, 'Busy');
    const wallet = await money(secret);
    await wallet.addMint(MINT);
    await fund(wallet, topup);
    const listing = await wallet.api.listing(listing_id);
    const [records, reconciled] = await Promise.all([
      Promise.all(prices.map((price) => wallet.market.makeOffer(listing, MINT, { price }))),
      wallet.reconcile(),
    ]);
    const inputs = records.flatMap((r) => r.inputs.map((p) => p.secret));
    const balance = await balanceOf(wallet);
    await wallet.dispose();
    return { stages: records.map((r) => r.stage), amounts: records.map((r) => r.manifest.payment.amount), distinctInputs: new Set(inputs).size === inputs.length, attention: reconciled.attention, balance };
  },

  /** Seller on a new device: recover the NFT vault, review and accept. */
  async seller_accept({ secret, offer_id, expect_fail }) {
    const pubkey = profileKey(secret);
    const { manager, wallet: nft } = await nftWallet(secret);
    await nft.recover();
    const wallet = await money(secret);
    const offer = await wallet.api.offer(offer_id);
    const review = await wallet.market.review(offer);
    const listed = (await get(`/api/profiles/${pubkey}`)).cards.find((c) => c.id === offer.card_id);
    let result = null, error = null;
    try { result = await wallet.market.accept(nft, offer, listed); } catch (e) { error = e.message; }
    await wallet.dispose(); await manager.dispose();
    if (!expect_fail && error) throw new Error(error);
    return { review, nft_leg: result?.offer?.nft_leg ?? null, error };
  },

  /** Anyone: a fresh browser reconciles journal + server state. */
  async reconcile({ secret, nft: withNft }) {
    const pubkey = profileKey(secret);
    let nftHandle = null;
    if (withNft) nftHandle = await nftWallet(secret);
    const wallet = await money(secret);
    const result = await wallet.reconcile(nftHandle?.wallet);
    const balance = await balanceOf(wallet);
    const journal = (await wallet.market.journal.all()).map((r) => ({ id: r.id, kind: r.kind, stage: r.stage }));
    const cards = (await get(`/api/profiles/${pubkey}`)).cards.map((c) => ({ id: c.id, status: c.status, h: c.h }));
    await wallet.dispose(); if (nftHandle) await nftHandle.manager.dispose();
    return { result, balance, journal, cards };
  },

  /** The buyer's recovered NFT is a normal, usable credential: send it on. */
  async buyer_use_nft({ secret, card_id }) {
    const pubkey = profileKey(secret);
    const { manager, wallet: nft } = await nftWallet(secret);
    await nft.recover();
    const owned = await card(pubkey, card_id);
    const { token } = await nft.sendToken(owned);
    await manager.dispose();
    return { token_prefix: token.slice(0, 6) };
  },

  async fund({ secret, amount }) {
    await profile(secret, 'Collector');
    const wallet = await money(secret);
    await wallet.addMint(MINT);
    await fund(wallet, amount);
    const balance = await balanceOf(wallet);
    await wallet.dispose();
    return { balance };
  },

  /** Ordinary wallet: send a token between profiles and check the lease. */
  async ordinary({ secret, other }) {
    const a = await money(secret);
    const token = await a.send(MINT, 21);
    const afterSend = await balanceOf(a);
    await a.dispose();
    const b = await money(other);
    const received = await b.receive(token);
    const bBalance = await balanceOf(b);
    await b.dispose();
    return { afterSend, received, bBalance };
  },

  /** A browser that stays open (or crashed) keeps the single-writer lease. */
  async hold({ secret }) {
    await MoneyWallet.open(secret, { devMints: [MINT], takeover: true });
    return { held: true }; // exits without dispose
  },
  async second_device({ secret }) {
    const wallet = await MoneyWallet.open(secret, { devMints: [MINT] });
    const readOnly = wallet.readOnly;
    let blocked = null;
    try { await wallet.send(MINT, 1); } catch (e) { blocked = e.message; }
    const before = await balanceOf(wallet);
    const tookOver = await wallet.takeOver();
    const token = await wallet.send(MINT, 1);
    const after = await balanceOf(wallet);
    await wallet.dispose();
    return { readOnly, blocked, tookOver, before, after, token: token.slice(0, 5) };
  },
};

const result = await phases[phase](input);
console.log('RESULT ' + JSON.stringify(result));
process.exit(0);
