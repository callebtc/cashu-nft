"""Public image files: which formats an NFT can be, and the rules that keep a
transfer envelope out of an asset's byte identity.

An NFT commits to the hash of its public file's exact bytes. A transfer file
is that public file plus one envelope carrying the token; removing the
envelope must give back the same bytes. Each format below knows how to check a
file's structure, find and strip our envelope, and re-encode an upload.
Adding a format means adding an ``ImageFormat`` to ``FORMATS`` and its browser
counterpart in ``portfolio_web/src/wallet/image.ts``.
"""

import io
import re
import warnings
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

from .imgmeta import embed_token, extract_token, png_text_chunk

MAX_PIXELS = 25_000_000
# What viewers see behind transparent areas: the site's light paper, and its
# dark theme's background.
PAPER = (255, 253, 247)
DARK = (18, 18, 18)

Split = Tuple[bytes, Optional[str]]
# What Pillow raises for a damaged or oversized file. A bad PNG checksum is a
# SyntaxError.
DECODE_ERRORS = (
    UnidentifiedImageError,
    OSError,
    SyntaxError,
    Image.DecompressionBombError,
    Image.DecompressionBombWarning,
)


@dataclass(frozen=True)
class ImageFormat:
    name: str  # also the file extension
    label: str  # how the UI names it
    mime: str
    pillow: str  # Pillow's format name
    magic: bytes
    # Structural checks Pillow doesn't make, such as nothing after the end.
    check: Callable[[bytes], None]
    split: Callable[[bytes], Split]
    # Re-encode an upright decoded image, keeping its ICC profile.
    encode: Callable[[Image.Image, Optional[bytes]], bytes]


# --- JPG ---------------------------------------------------------------------

JPG_MAGIC = b"\xff\xd8"


def _check_jpg(data: bytes) -> None:
    if not data.endswith(b"\xff\xd9"):
        raise ValueError("This JPG is truncated or has bytes after its end marker.")


def split_transfer_jpg(data: bytes) -> Split:
    """Remove only our exact EXIF envelope; preserve every other byte.

    Duplicate envelopes are ambiguous and rejected, including identical copies.
    Matching by the complete segment prevents deleting arbitrary user metadata.
    """
    if not data.startswith(JPG_MAGIC):
        raise ValueError("Only JPG files are supported.")
    pos = 2
    chunks = [data[:2]]
    token: Optional[str] = None
    while pos < len(data):
        start = pos
        if data[pos] != 0xFF:
            raise ValueError("Invalid JPG marker.")
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            raise ValueError("Truncated JPG marker.")
        marker = data[pos]
        pos += 1
        if marker in (0xDA, 0xD9):
            chunks.append(data[start:])
            return b"".join(chunks), token
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            chunks.append(data[start:pos])
            continue
        if pos + 2 > len(data):
            raise ValueError("Truncated JPG segment.")
        length = int.from_bytes(data[pos : pos + 2], "big")
        end = pos + length
        if length < 2 or end > len(data):
            raise ValueError("Truncated JPG segment.")
        segment = data[start:end]
        embedded = extract_token(b"\xff\xd8" + segment + b"\xff\xd9")
        dedicated = (
            embedded is not None
            and embedded.startswith("psnft1")
            and segment == embed_token(b"\xff\xd8\xff\xd9", embedded)[2:-2]
        )
        if dedicated:
            if token is not None:
                raise ValueError("This JPG contains multiple transfer tokens.")
            token = embedded
        else:
            chunks.append(segment)
        pos = end
    raise ValueError("Truncated JPG.")


def _encode_jpg(image: Image.Image, icc: Optional[bytes]) -> bytes:
    output = io.BytesIO()
    image.convert("RGB").save(
        output, format="JPEG", quality=95, subsampling=0, optimize=True, icc_profile=icc
    )
    return output.getvalue()


# --- PNG ---------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# The tEXt keyword and NUL separator in front of a token, and the token itself
# (a 193-byte credential in hex), exactly as the browser parser requires.
_PNG_TOKEN_TEXT = b"PSNFT\x00"
_PNG_TOKEN = re.compile(rb"psnft1[0-9a-f]{386}")
# Modes Pillow writes to PNG as they are; anything else becomes RGBA.
_PNG_MODES = {"1", "L", "LA", "I", "I;16", "P", "RGB", "RGBA"}


def _png_chunks(data: bytes) -> List[Tuple[int, int, bytes]]:
    """(start, end, type) of every chunk from IHDR to IEND. The file must end
    exactly at IEND, and animated PNGs (acTL) are refused for now."""
    if not data.startswith(PNG_MAGIC):
        raise ValueError("Only PNG files are supported.")
    chunks: List[Tuple[int, int, bytes]] = []
    pos = len(PNG_MAGIC)
    while True:
        if pos + 12 > len(data):
            raise ValueError("This PNG is truncated.")
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise ValueError("This PNG is truncated.")
        if not chunks and ctype != b"IHDR":
            raise ValueError("This PNG is damaged.")
        if ctype == b"acTL":
            raise ValueError("Animated PNGs aren't supported yet.")
        chunks.append((pos, end, ctype))
        pos = end
        if ctype == b"IEND":
            if pos != len(data):
                raise ValueError("This PNG has bytes after its end chunk.")
            return chunks


