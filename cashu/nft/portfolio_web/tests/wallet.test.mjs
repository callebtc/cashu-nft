import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import 'fake-indexeddb/auto';
import { bytesToHex } from '@noble/hashes/utils.js';
import { EncryptedVault, walletSeed } from '../src/wallet/vault.ts';
import { hashAsset, integer, encodeToken, decodeToken, verifyCredential, publicCard } from '../src/wallet/ps.ts';
import { splitJpg, transferJpg } from '../src/wallet/jpg.ts';
import { crc32, splitPng, transferPng } from '../src/wallet/png.ts';
import { splitImage, transferImage } from '../src/wallet/image.ts';
import { ACCEPT, LABELS, fileFormat, formatOf, withoutExtension } from '../src/formats.mjs';
import { profileKey, verifyCard } from '../src/crypto.mjs';
import { openWallet } from '../src/wallet/index.ts';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/wallet.json', import.meta.url)));
const jpg = Uint8Array.from(Buffer.from(fixture.jpg, 'base64'));
const secret = '33'.repeat(32), pubkey = profileKey(secret);

test('Python credentials and EXIF transfers match the browser implementation', () => {
  const token = encodeToken(fixture.credential);
  assert.deepEqual(decodeToken(token), fixture.credential);
  verifyCredential(fixture.credential, fixture.config);
  assert.equal(bytesToHex(integer(hashAsset(jpg))), fixture.credential.h);
  const wrapped = transferJpg(jpg, token);
  assert.deepEqual(wrapped, Uint8Array.from(Buffer.from(fixture.transfer, 'base64')));
  assert.deepEqual(splitJpg(wrapped), {jpg, token});
  assert.deepEqual(splitJpg(jpg), {jpg, token:null});
  const segment = wrapped.slice(2, wrapped.length - jpg.length + 2);
  const duplicate = new Uint8Array([...wrapped.slice(0,2), ...segment, ...wrapped.slice(2)]);
  assert.throws(() => splitJpg(duplicate), /multiple/);
  assert.throws(() => decodeToken(token.slice(0,-2)), /Invalid/);
});

