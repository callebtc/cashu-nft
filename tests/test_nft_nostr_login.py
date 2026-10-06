"""Nostr login on the server (cashu/nft/NOSTR_LOGIN_PLAN.md): profile
signatures from a signing extension, session keys and the wallet-key vault."""

import base64
import hashlib
import json
import secrets
import time
from typing import Optional

import pytest
from coincurve import PrivateKey, PublicKeyXOnly

from cashu.core.crypto.ps import present
from cashu.nft import market_protocol as mp
from cashu.nft.nostr_sig import (
    SIG_KIND,
    SIG_LABELS,
    event_id,
    signature_event_id,
    verify_signature,
)
from cashu.nft.portfolio import claim_digest
from tests.test_nft_market import env, give_nft  # noqa: F401
from tests.test_nft_portfolio import Profile, client, make_jpg  # noqa: F401


def event_signature(
    key: PrivateKey, purpose: str, digest: bytes, created_at: Optional[int] = None
) -> str:
    """What portfolio_web/src/nostr signs through window.nostr.signEvent."""
    created_at = int(time.time()) if created_at is None else created_at
    pubkey = PublicKeyXOnly.from_secret(key.secret).format().hex()
    sig = key.sign_schnorr(signature_event_id(pubkey, purpose, digest, created_at))
    return f"n1:{created_at}:{sig.hex()}"


class NostrProfile(Profile):
    """A profile whose key lives in a signing extension: it only signs events."""

    def sign(self, message: str, key: Optional[PrivateKey] = None) -> str:
        if key is not None:
            return super().sign(message, key)
        return event_signature(
            self.key, "auth", hashlib.sha256(message.encode()).digest()
        )

    def sign_claim(self, showing: str) -> str:
        return event_signature(self.key, "claim", claim_digest(showing))


class Session:
    """A session key a NostrProfile authorized; it signs owner requests raw."""

    def __init__(self, owner: Profile):
        self.owner = owner
        self.key = PrivateKey(secrets.token_bytes(32))
        self.pubkey = PublicKeyXOnly.from_secret(self.key.secret).format().hex()

    def authorize(self, expires: Optional[int] = None):
        body = json.dumps(
            {
                "session_pubkey": self.pubkey,
                "expires": expires or int(time.time()) + 86400,
            }
        ).encode()
        return self.owner.post(f"{self.owner.base}/session", body)

    def post(self, path: str, body: bytes = b"", session: Optional[str] = None):
        challenge = self.owner.challenge(path, body)
        return self.owner.client.post(
            path,
            content=body,
            headers={
                "X-Portfolio-Challenge": challenge["nonce"],
                "X-Portfolio-Signature": self.owner.sign(
                    challenge["message"], self.key
                ),
                "X-Portfolio-Session": session or self.pubkey,
            },
        )


def vault(check: str = "ab" * 16) -> dict:
    return {
        "ciphertext": base64.b64encode(secrets.token_bytes(129)).decode(),
        "check": check,
    }


# --- signature format --------------------------------------------------------


def test_signatures_are_raw_or_events_that_commit_to_the_digest():
    key = PrivateKey(bytes.fromhex("42" * 32))
    pubkey = PublicKeyXOnly.from_secret(key.secret).format().hex()
    digest = hashlib.sha256(b"payload").digest()
    raw = key.sign_schnorr(digest).hex()
    assert verify_signature(pubkey, "claim", digest, raw)
    signed = event_signature(key, "listing", digest, 1700000000)
    assert verify_signature(pubkey, "listing", digest, signed)
    sig = signed.split(":")[2]
    # Bound to purpose, digest, time and key.
    assert not verify_signature(pubkey, "offer", digest, signed)
    assert not verify_signature(
        pubkey, "listing", hashlib.sha256(digest).digest(), signed
    )
    assert not verify_signature(pubkey, "listing", digest, f"n1:1700000001:{sig}")
    other = PublicKeyXOnly.from_secret(b"\x43" * 32).format().hex()
    assert not verify_signature(other, "listing", digest, signed)
    for bad in (
        "",
        f"n1::{sig}",
        f"n1:01:{sig}",
        f"n1:1700000000:{sig.upper()}",
        f"n1:1700000000:{sig}\n",
        raw + "\n",
        f"n2:1700000000:{sig}",
    ):
        assert not verify_signature(pubkey, "listing", digest, bad), bad
    assert not verify_signature(pubkey, "unknown", digest, raw)
    assert not verify_signature("zz" * 32, "claim", digest, raw)


