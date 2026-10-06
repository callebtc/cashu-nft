// No fake-indexeddb here: this file runs as a browser without IndexedDB
// (Safari Lockdown Mode, some in-app browsers).
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { local, onDeviceStorage, openRecordStore } from '../src/storage.ts';
import { EncryptedVault } from '../src/wallet/vault.ts';
import { EncryptedRepositories } from '../src/money/store.ts';
import { openWallet } from '../src/wallet/index.ts';
import { profileKey } from '../src/crypto.mjs';

const fixture = JSON.parse(readFileSync(new URL('./fixtures/wallet.json', import.meta.url)));
const secret = '66'.repeat(32);

test('without IndexedDB, storage falls back to memory', async () => {
  assert.equal(typeof indexedDB, 'undefined');
  assert.equal(await onDeviceStorage(), false);
  const store = await openRecordStore('x', 'y');
  assert.equal(store.persistent, false);
  await store.put({ id: 'a', v: 1 });
  assert.deepEqual(await store.get('a'), { id: 'a', v: 1 });
  assert.equal((await store.all()).length, 1);
  await store.remove('a');
  assert.equal(await store.get('a'), undefined);
  // localStorage is missing too: the helper must not throw.
  assert.equal(local.get('k'), null);
  local.set('k', 'v');
});

test('the credential vault works in memory', async () => {
  const vault = new EncryptedVault(secret, profileKey(secret), fixture.config.keyset_id);
  const envelope = await vault.encrypt({ hello: 'world' }, 'scope');
  await vault.put('one', envelope);
  assert.deepEqual(await vault.decrypt(await vault.get('one'), 'scope'), { hello: 'world' });
  await vault.close();
});

test('in memory, the ecash wallet pushes every change to its server backup', async () => {
  let backup = { revision: 0, envelope: null }, pushes = 0;
  const remote = {
    async lease(device) { return { granted: true, holder: device, until: Date.now() / 1000 + 600 }; },
    async get() { return backup; },
    async put(_device, base, revision, envelope) { assert.equal(base, backup.revision); backup = { revision, envelope }; pushes++; return { revision }; },
  };
  const repos = await EncryptedRepositories.open(secret, profileKey(secret), remote);
  assert.equal(repos.ephemeral, true);
  assert.equal(await repos.acquireLease(), true);
  await repos.counterRepository.setCounter('https://mint.test', 'keyset', 7);
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(pushes, 1);
  assert.ok(backup.revision > 0);
  await repos.close();

  // A fresh visit has nothing locally and restores from the backup.
  const again = await EncryptedRepositories.open(secret, profileKey(secret), remote);
  assert.equal((await again.counterRepository.getCounter('https://mint.test', 'keyset')).counter, 7);
  await again.close();
});

test('the NFT wallet opens on in-memory Coco repositories', async () => {
  globalThis.location = { origin: 'https://wallet.test' };
  const { manager, wallet } = await openWallet(secret, fixture.config);
  assert.equal(manager.ext.nft, wallet);
  await manager.dispose();
});
