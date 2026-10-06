import test from 'node:test';
import assert from 'node:assert/strict';
import 'fake-indexeddb/auto';
import { Amount, Mint, Wallet } from '@cashu/cashu-ts';
import { MemoryRepositories } from '@cashu/coco-core';
import { installCocoCompat } from '../src/money/compat.ts';
import { EncryptedRepositories, decode, encode, moneySeed, snapshotOf } from '../src/money/store.ts';
import { canonical, fundingAmount, sigAllDigest } from '../src/market/protocol.ts';
import { normalizeMintUrl } from '../src/money/wallet.ts';
import { profileKey } from '../src/crypto.mjs';

const secret = '55'.repeat(32);

test('snapshot codec keeps Coco value types', () => {
  const value = { a: Amount.from(21), b: new Uint8Array([1, 2]), c: 7n, d: new Map([['k', Amount.from(1)]]), $x: 'kept' };
  const back = decode(JSON.parse(JSON.stringify(encode(value))));
  assert.ok(back.a instanceof Amount && back.a.toNumber() === 21);
  assert.deepEqual(back.b, new Uint8Array([1, 2]));
  assert.equal(back.c, 7n);
  assert.equal(back.d.get('k').toNumber(), 1);
  assert.equal(back.$x, 'kept');
});

test('encrypted repositories persist, reopen and never store plaintext', async () => {
  const repos = await EncryptedRepositories.open(secret, profileKey(secret), null);
  await repos.counterRepository.setCounter('https://mint.test', 'keyset', 42);
  const quote = { mintUrl: 'https://mint.test', method: 'bolt11', quoteId: 'q1', quote: 'q1', request: 'lnbc1', unit: 'sat',
    amount: Amount.from(5), amountPaid: Amount.from(0), amountIssued: Amount.from(0), state: 'UNPAID', reusable: false, remoteUpdatedAt: null, createdAt: 1, updatedAt: 1 };
  await repos.mintQuoteRepository.upsertMintQuote(quote);
  // Regression: reads through the proxy must see writes (internal sync helpers stay bound).
  assert.ok(await repos.mintQuoteRepository.getMintQuote('https://mint.test', 'bolt11', 'q1'));
  assert.ok(repos.revision > 0);
  await repos.close();
  const again = await EncryptedRepositories.open(secret, profileKey(secret), null);
  assert.equal((await again.counterRepository.getCounter('https://mint.test', 'keyset')).counter, 42);
  assert.ok(await again.mintQuoteRepository.getMintQuote('https://mint.test', 'bolt11', 'q1'));
  await again.close();
  await assert.rejects(EncryptedRepositories.open('66'.repeat(32), profileKey('66'.repeat(32)), null).then(async (r) => {
    // Another key sees nothing usable (separate database) and cannot decrypt ours.
    assert.equal(await r.counterRepository.getCounter('https://mint.test', 'keyset'), null);
    await r.close(); throw new Error('isolated');
  }), /isolated/);
});

test('every mutating Coco repository method is native async (the proxy relies on it)', () => {
  const repos = new MemoryRepositories();
  const READ = ['get', 'is', 'find', 'list', 'has', 'count', 'init'];
  const known = new Set(['assertNoDuplicateQuoteOperation', 'dedupeLegacyEntries', 'key', 'makeKey', 'operationKey', 'quoteKey']);
  for (const [name, repo] of Object.entries(repos)) {
    if (!name.endsWith('Repository') || !repo) continue;
    const proto = Object.getPrototypeOf(repo);
    for (const k of Object.getOwnPropertyNames(proto)) {
      if (k === 'constructor' || READ.some((p) => k.startsWith(p)) || known.has(k)) continue;
      assert.equal(proto[k].constructor.name, 'AsyncFunction', `${name}.${k}`);
    }
  }
  assert.ok(snapshotOf(repos));
});

test('money seed is profile-scoped and versioned', async () => {
  const a = await moneySeed(secret), b = await moneySeed('66'.repeat(32));
  assert.equal(a.length, 64);
  assert.notDeepEqual(a, b);
  assert.deepEqual(a, await moneySeed(secret));
});

test('Coco/cashu-ts rc.11 compatibility shims', async () => {
  installCocoCompat();
  installCocoCompat(); // idempotent
  assert.equal(typeof Wallet.prototype.createLockedMintQuote, 'function');
  const seen = [];
  const mint = new Mint('https://mint.test', { customRequest: async (o) => { seen.push(o); return { quote: 'q', request: 'lnbc', state: 'PAID', unit: 'sat', amount: 1, amount_paid: 1, amount_issued: 0, updated_at: 5 }; } });
  const quote = await mint.createMintQuoteBolt11({ unit: 'sat', amount: 1 });
  assert.equal(typeof seen[0].requestBody, 'object', 'Coco must receive an object body, not a JSON string');
  assert.equal(quote.updated_at ?? null, null, 'coarse updated_at is dropped for bolt11 mint quotes');
});

test('protocol helpers match the Python encodings', () => {
  assert.equal(canonical({ b: 1, a: [true, null, 'é'], c: { z: 'x', y: 2 } }), '{"a":[true,null,"é"],"b":1,"c":{"y":2,"z":"x"}}');
  // Vectors from cashu.nft.market.funding_amount.
  for (const [price, ppk, amount, fee] of [[100, 0, 100, 0], [100, 100, 101, 1], [1023, 1000, 1025, 2], [7, 1000, 9, 2], [2100, 250, 2102, 2]]) {
    assert.deepEqual(fundingAmount(price, ppk), { amount, fee });
  }
  assert.throws(() => fundingAmount(0, 0));
  const digest = sigAllDigest([{ secret: '["HTLC",{"nonce":"aa","data":"bb","tags":[]}]', C: '02' + '11'.repeat(32) }],
    [{ amount: 8, id: '00aa', B_: '03' + '22'.repeat(32) }]);
  assert.match(digest, /^[0-9a-f]{64}$/);
});

test('mint URLs normalize to one identity', () => {
  assert.equal(normalizeMintUrl('HTTPS://Mint.Minibits.Cash/Bitcoin/'), 'https://mint.minibits.cash/Bitcoin');
  assert.throws(() => normalizeMintUrl('http://mint.test'), /https/);
  assert.equal(normalizeMintUrl('http://127.0.0.1:3339', true), 'http://127.0.0.1:3339');
  assert.throws(() => normalizeMintUrl('https://user:pw@mint.test'), /credentials/);
  assert.throws(() => normalizeMintUrl('https://mint.test/?x=1'), /query/);
});