def test_event_ids_match_the_browser_serialization():
    # The same event as portfolio_web/tests/identity.test.mjs.
    event = ["a" * 64, 1, 1, [["e", "b" * 64]], 'hello "nostr"\n']
    serialized = f'[0,"{"a" * 64}",1,1,[["e","{"b" * 64}"]],"hello \\"nostr\\"\\n"]'
    assert event_id(*event) == hashlib.sha256(serialized.encode()).digest()
    assert SIG_KIND == 27711 and set(SIG_LABELS) == {
        "auth",
        "claim",
        "listing",
        "offer",
        "accept",
    }


def test_market_purposes_accept_extension_signatures():
    key = PrivateKey(secrets.token_bytes(32))
    pubkey = PublicKeyXOnly.from_secret(key.secret).format().hex()
    digest = secrets.token_bytes(32)
    for domain, purpose in mp.PURPOSES.items():
        inner = hashlib.sha256(domain + digest).digest()
        assert mp.verify_purpose(
            domain, digest, event_signature(key, purpose, inner), pubkey
        )
        assert mp.verify_purpose(
            domain, digest, mp.sign_purpose(domain, digest, key.secret), pubkey
        )
        # A listing signature is not an offer signature.
        wrong = "offer" if purpose != "offer" else "listing"
        assert not mp.verify_purpose(
            domain, digest, event_signature(key, wrong, inner), pubkey
        )
    assert not mp.verify_purpose(
        mp.RECEIPT_DOMAIN, digest, key.sign_schnorr(digest).hex(), pubkey
    )


# --- extension-signed requests and showings ----------------------------------


def test_extension_signs_requests_and_publishes_showings(client):  # noqa: F811
    nostr = NostrProfile(client)
    nostr.create("Nostr collector")
    card = nostr.mint(make_jpg())
    assert card.status_code == 200, card.text
    assert card.json()["signature"].startswith("n1:")
    assert nostr.claim(card.json()).status_code == 200
    # A raw signature from the wrong key is still refused.
    path = f"{nostr.base}/cards/pending"
    challenge = nostr.challenge(path)
    stranger = PrivateKey(secrets.token_bytes(32))
    response = client.post(
        path,
        headers={
            "X-Portfolio-Challenge": challenge["nonce"],
            "X-Portfolio-Signature": event_signature(
                stranger, "auth", hashlib.sha256(challenge["message"].encode()).digest()
            ),
        },
    )
    assert response.status_code == 403
    # An event signed for another purpose is not an auth signature.
    challenge = nostr.challenge(path)
    digest = hashlib.sha256(challenge["message"].encode()).digest()
    response = client.post(
        path,
        headers={
            "X-Portfolio-Challenge": challenge["nonce"],
            "X-Portfolio-Signature": event_signature(nostr.key, "claim", digest),
        },
    )
    assert response.status_code == 403


# --- session keys ----------------------------------------------------------


def test_session_keys_sign_owner_requests_until_revoked(client):  # noqa: F811
    nostr = NostrProfile(client)
    session = Session(nostr)
    # A session can be authorized before the collection exists.
    assert session.authorize().status_code == 200
    created = session.post(nostr.base, b'{"name": "Via session"}')
    assert created.status_code == 200, created.text
    assert session.post(f"{nostr.base}/cards/pending").json() == {"ids": []}
    # Sessions are bound to their profile.
    other = NostrProfile(client)
    other.create()
    path = f"{other.base}/cards/pending"
    challenge = other.challenge(path)
    response = client.post(
        path,
        headers={
            "X-Portfolio-Challenge": challenge["nonce"],
            "X-Portfolio-Signature": other.sign(challenge["message"], session.key),
            "X-Portfolio-Session": session.pubkey,
        },
    )
    assert response.status_code == 401
    # A session key can't add another session: only the profile key can.
    second = Session(nostr)
    body = json.dumps(
        {"session_pubkey": second.pubkey, "expires": int(time.time()) + 60}
    ).encode()
    assert session.post(f"{nostr.base}/session", body).status_code == 403
    # Revoking ends it.
    revoke = json.dumps({"session_pubkey": session.pubkey}).encode()
    assert session.post(f"{nostr.base}/session/revoke", revoke).status_code == 200
    assert session.post(f"{nostr.base}/cards/pending").status_code == 401


