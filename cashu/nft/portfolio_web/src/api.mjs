import { bytesToHex, sha256 } from './crypto.mjs';
import { asSigner } from './signer.ts';

export async function checked(response) {
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.detail === 'string' ? payload.detail : `Request failed (${response.status}).`);
  }
  return response;
}
export async function getJSON(path) { return (await checked(await fetch(path, { cache: 'no-store' }))).json(); }
// `signer` is a Signer (signer.ts) or a raw private key. `origin` is empty in
// the browser (same-origin); Node tests pass the server URL.
/** @param {string | import('./signer.ts').Signer} signer @param {string} path @param {string | Uint8Array} [body]
 *  @param {string} [contentType] @param {string} [origin] @param {Record<string, string>} [headers] */
export async function signedRequest(signer, path, body = new Uint8Array(), contentType = 'application/octet-stream', origin = '', headers = {}) {
  const bytes = typeof body === 'string' ? new TextEncoder().encode(body) : body;
  signer = asSigner(signer);
  const pubkey = signer.pubkey;
  const challenge = await (await checked(await fetch(origin + '/api/auth/challenge', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pubkey, method: 'POST', path, body_hash: bytesToHex(sha256(bytes)) }),
  }))).json();
  const expected = `Cashu_NFT_Portfolio_Auth_v1\n${pubkey}\nPOST\n${path}\n${bytesToHex(sha256(bytes))}\n${challenge.nonce}\n${challenge.expires}`;
  if (challenge.message !== expected) throw new Error('Unexpected signing challenge.');
  const { signature, session } = await signer.auth(expected);
  return checked(await fetch(origin + path, {
    method: 'POST', body: bytes,
    headers: { ...headers, 'Content-Type': contentType, 'X-Portfolio-Challenge': challenge.nonce,
      'X-Portfolio-Signature': signature, ...(session ? { 'X-Portfolio-Session': session } : {}) },
  }));
}
export function download(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
