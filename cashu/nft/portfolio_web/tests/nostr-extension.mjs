// A NIP-07 signing extension for tests: `window.nostr` backed by a key.
import { hexToBytes } from '@noble/hashes/utils.js';
import * as nip44 from 'nostr-tools/nip44';
import { finalizeEvent, getPublicKey } from 'nostr-tools/pure';

export function fakeExtension(secret, { tamper = false } = {}) {
  const key = hexToBytes(secret), pubkey = getPublicKey(key);
  const calls = { getPublicKey: 0, signEvent: 0, encrypt: 0, decrypt: 0 };
  const conversation = (peer) => nip44.getConversationKey(key, peer);
  return {
    calls,
    async getPublicKey() { calls.getPublicKey++; return pubkey; },
    async signEvent(template) {
      calls.signEvent++;
      const event = finalizeEvent({ ...template }, key);
      return tamper ? { ...event, sig: event.sig.replace(/^./, (c) => (c === '0' ? '1' : '0')) } : event;
    },
    nip44: {
      async encrypt(peer, plaintext) { calls.encrypt++; return nip44.encrypt(plaintext, conversation(peer)); },
      async decrypt(peer, ciphertext) { calls.decrypt++; return nip44.decrypt(ciphertext, conversation(peer)); },
    },
  };
}
