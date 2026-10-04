# PNG and GIF NFTs

Research note: can "the NFT is the JPG" extend to PNG and GIF files?

**Status:** static PNG is implemented (`cashu/nft/portfolio_image.py`,
`portfolio_web/src/wallet/png.ts`). One deviation from the recommendation
below: the `PSNFT` chunk is written right before `IEND`, not after `IHDR`,
because `imgmeta.py` already embedded it there and the legacy export path uses
it; in a validated PNG `IEND` is always the last 12 bytes, so this is equally
simple. GIF and animated PNG are not implemented.

Baseline for the research: branch `rename-and-rebrand`, commit `1799609`.
File and line references are to that commit. Pillow references are to Pillow
12.3.0 as installed in the project virtualenv
(`site-packages/PIL/...`). Experiments were run with that Pillow (built against
zlib-ng, `features.version("zlib") == "1.3.1.zlib-ng"`, libjpeg-turbo 3.1.4.1)
on macOS.

Labels: **[code]** read from this repository, **[spec]** from a specification,
**[Pillow]** from Pillow source or docs, **[test]** confirmed by a local
experiment, **[inference]** my reasoning, **[unverified]** a claim I could not
confirm from a primary source.

## Answer

Yes. Static PNG is a small, low-risk extension. GIF is feasible, but animation
and platform behaviour make it costlier and less reliable as a bearer file.

- The cryptography is already format-agnostic. `h` is a hash of arbitrary bytes
  and the mint never sees the file. Neither the credential, the issuance
  protocol, the duplicate registry nor the token encoding has to change. What
  changes is the *transfer-file envelope*: where the token sits in each format
  and how it is removed. Python and browser wallets must agree on it, so treat it
  as an interoperable format needing maintainer sign-off.
- PNG: carry the token in one `tEXt` chunk (keyword `PSNFT`) inserted right after
  `IHDR`. Hash the raw file bytes with that one exact chunk removed, as for
  JPG. Normalize uploads by re-encoding with Pillow, which is pixel-lossless for
  PNG. Reject APNG in the first version.
- GIF: carry the token in one GIF89a Application Extension inserted right after
  the Logical Screen Descriptor and Global Color Table. Do not re-encode GIFs
  with Pillow: that is lossy for some animated GIFs (verified below). Normalize
  structurally instead, by dropping comment, plain-text and foreign application
  blocks while keeping the image data byte-for-byte. Animated GIFs need a
  frame/pixel budget, and moderation that looks at more than the first frame.
- Biggest risks: (1) chat apps re-encode or convert GIFs (often to MP4) and
  strip PNG metadata unless sent "as a file"; (2) moderation and OG previews
  currently look only at frame 0 and drop alpha; (3) small animated files can
  decode to gigabytes; (4) ".jpg" and `image/jpeg` assumptions are spread
  across the server, the web app, the copy and the tests.

## 1. The current JPG design

### What is hashed

- `h = SHA-256("Cashu_PS_Asset_v1" ‖ len(asset) as 4-byte big-endian ‖ asset) mod r`
  in Python (`cashu/core/crypto/ps.py:70`, `cashu/core/crypto/ps.py:132-137`)
  and the browser (`cashu/nft/portfolio_web/src/wallet/ps.ts:28`, `frame(jpg, 4)`
  is the same 4-byte length prefix, `ps.ts:10`). **[code]**
- `asset` is the complete **public file**: the normalized JPG exactly as the
  server stores it in `portfolio_images.jpg` (`cashu/nft/portfolio.py:198-200`,
  `portfolio.py:262-265`). Nothing is decoded or canonicalized before hashing,
  so the identity is byte identity, not pixel identity. The README says so:
  visually identical images with different bytes can each be minted
  (`cashu/nft/portfolio_web/README.md:149-152`). **[code]**
- Nothing in `hash_asset` is JPG-specific. The Python CLI wallet hashes any file
  (`cashu/nft/wallet.py:332-343`, `wallet.py:362-370`; `cashu nft mint my.jpg`,
  `cashu/nft/cli.py:10`). **[code]**

### Mint path (browser wallet, the live path)

1. The browser rejects a file that already carries a transfer token
   (`wallet/index.ts:54`), then uploads the raw bytes to `POST
   /api/profiles/{pubkey}/wallet/prepare?kind=mint` (`index.ts:48-52`,
   `portfolio.py:900-921`).
2. The server runs `normalize_jpg` (`portfolio_wallet.py:120-122`):
   `validate_jpg`, then refusing a transfer JPG (`portfolio_jpg.py:121-125`), then
   decode, apply EXIF orientation, convert to RGB, and save a fresh JPG at
   quality 95, 4:4:4, `optimize=True`, keeping only the ICC profile
   (`portfolio_jpg.py:126-142`). EXIF, XMP and comments are discarded.
3. The moderation check runs on the normalized bytes (`portfolio_wallet.py:122`).
   Size is checked against `max_jpg_bytes` (`portfolio_wallet.py:183-184`), and
   `h` is computed (`portfolio_wallet.py:185`). Burned or already-minted hashes
   are refused (`portfolio_wallet.py:186-194`).
4. The server returns the normalized bytes base64-encoded (`portfolio_wallet.py:249-257`).
   The browser hashes **those** bytes and checks them against the server's `h`
   (`index.ts:56-58`). It then runs committed issuance with the mint.

So the server is the single authority for the canonical bytes. The browser
never has to reproduce the JPEG encoder; it only hashes bytes it was given.
**[code]** The older custodial path does the same in `Portfolio.mint`
(`portfolio.py:286-312`).

