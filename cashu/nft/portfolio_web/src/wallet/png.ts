// The PNG transfer envelope: one tEXt chunk, keyword "PSNFT", holding the
// token. It is written right before IEND and recognised at any chunk boundary.
// Byte-for-byte the same as cashu/nft/imgmeta.py and portfolio_image.py.
import { concatBytes } from '@noble/hashes/utils.js';
import { utf8, integer } from './ps.ts';

const MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const KEYWORD = utf8('PSNFT\0');
const TOKEN = /^psnft1[0-9a-f]{386}$/;
const equal = (a: Uint8Array, b: Uint8Array) => a.length === b.length && a.every((v, i) => b[i] === v);

// CRC-32 as in zlib and the PNG spec (reflected polynomial 0xedb88320).
const TABLE = Uint32Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
export function crc32(bytes: Uint8Array): number {
  let c = 0xffffffff;
  for (const b of bytes) c = TABLE[(c ^ b) & 255] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function envelope(token: string) {
  if (!TOKEN.test(token)) throw new Error('Invalid NFT transfer token');
  const body = concatBytes(utf8('tEXt'), KEYWORD, utf8(token));
  return concatBytes(integer(BigInt(body.length - 4), 4), body, integer(BigInt(crc32(body)), 4));
}

export const isPng = (bytes: Uint8Array) => MAGIC.every((b, i) => bytes[i] === b);

/** [start, end, type] of every chunk from IHDR to IEND. Like the server, this
 *  requires the file to end at IEND and refuses animated PNGs for now. */
function chunks(bytes: Uint8Array): [number, number, string][] {
  if (!isPng(bytes)) throw new Error('Choose a PNG file');
  const out: [number, number, string][] = [];
  let pos = MAGIC.length;
  for (;;) {
    if (pos + 12 > bytes.length) throw new Error('This PNG is truncated');
    const length = ((bytes[pos] << 24) | (bytes[pos + 1] << 16) | (bytes[pos + 2] << 8) | bytes[pos + 3]) >>> 0;
    const type = String.fromCharCode(...bytes.slice(pos + 4, pos + 8)), end = pos + 12 + length;
    if (end > bytes.length) throw new Error('This PNG is truncated');
    if (!out.length && type !== 'IHDR') throw new Error('This PNG is damaged');
    if (type === 'acTL') throw new Error('Animated PNGs aren’t supported yet');
    out.push([pos, end, type]);
    pos = end;
    if (type === 'IEND') {
      if (pos !== bytes.length) throw new Error('This PNG has bytes after its end');
      return out;
    }
  }
}

export function splitPng(bytes: Uint8Array): { image: Uint8Array; token: string | null } {
  const kept = [bytes.slice(0, MAGIC.length)];
  let token: string | null = null;
  for (const [start, end, type] of chunks(bytes)) {
    const chunk = bytes.slice(start, end);
    // Only our exact, canonical tEXt chunk is removable. Other text chunks
    // remain part of the picture's identity, matching the Python parser.
    const text = type === 'tEXt' && equal(chunk.slice(8, 8 + KEYWORD.length), KEYWORD)
      ? String.fromCharCode(...chunk.slice(8 + KEYWORD.length, -4)) : '';
    if (TOKEN.test(text) && equal(chunk, envelope(text))) {
      if (token) throw new Error('This PNG contains multiple transfer tokens');
      token = text;
    } else kept.push(chunk);
  }
  return { image: concatBytes(...kept), token };
}

export function transferPng(image: Uint8Array, token: string) {
  if (splitPng(image).token) throw new Error('The public PNG already contains a transfer token');
  const iend = image.length - 12; // splitPng checked that IEND ends the file
  return concatBytes(image.slice(0, iend), envelope(token), image.slice(iend));
}
