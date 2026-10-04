// One entry point for transfer files of every supported format: find the
// format from the file's first bytes, then strip or add our envelope.
import { LABELS, formatOf } from '../formats.mjs';
import { splitJpg, transferJpg } from './jpg.ts';
import { splitPng, transferPng } from './png.ts';

export interface Split { image: Uint8Array; token: string | null; }
interface Parser { split(bytes: Uint8Array): Split; transfer(image: Uint8Array, token: string): Uint8Array; }

const PARSERS: Record<string, Parser> = {
  jpg: { split: (bytes) => { const { jpg, token } = splitJpg(bytes); return { image: jpg, token }; }, transfer: transferJpg },
  png: { split: splitPng, transfer: transferPng },
};

function parser(bytes: Uint8Array): Parser {
  const format = formatOf(bytes);
  if (!format || !PARSERS[format.name]) throw new Error(`Choose a ${LABELS} file`);
  return PARSERS[format.name];
}

/** The public picture and the transfer token of a file (null if it has none). */
export const splitImage = (bytes: Uint8Array): Split => parser(bytes).split(bytes);
/** The public picture with a transfer token added: a transfer file. */
export const transferImage = (image: Uint8Array, token: string): Uint8Array => parser(image).transfer(image, token);