### Embedding, locating and stripping the token

- Token: `psnft1` followed by 386 lowercase hex characters, the 193-byte
  credential (`wallet/jpg.ts:8`, `cashu/nft/wallet.py:52`). That is 392 ASCII bytes.
- Envelope: a minimal EXIF APP1 segment, little-endian TIFF, IFD0 → ExifIFD →
  `UserComment` (0x9286) = `"ASCII\0\0\0" + token`, inserted immediately after
  SOI (`cashu/nft/imgmeta.py:24-34`, `imgmeta.py:49-83`;
  `wallet/jpg.ts:7-13`, `jpg.ts:40-43`). The same bytes are produced in Python
  and TypeScript.
- Stripping (`split_transfer_jpg`, `portfolio_jpg.py:71-118`; `splitJpg`,
  `jpg.ts:14-39`) walks every marker segment before SOS/EOI. A segment is
  removed only if it is **byte-equal to the canonical envelope** of the token it
  contains (`portfolio_jpg.py:104-110`, `jpg.ts:31-32`). Every other byte is
  kept. Two envelopes, even identical ones, are rejected (`portfolio_jpg.py:111-113`,
  `jpg.ts:33`). The envelope may sit at any segment position, but it is always
  emitted after SOI.
- Receiving (`index.ts:63-74`): split locally, check
  `hashAsset(public bytes) == cred.h` before any network call, verify the PS
  signature, check that the nullifier is unspent, then upload **only the public
  JPG** (`portfolio_wallet.py:123-131` refuses an upload that still has a
  token). The server runs `validate_jpg` and moderation, but cannot normalize:
  normalizing would change `h`.
- Sending (`index.ts:158-166`): fetch `/api/images/{h}.jpg`, re-check the hash,
  insert the envelope after SOI. Transfer links do the same in `claim.jsx:15-23`.

### Invariants relied on

1. `strip(embed(public, token)) == public`, byte for byte, in Python and the
   browser, and `embed` is deterministic. **[code]**
2. Any public image that passes the hash check is byte-identical to the stored
   normalized file. Portfolio receive therefore accepts only bytes the server
   once produced, or bytes minted elsewhere with the same `h`. **[inference]**
   from the hash check at `index.ts:68`/`portfolio.py:323`.
3. A picture can be minted once per exact byte string. The deterministic tag
   `D = h·G_ASSET` sits in a shared registry, burned assets included
   (`cashu/nft/BLIND_ISSUANCE.md`, "Protocol" step 3). **[code]**
4. No trailing bytes: `validate_jpg` requires the file to end with EOI
   (`portfolio_jpg.py:17-18`). **[code]**
5. Normalization is deterministic for a given Pillow/libjpeg build
   (`tests/test_nft_portfolio.py:559`). It is not promised across library
   versions, which only weakens duplicate detection: re-uploading the same
   source after a Pillow upgrade can yield new bytes. **[inference]**

### Limits

- `max_jpg_bytes` 10 MiB (`portfolio.py:175`, env `NFT_PORTFOLIO_MAX_JPG_BYTES`
  `portfolio.py:1295-1297`), applied to the normalized output. Upload bodies may
  be `max_jpg_bytes` (`portfolio.py:909`). The browser allows 64 KiB extra
  for transfer files (`main.jsx:364`).
- `MAX_PIXELS = 25_000_000` (`portfolio_jpg.py:11`, `portfolio_jpg.py:25-26`).
  Pillow's `DecompressionBombWarning` is promoted to an error
  (`portfolio_jpg.py:20-21`). Pillow's own default limit is
  `MAX_IMAGE_PIXELS = 1024*1024*1024 // 4 // 3` ≈ 89.5 MP: a warning above it,
  an error above twice it (`PIL/Image.py:86`, `Image.py:3564-3582`). **[Pillow]**
- `Image.verify()` is a no-op for JPEG: the base `ImageFile.verify` just closes
  the file (`PIL/ImageFile.py:282-289`, `PIL/Image.py:1006-1015`). The code
  already notes that truncation only surfaces on decode (`portfolio_jpg.py:129`).
  On receive, the full decode happens in moderation (`moderation.py:50-54`). **[Pillow] [code]**

### Where JPG is assumed

| Area | Location |
| --- | --- |
| Magic-byte checks | `portfolio_jpg.py:15-18`, `portfolio_jpg.py:23`, `portfolio_jpg.py:77`; `jpg.ts:15` |
| Envelope code | `imgmeta.py` (JPEG and a demo PNG `tEXt` path), `portfolio_jpg.py:71-118`, `jpg.ts` |
| Normalization | `portfolio_jpg.py:121-142` saves `format="JPEG"` |
| Served MIME and URL | `/api/images/{h}.jpg` → `image/jpeg` (`portfolio.py:1059-1068`); `X-Content-Type-Options: nosniff` on every response (`portfolio.py:542`) |
| URLs in the web app | `imageUrl = /api/images/${h}.jpg` (`main.jsx:66`, `social.jsx:9`, `claim.jsx:9`), `index.ts:161` |
| Download names and types | `cashu-${h}.jpg` (`main.jsx:264`), `cashu-transfer-${h}.jpg` with `image/jpeg` blob (`main.jsx:666`, copy at `main.jsx:335`) |
| Upload filter | extension/MIME check (`main.jsx:363`), `accept="image/jpeg,.jpg,.jpeg"` (`main.jsx:406`), title stripping `.jpe?g` (`main.jsx:371`) |
| Config naming | `max_jpg_bytes` in `/api/config` (`portfolio.py:635`) and `MintConfig` (`ps.ts:25`) |
| DB naming | `portfolio_images(h, jpg BLOB)`, `portfolio_wallet_ops.jpg` (`portfolio.py:198-200`, `portfolio_wallet.py:95`) |
| Copy | "The NFT is the JPG" and EXIF wording (`HowItWorks.jsx:9`, `HowItWorks.jsx:27-33`, `HowItWorks.jsx:74`, `HowItWorks.jsx:109`; `HowBasics.jsx:41`, `HowBasics.jsx:137`, `HowBasics.jsx:151`; modal copy `main.jsx:399-409`; README "JPG handling" `README.md:147-162`); default titles "Untitled JPG"/"Collected JPG" (`portfolio.py:831`, `portfolio.py:841`, `portfolio.py:905`) |
| Moderation decode | `moderation.py:50-54`: `draft()` (JPEG-only speedup), then `convert("RGB")` on frame 0 |
| OG previews | `portfolio_og_image.py:243-262`: same pattern; preview routes are `.jpg` and render JPEG (`portfolio.py:1087-1100`) |
| Tests | `tests/test_nft_portfolio.py:538` asserts that PNG is rejected; JPG fixtures across `tests/test_nft_*.py`, `tests/test_ps_*.py`, `portfolio_web/tests/*.mjs` |

