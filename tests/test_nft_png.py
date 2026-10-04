"""PNG NFTs: the transfer envelope, normalization, validation, moderation and the
full mint, export and receive flow (cashu/nft/portfolio_image.py)."""

import base64
import io
import json
import zlib
from pathlib import Path
from typing import Iterator, List

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageCms, PngImagePlugin

from cashu.core.crypto.ps import hash_asset
from cashu.nft.imgmeta import embed_token
from cashu.nft.moderation import appearances
from cashu.nft.portfolio import create_portfolio_app
from cashu.nft.portfolio_image import (
    DARK,
    PAPER,
    image_format,
    normalize_image,
    split_transfer,
    split_transfer_jpg,
    split_transfer_png,
    validate_image,
)
from cashu.nft.wallet import TOKEN_PREFIX
from tests.test_nft_portfolio import Profile, make_jpg

FIXTURE = json.loads(
    (
        Path(__file__).parent.parent
        / "cashu/nft/portfolio_web/tests/fixtures/wallet.json"
    ).read_text()
)
TOKEN = TOKEN_PREFIX + "ab" * 193


def png(image: Image.Image, **options) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG", **options)
    return out.getvalue()


def chunk(ctype: bytes, data: bytes = b"") -> bytes:
    return (
        len(data).to_bytes(4, "big")
        + ctype
        + data
        + zlib.crc32(ctype + data).to_bytes(4, "big")
    )


def chunk_types(data: bytes) -> List[bytes]:
    pos, types = 8, []
    while pos < len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        types.append(data[pos + 4 : pos + 8])
        pos += 12 + length
    return types


def transparent() -> Image.Image:
    """Half transparent: the hidden half has colour nobody should see."""
    image = Image.new("RGBA", (40, 30), (200, 10, 10, 0))
    image.paste((20, 120, 220, 255), (0, 0, 20, 30))
    return image


# --- envelope ------------------------------------------------------------------


def test_python_and_browser_produce_the_same_transfer_png():
    public = base64.b64decode(FIXTURE["png"])
    transfer = base64.b64decode(FIXTURE["png_transfer"])
    _, token = split_transfer_jpg(base64.b64decode(FIXTURE["transfer"]))
    assert token is not None
    assert embed_token(public, token) == transfer
    assert split_transfer_png(transfer) == (public, token)
    assert split_transfer(transfer) == (public, token)
    assert split_transfer(public) == (public, None)


def test_envelope_round_trip_keeps_every_other_byte():
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "Ana")
    public = png(transparent(), pnginfo=info)
    transfer = embed_token(public, TOKEN)
    # Written right before IEND; the owner's own text chunk survives.
    assert transfer[-12:] == public[-12:] and transfer[:-12].startswith(public[:-12])
    assert split_transfer(transfer) == (public, TOKEN)
    assert b"Author" in split_transfer(transfer)[0]


def test_envelope_found_anywhere_but_only_once_and_only_exactly():
    public = png(transparent())
    envelope = embed_token(public, TOKEN)[len(public) - 12 : -12]
    early = public[:33] + envelope + public[33:]  # right after IHDR
    assert split_transfer_png(early) == (public, TOKEN)
    with pytest.raises(ValueError, match="multiple"):
        split_transfer_png(early[:-12] + envelope + early[-12:])
    # A lookalike tEXt chunk stays in the picture, so its hash no longer matches.
    lookalike = chunk(b"tEXt", b"PSNFT\x00" + TOKEN.encode() + b" ")
    tampered = public[:-12] + lookalike + public[-12:]
    assert split_transfer_png(tampered) == (tampered, None)


# --- validation ------------------------------------------------------------------


def test_validation_accepts_png_and_rejects_broken_or_animated_files():
    public = png(transparent())
    assert validate_image(public).name == "png"
    assert image_format(public).mime == "image/png"
    with pytest.raises(ValueError, match="after its end"):
        validate_image(public + b"\x00")
    with pytest.raises(ValueError, match="truncated"):
        validate_image(public[:-6])
    bad_crc = bytearray(public)
    bad_crc[-20] ^= 0xFF  # inside IDAT
    with pytest.raises(ValueError, match="damaged"):
        validate_image(bytes(bad_crc))
    frames = [transparent(), Image.new("RGBA", (40, 30), (0, 255, 0, 255))]
    animated = png(frames[0], save_all=True, append_images=frames[1:])
    assert b"acTL" in animated
    with pytest.raises(ValueError, match="Animated PNGs"):
        validate_image(animated)


# --- normalization ---------------------------------------------------------------


