// The Nostr login refactor (NOSTR_LOGIN_PLAN.md) must derive bit-identical
// wallets, encryption keys and market keys for collections that use their own
// key. fixtures/derivations.json was captured from the code before it.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import 'fake-indexeddb/auto';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';
import { schnorr } from '@noble/curves/secp256k1.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { EncryptedVault, walletSeed } from '../src/wallet/vault.ts';
import { authorizationKey, moneyKey, moneySeed, open } from '../src/money/store.ts';
import { SIG_KIND, SIG_LABELS, claimDigest, eventHash, eventSignature, profileKey, signClaim, signatureEvent, verifySignature } from '../src/crypto.mjs';
import { keyProfile, localSigner } from '../src/signer.ts';

const golden = JSON.parse(readFileSync(new URL('./fixtures/derivations.json', import.meta.url)));

test('a collection that uses its own key keeps every derivation', async () => {
  const profile = keyProfile(golden.secret);
  assert.equal(profile.pubkey, golden.pubkey);
  assert.equal(profile.root, golden.secret);
  assert.equal(bytesToHex(await walletSeed(profile.root, golden.keyset)), golden.wallet_seed);
  assert.equal(bytesToHex(await moneySeed(profile.root)), golden.money_seed);
  for (const [label, key] of Object.entries(golden.authorization)) assert.equal(bytesToHex(await authorizationKey(profile.root, label)), key);
  const vault = new EncryptedVault(profile.root, profile.pubkey, golden.keyset);
  try { assert.deepEqual(await vault.decrypt(golden.vault_envelope, 'card:golden'), { hello: 'vault' }); }
  finally { await vault.close(); }
  assert.deepEqual(await open(await moneyKey(profile.root, 'snapshot'), `Cashu_Money_Snapshot_v1\n${golden.pubkey}`, golden.snapshot_envelope), { hello: 'snapshot' });
  assert.deepEqual(await open(await moneyKey(profile.root, 'market-journal'), `Cashu_Market_Journal_v1\n${golden.pubkey}\noffer:golden`, golden.journal_envelope), { hello: 'journal' });
});

test('the local signer produces the raw signatures it always did', async () => {
  const signer = localSigner(golden.secret), message = 'Cashu_NFT_Portfolio_Auth_v1\nexample';
  const { signature, session } = await signer.auth(message);
  assert.equal(session, undefined);
  assert.ok(schnorr.verify(hexToBytes(signature), sha256(new TextEncoder().encode(message)), hexToBytes(golden.pubkey)));
  const showing = 'pshow1' + '00'.repeat(8);
  const claim = await signer.sign('claim', claimDigest(showing));
  assert.ok(verifySignature(golden.pubkey, 'claim', claimDigest(showing), claim));
  assert.ok(verifySignature(golden.pubkey, 'claim', claimDigest(showing), signClaim(golden.secret, showing)));
});

test('extension signatures are events that commit to the digest', () => {
  const secret = '42'.repeat(32), pubkey = profileKey(secret), digest = sha256(new TextEncoder().encode('payload'));
  const event = signatureEvent(pubkey, 'listing', digest, 1700000000);
  assert.deepEqual(event, { pubkey, created_at: 1700000000, kind: SIG_KIND, tags: [['x', bytesToHex(digest)]], content: SIG_LABELS.listing });
  const sig = bytesToHex(schnorr.sign(eventHash(event), hexToBytes(secret)));
  const signature = eventSignature({ ...event, sig });
  assert.equal(signature, `n1:1700000000:${sig}`);
  assert.ok(verifySignature(pubkey, 'listing', digest, signature));
  // Bound to purpose, digest, time and key.
  assert.equal(verifySignature(pubkey, 'offer', digest, signature), false);
  assert.equal(verifySignature(pubkey, 'listing', sha256(digest), signature), false);
  assert.equal(verifySignature(pubkey, 'listing', digest, `n1:1700000001:${sig}`), false);
  assert.equal(verifySignature(profileKey('43'.repeat(32)), 'listing', digest, signature), false);
  // Malformed encodings fail closed.
  for (const bad of ['', 'n1::' + sig, `n1:01:${sig}`, `n1:1700000000:${sig.toUpperCase()}`, `n2:1700000000:${sig}`, null]) assert.equal(verifySignature(pubkey, 'listing', digest, bad), false);
  assert.throws(() => signatureEvent(pubkey, 'unknown', digest, 1), /Unknown/);
});

test('the event id matches the NIP-01 serialization of a known event', () => {
  // NIP-01 example layout: [0, pubkey, created_at, kind, tags, content].
  const event = { pubkey: 'a'.repeat(64), created_at: 1, kind: 1, tags: [['e', 'b'.repeat(64)]], content: 'hello "nostr"\n' };
  const serialized = `[0,"${'a'.repeat(64)}",1,1,[["e","${'b'.repeat(64)}"]],"hello \\"nostr\\"\\n"]`;
  assert.equal(bytesToHex(eventHash(event)), bytesToHex(sha256(new TextEncoder().encode(serialized))));
});
