// Nostr login building blocks (NOSTR_LOGIN_PLAN.md). The server side is
// covered by tests/test_nft_nostr_login.py and the browser flows end to end
// by tests/test_nft_nostr_browser.py (tests/nostr.e2e.mjs).
import test from 'node:test';
import assert from 'node:assert/strict';
import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';
import { sha256 } from '@noble/hashes/sha2.js';
import { encrypt as encryptKey } from 'nostr-tools/nip49';
import { nsecEncode, npubEncode as toolsNpub } from 'nostr-tools/nip19';
import { cleanName, nameFrom, parseKey, pictureFrom, unlockKey, vaultCheck } from '../src/nostr.ts';
import { npubDecode, npubEncode, publicKeyIn } from '../src/npub.mjs';
import { profileKey, verifySignature } from '../src/crypto.mjs';
import { extensionSignature, extensionSigner, identityFor } from '../src/identity.ts';
import { SignatureDeferred, deferred } from '../src/signer.ts';
import { fakeExtension } from './nostr-extension.mjs';

const secret = '3501454135014541350145413501453fefb02227e449e57cf4d3a3ce05378683', pubkey = profileKey(secret);

test('pasted keys: hex, nsec, ncryptsec and npub', () => {
  assert.deepEqual(parseKey(` ${secret.toUpperCase()} `), { kind: 'hex', secret });
  assert.deepEqual(parseKey(nsecEncode(hexToBytes(secret))), { kind: 'nsec', secret });
  assert.deepEqual(parseKey('nostr:' + nsecEncode(hexToBytes(secret))), { kind: 'nsec', secret });
  assert.deepEqual(parseKey(toolsNpub(pubkey)), { kind: 'npub', pubkey });
  const ncryptsec = encryptKey(hexToBytes(secret), 'correct horse', 8);
  assert.deepEqual(parseKey(ncryptsec), { kind: 'ncryptsec', ncryptsec });
  assert.equal(unlockKey(ncryptsec, 'correct horse'), secret);
  assert.throws(() => unlockKey(ncryptsec, 'wrong'), /Wrong password/);
  for (const bad of ['', 'nsec1qqqq', 'abc', secret.slice(1)]) assert.throws(() => parseKey(bad), /Paste a private key/);
});

test('npub routes and lookups match nostr-tools', () => {
  assert.equal(npubEncode(pubkey), toolsNpub(pubkey));
  assert.equal(npubDecode(toolsNpub(pubkey)), pubkey);
  assert.equal(npubDecode(nsecEncode(hexToBytes(secret))), null);
  assert.equal(publicKeyIn(pubkey), pubkey);
  assert.equal(publicKeyIn(`https://nonfungible.cash/p/${toolsNpub(pubkey)}`), pubkey);
  assert.equal(publicKeyIn(`nostr:${toolsNpub(pubkey)}`), pubkey);
  assert.equal(publicKeyIn('https://nonfungible.cash/p/' + pubkey + '/'), pubkey);
  assert.equal(publicKeyIn('npub1nope'), null);
});

test('the name comes from display_name, name, then the NIP-05 local part', () => {
  assert.equal(nameFrom({ display_name: ' Alice  in\nChains ', name: 'alice' }), 'Alice in Chains');
  assert.equal(nameFrom({ display_name: '', name: 'alice' }), 'alice');
  assert.equal(nameFrom({ nip05: 'bob@example.com' }), 'bob');
  assert.equal(nameFrom({ nip05: '_@example.com' }), null);
  assert.equal(nameFrom({ name: 42 }), null);
  assert.equal(cleanName('x'.repeat(50)).length, 40);
  // Cut by code points, like the server's limit, never inside an emoji.
  assert.equal(cleanName('🦩'.repeat(41)), '🦩'.repeat(40));
  assert.equal(cleanName('a\u0000b‮c'), 'abc');
  assert.equal(pictureFrom({ picture: 'https://image.nostr.build/a.jpg' }), 'https://image.nostr.build/a.jpg');
  assert.equal(pictureFrom({ picture: 'http://insecure.example/a.jpg' }), null);
  assert.equal(pictureFrom({ picture: 'javascript:alert(1)' }), null);
  assert.equal(pictureFrom({}), null);
});

