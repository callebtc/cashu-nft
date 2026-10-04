// Token QR codes per NUT-16: tokens with up to two proofs fit a static QR code,
// larger ones are shown as an animated sequence of UR fountain-code parts that
// the receiving wallet scans until it can reassemble the token.
import { Buffer } from 'buffer';
// The ES build: the package's CommonJS entry require()s cborg, which is ESM-only.
import { UR, UREncoder } from '@gandlaf21/bc-ur/dist/lib/es6/index.js';
import { getTokenMetadata } from '@cashu/coco-core';

export const STATIC_MAX_PROOFS = 2;
// Same defaults as Cashu.me so scanners tuned for it read ours just as well.
export const UR_FRAGMENT_LENGTH = 150;
export const UR_FRAME_MS = 150;

export const needsAnimatedQr = (token) => getTokenMetadata(token).proofAmounts.length > STATIC_MAX_PROOFS;

/** Returns a function yielding the next UR part, forever. Parts are uppercased
 *  so the QR code can use the denser alphanumeric mode; UR decoders accept both. */
export function urFrames(token, fragmentLength = UR_FRAGMENT_LENGTH) {
  const encoder = new UREncoder(UR.fromBuffer(Buffer.from(token)), fragmentLength, 0);
  return () => encoder.nextPart().toUpperCase();
}
