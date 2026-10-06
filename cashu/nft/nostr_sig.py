"""Profile signatures from a raw key or from a Nostr signing extension.

Every profile signature signs a 32-byte digest. A key held in the page signs
it directly (BIP-340). A NIP-07 extension can only sign Nostr events, so it
signs an event of ``SIG_KIND`` whose ``x`` tag is the digest and whose content
is a fixed label per purpose; the signature is then encoded as
``n1:<created_at>:<sig>`` and verifiers rebuild the event around the digest.
Mirrors ``portfolio_web/src/crypto.mjs`` (``verifySignature``).
"""

import hashlib
import json
import re

from coincurve import PublicKeyXOnly

SIG_KIND = 27711
SIG_LABELS = {
    "auth": "Sign in to Nonfungible.cash",
    "claim": "Publish an NFT to your Nonfungible.cash collection",
    "listing": "List an NFT for sale on Nonfungible.cash",
    "offer": "Make an offer on Nonfungible.cash",
    "accept": "Accept an offer on Nonfungible.cash",
}
# Field pattern for request models that carry a profile signature.
PROFILE_SIG = r"^(?:[0-9a-f]{128}|n1:(?:0|[1-9][0-9]{0,9}):[0-9a-f]{128})$"
_RAW = re.compile(r"[0-9a-f]{128}")
_EVENT = re.compile(r"n1:(0|[1-9][0-9]{0,9}):([0-9a-f]{128})")


def event_id(
    pubkey: str, created_at: int, kind: int, tags: list, content: str
) -> bytes:
    """NIP-01 event id."""
    serialized = json.dumps(
        [0, pubkey, created_at, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode()).digest()


def signature_event_id(
    pubkey: str, purpose: str, digest: bytes, created_at: int
) -> bytes:
    return event_id(
        pubkey, created_at, SIG_KIND, [["x", digest.hex()]], SIG_LABELS[purpose]
    )


def verify_signature(pubkey: str, purpose: str, digest: bytes, signature: str) -> bool:
    """A raw BIP-340 signature over ``digest``, or an event that commits to it."""
    if purpose not in SIG_LABELS or not isinstance(signature, str):
        return False
    try:
        key = PublicKeyXOnly(bytes.fromhex(pubkey))
        if _RAW.fullmatch(signature):
            return key.verify(bytes.fromhex(signature), digest)
        event = _EVENT.fullmatch(signature)
        if event is None:
            return False
        message = signature_event_id(pubkey, purpose, digest, int(event[1]))
        return key.verify(bytes.fromhex(event[2]), message)
    except (TypeError, ValueError):
        return False