test('the vault check value is a stable commitment to the secret', () => {
  assert.match(vaultCheck(secret), /^[0-9a-f]{32}$/);
  assert.equal(vaultCheck(secret), vaultCheck(secret));
  assert.notEqual(vaultCheck(secret), vaultCheck('11'.repeat(32)));
});

test('an extension signs public signatures as events, never in the background', async () => {
  globalThis.nostr = fakeExtension(secret);
  const session = { secret: '22'.repeat(32), expires: Math.floor(Date.now() / 1000) + 3600 };
  const signer = extensionSigner(pubkey, session);
  const digest = sha256(new TextEncoder().encode('showing'));
  const signature = await signer.sign('claim', digest);
  assert.match(signature, /^n1:\d+:[0-9a-f]{128}$/);
  assert.ok(verifySignature(pubkey, 'claim', digest, signature));
  assert.equal(globalThis.nostr.calls.signEvent, 1);
  // Background work defers instead of opening a prompt.
  deferred.clear();
  const seen = [];
  const stop = deferred.subscribe((n) => seen.push(n));
  await assert.rejects(signer.sign('claim', digest, { interactive: false, id: 'published:op1' }), SignatureDeferred);
  await assert.rejects(signer.sign('claim', sha256(digest), { interactive: false, id: 'published:op1' }), SignatureDeferred);
  assert.equal(deferred.count, 1);
  assert.deepEqual(seen, [1]);
  assert.equal(globalThis.nostr.calls.signEvent, 1);
  stop(); deferred.clear();
  // Owner requests use the session key, not the extension.
  const { signature: auth, session: sessionPubkey } = await signer.auth('message');
  assert.equal(sessionPubkey, profileKey(session.secret));
  assert.ok(verifySignature(sessionPubkey, 'auth', sha256(new TextEncoder().encode('message')), auth));
  assert.equal(globalThis.nostr.calls.signEvent, 1);
  await assert.rejects(extensionSigner(pubkey, { ...session, expires: 1 }).auth('m'), /expired/);
});

test('an extension on another account, or a forged reply, is refused', async () => {
  globalThis.nostr = fakeExtension('44'.repeat(32));
  await assert.rejects(extensionSignature(pubkey, 'claim', new Uint8Array(32)), /another account/);
  globalThis.nostr = fakeExtension(secret, { tamper: true });
  await assert.rejects(extensionSignature(pubkey, 'claim', new Uint8Array(32)), /invalid signature/);
  delete globalThis.nostr;
  await assert.rejects(extensionSignature(pubkey, 'claim', new Uint8Array(32)), /No Nostr extension/);
});

test('stored entries unlock the right identity', () => {
  const vault = 'ab'.repeat(32);
  const key = identityFor(pubkey, secret);
  assert.equal(key.kind, 'key'); assert.equal(key.root, secret); assert.equal(key.nostr, false); assert.equal(key.secret, secret);
  const nsec = identityFor(pubkey, { kind: 'nsec', secret, vault });
  assert.equal(nsec.kind, 'nsec'); assert.equal(nsec.root, vault); assert.equal(nsec.nostr, true); assert.equal(nsec.secret, undefined);
  const live = { secret: '22'.repeat(32), expires: Math.floor(Date.now() / 1000) + 3600 };
  assert.equal(identityFor(pubkey, { kind: 'nip07', session: live, vault }).signer.pubkey, pubkey);
  // An expired session or a locked ncryptsec needs a sign-in first.
  assert.equal(identityFor(pubkey, { kind: 'nip07', session: { ...live, expires: 1 }, vault }), null);
  assert.equal(identityFor(pubkey, { kind: 'ncryptsec', ncryptsec: 'ncryptsec1x' }), null);
  // Entries filed under another public key are ignored.
  assert.equal(identityFor(profileKey('44'.repeat(32)), { kind: 'nsec', secret, vault }), null);
  assert.equal(identityFor(pubkey, { kind: 'nsec', secret, vault: 'short' }), null);
});