def test_normalize_keeps_pixels_transparency_and_profile_and_drops_the_rest():
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    info = PngImagePlugin.PngInfo()
    info.add_text("Location", "52.52N 13.40E")
    info.add(b"prVt", b"private data")  # a private ancillary chunk
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise
    source = transparent()
    upload = png(source, pnginfo=info, exif=exif, icc_profile=srgb)
    out = normalize_image(upload)
    assert out == normalize_image(upload)  # deterministic
    assert chunk_types(out) == [b"IHDR", b"iCCP", b"IDAT", b"IEND"]
    assert b"52.52N" not in out and b"private data" not in out
    with Image.open(io.BytesIO(out)) as image:
        assert image.format == "PNG" and image.mode == "RGBA"
        assert image.size == (30, 40)
        expected = source.transpose(Image.Transpose.ROTATE_270)
        assert image.tobytes() == expected.tobytes()  # lossless


def test_normalize_keeps_palette_transparency():
    palette = Image.new("P", (16, 16), 0)
    palette.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    palette.paste(1, (0, 0, 8, 16))
    palette.info["transparency"] = 0
    out = normalize_image(png(palette))
    with Image.open(io.BytesIO(out)) as image:
        assert image.mode == "P" and image.info["transparency"] == 0
        assert image.tobytes() == palette.tobytes()


def test_normalize_refuses_transfer_files():
    with pytest.raises(ValueError, match="transfer file"):
        normalize_image(embed_token(png(transparent()), TOKEN))


# --- moderation ------------------------------------------------------------------


def test_moderation_sees_transparency_on_both_backgrounds():
    light, dark = appearances(png(transparent()))
    assert light.getpixel((0, 0)) == dark.getpixel((0, 0)) == (20, 120, 220)
    # The hidden red never reaches the classifier.
    assert light.getpixel((39, 0)) == PAPER and dark.getpixel((39, 0)) == DARK
    assert len(appearances(make_jpg())) == 1


class FlagsDarkBackground:
    """Stands in for the model: flags only what shows on the dark theme."""

    def __init__(self):
        self.seen: List[tuple] = []

    def nsfw_probability(self, image: Image.Image) -> float:
        self.seen.append(image.getpixel((image.width - 1, 0)))
        return 0.99 if image.getpixel((image.width - 1, 0)) == DARK else 0.01


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with TestClient(create_portfolio_app(str(tmp_path / "portfolio"))) as c:
        yield c


def test_upload_rejected_when_any_background_shows_it(client):
    alice = Profile(client)
    alice.create()
    classifier = FlagsDarkBackground()
    client.app.state.portfolio.moderation.classifier = classifier  # type: ignore[attr-defined]
    assert alice.prepare("mint", png(transparent())).status_code == 422
    assert PAPER in classifier.seen and DARK in classifier.seen
    # An opaque PNG has one appearance and passes.
    opaque = png(Image.new("RGB", (40, 30), (10, 200, 10)))
    assert alice.prepare("mint", opaque).status_code == 200


# --- end to end ------------------------------------------------------------------


def test_mint_export_and_receive_a_png(client):
    alice, bob = Profile(client), Profile(client)
    alice.create("Alice")
    bob.create("Bob")
    card = alice.mint(png(transparent()), "Glass").json()
    public = client.get(f"/api/images/{card['h']}")
    assert public.status_code == 200
    assert public.headers["content-type"] == "image/png"
    assert hash_asset(public.content).to_bytes(32, "big").hex() == card["h"]
    # Old links end in .jpg; the stored bytes still decide the type.
    for suffix in (".jpg", ".png"):
        old = client.get(f"/api/images/{card['h']}{suffix}")
        assert old.content == public.content
        assert old.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(public.content)) as image:
        assert image.mode == "RGBA"

    assert alice.claim(card).status_code == 200
    transfer = alice.export(card["id"])
    assert transfer.headers["content-type"] == "image/png"
    assert split_transfer(transfer.content)[0] == public.content

    received = bob.receive(transfer.content, "Glass")
    assert received.status_code == 200, received.text
    assert received.json()["h"] == card["h"]
    assert [c["status"] for c in alice.get()["cards"]] == ["sent"]
    # The same transfer file can't be redeemed twice.
    assert Profile(client).receive(transfer.content).status_code in (400, 409)


def test_jpg_and_png_of_the_same_picture_are_separate_nfts(client):
    alice = Profile(client)
    alice.create()
    picture = Image.new("RGB", (32, 32), (90, 40, 160))
    jpg_card = alice.mint(make_jpg(32, 32, (90, 40, 160)), "JPG").json()
    png_card = alice.mint(png(picture), "PNG").json()
    assert jpg_card["h"] != png_card["h"]
    types = {
        client.get(f"/api/images/{c['h']}").headers["content-type"]
        for c in (jpg_card, png_card)
    }
    assert types == {"image/jpeg", "image/png"}


def test_damaged_png_is_a_clean_error_not_a_crash(client):
    alice = Profile(client)
    alice.create()
    broken = bytearray(png(transparent()))
    broken[-20] ^= 0xFF  # breaks the IDAT checksum: Pillow raises SyntaxError
    assert alice.prepare("mint", bytes(broken)).status_code == 400
    assert alice.prepare("receive", bytes(broken)).status_code == 400
    assert alice.post(f"{alice.base}/avatar", bytes(broken)).status_code == 400
