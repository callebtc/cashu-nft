"""Upload screening: an NSFW image classifier and a list of rejected images.

The classifier is Marqo's nsfw-image-detection-384 (a ViT-tiny, Apache-2.0)
exported to ONNX by ``scripts/export_nsfw_model.py``. A rejected image is
remembered by its SHA-256 and a perceptual difference hash, so the same
picture re-saved, recompressed or resized is refused without running the
model again.
"""

import asyncio
import hashlib
import io
import math
import time
from typing import List, Optional, Protocol

import numpy as np
import onnxruntime
from fastapi import HTTPException
from loguru import logger
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from ..core.db import Database
from .portfolio_image import DARK, DECODE_ERRORS, PAPER, flatten

INPUT_SIZE = 384
MAX_CROPS = 4
# NSFW probability at which an image is refused. The model card's evaluation
# puts NSFW recall near 96% and safe-image recall near 99% here. Abstract
# digital art scores high more often than photos: lowering the threshold
# rejects much more legitimate art (about a fifth of abstract images at 0.5).
DEFAULT_THRESHOLD = 0.8
HASH_SIZE = 16
HASH_BITS = 3 * HASH_SIZE * HASH_SIZE
# Bits of the difference hash that may differ for a near duplicate. Resized,
# recompressed or slightly cropped copies stay within about 8%.
MAX_HASH_DISTANCE = HASH_BITS * 8 // 100
# Hashes of near-flat images carry too little detail to match on.
MIN_HASH_BITS = HASH_BITS * 5 // 100

REJECTED_MESSAGE = (
    "This image can't be published here. It looks like adult or explicit content."
)


class Classifier(Protocol):
    def nsfw_probability(self, image: Image.Image) -> float: ...


def appearances(data: bytes) -> List[Image.Image]:
    """The picture as viewers see it: upright and opaque, decoded at a reduced
    scale where JPEG allows. A picture with transparency comes twice, on the
    site's light and dark backgrounds: what shows through differs, and the
    colour of fully transparent pixels (which nobody sees) is dropped."""
    with Image.open(io.BytesIO(data)) as source:
        source.draft("RGB", (INPUT_SIZE, INPUT_SIZE))
        upright = ImageOps.exif_transpose(source)
        if not upright.has_transparency_data:
            return [upright.convert("RGB")]
        return [flatten(upright, PAPER), flatten(upright, DARK)]


def decode(data: bytes) -> Image.Image:
    """The picture as it shows on the site's light background."""
    return appearances(data)[0]


def views(image: Image.Image) -> List[Image.Image]:
    """Square crops covering the whole image, plus the whole image padded to a
    square when it isn't one, so nothing outside a centre crop goes unseen."""
    width, height = image.size
    short, long = min(width, height), max(width, height)
    count = 1 if long <= short * 1.2 else min(MAX_CROPS, math.ceil(long / short))
    result = []
    for i in range(count):
        offset = (
            (long - short) // 2 if count == 1 else i * (long - short) // (count - 1)
        )
        box = (
            (offset, 0, offset + short, short)
            if width > height
            else (0, offset, short, offset + short)
        )
        result.append(image.crop(box))
    if count > 1:
        padded = Image.new("RGB", (long, long), (128, 128, 128))
        padded.paste(image, ((long - width) // 2, (long - height) // 2))
        result.append(padded)
    return [
        view.resize((INPUT_SIZE, INPUT_SIZE), Image.Resampling.BICUBIC)
        for view in result
    ]


class NSFWClassifier:
    def __init__(self, model_path: str, threads: int = 2):
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            model_path, options, providers=["CPUExecutionProvider"]
        )

    def nsfw_probability(self, image: Image.Image) -> float:
        """The highest NSFW probability over the image's views."""
        pixels = np.stack([np.asarray(view, dtype=np.float32) for view in views(image)])
        # The model's normalisation: mean 0.5, std 0.5, channels first.
        pixels = (pixels / 127.5 - 1.0).transpose(0, 3, 1, 2)
        logits = self.session.run(None, {"pixels": np.ascontiguousarray(pixels)})[0]
        # Labels are [NSFW, SFW]; softmax over two classes.
        nsfw = 1.0 / (1.0 + np.exp(logits[:, 1] - logits[:, 0]))
        return float(nsfw.max())


def difference_hash(image: Image.Image) -> int:
    """A colour dHash: per RGB channel, whether each pixel of a 17x16
    thumbnail is brighter than its right neighbour. Colour keeps images that
    share a layout but not their colours (such as pixel-art avatars) apart."""
    small = np.asarray(
        image.resize((HASH_SIZE + 1, HASH_SIZE), Image.Resampling.LANCZOS),
        dtype=np.int16,
    )
    bits = (small[:, :-1, :] > small[:, 1:, :]).transpose(2, 0, 1).flatten()
    return int("".join("1" if bit else "0" for bit in bits), 2)


class Moderation:
    """Screens every image the portfolio would publish. Without a classifier
    (development and tests) only the rejected-image list applies."""

    def __init__(
        self,
        db: Database,
        classifier: Optional[Classifier] = None,
        threshold: float = DEFAULT_THRESHOLD,
    ):
        self.db = db
        self.classifier = classifier
        self.threshold = threshold
        # One inference at a time keeps a small server responsive.
        self.lock = asyncio.Lock()

    async def migrate(self) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS portfolio_rejected_images (
                    sha256 TEXT PRIMARY KEY, dhash TEXT NOT NULL, score REAL NOT NULL,
                    pubkey TEXT NOT NULL, created INTEGER NOT NULL)"""
            )

    async def rejected(self, sha256: str, dhash: int) -> bool:
        rows = await self.db.fetchall(
            "SELECT sha256, dhash FROM portfolio_rejected_images"
        )
        for row in rows:
            if row["sha256"] == sha256:
                return True
            known = int(row["dhash"], 16)
            if (
                MIN_HASH_BITS <= known.bit_count() <= HASH_BITS - MIN_HASH_BITS
                and (known ^ dhash).bit_count() <= MAX_HASH_DISTANCE
            ):
                return True
        return False

    async def check(self, pubkey: str, image: bytes) -> None:
        """Raise 422 for an image that was rejected before or that the
        classifier scores at or above the threshold; remember the latter."""
        sha256 = hashlib.sha256(image).hexdigest()
        try:
            shown = await run_in_threadpool(appearances, image)
        except (*DECODE_ERRORS, ValueError):
            raise HTTPException(400, "This image is damaged or can't be decoded.")
        dhash = difference_hash(shown[0])
        if await self.rejected(sha256, dhash):
            raise HTTPException(422, REJECTED_MESSAGE)
        if self.classifier is None:
            return
        classifier = self.classifier
        async with self.lock:
            score = max(
                [
                    await run_in_threadpool(classifier.nsfw_probability, view)
                    for view in shown
                ]
            )
        if score < self.threshold:
            return
        logger.info(f"Rejected an upload from {pubkey} (NSFW score {score:.2f})")
        await self.db.execute(
            """INSERT INTO portfolio_rejected_images(sha256,dhash,score,pubkey,created)
            VALUES(:s,:d,:score,:p,:t) ON CONFLICT(sha256) DO NOTHING""",
            {
                "s": sha256,
                "d": f"{dhash:0{HASH_BITS // 4}x}",
                "score": score,
                "p": pubkey,
                "t": int(time.time()),
            },
        )
        raise HTTPException(422, REJECTED_MESSAGE)
