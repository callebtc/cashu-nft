"""Upload screening for the NFT portfolio (cashu/nft/moderation.py)."""

import io
import os
import random
from typing import Iterator, List

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from cashu.nft.moderation import (
    MAX_HASH_DISTANCE,
    REJECTED_MESSAGE,
    NSFWClassifier,
    decode,
    difference_hash,
)
from cashu.nft.portfolio import create_portfolio_app
from tests.test_nft_portfolio import Profile


def art(seed: int, size=(640, 480), quality: int = 92) -> bytes:
    """A deterministic picture with enough structure for perceptual hashing."""
    rng = random.Random(seed)
    image = Image.new("RGB", size, tuple(rng.randrange(256) for _ in range(3)))
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        w, h = rng.randrange(40, 240), rng.randrange(40, 240)
        fill = tuple(rng.randrange(256) for _ in range(3))
        if rng.random() < 0.5:
            draw.ellipse((x, y, x + w, y + h), fill=fill)
        else:
            draw.rectangle((x, y, x + w, y + h), fill=fill)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


def resaved(data: bytes, scale: float, quality: int) -> bytes:
    image = Image.open(io.BytesIO(data))
    image = image.resize((int(image.width * scale), int(image.height * scale)))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=quality)
    return out.getvalue()


class FlagSeeds:
    """Stands in for the model: flags pictures made from chosen seeds, by
    their exact top-left colour."""

    def __init__(self, seeds: List[int]):
        self.colours = {Image.open(io.BytesIO(art(s))).getpixel((0, 0)) for s in seeds}
        self.calls = 0

    def nsfw_probability(self, image: Image.Image) -> float:
        self.calls += 1
        return 0.99 if image.getpixel((0, 0)) in self.colours else 0.01


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with TestClient(create_portfolio_app(str(tmp_path / "portfolio"))) as c:
        yield c


def flag(client: TestClient, seeds: List[int]) -> FlagSeeds:
    classifier = FlagSeeds(seeds)
    client.app.state.portfolio.moderation.classifier = classifier  # type: ignore[attr-defined]
    return classifier


def test_difference_hash_matches_copies_and_separates_pictures():
    original = art(1)
    known = difference_hash(decode(original))
    for scale, quality in ((1.0, 60), (0.5, 85), (0.75, 70)):
        copy = difference_hash(decode(resaved(original, scale, quality)))
        assert (known ^ copy).bit_count() <= MAX_HASH_DISTANCE
    for seed in range(2, 30):
        other = difference_hash(decode(art(seed)))
        assert (known ^ other).bit_count() > MAX_HASH_DISTANCE


def test_flagged_mint_is_refused_and_copies_stay_refused(client):
    classifier = flag(client, [1])
    alice = Profile(client)
    alice.create()
    resp = alice.prepare("mint", art(1))
    assert resp.status_code == 422
    assert resp.json()["detail"] == REJECTED_MESSAGE
    assert classifier.calls == 1
    # A smaller, recompressed copy is refused from the list, without the model.
    resp = alice.prepare("mint", resaved(art(1), 0.5, 70))
    assert resp.status_code == 422
    assert classifier.calls == 1
    # Another collector gets the same answer.
    bob = Profile(client)
    bob.create()
    assert bob.prepare("mint", art(1)).status_code == 422
    assert classifier.calls == 1
    # Unrelated pictures still mint.
    assert alice.prepare("mint", art(2)).status_code == 200
    assert classifier.calls == 2


def test_rejected_list_applies_without_a_classifier(client):
    alice = Profile(client)
    alice.create()
    flag(client, [3])
    assert alice.prepare("mint", art(3)).status_code == 422
    client.app.state.portfolio.moderation.classifier = None  # type: ignore[attr-defined]
    assert alice.prepare("mint", art(3)).status_code == 422
    assert alice.prepare("mint", art(4)).status_code == 200


def test_flagged_receive_and_profile_picture_are_refused(client):
    alice = Profile(client)
    alice.create()
    flag(client, [5])
    assert alice.prepare("receive", art(5)).status_code == 422
    assert alice.post(f"{alice.base}/avatar", art(5)).status_code == 422
    assert alice.get()["avatar"] is None
    assert alice.post(f"{alice.base}/avatar", art(6)).status_code == 200


def solid(colour) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(out, "JPEG", quality=95)
    return out.getvalue()


def test_flat_images_never_match_by_hash(client):
    alice = Profile(client)
    alice.create()
    classifier = flag(client, [])
    red = solid((200, 30, 30))
    classifier.colours = {decode(red).getpixel((0, 0))}
    assert alice.prepare("mint", red).status_code == 422
    # Another flat picture has the same empty hash; only the model decides.
    assert alice.prepare("mint", solid((30, 30, 200))).status_code == 200


@pytest.mark.skipif(
    not os.environ.get("NFT_PORTFOLIO_NSFW_MODEL"),
    reason="set NFT_PORTFOLIO_NSFW_MODEL to the exported ONNX model",
)
def test_exported_model_passes_ordinary_pictures():
    classifier = NSFWClassifier(os.environ["NFT_PORTFOLIO_NSFW_MODEL"])
    for seed in range(5):
        score = classifier.nsfw_probability(decode(art(seed, size=(1200, 400))))
        assert 0.0 <= score < 0.8
