// File formats an NFT can be. This module has no dependencies so the UI can
// use it directly; parsing lives in wallet/image.ts, and the server's rules in
// cashu/nft/portfolio_image.py. Adding a format means adding it in all three.
export const FORMATS = [
  { name: 'jpg', label: 'JPG', mime: 'image/jpeg', extensions: ['jpg', 'jpeg'], magic: [0xff, 0xd8] },
  { name: 'png', label: 'PNG', mime: 'image/png', extensions: ['png'], magic: [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] },
];

const labels = FORMATS.map((f) => f.label);
/** "JPG or PNG" (or "JPG, PNG or WebP"), for copy. */
export const LABELS = labels.length > 1 ? `${labels.slice(0, -1).join(', ')} or ${labels.at(-1)}` : labels[0];
/** For <input type="file" accept>. */
export const ACCEPT = FORMATS.flatMap((f) => [f.mime, ...f.extensions.map((e) => '.' + e)]).join(',');

/** The format of a file's bytes, or null. */
export const formatOf = (bytes) => FORMATS.find((f) => f.magic.every((b, i) => bytes[i] === b)) || null;
/** The format a chosen file claims by its type or name, or null. */
export const fileFormat = (file) => FORMATS.find((f) => f.mime === file.type || f.extensions.some((e) => file.name.toLowerCase().endsWith('.' + e))) || null;
/** A file name without a known image extension, for default titles. */
export function withoutExtension(name) {
  const lower = name.toLowerCase();
  const ext = FORMATS.flatMap((f) => f.extensions).find((e) => lower.endsWith('.' + e));
  return ext ? name.slice(0, -ext.length - 1) : name;
}
/** Where the public picture of an NFT with asset hash h is served. */
export const imageUrl = (h) => `/api/images/${h}`;