Already format-agnostic: `hash_asset`, the CLI wallet, the ledger and duplicate
registry, and avatars. Avatars accept JPEG/PNG/WebP/GIF, take frame 0 and
composite onto paper before re-encoding to JPG (`portfolio_jpg.py:37-68`).
`imgmeta.py` already embeds a token in a PNG `tEXt` chunk before `IEND`
(`imgmeta.py:9-10`, `imgmeta.py:158-199`; tests in `tests/test_ps_imgmeta.py`).
Only the demo uses it (`demo.py:311`, `demo.py:343`). It has no strict
"split" and returns the last match, so the portfolio would need a stricter
counterpart.

## 2. PNG

### Format facts **[spec]**

From the W3C PNG Specification, Third Edition (<https://www.w3.org/TR/png-3/>):

- Chunk = 4-byte length, 4-byte type, data, 4-byte CRC. The CRC covers type and
  data, not the length (§5.3). The length is limited to 2^31−1 (§5.3).
- Chunk-name property bits, bit 5 of each byte (§5.4, Table 6):
  1st lowercase = ancillary, 2nd lowercase = private, 3rd **must** be uppercase
  ("If the reserved bit is 1, the datastream does not conform"), 4th lowercase =
  safe-to-copy. A decoder "can safely ignore" an unknown ancillary chunk. For an
  unknown critical chunk it "shall indicate to the user" that it cannot safely
  interpret the image.
- Ordering (§5.6, chunk ordering table): `IEND` "Shall be last". `tEXt`, `zTXt`
  and `iTXt` have no ordering constraint and may repeat. `eXIf` must come before
  `IDAT`, and only one is allowed (§11.3.4.5). `acTL` must come before `IDAT`.
  One `fcTL` may come before `IDAT`, all others after. `fdAT` comes after `IDAT`.
  Multiple `IDAT` chunks must be consecutive.
- `tEXt` (§11.3.3.1-2): keyword of 1-79 bytes, printable Latin-1, case-sensitive,
  no leading, trailing or double spaces; then NUL; then text with no NUL. Other
  keywords "MAY be defined by any application".
- Private chunks (§5.7.3): "MAY" be defined. A private chunk "SHOULD NOT be
  defined merely to carry textual information of interest to a human user". It
  should start with identifying data. Annex B (non-normative) adds: for Latin-1
  text, "avoid defining a new chunk type. Use a tEXt or zTXt chunk with a
  suitable keyword". Also: "Avoid defining chunks that depend on total
  datastream contents. If such chunks have to be defined, make them critical
  chunks."
- Editors (§14.2): an unknown chunk with the safe-to-copy bit set "may be copied
  ... regardless of the extent of the datastream modifications". An unknown
  unsafe-to-copy chunk "shall not be copied" once critical chunks were changed.
  Copied safe-to-copy chunks must not move across `IDAT`. §14.2 also says
  "Ordinary image editors are not PNG editors because they usually discard all
  unrecognized information". §14.3.2: decoders must not assume an ancillary
  chunk's position relative to other ancillary chunks.
- Data after `IEND`: the datastream is defined to end with `IEND` (§5.2). §13.3
  acknowledges "there could be data after the IEND chunk which could contain
  anything". Nothing requires decoders to reject it.
- APNG (§4.9, §11.3.6): `acTL` before `IDAT` marks the file as animated. Frames
  are `fcTL` plus `IDAT`/`fdAT`, with a shared sequence number. The static
  `IDAT` image may or may not be frame 0. Non-APNG decoders show the static
  image. The `image/apng` registration (Annex A.2) warns that a file with
  "unrelated static image and animated image chunks ... could be used e.g. to
  bypass moderation". The APNG chunk names have a lowercase second letter and
  an uppercase fourth: they read as private and unsafe-to-copy under §5.4
  **[inference]** from the names.

### Where the token can live

| Option | Fit |
| --- | --- |
| `tEXt` keyword `PSNFT` | **Recommended.** The token is ASCII, so it is valid Latin-1 text. Annex B recommends exactly this for Latin-1 data. No ordering constraint and repeats allowed, so it never conflicts with the image's own metadata. It is safe-to-copy (`tEXt`). Trivial to write in Python and TypeScript: no compression, one CRC. Already used by `imgmeta.py`. Downsides: visible in metadata viewers, and removed by every "strip metadata" optimizer. |
| `zTXt` / compressed `iTXt` | No. It would save only ~200 bytes of hex, add zlib to the browser path, and make the bytes depend on the compressor unless pinned. Pillow caps decompressed text at `MAX_TEXT_CHUNK` = 1 MiB (`PngImagePlugin.py:96-104`). |
| `eXIf` UserComment | No. Only one `eXIf` is allowed, before `IDAT` (§11.3.4.5), so it collides with any EXIF the public image carries. It also brings TIFF parsing into the browser for no gain. |
| Private ancillary chunk, e.g. `psNf` (ancillary, private, reserved, safe-to-copy) or `psNF` (unsafe-to-copy) | Workable. Pillow keeps it in `im.private_chunks` and never writes it back unless asked (see below). An unsafe-to-copy variant would correctly tell PNG editors to drop the token when they change pixels. But §5.7.3/Annex B steer textual data to text chunks, and no mainstream tool treats it differently in practice **[unverified]**. Not worth deviating from the spec's advice. |

### Canonical "picture without the token"

Use the same rule as JPG: hash **the raw file bytes with the one canonical
envelope chunk removed**. The canonical chunk is
`len ‖ "tEXt" ‖ "PSNFT\0" ‖ token ‖ CRC32("tEXt" ‖ "PSNFT\0" ‖ token)`: 4 + 4 +
6 + 392 + 4 = 410 bytes.

- Emit it immediately after `IHDR`. `IHDR` is always the first chunk and always
  25 bytes long (8 + 13 + 4), so the envelope is always at offset 33:
  `transfer = public[:33] + chunk + public[33:]`.
  This mirrors "right after SOI".
- When stripping, accept it at any chunk boundary between `IHDR` and `IEND`. That
  covers PNG editors that reorder safe-to-copy chunks (§14.2) and `imgmeta.py`'s
  before-`IEND` placement. Remove only a chunk byte-equal to the canonical
  encoding of a well-formed token. Reject two or more. Require the stripped
  file to end exactly at `IEND`.
- Hash raw bytes, not decoded pixels. PNG decoding is lossless, so pixels are
  well defined. But a pixel hash would need a bit-exact PNG decoder in the
  browser: canvas `getImageData` is colour-managed and premultiplied, so it is
  unsuitable **[inference]**. It would also exclude `iCCP`/`gAMA` (which change
  how the picture looks) and break parity with the existing JPG rule and
  `hash_asset`. The raw-bytes rule needs no new crypto and no change to `h`.

### Pillow behaviour for PNG **[Pillow] [test]**

- On load, `tEXt`/`zTXt`/`iTXt` go to `im.info` and `im.text`
  (`PngImagePlugin.py:576-594`). Text **after** `IDAT` is only parsed by
  `load()` (`load_end`, `PngImagePlugin.py:1037-1067`). In the experiment, a
  `PSNFT` chunk before `IEND` was absent from `info` until load. Unknown chunks
  are skipped. Private ones (lowercase second letter) are kept in
  `im.private_chunks` (`PngImagePlugin.py:789-793`, `PngImagePlugin.py:1063-1067`).
- `save()` writes none of these unless passed explicitly. Text and private
  chunks need `pnginfo=`, EXIF needs `exif=` (`PngImagePlugin.py:1427-1440`,
  `PngImagePlugin.py:1508-1513`). It does carry over `icc_profile`, `transparency`
  and palette from `im.info` (`PngImagePlugin.py:1414-1489`). Test: a file with
  `tEXt`, two private chunks and an extra private chunk before `IEND` re-saved
  as `IHDR, IDAT, IEND`.
- Without `save_all=True`, a re-save writes one frame. A 4-frame APNG re-saved
  plainly became a 1-frame PNG. With `save_all`, Pillow writes `acTL/fcTL/fdAT`.
  It also merges identical consecutive frames: a 50-frame GIF of identical
  frames came back as 1 frame (`GifImagePlugin.py:697-704` for GIF; PNG is
  analogous at `PngImagePlugin.py:1239-1246`).
- Determinism: two saves of the same image in one process were byte-identical
  (with and without `optimize`), and so was an APNG `save_all`. **[test]** But the
  bytes depend on the deflate implementation. Pillow wheels switched to zlib-ng
  in 11.1.0 ("Wheels are now built against zlib-ng for improved speed",
  <https://pillow.readthedocs.io/en/stable/releasenotes/11.1.0.html>).
  Recompressing a Pillow `IDAT` stream with CPython's zlib 1.2.12 at the same
  level gave different bytes (165 996 vs 166 507). **[test]** This is the
  same situation as libjpeg-turbo today: harmless for stored assets, but it
  weakens exact-duplicate detection across upgrades.
- `verify()` for PNG checks every chunk CRC up to `IEND` (`PngImagePlugin.py:849-863`,
  `PngImagePlugin.py:234-253`). A bad CRC in a private chunk after `IDAT` was
  caught by `verify()` but **not** by `load()`. Trailing bytes after `IEND` load
  without error. **[test]** So validation must check "ends at `IEND`" itself,
  as `validate_jpg` does for EOI.
- `eXIf` orientation is honoured: `ImageOps.exif_transpose` rotated a PNG with
  `eXIf` orientation 6 from 64×48 to 48×64. **[test]**
- `draft()` returns `None` for PNG, so it is a no-op (`moderation.py:53` and
  `portfolio_og_image.py:251` get a full-resolution decode). **[test]**
- `convert("RGB")` discards alpha without compositing. A transparent pixel
  `(200,10,10,0)` became `(200,10,10)`, and a palette index marked transparent
  became its opaque palette colour. **[test]**

## 3. GIF

### Format facts **[spec]**

From GIF89a (<https://www.w3.org/Graphics/GIF/spec-gif89a.txt>):

- Grammar (Appendix B):
  `<GIF Data Stream> ::= Header <Logical Screen> <Data>* Trailer`,
  `<Logical Screen> ::= Logical Screen Descriptor [Global Color Table]`,
  `<Data> ::= <Graphic Block> | <Special-Purpose Block>`,
  `<Graphic Block> ::= [Graphic Control Extension] <Graphic-Rendering Block>`,
  `<Special-Purpose Block> ::= Application Extension | Comment Extension`.
  A special-purpose block may therefore appear between any two graphic blocks,
  but **not** between a Graphic Control Extension and its image.
- Application Extension (§26): `0x21 0xFF`, block size 11, an 8-byte
  Application Identifier ("eight printable ASCII characters"), a 3-byte
  Authentication Code, then data sub-blocks, then a `0x00` terminator.
  Sub-blocks hold 0-255 bytes each with a size prefix (§15). Special-purpose
  blocks "are transparent to the decoding process" (§12). Identifiers are only
  coordinated by a voluntary, unofficial directory (cover notes).
- Comment Extension (§24): "This block is intended for humans ... should not be
  used to store control information for custom processing". So it is not the
  place for the token.
- §14 discourages Application Extensions in general ("become overhead for all
  other applications"). The concern is decoder overhead, which is negligible
  for one 409-byte block **[inference]**.
- Trailer `0x3B` terminates the stream (§27).
- Animation looping (`NETSCAPE2.0` application extension, sub-block
  `01 <loop count LE16>`) is not part of GIF89a. It is a de facto convention.
  Pillow reads and writes it (`GifImagePlugin.py:263-271`,
  `GifImagePlugin.py:1090-1099`). **[Pillow]** I found no primary Netscape document
  online **[unverified]**.

### Envelope

`0x21 0xFF 0x0B ‖ "PSNFTTOK" ‖ "001" ‖ 0xFF ‖ token[0:255] ‖ 0x89 ‖ token[255:392] ‖ 0x00`:
3 + 11 + 1 + 255 + 1 + 137 + 1 = 409 bytes. The identifier and auth code are
placeholders for the maintainer to choose.

- Emit it right after the Logical Screen Descriptor and Global Color Table,
  before any other block. The offset is `13 + (GCT flag ? 3·2^(N+1) : 0)`, where
  N is the low 3 bits of the LSD packed byte. That is the first legal block
  position in every GIF.
- When stripping, walk the blocks properly: header, LSD, GCT, then each block by
  label (`0x21` extension plus sub-blocks, `0x2C` image descriptor plus optional
  LCT plus LZW minimum code size plus sub-blocks, `0x3B` trailer). Remove only a
  block byte-equal to the canonical envelope. Reject two or more. Require the
  file to end exactly at the trailer.
- Hash the remaining raw bytes, as above.

### Pillow behaviour for GIF **[Pillow] [test]**

- `info["comment"]` is collected (`GifImagePlugin.py:247-260`). An application
  extension is recorded in `info["extension"]` **only on frame 0**, and a later
  one overwrites an earlier one (`GifImagePlugin.py:263-267`). With the token
  inserted before `NETSCAPE2.0`, `info["extension"]` showed `NETSCAPE2.0`. An
  application extension placed between frames decoded fine (4 frames).
- `save()` writes only `NETSCAPE2.0` and comments in the global header
  (`GifImagePlugin.py:1054-1111`). The custom extension was dropped by both a
  plain and a `save_all` re-save. A plain re-save of a 4-frame GIF wrote 1 frame.
- Frames after the first are loaded as RGB/RGBA by default
  (`LoadingStrategy.RGB_AFTER_FIRST`, `GifImagePlugin.py:58-67`). Re-encoding
  must quantize them back to ≤256 colours. **Test:** a hand-built 2-frame GIF
  whose second frame updates half the canvas with its own 256-colour local
  palette (511 colours on the composited canvas) came back from
  `save(save_all=True)` with frame 2 **not** pixel-identical. Pillow re-encoding
  is lossy for valid animated GIFs. (A GIF Pillow had written itself
  round-tripped to identical bytes.)
- `verify()` is the base no-op for GIF (`ImageFile.py:282-289`). Trailing bytes
  after the trailer load without error. **[test]**
- `n_frames` scans the whole file (`GifImagePlugin.py:130-139`). The
  `MAX_IMAGE_PIXELS` check is per canvas, not per animation (`Image.py:3564-3582`).
  **Test:** a 14.5 KB GIF with a 4000×4000 canvas and 60 frames passes the
  25 MP limit. Decoding every frame took 0.5-0.8 s. Holding all 60 frames as RGB
  took ~3.7 GB peak RSS. Sequential decode-and-discard is cheap in memory
  (about two canvases), but CPU is frames × canvas pixels.

## 4. Pipeline implications

### Normalization

- **Static PNG: re-encode with Pillow.** Decode, apply `exif_transpose`, keep
  the mode (`RGBA`, `LA`, `P` with `tRNS`, `RGB`, `L`, 16-bit) so transparency
  survives, keep `icc_profile`, save PNG. It is pixel-lossless and deterministic
  per build. It strips text, `eXIf`, `tIME`, private chunks, APNG chunks
  (unless `save_all`) and trailing data, which removes pre-existing `PSNFT`
  chunks, polyglot payloads and location data, just as `normalize_jpg` does.
  16-bit images round-trip in Pillow as `I;16`/`RGB` with caveats **[unverified]**.
  The simplest policy is to accept them and let Pillow choose, then assert the
  output decodes.
- **GIF: normalize structurally, not by re-encoding.** Keep Header (force
  `GIF89a`), LSD, GCT, a canonical `NETSCAPE2.0` block if present, and every
  Graphic Control Extension, Image Descriptor, Local Color Table and LZW data
  sub-block byte-for-byte. Drop Comment Extensions, Plain Text Extensions
  (rendered by almost no decoder **[unverified]**), all other Application
  Extensions (XMP, and `ICCRGBG1` colour profiles; dropping the profile can
  shift colours in colour-managed viewers **[inference]**) and anything after
  the trailer. This is lossless, cheap and independent of Pillow's encoder.
  Then fully decode the result with Pillow inside the frame budget to prove
  it parses.
- **APNG: reject in v1.** If allowed later, apply the same structural approach
  (keep `acTL/fcTL/fdAT/IDAT` and colour chunks byte-for-byte), or Pillow
  `save_all`. Either way, moderate all frames *and* the default image (Annex A.2).
- Re-encoding does not break determinism of the protocol. Only the server
  normalizes, it returns the exact bytes, and the browser hashes what it
  receives (`portfolio_wallet.py:249-257`, `index.ts:56-58`). Receive never
  normalizes. Cross-version byte drift only affects exact-duplicate detection,
  as with JPG today.

### Validation (`validate_*`)

Shared rules for both formats: magic bytes; Pillow `format` in {`PNG`, `GIF`};
canvas ≤ `MAX_PIXELS`; bomb warning promoted to an error; the file ends exactly
at `IEND`/trailer; PNG `verify()` (CRCs). Then a full decode, because neither
format's `verify()` decodes pixels. Animated GIF also needs `n_frames ≤
MAX_FRAMES` and `n_frames × width × height ≤ MAX_FRAME_PIXELS` (e.g. 300 frames
and 100 MP). The receive path must apply the same checks, since it cannot
normalize.

### Moderation (`moderation.py`)

- `decode()` takes frame 0 only and calls `convert("RGB")`
  (`moderation.py:50-54`). For PNG/GIF it must composite alpha onto the site
  background, as `avatar_jpg` does (`portfolio_jpg.py:55-57`). Otherwise the
  classifier sees colour data that viewers never see. **[test]** for the alpha
  drop; the attack value is **[inference]**.
- For an animated GIF, classify frame 0 plus up to K evenly spaced composited
  frames (e.g. K = 8), and reject if any scores ≥ threshold. Each frame costs up
  to `MAX_CROPS + 1` = 5 ViT inferences at 384² (`moderation.py:26-27`,
  `moderation.py:57-81`). With K = 8 that is ≤ 45 inferences per upload, behind
  the existing classifier lock (`moderation.py:168-169`). Store dhashes for the
  sampled frames so that re-uploads of a rejected animation are recognised.
- Sampling can miss single objectionable frames. Classifying every frame is
  exact but costs frames × 5 inferences. The maintainer has to pick that
  trade-off.

### OG previews and avatars

- `portfolio_og_image._open` (`portfolio_og_image.py:243-262`) works on PNG/GIF
  as is: frame 0, and no `draft()` speedup. It should composite alpha onto the
  card's paper colour instead of `convert("RGB")`. Otherwise a transparent PNG
  previews with its hidden RGB, often black. Preview output stays JPEG.
- Avatars need no change (`portfolio_jpg.py:37-68`).

### Serving and storage

- Detect the format from magic bytes. No schema change is needed:
  `portfolio_images.jpg` is a BLOB. Renaming the column would be a migration
  and needs human review.
- Serve `image/png` / `image/gif` by sniffing the stored bytes. Add a
  format-neutral route (e.g. `/api/images/{h}`) or keep `.jpg` for old links and
  add `.png`/`.gif`. Serving a PNG as `image/jpeg` under `nosniff` is wrong
  regardless of what browsers tolerate. I did not test browser behaviour
  **[unverified]**.

### Browser wallet

- `hashAsset` is unchanged.
- Add `png.ts` (chunk walk, CRC32 via a small table, canonical `tEXt`) and
  `gif.ts` (block walk), plus a `splitImage`/`transferImage` dispatcher by magic
  bytes. Use it in `index.ts:54`, `index.ts:65`, `index.ts:163`, `claim.jsx:15-23`
  and `main.jsx:368`. Only byte parsing is involved, no image decoding. `<img>`
  previews of blob URLs already work for PNG, GIF and APNG (APNG in all major
  browsers per MDN,
  <https://developer.mozilla.org/en-US/docs/Web/Media/Guides/Formats/Image_types>).
- Python/browser parity: share golden vectors (public bytes, token, transfer
  bytes) for each format in `portfolio_web/tests/fixtures`, checked by both
  `tests/` and `portfolio_web/tests/`.

## 5. Platforms

The envelope survives only if the receiver gets the exact bytes. The table
compares JPG with the new formats. P = byte-preserving, RE = re-encoded or
converted, S = metadata partly stripped, ? = not verified. I spot-checked the
Signal source and the Telegram GIF statement myself. Everything else comes from
the cited pages, with secondary sources labelled as such.

| Channel | JPG | PNG | GIF |
| --- | --- | --- | --- |
| Signal Android, media | RE | RE (re-saved as PNG, chunks lost) | P (≤ 25 MB) |
| Signal Desktop | RE | RE (large files become JPEG) | P |
| WhatsApp, photo | RE | RE | RE (MP4; secondary source) |
| WhatsApp, document | P (secondary) | P? | P? |
| Telegram, photo / animation | RE | RE | RE (MP4) |
| Telegram, "send as file" | P | P | P on Android/Web?; RE on Desktop (bug reports) |
| Slack | S | ? | ? |
| Discord | S (location EXIF) | ? | ? |
| iMessage | ? | ? | ? |
| X, Facebook, Instagram | RE (secondary) | RE (secondary) | RE (X serves GIFs as MP4) |
| Email attachment | P (MIME transport; Outlook's optional resize is the exception) | P | P |
| Google Drive link | P | P | P |

Sources:

- **Signal Android:** `MediaConstraints.canResize()` is
  `isImageType(...) && !isGif(...)` (`MediaConstraints.java:94-95`,
  <https://github.com/signalapp/Signal-Android/blob/main/feature/media-send/src/main/java/org/signal/mediasend/MediaConstraints.java>).
  Every resizable image goes through `compressImage`, which is documented as
  "stripping all EXIF data" (`AttachmentCompressionJob.java:204-206`,
  `AttachmentCompressionJob.java:396-399`,
  <https://github.com/signalapp/Signal-Android/blob/main/app/src/main/java/org/thoughtcrime/securesms/jobs/AttachmentCompressionJob.java>).
  `ImageCompressionUtil` re-encodes a Bitmap as JPEG, WebP or PNG
  (`ImageCompressionUtil.java:146-176`,
  <https://github.com/signalapp/Signal-Android/blob/main/app/src/main/java/org/thoughtcrime/securesms/util/ImageCompressionUtil.java>).
  **[code, third-party]** The subagent read Android "File" attachments as also
  being compressed, which is not tested and is contradicted by a user report in
  Signal-Desktop issue #6341 **[unverified]**. Signal iOS **[unverified]**.
- **Signal Desktop:** `canBeTranscoded` excludes GIF, and `scaleImageToLevel`
  "Always encode[s] through canvas" (`ts/util/Attachment.std.ts`,
  `ts/util/scaleImageToLevel.preload.ts`, <https://github.com/signalapp/Signal-Desktop>).
  Reported by the subagent, not re-checked by me.
- **Telegram:** "if the user tries to upload an actual GIF file, it will be
  automatically converted to an MPEG4 file by the server"
  (<https://core.telegram.org/api/gifs>, checked). Documents are stored as
  uploaded, and photos are server-generated sizes
  (<https://core.telegram.org/api/files>,
  <https://core.telegram.org/constructor/inputMediaUploadedDocument> `force_file`).
  Desktop compresses GIFs even "as file":
  <https://github.com/telegramdesktop/tdesktop/issues/28353>,
  <https://github.com/telegramdesktop/tdesktop/issues/30186>.
- **WhatsApp:** official FAQ pages on HD media and documents
  (<https://faq.whatsapp.com/759301289012856>, <https://faq.whatsapp.com/641217966682199>)
  did not render fully, so their wording is **[unverified]**. Amnesty Citizen
  Evidence Lab tested gallery sends (metadata stripped) against document sends
  (metadata kept)
  (<https://citizenevidence.org/2020/04/20/sending-encrypted-photos-while-preserving-metadata/>,
  secondary). GIF → MP4 rests only on Android Police, 2016 (secondary).
- **Slack:** began "stripping EXIF ... metadata from images", per TechCrunch,
  2020-05-11 (<https://techcrunch.com/2020/05/11/slack-strips-location-data/>,
  secondary report of a Slack statement).
- **Discord:** only a second-hand quote of a 2018 Discord tweet about stripping
  "EXIF geocoding data" **[unverified]**.
- **X:** animated GIFs are served as `video/mp4` variants
  (<https://devcommunity.x.com/t/retiring-mp4-video-output/66093>). The EXIF
  stripping tests for Meta and X are secondary
  (<https://exifdata.org/blog/do-social-media-sites-strip-exif-data-2025-test>).
- **Outlook:** "Resize large images when I send this message"
  (<https://support.microsoft.com/en-us/office/reduce-attachment-size-to-send-large-files-with-outlook-8c698842-b462-4a4c-8d53-5c5dd04f77ef>).
  Google Drive's conversion setting covers office and text formats only
  (<https://support.google.com/drive/answer/2424368>). Google Photos "Storage
  saver" recompresses (<https://support.google.com/photos/answer/6220791>).
  Dropbox, iCloud Drive and iMessage: no first-party statement found
  **[unverified]**.

What follows:

- Neither PNG nor GIF is more fragile than JPG on the channels that already
  break JPG transfers. Most messengers' photo paths re-encode all three.
  Signal is an exception in GIF's favour: it passes GIFs through and
  re-encodes JPG and PNG.
- GIF has a failure mode of its own: conversion to MP4 (Telegram, X, probably
  WhatsApp). The recipient gets a video with no token.
- The reliable channels are the same for every format: email attachments,
  cloud-drive links, WhatsApp "Document", Telegram "send as file" (not Desktop
  for GIF). The existing transfer links (`portfolio_links.py`) avoid the
  problem entirely, so the UI should keep steering users to them. The current
  copy already warns that "chat apps strip out the token" (`HowBasics.jsx:151`).
- **[inference]** A transfer file with the right extension and MIME type
  matters: a PNG named `.jpg` may be routed into a photo pipeline and
  transcoded, or rejected.

## 6. Recommendation

### Design

| | PNG | GIF |
| --- | --- | --- |
| Accepted | Static PNG. APNG (`acTL` present) rejected in v1 | GIF87a/89a, static or animated within budget |
| Envelope | One `tEXt` chunk, keyword `PSNFT`, text = `psnft1…` (392 B); 410 B total | One Application Extension, id `PSNFTTOK` + auth `001` (placeholder), token in 255 + 137-byte sub-blocks; 409 B total |
| Emit at | Offset 33, right after `IHDR` | Right after LSD + GCT |
| Strip | Any chunk boundary; exact canonical bytes only; reject duplicates; must end at `IEND` | Any block boundary; exact canonical bytes only; reject duplicates; must end at trailer |
| `h` | `hash_asset(public bytes)`, unchanged | same |
| Normalize | Pillow re-encode (mode + ICC kept, orientation applied) | Structural block filter, image data untouched |
| Limits | 10 MiB normalized, 25 MP | 10 MiB, 25 MP canvas, ≤ 300 frames, ≤ 100 MP total frame pixels (placeholders) |
| Moderation | Composite alpha; one image | Composite alpha; frame 0 + K sampled frames |

### Protocol and crypto impact

- No change to `hash_asset`, the PS credential, committed issuance, transfer
  proofs, nullifiers, the duplicate registry, the token encoding or the NUT
  wire format. The mint never handles image bytes.
- Changes: the transfer-file envelope for two new formats, and the set of
  public-file formats the portfolio accepts. Both affect interoperability
  between independent wallets. Per `AGENTS.md`, "Cashu protocol ... changes"
  and "API breaking changes" need human review. Choosing the keyword and
  identifier, and promising to support them forever, is a maintainer decision.
- Same picture, different format → different `h` → separate NFTs. That is
  already true for two JPG encodings of one picture.

### Code changes and rough effort

| Part | Files | Effort |
| --- | --- | --- |
| PNG/GIF envelope, split, validate, normalize; format dispatch | `imgmeta.py` (or new `portfolio_png.py`, `portfolio_gif.py`), `portfolio_jpg.py` → generalize to `portfolio_image.py`, `portfolio.py:286-349`, `portfolio_wallet.py:120-131` | 1.5-2 days |
| Frame/pixel budget, alpha compositing, multi-frame moderation | `moderation.py`, `portfolio_og_image.py:243-262` | 1 day |
| Serving and MIME, routes, config naming (`max_image_bytes`, keeping `max_jpg_bytes` as an alias) | `portfolio.py:635`, `portfolio.py:1059-1068`, `portfolio_og.py` if image URLs are emitted | 0.5 day |
| Browser envelope code and dispatcher | `wallet/png.ts`, `wallet/gif.ts`, `wallet/index.ts`, `claim.jsx` | 1 day |
| UI: accept list, file names and extensions, MIME of downloads, copy ("The NFT is the file"?), README | `main.jsx:264`, `main.jsx:335`, `main.jsx:359-410`, `main.jsx:666`, `HowItWorks.jsx`, `HowBasics.jsx`, `README.md` | 0.5-1 day |
| Tests | see below | 1-1.5 days |

### Tests to add

- Round-trip per format: `strip(embed(x)) == x`, Python and TS; golden vectors
  shared by both.
- Strip rejects: two envelopes; non-canonical `PSNFT` text (kept, so the hash
  check fails); a token with a bad CRC; trailing bytes; an envelope inside an
  image's sub-blocks (GIF) or between GCE and image.
- Normalize: PNG keeps alpha, palette transparency and ICC; drops text, `eXIf`,
  `tIME`, private chunks and trailing bytes; applies orientation; is
  deterministic. GIF normalize is pixel-identical on all frames, including the
  >256-colour composited case above, and drops comment, XMP and foreign
  application blocks.
- Validation: APNG rejected; GIF frame budget enforced (the 14.5 KB / 60 × 16 MP
  case); a decompression bomb is refused at open.
- Moderation: frame sampling picks up a later NSFW frame (fake classifier);
  transparent pixels with hidden RGB are composited away.
- Update `tests/test_nft_portfolio.py:538` (`test_validate_jpg_rejects_png_directly`).
- End to end: mint PNG and GIF, export, receive in another profile, via file
  and via link.

### Risks

1. **Delivery channels** (section 5): GIF is the format most likely to be
   transcoded. The bearer file only works over byte-preserving channels, so the
   transfer link is the reliable path.
2. **Moderation bypass** through later frames, the APNG default image or alpha.
3. **Resource exhaustion** from animations: frames × pixels, not file size.
4. **Lossy normalization** if GIFs are re-encoded with Pillow (verified).
5. **Spread-out JPG assumptions**: a missed `.jpg`/`image/jpeg` gives wrong
   extensions or MIME types, and transfer files that chat apps then treat as
   JPEG and re-encode **[inference]**.
6. Encoder drift (zlib-ng) weakens exact-duplicate detection across upgrades.
   Pre-existing behaviour.

### Open questions for the maintainer

1. Ship PNG only first, then GIF? GIF carries most of the risk.
2. Allow animated GIF at all? If yes: frame and pixel budgets, and sampled
   versus exhaustive moderation.
3. APNG: reject, flatten to the default image, or support?
4. Keyword `PSNFT` and GIF identifier/auth code: final names, to be frozen once
   files exist in the wild.
5. Structural GIF normalization keeps the uploader's LZW data, while re-encoding
   would launder it. Is keeping the original encoding (and anything hidden in
   unused LZW codes) acceptable? Same question for PNG if structural filtering
   is preferred over re-encoding.
6. Branding and copy: "The NFT is the JPG" is the tagline.
7. Route naming: keep `/api/images/{h}.jpg` for old links and add a neutral route?
