import test from 'node:test';
import assert from 'node:assert/strict';
import { signedRequest } from '../src/api.mjs';

test('owner requests sign the path exactly as fetch sends it', async () => {
  // encodeURIComponent leaves ' alone, but the URL parser escapes it in a
  // query (%27). The server checks the signature against what it received.
  const realFetch = globalThis.fetch, paths = {};
  globalThis.fetch = async (url, init) => {
    const wire = new URL(url);
    if (wire.pathname === '/api/auth/challenge') {
      const b = JSON.parse(init.body);
      paths.signed = b.path;
      return Response.json({ nonce: 'n', expires: 1, message: `Cashu_NFT_Portfolio_Auth_v1\n${b.pubkey}\nPOST\n${b.path}\n${b.body_hash}\nn\n1` });
    }
    paths.sent = wire.pathname + wire.search;
    return Response.json({});
  };
  try {
    const title = "you wouldn't mint (an) HTLC!";
    await signedRequest('11'.repeat(32), `/api/profiles/${'ab'.repeat(32)}/wallet/prepare?kind=mint&title=${encodeURIComponent(title)}`, '', 'application/json', 'https://portfolio.test');
    assert.equal(paths.signed, paths.sent);
    assert.equal(new URLSearchParams(paths.sent.split('?')[1]).get('title'), title);
  } finally { globalThis.fetch = realFetch; }
});
