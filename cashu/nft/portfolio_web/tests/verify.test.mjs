// Browser verifier checked against showings produced by the Python mint
// (tests/fixtures/showings.json was captured from a running portfolio app).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { bytesToHex, hexToBytes, newPrivateKey, parseShowing, signClaim, profileKey, verifyCard } from '../src/crypto.mjs';

const fx = JSON.parse(readFileSync(new URL('./fixtures/showings.json', import.meta.url)));
const blind = JSON.parse(readFileSync(new URL('./fixtures/blind-showing.json', import.meta.url)));
const ORDER = 52435875175126190479447740508185965837690552500527637822603658699938581184513n;

function withPresentation(card, offset, bytes) {
  const { context, presentation } = parseShowing(card.showing);
  const p = presentation.slice();
  p.set(bytes, offset);
  const len = new Uint8Array([context.length >> 8, context.length & 255]);
  return { ...card, showing: 'pshow1' + bytesToHex(len) + bytesToHex(context) + bytesToHex(p) };
}
const reason = (card, pubkey = fx.bob, config = fx.config) => verifyCard(card, pubkey, config);

test('accepts a Python-generated showing with a valid profile signature', () => {
  const r = reason(fx.owned);
  assert.equal(r.valid, true, r.reason);
  assert.match(r.nullifier, /^[0-9a-f]{96}$/);
});

test('accepts an unblinded issuance credential through the existing public verifier', () => {
  const result = verifyCard(blind.card, blind.card.pubkey, blind.config);
  assert.equal(result.valid, true, result.reason);
  assert.match(result.nullifier, /^[0-9a-f]{96}$/);
});

test('accepts a historical (spent) showing; spent status comes from the mint', () => {
  const r = reason(fx.sent, fx.alice);
  assert.equal(r.valid, true, r.reason);
  assert.notEqual(r.nullifier, reason(fx.owned).nullifier);
});

test('rejects the card under a different profile', () => {
  assert.equal(reason(fx.owned, fx.alice).valid, false);
  assert.equal(reason({ ...fx.owned, pubkey: fx.alice }, fx.alice).reason, 'Wrong profile context');
});

test('rejects altered picture hash, keyset and mint parameters', () => {
  assert.equal(reason({ ...fx.owned, h: fx.owned.h.replace(/.$/, (c) => c === '0' ? '1' : '0') }).reason, 'Wrong picture identity');
  assert.equal(reason(fx.owned, fx.bob, { ...fx.config, keyset_id: '03' + '00'.repeat(32) }).reason, 'Untrusted mint keyset');
  const pk = fx.config.public_key;
  const badKey = pk.slice(0, 20) + (pk[20] === 'a' ? 'b' : 'a') + pk.slice(21);
  assert.equal(reason(fx.owned, fx.bob, { ...fx.config, public_key: badKey }).valid, false);
});

test('rejects missing, tampered and foreign profile signatures', () => {
  assert.equal(reason({ ...fx.owned, signature: null }).pending, true);
  const sig = fx.owned.signature;
  const flipped = (sig[10] === 'f' ? 'e' : 'f');
  assert.equal(reason({ ...fx.owned, signature: sig.slice(0, 10) + flipped + sig.slice(11) }).valid, false);
  const stranger = newPrivateKey();
  assert.notEqual(profileKey(stranger), fx.bob);
  assert.equal(reason({ ...fx.owned, signature: signClaim(stranger, fx.owned.showing) }).reason, 'Invalid profile signature');
  // A pubkey in the showing context without the matching signature is insufficient.
  assert.equal(reason({ ...fx.owned, signature: fx.sent.signature }).reason, 'Invalid profile signature');
});

test('rejects out-of-range scalars and tampered proof responses', () => {
  const big = hexToBytes((ORDER + 1n).toString(16).padStart(64, '0'));
  assert.equal(reason(withPresentation(fx.owned, 257, big)).reason, 'Noncanonical scalar');
  assert.equal(reason(withPresentation(fx.owned, 289, big)).reason, 'Noncanonical scalar');
  const r = parseShowing(fx.owned.showing).presentation.slice(289, 321);
  r[31] ^= 1;
  assert.equal(reason(withPresentation(fx.owned, 289, r)).reason, 'Invalid owner proof');
});

test('rejects infinity, off-curve and swapped points', () => {
  const infinity = new Uint8Array(48); infinity[0] = 0xc0;
  assert.equal(reason(withPresentation(fx.owned, 65, infinity)).valid, false);
  const offCurve = new Uint8Array(48).fill(0xff); offCurve[0] = 0x9f;
  assert.equal(reason(withPresentation(fx.owned, 113, offCurve)).valid, false);
  // Swap in the sigma v from another credential: the pairing must fail.
  const otherV = parseShowing(fx.sent.showing).presentation.slice(113, 161);
  assert.equal(reason(withPresentation(fx.owned, 113, otherV)).valid, false);
});

test('rejects malformed encodings', () => {
  assert.equal(reason({ ...fx.owned, showing: 'pshow2' + fx.owned.showing.slice(6) }).valid, false);
  assert.equal(reason({ ...fx.owned, showing: fx.owned.showing.slice(0, -2) }).valid, false);
  assert.equal(reason({ ...fx.owned, showing: fx.owned.showing + '00' }).valid, false);
});
