// Nostr login phases for tests/test_nft_nostr_browser.py. Each phase is a
// separate Node process (a fresh browser) against a real portfolio server.
// The signing extension is tests/nostr-extension.mjs; no relays are used.
// Usage: node --import tsx tests/nostr.e2e.mjs <phase> '<json>'
import 'fake-indexeddb/auto';
import { readFileSync } from 'node:fs';
import { connectExtension, connectKey, findVault, newRoot, rootMatches, sealVault, createCollection, storeVault, useRelays } from '../src/nostr.ts';
import { identityFor } from '../src/identity.ts';
import { signedRequest } from '../src/api.mjs';
import { deferred, isDeferred } from '../src/signer.ts';
import { openWallet } from '../src/wallet/index.ts';
import { fakeExtension } from './nostr-extension.mjs';

const ORIGIN = process.env.PORTFOLIO_URL;
if (!ORIGIN) throw new Error('PORTFOLIO_URL is required');
const [phase, raw = '{}'] = process.argv.slice(2);
const input = JSON.parse(raw);

const realFetch = globalThis.fetch;
let dropPublish = false;
globalThis.fetch = async (url, init) => {
  const target = typeof url === 'string' && url.startsWith('/') ? ORIGIN + url : url;
  if (dropPublish && String(target).includes('/wallet/operations/') && String(target).endsWith('/publish')) {
    dropPublish = false;
    throw new TypeError('connection lost before publishing');
  }
  return realFetch(target, init);
};
globalThis.location = { origin: ORIGIN };
useRelays([]);

const fixture = JSON.parse(readFileSync(new URL('./fixtures/wallet.json', import.meta.url)));
const jpg = Uint8Array.from(Buffer.from(fixture.jpg, 'base64')), png = Uint8Array.from(Buffer.from(fixture.png, 'base64'));
const get = async (path) => (await realFetch(ORIGIN + path)).json();
const extensionIdentity = (login, root) => identityFor(login.pubkey, { kind: 'nip07', session: login.session, vault: root });

const phases = {
  /** First login with an extension: a new collection, its vault, one NFT. */
  async extension_signup({ secret, name }) {
    globalThis.nostr = fakeExtension(secret);
    const login = await connectExtension();
    const before = { existing: login.existing, vault: await findVault(login) };
    const vault = await sealVault(login, newRoot());
    const { profile, published } = await createCollection(login, name, vault, false);
    const { manager, wallet } = await openWallet(extensionIdentity(login, vault.root), await get('/api/config'));
    try {
      const card = await wallet.mint(jpg, 'Signed by an extension');
      return { pubkey: login.pubkey, before, profile, published, root: vault.root, check: vault.check, signature: card.signature, calls: globalThis.nostr.calls };
    } finally { await manager.dispose(); }
  },

  /** Another browser: the extension decrypts the server's vault copy. */
  async extension_new_device({ secret }) {
    globalThis.nostr = fakeExtension(secret);
    const login = await connectExtension();
    const vault = await findVault(login);
    const { manager, wallet } = await openWallet(extensionIdentity(login, vault.root), await get('/api/config'));
    try {
      const recovered = await wallet.recover();
      return { nostr: login.existing.nostr, check: vault.check, onServer: vault.onServer, recovered, calls: globalThis.nostr.calls };
    } finally { await manager.dispose(); }
  },

  /** The pasted nsec of the same key opens the same wallet. */
  async nsec_login({ secret }) {
    const login = await connectKey(secret);
    const vault = await findVault(login);
    return { pubkey: login.pubkey, nostr: login.existing.nostr, check: vault.check };
  },

  /** A collection that uses its own key, created as before Nostr login. */
  async key_collection({ secret }) {
    const login = await connectKey(secret);
    await signedRequest(secret, `/api/profiles/${login.pubkey}`, JSON.stringify({ name: 'Own key' }), 'application/json');
    return { pubkey: login.pubkey };
  },
  /** An extension on a collection that uses its own key is refused. */
  async extension_on_key_collection({ secret }) {
    globalThis.nostr = fakeExtension(secret);
    try { await connectExtension(); return { refused: false }; }
    catch (error) { return { refused: true, message: error.message, calls: globalThis.nostr.calls }; }
  },

  /** A publish lost in transit leaves an operation that needs a signature. */
  async interrupted_mint({ secret, root }) {
    globalThis.nostr = fakeExtension(secret);
    const login = await connectExtension();
    const { manager, wallet } = await openWallet(extensionIdentity(login, root), await get('/api/config'));
    dropPublish = true;
    try { await wallet.mint(png, 'Interrupted'); return { interrupted: false }; }
    catch (error) { return { interrupted: /connection lost/.test(error.message), error: error.message }; }
    finally { await manager.dispose(); }
  },
  /** Recovery in the background defers that signature; asking signs it. */
  async deferred_recovery({ secret, root }) {
    globalThis.nostr = fakeExtension(secret);
    const login = await connectExtension();
    const signsBefore = globalThis.nostr.calls.signEvent;
    const { manager, wallet } = await openWallet(extensionIdentity(login, root), await get('/api/config'));
    try {
      let background = null;
      try { await wallet.recover({ interactive: false }); } catch (error) { background = isDeferred(error) ? 'deferred' : error.message; }
      const waiting = deferred.count, promptsInBackground = globalThis.nostr.calls.signEvent - signsBefore;
      deferred.clear();
      const recovered = await wallet.recover({ interactive: true });
      return { background, waiting, promptsInBackground, recovered, prompts: globalThis.nostr.calls.signEvent - signsBefore };
    } finally { await manager.dispose(); }
  },

  /** The server lost its vault copy: only the backed-up wallet key restores it. */
  async restore({ secret, root }) {
    const login = await connectKey(secret);
    const missing = (await findVault(login)) === null;
    const wrong = await rootMatches(login, newRoot());
    const right = await rootMatches(login, root);
    await storeVault(login, await sealVault(login, root), { relays: false });
    const again = await findVault(login);
    return { missing, wrong, right, restored: again?.root === root };
  },
};

const result = await phases[phase](input);
console.log('RESULT ' + JSON.stringify(result));
process.exit(0);