def _check_png(data: bytes) -> None:
    _png_chunks(data)


def split_transfer_png(data: bytes) -> Split:
    """Remove only our exact tEXt envelope; preserve every other byte.

    The envelope is emitted right before IEND but recognised at any chunk
    boundary. Only a chunk byte-equal to the canonical envelope of the token it
    carries is removed; other text chunks stay part of the picture. Duplicate
    envelopes are ambiguous and rejected.
    """
    kept = [data[: len(PNG_MAGIC)]]
    token: Optional[str] = None
    for start, end, ctype in _png_chunks(data):
        payload = data[start + 8 : end - 4]
        candidate = (
            payload[len(_PNG_TOKEN_TEXT) :]
            if ctype == b"tEXt" and payload.startswith(_PNG_TOKEN_TEXT)
            else b""
        )
        if _PNG_TOKEN.fullmatch(candidate) and data[start:end] == png_text_chunk(
            candidate
        ):
            if token is not None:
                raise ValueError("This PNG contains multiple transfer tokens.")
            token = candidate.decode("ascii")
        else:
            kept.append(data[start:end])
    return b"".join(kept), token


def _encode_png(image: Image.Image, icc: Optional[bytes]) -> bytes:
    # Pillow carries transparency (tRNS) and the palette over by itself, and
    # writes no text, EXIF, time or private chunks unless asked to.
    pixels = image if image.mode in _PNG_MODES else image.convert("RGBA")
    output = io.BytesIO()
    pixels.save(output, format="PNG", icc_profile=icc)
    return output.getvalue()


# --- Registry ----------------------------------------------------------------

FORMATS: Tuple[ImageFormat, ...] = (
    ImageFormat(
        "jpg",
        "JPG",
        "image/jpeg",
        "JPEG",
        JPG_MAGIC,
        _check_jpg,
        split_transfer_jpg,
        _encode_jpg,
    ),
    ImageFormat(
        "png",
        "PNG",
        "image/png",
        "PNG",
        PNG_MAGIC,
        _check_png,
        split_transfer_png,
        _encode_png,
    ),
)
# "JPG or PNG" (or "JPG, PNG or WebP"), for messages.
LABELS = " or ".join(
    filter(None, [", ".join(f.label for f in FORMATS[:-1]), FORMATS[-1].label])
)


def image_format(data: bytes) -> ImageFormat:
    """The format of a file, from its first bytes."""
    for fmt in FORMATS:
        if data.startswith(fmt.magic):
            return fmt
    raise ValueError(f"Use a {LABELS} file.")


def validate_image(data: bytes) -> ImageFormat:
    """Check that a public file is a well-formed, safely decodable image."""
    fmt = image_format(data)
    fmt.check(data)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != fmt.pillow:
                    raise ValueError(f"Use a {LABELS} file.")
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError(
                        f"Use a {fmt.label} with at most 25 million pixels."
                    )
                image.verify()
    except DECODE_ERRORS:
        raise ValueError(f"This {fmt.label} is damaged or too large to decode safely.")
    return fmt


def split_transfer(data: bytes) -> Split:
    """The public file and the token of a transfer file (None if it has none)."""
    return image_format(data).split(data)


def normalize_image(data: bytes) -> bytes:
    """Re-encode an upload in its own format: upright, with only its pixels,
    transparency and colour profile. Nothing else from the file survives."""
    fmt = validate_image(data)
    if fmt.split(data)[1] is not None:
        raise ValueError(
            "This is a transfer file. Add it to your collection to receive the NFT."
        )
    with Image.open(io.BytesIO(data)) as source:
        icc = source.info.get("icc_profile")
        try:
            # verify() does not decode pixel data; truncation surfaces here.
            return fmt.encode(ImageOps.exif_transpose(source), icc)
        except DECODE_ERRORS:
            raise ValueError(
                f"This {fmt.label} is damaged or too large to decode safely."
            )


def flatten(
    image: Image.Image, background: Tuple[int, int, int] = PAPER
) -> Image.Image:
    """An opaque RGB copy, with transparent areas shown on ``background``."""
    if not image.has_transparency_data:
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    flat = Image.new("RGB", rgba.size, background)
    flat.paste(rgba, mask=rgba.getchannel("A"))
    return flat


# --- Profile pictures ----------------------------------------------------------

AVATAR_SIZE = 256
AVATAR_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


def avatar_jpg(data: bytes, size: int = AVATAR_SIZE) -> bytes:
    """Re-encode an uploaded profile picture: decode it safely, apply the
    orientation flag, crop the centre square, scale it to ``size`` and save a
    fresh JPG. Nothing from the original file (metadata included) survives."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in AVATAR_FORMATS:
                    raise ValueError("Use a JPG, PNG, WebP or GIF picture.")
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError("Use a picture with at most 25 million pixels.")
                image.seek(0)
                flat = flatten(ImageOps.exif_transpose(image))
                square = ImageOps.fit(flat, (size, size), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                square.save(out, "JPEG", quality=86, optimize=True, progressive=True)
                return out.getvalue()
    except DECODE_ERRORS:
        raise ValueError("This picture is damaged or too large to decode safely.")
