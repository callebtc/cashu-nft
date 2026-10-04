"""Embedding PS-NFT tokens into image metadata, dependency-free.

JPEG: a minimal EXIF APP1 segment carrying a UserComment (tag 0x9286) is
inserted right after the SOI marker. If the file already carries an EXIF
APP1 without a UserComment, a SECOND APP1 with our own minimal EXIF is
added -- legal, and decoders tolerate it. The newest segment sits first,
so extraction returns the latest embedded token.

PNG: a tEXt chunk (keyword "PSNFT") is inserted before IEND. Extraction
returns the last matching chunk, so the latest embedded token wins.

Everything else in the file is left byte-identical.
"""

import zlib
from typing import Iterator, Literal, Optional, Tuple

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_JPEG_SOI = b"\xff\xd8"
_USERCOMMENT_CHARSET = b"ASCII\x00\x00\x00"
_PNG_KEYWORD = b"PSNFT"


def embed_token(data: bytes, token: str) -> bytes:
    """Return the image with the token embedded in its metadata."""
    try:
        raw_token = token.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("token must be ASCII")
    if data.startswith(_JPEG_SOI):
        return data[:2] + _jpeg_app1(raw_token) + data[2:]
    if data.startswith(_PNG_SIG):
        return _png_embed(data, raw_token)
    raise ValueError("token embedding is supported for JPEG and PNG only")


def extract_token(data: bytes) -> Optional[str]:
    """Return the embedded token, or None if the image carries none."""
    if data.startswith(_JPEG_SOI):
        return _jpeg_extract(data)
    if data.startswith(_PNG_SIG):
        return _png_extract(data)
    raise ValueError("token embedding is supported for JPEG and PNG only")


# --- JPEG: EXIF APP1 with a UserComment ----------------------------------


def _jpeg_app1(token: bytes) -> bytes:
    """Minimal EXIF APP1 segment. Segment header fields are big-endian;
    the TIFF part is little-endian ("II"). Layout inside the TIFF block:

        0   "II" + uint16 42 + uint32 8        (header, IFD0 at offset 8)
        8   IFD0: 1 entry -> ExifIFDPointer (0x8769, LONG) = 26; next = 0
        26  EXIF IFD: 1 entry -> UserComment (0x9286, UNDEFINED, count > 4
            so the value field is the uint32 offset of the data); next = 0
        44  b"ASCII\\x00\\x00\\x00" + token
    """
    exif_ifd_offset = 8 + 2 + 12 + 4
    data_offset = exif_ifd_offset + 2 + 12 + 4
    comment = _USERCOMMENT_CHARSET + token
    tiff = (
        b"II"
        + (42).to_bytes(2, "little")
        + (8).to_bytes(4, "little")
        # IFD0
        + (1).to_bytes(2, "little")
        + (0x8769).to_bytes(2, "little")
        + (4).to_bytes(2, "little")
        + (1).to_bytes(4, "little")
        + exif_ifd_offset.to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        # EXIF IFD
        + (1).to_bytes(2, "little")
        + (0x9286).to_bytes(2, "little")
        + (7).to_bytes(2, "little")
        + len(comment).to_bytes(4, "little")
        + data_offset.to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + comment
    )
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def _jpeg_extract(data: bytes) -> Optional[str]:
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker in (0xD9, 0xDA):  # EOI / SOS: the metadata region ends here
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone markers
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2 : pos + 4], "big")
        if length < 2 or pos + 2 + length > len(data):
            return None
        payload = data[pos + 4 : pos + 2 + length]
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            comment = _exif_user_comment(payload[6:])
            if comment is not None:
                return comment
        pos += 2 + length
    return None


def _exif_user_comment(tiff: bytes) -> Optional[str]:
    if len(tiff) < 8:
        return None
    endian: Literal["little", "big"]
    if tiff[:2] == b"II":
        endian = "little"
    elif tiff[:2] == b"MM":
        endian = "big"
    else:
        return None
    if int.from_bytes(tiff[2:4], endian) != 42:
        return None

    def find_entry(offset: int, want_tag: int) -> Optional[Tuple[int, bytes]]:
        """Return (count, value field) for the tag, or None."""
        if offset + 2 > len(tiff):
            return None
        count = int.from_bytes(tiff[offset : offset + 2], endian)
        for i in range(count):
            entry = tiff[offset + 2 + 12 * i : offset + 14 + 12 * i]
            if len(entry) != 12:
                return None
            if int.from_bytes(entry[0:2], endian) == want_tag:
                return int.from_bytes(entry[4:8], endian), entry[8:12]
        return None

    ifd0_offset = int.from_bytes(tiff[4:8], endian)
    exif_ptr = find_entry(ifd0_offset, 0x8769)
    if exif_ptr is None:
        return None
    user_comment = find_entry(int.from_bytes(exif_ptr[1], endian), 0x9286)
    if user_comment is None:
        return None
    count, value = user_comment
    if count <= 4:
        raw = value[:count]
    else:
        data_offset = int.from_bytes(value, endian)
        if data_offset + count > len(tiff):
            return None
        raw = tiff[data_offset : data_offset + count]
    if raw.startswith(_USERCOMMENT_CHARSET):
        raw = raw[len(_USERCOMMENT_CHARSET) :]
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return None


# --- PNG: tEXt chunk -------------------------------------------------------


def _png_chunks(data: bytes) -> Iterator[Tuple[int, bytes, bytes]]:
    """Yield (offset, type, payload) for each chunk up to IEND."""
    pos = len(_PNG_SIG)
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        ctype = data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG chunk")
        yield pos, ctype, data[pos + 8 : pos + 8 + length]
        pos = end
        if ctype == b"IEND":
            return
    raise ValueError("PNG has no IEND chunk")


def png_text_chunk(token: bytes) -> bytes:
    """The tEXt chunk (keyword "PSNFT") that carries a token in a PNG."""
    payload = _PNG_KEYWORD + b"\x00" + token
    return (
        len(payload).to_bytes(4, "big")
        + b"tEXt"
        + payload
        + zlib.crc32(b"tEXt" + payload).to_bytes(4, "big")
    )


def _png_embed(data: bytes, token: bytes) -> bytes:
    chunk = png_text_chunk(token)
    for pos, ctype, _ in _png_chunks(data):
        if ctype == b"IEND":
            return data[:pos] + chunk + data[pos:]
    raise ValueError("PNG has no IEND chunk")


def _png_extract(data: bytes) -> Optional[str]:
    token = None
    for _, ctype, payload in _png_chunks(data):
        if ctype == b"tEXt" and payload.startswith(_PNG_KEYWORD + b"\x00"):
            try:
                token = payload[len(_PNG_KEYWORD) + 1 :].decode("ascii")
            except UnicodeDecodeError:
                continue
    return token