def test_session_lifetime_and_cap(client, monkeypatch):  # noqa: F811
    nostr = NostrProfile(client)
    nostr.create()
    now = int(time.time())
    assert Session(nostr).authorize(now + 31 * 86400).status_code == 400
    assert Session(nostr).authorize(now - 1).status_code == 400
    short = Session(nostr)
    assert short.authorize(now + 60).status_code == 200
    monkeypatch.setattr(time, "time", lambda: now + 120)
    assert short.post(f"{nostr.base}/cards/pending").status_code == 401
    monkeypatch.undo()
    sessions = [Session(nostr) for _ in range(21)]
    for i, s in enumerate(sessions):
        assert s.authorize(now + 3600 + i).status_code == 200
    # The one closest to expiry made room for the newest.
    assert sessions[0].post(f"{nostr.base}/cards/pending").status_code == 401
    assert sessions[-1].post(f"{nostr.base}/cards/pending").status_code == 200


# --- vault ------------------------------------------------------------------


def test_nostr_collections_store_a_write_once_vault(client):  # noqa: F811
    nostr = NostrProfile(client)
    stored = vault()
    body = json.dumps({"name": "Nostr", "vault": stored}).encode()
    assert nostr.post(nostr.base, body).status_code == 200
    profile = nostr.get()
    assert profile["nostr"] is True
    assert "vault" not in json.dumps(profile) and stored[
        "ciphertext"
    ] not in json.dumps(profile)
    got = nostr.post(f"{nostr.base}/vault/get")
    assert got.json() == stored
    # Only the owner reads it.
    stranger = Profile(client)
    path = f"{nostr.base}/vault/get"
    challenge = nostr.challenge(path)
    response = client.post(
        path,
        headers={
            "X-Portfolio-Challenge": challenge["nonce"],
            "X-Portfolio-Signature": stranger.sign(challenge["message"]),
        },
    )
    assert response.status_code == 403
    # Re-storing needs the same wallet key (check value).
    same = vault(stored["check"])
    assert (
        nostr.post(f"{nostr.base}/vault", json.dumps(same).encode()).status_code == 200
    )
    assert nostr.post(f"{nostr.base}/vault/get").json() == same
    other = vault("cd" * 16)
    assert (
        nostr.post(f"{nostr.base}/vault", json.dumps(other).encode()).status_code == 409
    )
    # A vault on an existing collection's create call is ignored.
    again = json.dumps({"name": "Nostr", "vault": other}).encode()
    assert nostr.post(nostr.base, again).status_code == 200
    assert nostr.post(f"{nostr.base}/vault/get").json() == same


def test_key_collections_have_no_vault(client):  # noqa: F811
    keyed = Profile(client)
    keyed.create()
    assert keyed.get()["nostr"] is False
    assert keyed.post(f"{keyed.base}/vault/get").status_code == 404
    body = json.dumps(vault()).encode()
    assert keyed.post(f"{keyed.base}/vault", body).status_code == 409
    missing = NostrProfile(client)
    assert missing.post(f"{missing.base}/vault", body).status_code == 404
    malformed = json.dumps({"ciphertext": "not base64!", "check": "ab" * 16})
    nostr = NostrProfile(client)
    nostr.post(nostr.base, json.dumps({"name": "N", "vault": vault()}).encode())
    assert nostr.post(f"{nostr.base}/vault", malformed.encode()).status_code == 400


@pytest.mark.asyncio
async def test_extension_signed_listing(env):  # noqa: F811
    seller = await give_nft(env, await env.actor("Nia"))
    key = PrivateKey(seller.actor.secret)

    async def post(price: int, purpose: str):
        listing = {
            "v": mp.LISTING_PROTOCOL,
            "listing_id": secrets.token_hex(16),
            "revision": 1,
            "card_id": seller.card_id,
            "seller": seller.actor.pubkey,
            "h": seller.cred.h.to_bytes(32, "big").hex(),
            "nullifier": present(seller.cred).nullifier.format().hex(),
            "nft_keyset": env.ledger.keyset.keyset_id,
            "price": price,
            "claim_pubkey": seller.claim_pub,
            "created": env.market.clock(),
        }
        digest = hashlib.sha256(mp.LISTING_DOMAIN + mp.listing_hash(listing)).digest()
        body = {"listing": listing, "signature": event_signature(key, purpose, digest)}
        return await seller.actor.post(seller.actor.base() + "/market/listings", body)

    # An offer signature can't stand in for a listing signature.
    assert (await post(22, "offer")).status_code == 403
    response = await post(21, "listing")
    assert response.status_code == 200, response.text
    assert response.json()["price"] == 21
