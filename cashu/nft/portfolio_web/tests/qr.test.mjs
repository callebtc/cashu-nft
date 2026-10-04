// NUT-16 token QR codes: small tokens stay static, larger ones are encoded as
// UR parts that a standard UR decoder (as used by Cashu.me) reassembles.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import QRCode from 'qrcode';
import { URDecoder } from '@gandlaf21/bc-ur/dist/lib/es6/index.js';
import { getEncodedToken } from '@cashu/coco-core';
import { needsAnimatedQr, urFrames } from '../src/qr.mjs';

const proof = (amount, i) => ({ id: '00ad268c4d1f5826', amount, secret: `${i}`.padStart(64, '0'), C: '02' + `${i}`.padStart(64, 'a') });
const tokenWith = (n) => getEncodedToken({ mint: 'https://testnut.cashu.space', unit: 'sat', proofs: Array.from({ length: n }, (_, i) => proof(2 ** i, i)) });

test('tokens with up to two proofs use a static QR code', () => {
  assert.equal(needsAnimatedQr(tokenWith(1)), false);
  assert.equal(needsAnimatedQr(tokenWith(2)), false);
  assert.equal(needsAnimatedQr(tokenWith(3)), true);
});

test('animated QR frames decode back to the token', async () => {
  const token = tokenWith(40);
  const next = urFrames(token);
  const decoder = new URDecoder();
  for (let i = 0; i < 500 && !decoder.isComplete(); i++) {
    const part = next();
    assert.match(part, /^UR:BYTES\/\d+-\d+\/[A-Z]+$/);
    await QRCode.toString(part, { type: 'svg', margin: 1, errorCorrectionLevel: 'M' });
    decoder.receivePart(part);
  }
  assert.ok(decoder.isSuccess());
  assert.equal(new TextDecoder().decode(decoder.resultUR().decodeCBOR()), token);
});