const png = Uint8Array.from(Buffer.from(fixture.png, 'base64'));
const pngTransfer = Uint8Array.from(Buffer.from(fixture.png_transfer, 'base64'));
const concat = (...parts) => Uint8Array.from(parts.flatMap((p) => [...p]));
const pngChunk = (type, data = []) => {
  const body = concat(new TextEncoder().encode(type), data);
  const u32 = (n) => [n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
  return concat(u32(data.length), body, u32(crc32(body)));
};

test('PNG transfer files match the Python implementation', () => {
  const token = encodeToken(fixture.credential);
  assert.equal(crc32(new TextEncoder().encode('123456789')), 0xcbf43926);
  assert.deepEqual(transferPng(png, token), pngTransfer);
  assert.deepEqual(splitPng(pngTransfer), { image: png, token });
  assert.deepEqual(splitPng(png), { image: png, token: null });
  // The owner's own text chunk stays part of the public picture.
  assert.ok(Buffer.from(png).includes('Author'));
  // Found at any chunk boundary, not only before IEND, but never twice.
  const envelope = pngTransfer.slice(png.length - 12, pngTransfer.length - 12);
  const early = concat(png.slice(0, 33), envelope, png.slice(33));
  assert.deepEqual(splitPng(early), { image: png, token });
  assert.throws(() => splitPng(concat(early.slice(0, -12), envelope, early.slice(-12))), /multiple/);
  assert.throws(() => transferPng(pngTransfer, token), /already contains/);
  // The file must end at IEND; animated PNGs aren't supported yet.
  assert.throws(() => splitPng(concat(pngTransfer, [0])), /after its end/);
  assert.throws(() => splitPng(concat(png.slice(0, 33), pngChunk('acTL', [0, 0, 0, 2, 0, 0, 0, 0]), png.slice(33))), /Animated/);
  // A tEXt chunk that only looks like ours is kept, so the hash check fails safely.
  const lookalike = pngChunk('tEXt', concat(new TextEncoder().encode('PSNFT\0'), new TextEncoder().encode(token + ' ')));
  const tampered = concat(png.slice(0, -12), lookalike, png.slice(-12));
  assert.deepEqual(splitPng(tampered), { image: tampered, token: null });
});

test('the format dispatcher reads the first bytes and covers every format', () => {
  const token = encodeToken(fixture.credential);
  assert.deepEqual(transferImage(png, token), pngTransfer);
  assert.deepEqual(splitImage(pngTransfer), { image: png, token });
  assert.deepEqual(splitImage(transferImage(jpg, token)), { image: jpg, token });
  assert.equal(formatOf(png).name, 'png');
  assert.equal(formatOf(jpg).name, 'jpg');
  assert.equal(formatOf(new TextEncoder().encode('GIF89a')), null);
  assert.throws(() => splitImage(new TextEncoder().encode('GIF89a')), /Choose a JPG or PNG file/);
  assert.equal(LABELS, 'JPG or PNG');
  assert.equal(ACCEPT, 'image/jpeg,.jpg,.jpeg,image/png,.png');
  assert.equal(fileFormat({ name: 'Sunset.PNG', type: '' }).name, 'png');
  assert.equal(fileFormat({ name: 'clip.gif', type: 'image/gif' }), null);
  assert.equal(withoutExtension('Sunset.jpeg'), 'Sunset');
  assert.equal(withoutExtension('notes.txt'), 'notes.txt');
});

test('browser-generated showings bind the collector and fail for another profile', () => {
  const card = {pubkey, h:fixture.credential.h, ...publicCard(fixture.credential, secret, pubkey)};
  assert.equal(verifyCard(card, pubkey, fixture.config).valid, true);
  assert.equal(verifyCard(card, profileKey('44'.repeat(32)), fixture.config).valid, false);
  assert.throws(() => verifyCredential({...fixture.credential, s:'00'.repeat(32)}, fixture.config));
  assert.throws(() => verifyCredential({...fixture.credential, h:'01'.repeat(32)}, fixture.config));
});

test('authenticated encryption binds owner, keyset and card and survives another vault', async () => {
  const vault = new EncryptedVault(secret, fixture.config.keyset_id);
  const other = new EncryptedVault('44'.repeat(32), fixture.config.keyset_id);
  const restored = new EncryptedVault(secret, fixture.config.keyset_id);
  try {
    const envelope = await vault.encrypt(fixture.credential, 'card:example');
    assert.equal(JSON.stringify(envelope).includes(fixture.credential.s), false);
    assert.notDeepEqual(envelope, await vault.encrypt(fixture.credential, 'card:example'));
    await vault.put('example', envelope);
    assert.deepEqual(await restored.decrypt(await restored.get('example'), 'card:example'), fixture.credential);
    await assert.rejects(other.decrypt(envelope, 'card:example'));
    await assert.rejects(restored.decrypt(envelope, 'card:another'));
    const tampered = {...envelope, ciphertext:(envelope.ciphertext.startsWith('00') ? '01':'00') + envelope.ciphertext.slice(2)};
    await assert.rejects(restored.decrypt(tampered, 'card:example'));
    const seed = await walletSeed(secret, fixture.config.keyset_id);
    assert.equal(seed.length, 64);
    assert.deepEqual(seed, await walletSeed(secret, fixture.config.keyset_id));
    assert.notDeepEqual(seed, await walletSeed('44'.repeat(32), fixture.config.keyset_id));
  } finally {await vault.close();await other.close();await restored.close();}
});

test('real Coco initializes the PS extension with the overridden cashu-ts rc.11', async () => {
  globalThis.location = {origin:'https://wallet.test'};
  const {manager, wallet} = await openWallet(secret, fixture.config);
  assert.equal(manager.ext.nft, wallet);
  assert.equal(wallet.pubkey, pubkey);
  await manager.dispose();
});
