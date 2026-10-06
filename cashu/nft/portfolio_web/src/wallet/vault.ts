import { bytesToHex, hexToBytes } from '@noble/hashes/utils.js';
import { utf8 } from './ps.ts';
import { openRecordStore, type RecordStore } from '../storage.ts';

export interface Envelope { version: 1; nonce: string; ciphertext: string; }
const copy = (bytes: Uint8Array) => Uint8Array.from(bytes);
// `root` is the profile's HKDF root (signer.ts `Profile.root`).
export async function walletSeed(root: string, keyset: string) {
  const material = await crypto.subtle.importKey('raw', copy(hexToBytes(root)), 'HKDF', false, ['deriveBits']);
  return new Uint8Array(await crypto.subtle.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt: copy(hexToBytes(keyset)), info: copy(utf8('Cashu_NFT_Coco_Seed_v1')) }, material, 512));
}
export class EncryptedVault {
  private key: Promise<CryptoKey>;
  private database: Promise<RecordStore>;
  constructor(root: string, private pubkey: string, private keyset: string) {
    this.key = (async () => {
      const material = await crypto.subtle.importKey('raw', copy(hexToBytes(root)), 'HKDF', false, ['deriveKey']);
      return crypto.subtle.deriveKey({ name: 'HKDF', hash: 'SHA-256', salt: copy(hexToBytes(keyset)), info: copy(utf8('Cashu_NFT_Credential_Encryption_v1')) }, material, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
    })();
    this.database = openRecordStore(`cashu-nft-vault-v2:${pubkey}:${keyset}`, 'encrypted');
  }
  private aad(scope: string) { return copy(utf8(`Cashu_NFT_Encrypted_v1\n${this.pubkey}\n${this.keyset}\n${scope}`)); }
  async encrypt(value: unknown, scope: string): Promise<Envelope> {
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const ciphertext = await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce, additionalData: this.aad(scope), tagLength: 128 }, await this.key, copy(utf8(JSON.stringify(value))));
    return { version: 1, nonce: bytesToHex(nonce), ciphertext: bytesToHex(new Uint8Array(ciphertext)) };
  }
  async decrypt<T>(envelope: Envelope, scope: string): Promise<T> {
    if (envelope.version !== 1 || !/^[0-9a-f]{24}$/.test(envelope.nonce) || !/^(?:[0-9a-f]{2}){16,}$/.test(envelope.ciphertext)) throw new Error('Invalid encrypted wallet backup');
    const raw = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: copy(hexToBytes(envelope.nonce)), additionalData: this.aad(scope), tagLength: 128 }, await this.key, copy(hexToBytes(envelope.ciphertext)));
    return JSON.parse(new TextDecoder().decode(raw)) as T;
  }
  async put(id: string, envelope: Envelope) { await (await this.database).put({ id, envelope }); }
  async get(id: string): Promise<Envelope | null> {
    const row = await (await this.database).get<{ id: string; envelope: Envelope }>(id);
    return row?.envelope || null;
  }
  async remove(id: string) { await (await this.database).remove(id); }
  async close() { (await this.database).close(); }
}
