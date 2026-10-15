"""Tests for the portfolio social layer (cashu/nft/portfolio_social.py)."""

import io
import json
import secrets
import time
from typing import Dict, Iterator, List, Tuple

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient
from PIL import Image

from cashu.nft.portfolio import create_portfolio_app
from tests.test_nft_portfolio import Profile, exported, make_jpg, minted_card

SECRET_KEYS = {"credential", "encrypted_credential", "secret", "s", "backup"}


class Clock:
    """Deterministic replacement for time.time used by the app."""

    def __init__(self, now: int = 1_700_000_000):
        self.now = now

    def __call__(self) -> float:
        return float(self.now)

    def tick(self, seconds: int = 10) -> int:
        self.now += seconds
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(time, "time", fake)
    return fake


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with TestClient(create_portfolio_app(str(tmp_path / "portfolio"))) as c:
        yield c


def people(client: TestClient, *names: str) -> List[Profile]:
    profiles = []
    for name in names:
        profile = Profile(client)
        profile.create(name)
        profiles.append(profile)
    return profiles


def toggle(actor: Profile, kind: str, target: Profile, on: bool = True):
    return actor.json_post(f"{actor.base}/{kind}/{target.pubkey}", {"on": on})


def settings(actor: Profile, **body):
    return actor.json_post(f"{actor.base}/settings", body)


def transfer(sender: Profile, receiver: Profile, jpg: bytes) -> Tuple[dict, dict]:
    card, transfer_jpg = exported(sender, jpg)
    resp = receiver.receive(transfer_jpg)
    assert resp.status_code == 200, resp.text
    return card, resp.json()


def send(sender: Profile, receiver: Profile, card: dict) -> dict:
    assert sender.claim(card).status_code == 200
    resp = sender.export(card["id"])
    assert resp.status_code == 200, resp.text
    resp = receiver.receive(resp.content)
    assert resp.status_code == 200, resp.text
    return resp.json()


def walk_keys(value) -> set:
    if isinstance(value, dict):
        keys = set(value)
        for item in value.values():
            keys |= walk_keys(item)
        return keys
    if isinstance(value, list):
        keys = set()
        for item in value:
            keys |= walk_keys(item)
        return keys
    return set()


# --- likes ---------------------------------------------------------------------


def test_like_unlike_toggles_counts(client):
    alice, bob, carol = people(client, "Alice", "Bob", "Carol")
    resp = toggle(bob, "likes", alice)
    assert resp.status_code == 200, resp.text
    assert resp.json()["likes"] == 1
    # Liking twice is idempotent.
    assert toggle(bob, "likes", alice).json()["likes"] == 1
    assert toggle(carol, "likes", alice).json()["likes"] == 2
    profile = alice.get()
    assert (profile["likes"], profile["followers"], profile["following"]) == (2, 0, 0)
    assert client.get(f"{bob.base}/relations").json() == {
        "likes": [alice.pubkey],
        "following": [],
    }
    assert toggle(bob, "likes", alice, on=False).json()["likes"] == 1
    # Unliking twice is also idempotent.
    assert toggle(bob, "likes", alice, on=False).json()["likes"] == 1
    assert client.get(f"{bob.base}/relations").json()["likes"] == []
    assert alice.get()["likes"] == 1


def test_like_requires_signature_from_liker(client):
    alice, bob = people(client, "Alice", "Bob")
    path = f"{bob.base}/likes/{alice.pubkey}"
    body = b'{"on": true}'
    other = PrivateKey(secrets.token_bytes(32))
    resp = client.post(
        path, content=body, headers=bob.headers(bob.challenge(path, body), other)
    )
    assert resp.status_code == 403
    # Unsigned requests are rejected as well.
    assert client.post(path, content=body).status_code == 401
    # Alice cannot obtain a challenge to like on Bob's behalf.
    resp = client.post(
        "/api/auth/challenge",
        json={
            "pubkey": alice.pubkey,
            "method": "POST",
            "path": path,
            "body_hash": "00" * 32,
        },
    )
    assert resp.status_code == 400
    assert alice.get()["likes"] == 0


def test_like_and_follow_validation(client):
    alice, bob = people(client, "Alice", "Bob")
    stranger = Profile(client)
    ghost = Profile(client)
    for kind in ("likes", "follows"):
        # Actor must have a profile.
        assert toggle(stranger, kind, alice).status_code == 404
        # Target must exist.
        assert toggle(bob, kind, ghost).status_code == 404
        # Self-like/self-follow are rejected.
        assert toggle(alice, kind, alice).status_code == 400
        # Malformed target and body.
        assert (
            bob.json_post(f"{bob.base}/{kind}/not-a-key", {"on": True}).status_code
            == 400
        )
        assert (
            bob.json_post(f"{bob.base}/{kind}/{alice.pubkey}", {"on": "x"}).status_code
            == 400
        )
    profile = alice.get()
    assert (profile["likes"], profile["followers"]) == (0, 0)


# --- follows, network, feed ----------------------------------------------------------


def test_follow_unfollow_relations_and_network(client):
    alice, bob, carol = people(client, "Alice", "Bob", "Carol")
    resp = toggle(bob, "follows", alice)
    assert resp.status_code == 200, resp.text
    assert resp.json()["followers"] == 1
    assert toggle(bob, "follows", alice).json()["followers"] == 1
    assert toggle(carol, "follows", alice).json()["followers"] == 2
    assert toggle(bob, "follows", carol).status_code == 200
    assert alice.get()["followers"] == 2
    assert bob.get()["following"] == 2
    assert sorted(client.get(f"{bob.base}/relations").json()["following"]) == sorted(
        [alice.pubkey, carol.pubkey]
    )
    network = client.get(f"{alice.base}/network").json()
    assert {p["pubkey"] for p in network["followers"]} == {bob.pubkey, carol.pubkey}
    assert {p["name"] for p in network["followers"]} == {"Bob", "Carol"}
    assert network["following"] == []
    network = client.get(f"{bob.base}/network").json()
    assert {p["pubkey"] for p in network["following"]} == {alice.pubkey, carol.pubkey}

    assert toggle(bob, "follows", alice, on=False).json()["followers"] == 1
    assert client.get(f"{bob.base}/relations").json()["following"] == [carol.pubkey]
    assert [
        p["pubkey"] for p in client.get(f"{alice.base}/network").json()["followers"]
    ] == [carol.pubkey]


def test_feed_only_shows_followees_cards_and_collections(client, clock):
    alice, bob, carol, dave = people(client, "Alice", "Bob", "Carol", "Dave")
    clock.tick()
    assert toggle(dave, "follows", alice).status_code == 200
    assert toggle(dave, "follows", bob).status_code == 200
    clock.tick()
    # Followee likes/follows are not feed events.
    assert toggle(alice, "likes", carol).status_code == 200
    assert toggle(alice, "follows", carol).status_code == 200
    clock.tick()
    minted = minted_card(carol, make_jpg(color=(1, 1, 1)))  # not followed
    clock.tick()
    card, received = transfer(alice, bob, make_jpg(color=(2, 2, 2)))
    feed = client.get(f"{dave.base}/feed").json()
    assert {e["kind"] for e in feed} <= {"mint", "receive", "collection"}
    assert {e["actor"] for e in feed} == {alice.pubkey, bob.pubkey}
    assert minted["id"] not in {e.get("card_id") for e in feed}
    kinds = {(e["kind"], e["actor"]) for e in feed}
    assert ("mint", alice.pubkey) in kinds
    assert ("receive", bob.pubkey) in kinds
    assert ("collection", alice.pubkey) in kinds
    assert ("collection", bob.pubkey) in kinds
    # Nobody followed: empty feed.
    assert client.get(f"{carol.base}/feed").json() == []


# --- settings / cover ------------------------------------------------------------


def test_settings_rename_validation(client):
    (alice,) = people(client, "Alice")
    resp = settings(alice, name="  Renamed  ")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed"
    for bad in ("", "   ", "x" * 41):
        assert settings(alice, name=bad).status_code == 400
    assert settings(alice, name="x" * 40).status_code == 200
    assert alice.get()["name"] == "x" * 40
    # Settings require the owner's signature.
    bob = Profile(client)
    bob.create("Bob")
    path = f"{alice.base}/settings"
    body = b'{"name": "Hijack"}'
    resp = client.post(
        path,
        content=body,
        headers=alice.headers(alice.challenge(path, body), bob.key),
    )
    assert resp.status_code == 403
    assert alice.get()["name"] == "x" * 40
    # Settings on a profile that does not exist.
    assert settings(Profile(client), name="Ghost").status_code == 404


def test_cover_selection_reset_and_fallback(client, clock):
    alice, bob = people(client, "Alice", "Bob")
    assert alice.get()["cover"] is None
    assert alice.get()["previews"] == []
    first = minted_card(alice, make_jpg(color=(10, 10, 10)))
    clock.tick()
    second = minted_card(alice, make_jpg(color=(20, 20, 20)))
    profile = alice.get()
    # Default cover is the newest NFT.
    assert profile["cover"] == second["h"] and profile["custom_cover"] is False
    assert profile["previews"] == [second["h"], first["h"]]

    resp = settings(alice, cover=first["h"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["cover"] == first["h"] and resp.json()["custom_cover"] is True

    # Covers must be one of the owner's active NFTs.
    bobs = minted_card(bob, make_jpg(color=(30, 30, 30)))
    assert settings(alice, cover=bobs["h"]).status_code == 400
    assert settings(alice, cover="0" * 64).status_code == 400
    assert settings(alice, cover="XYZ").status_code == 400
    assert alice.get()["cover"] == first["h"]

    # "" resets to the default.
    assert settings(alice, cover="").json()["custom_cover"] is False
    assert alice.get()["cover"] == second["h"]

    # A custom cover that leaves the collection falls back to the default.
    assert settings(alice, cover=first["h"]).status_code == 200
    clock.tick()
    received = send(alice, bob, first)
    assert received["h"] == first["h"]
    profile = alice.get()
    assert profile["cover"] == second["h"] and profile["custom_cover"] is False
    assert first["h"] not in profile["previews"]
    # A sent card cannot be chosen again.
    assert settings(alice, cover=first["h"]).status_code == 400
    assert bob.get()["previews"][0] == first["h"]


def test_banner_upload_nft_source_and_fallback(client, clock):
    alice, bob = people(client, "Alice", "Bob")
    assert alice.get()["banner"] is None
    # Any picture is re-encoded to a 1500x500 JPG, cropped around its centre.
    tall = io.BytesIO()
    Image.new("RGB", (900, 1200), (200, 40, 40)).save(tall, "PNG")
    resp = alice.post(f"{alice.base}/banner", tall.getvalue())
    assert resp.status_code == 200, resp.text
    version = resp.json()["banner"]
    assert version and resp.json()["banner_nft"] is None
    # An uploaded banner is the collection's cover on Explore.
    listed = client.get("/api/explore/collections?sort=new").json()["items"]
    mine = next(i for i in listed if i["pubkey"] == alice.pubkey)
    assert mine["banner"] == version and mine["banner_nft"] is None
    stored = client.get(f"/api/banners/{alice.pubkey}.jpg")
    assert stored.status_code == 200 and stored.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(stored.content)) as image:
        assert image.size == (1500, 500) and not image.getexif()
    assert alice.post(f"{alice.base}/banner", b"not an image").status_code == 400
    assert (
        alice.post(f"{alice.base}/banner", b"\0" * (5 * 1024 * 1024 + 1)).status_code
        == 413
    )
    # Only the owner can set it.
    assert (
        client.post(f"{alice.base}/banner", content=tall.getvalue()).status_code >= 400
    )

    # A banner cut from an NFT names it, and must be one of the owner's.
    first = minted_card(alice, make_jpg(color=(10, 10, 10)))
    bobs = minted_card(bob, make_jpg(color=(30, 30, 30)))
    crop = io.BytesIO()
    Image.new("RGB", (1500, 500), (10, 10, 10)).save(crop, "JPEG")
    assert (
        alice.post(f"{alice.base}/banner?nft={bobs['h']}", crop.getvalue()).status_code
        == 400
    )
    assert (
        alice.post(f"{alice.base}/banner?nft=XYZ", crop.getvalue()).status_code == 422
    )
    clock.tick()
    resp = alice.post(f"{alice.base}/banner?nft={first['h']}", crop.getvalue())
    assert resp.status_code == 200, resp.text
    assert resp.json()["banner_nft"] == first["h"] and resp.json()["banner"] > version

    # Once that NFT leaves the collection, the banner stops showing.
    clock.tick()
    send(alice, bob, first)
    assert alice.get()["banner"] is None

    assert alice.post(f"{alice.base}/banner", tall.getvalue()).status_code == 200
    assert alice.post(f"{alice.base}/banner/remove").json()["banner"] is None
    assert client.get(f"/api/banners/{alice.pubkey}.jpg").status_code == 404


# --- explore ---------------------------------------------------------------------


def test_explore_collections_sort_search_and_pagination(client, clock):
    alice, bob, carol = people(client, "Alice", "Bobby", "Carol")
    clock.tick()
    dave = Profile(client)
    dave.create("Dave")
    for liker in (alice, carol, dave):
        assert toggle(liker, "likes", bob).status_code == 200
    assert toggle(dave, "likes", carol).status_code == 200
    minted_card(alice, make_jpg(color=(40, 40, 40)))
    minted_card(alice, make_jpg(color=(50, 50, 50)))

    popular = client.get("/api/explore/collections").json()
    assert [i["pubkey"] for i in popular["items"][:2]] == [bob.pubkey, carol.pubkey]
    assert popular["items"][0]["likes"] == 3
    assert popular["more"] is False
    assert {"cover", "previews", "nfts", "followers", "name"} <= set(
        popular["items"][0]
    )

    largest = client.get("/api/explore/collections?sort=largest").json()
    assert largest["items"][0]["pubkey"] == alice.pubkey
    assert largest["items"][0]["nfts"] == 2
    assert len(largest["items"][0]["previews"]) == 2

    new = client.get("/api/explore/collections?sort=new").json()
    assert new["items"][0]["pubkey"] == dave.pubkey

    found = client.get("/api/explore/collections?q=BOB").json()
    assert [i["pubkey"] for i in found["items"]] == [bob.pubkey]
    assert client.get("/api/explore/collections?q=zzz").json() == {
        "items": [],
        "more": False,
    }

    page1 = client.get("/api/explore/collections?sort=new&limit=3").json()
    page2 = client.get("/api/explore/collections?sort=new&limit=3&offset=3").json()
    assert len(page1["items"]) == 3 and page1["more"] is True
    assert len(page2["items"]) == 1 and page2["more"] is False
    seen = [i["pubkey"] for i in page1["items"] + page2["items"]]
    assert sorted(seen) == sorted(p.pubkey for p in (alice, bob, carol, dave))
    assert client.get("/api/explore/collections?sort=bogus").status_code == 422
    assert client.get("/api/explore/collections?limit=0").status_code == 422


def test_explore_nfts_excludes_sent_and_sorts(client, clock):
    alice, bob = people(client, "Alice", "Bob")
    b = minted_card(alice, make_jpg(color=(60, 60, 60)))
    clock.tick()
    a = alice.mint(make_jpg(color=(70, 70, 70)), title="Aardvark")
    assert a.status_code == 200
    a_card = a.json()
    clock.tick()
    sent, received = transfer(alice, bob, make_jpg(color=(80, 80, 80)))

    new = client.get("/api/explore/nfts").json()
    ids = [i["id"] for i in new["items"]]
    assert sent["id"] not in ids
    assert ids == [received["id"], a_card["id"], b["id"]]
    assert new["items"][0]["owner_name"] == "Bob"
    assert all(i["status"] != "sent" for i in new["items"])

    old = client.get("/api/explore/nfts?sort=old").json()
    assert [i["id"] for i in old["items"]] == list(reversed(ids))

    titled = client.get("/api/explore/nfts?sort=title").json()
    assert titled["items"][0]["title"] == "Aardvark"

    assert [
        i["id"] for i in client.get("/api/explore/nfts?q=aard").json()["items"]
    ] == [a_card["id"]]
    page = client.get("/api/explore/nfts?limit=2").json()
    assert len(page["items"]) == 2 and page["more"] is True
    page = client.get("/api/explore/nfts?limit=2&offset=2").json()
    assert len(page["items"]) == 1 and page["more"] is False


# --- activity -------------------------------------------------------------------------


def test_activity_mint_vs_receive_before_and_actor(client, clock):
    alice, bob = people(client, "Alice", "Bob")
    t_profiles = clock.now
    clock.tick()
    assert toggle(bob, "follows", alice).status_code == 200
    t_follow = clock.now
    clock.tick()
    assert toggle(bob, "likes", alice).status_code == 200
    clock.tick()
    card, received = transfer(alice, bob, make_jpg(color=(90, 90, 90)))
    t_transfer = clock.now

    events = client.get("/api/activity").json()
    assert [e["created"] for e in events] == sorted(
        (e["created"] for e in events), reverse=True
    )
    by_kind: Dict[str, List[dict]] = {}
    for event in events:
        by_kind.setdefault(event["kind"], []).append(event)
    (mint,) = by_kind["mint"]
    assert mint["actor"] == alice.pubkey and mint["card_id"] == card["id"]
    assert mint["target"] is None and mint["actor_name"] == "Alice"
    (receive,) = by_kind["receive"]
    assert receive["actor"] == bob.pubkey and receive["card_id"] == received["id"]
    assert receive["target"] == alice.pubkey
    assert receive["target_name"] == "Alice"
    assert receive["h"] == card["h"]
    assert {e["actor"] for e in by_kind["collection"]} == {alice.pubkey, bob.pubkey}
    assert by_kind["like"][0]["target"] == alice.pubkey
    assert by_kind["follow"][0]["actor"] == bob.pubkey
    assert all(e["created"] <= t_transfer for e in events)

    # `before` is an exclusive upper bound on the event timestamp.
    older = client.get(f"/api/activity?before={t_follow + 1}").json()
    assert {e["kind"] for e in older} == {"collection", "follow"}
    assert client.get(f"/api/activity?before={t_profiles}").json() == []
    limited = client.get("/api/activity?limit=2").json()
    assert len(limited) == 2
    assert limited == events[:2]

    alices = client.get(f"/api/activity?actor={alice.pubkey}").json()
    assert {e["actor"] for e in alices} == {alice.pubkey}
    assert {e["kind"] for e in alices} == {"collection", "mint"}
    bobs = client.get(f"/api/activity?actor={bob.pubkey}").json()
    assert {e["kind"] for e in bobs} == {"collection", "follow", "like", "receive"}
    assert client.get("/api/activity?actor=nope").status_code == 422


def test_activity_receive_in_same_second_as_mint(client, clock):
    """Second-resolution timestamps must not turn a receipt into a mint."""
    alice, bob = people(client, "Alice", "Bob")
    card, received = transfer(alice, bob, make_jpg(color=(100, 100, 100)))
    events = client.get(f"/api/activity?actor={bob.pubkey}").json()
    (receive,) = [e for e in events if e.get("card_id") == received["id"]]
    assert receive["kind"] == "receive"
    assert receive["target"] == alice.pubkey
    alices = client.get(f"/api/activity?actor={alice.pubkey}").json()
    assert [e["kind"] for e in alices if e.get("card_id")] == ["mint"]


def test_activity_round_trip_receive_back(client, clock):
    """Alice -> Bob -> Alice: Alice's second card is a receipt from Bob."""
    alice, bob = people(client, "Alice", "Bob")
    jpg = make_jpg(color=(110, 110, 110))
    clock.tick()
    card, at_bob = transfer(alice, bob, jpg)
    clock.tick()
    back = send(bob, alice, at_bob)
    events = {
        e["card_id"]: e for e in client.get("/api/activity").json() if e.get("card_id")
    }
    assert events[card["id"]]["kind"] == "mint"
    assert events[at_bob["id"]]["target"] == alice.pubkey
    assert events[back["id"]]["kind"] == "receive"
    assert events[back["id"]]["target"] == bob.pubkey


# --- secrets ----------------------------------------------------------------------------


def test_public_social_responses_never_include_secrets(client, clock):
    alice, bob = people(client, "Alice", "Bob")
    toggle(bob, "likes", alice)
    toggle(bob, "follows", alice)
    minted_card(alice, make_jpg(color=(120, 120, 120)))
    clock.tick()
    transfer(alice, bob, make_jpg(color=(130, 130, 130)))
    rows = client.portal.call(
        client.app.state.portfolio.db.fetchall,
        "SELECT encrypted_credential FROM portfolio_cards WHERE encrypted_credential IS NOT NULL",
    )
    assert len(rows) == 2
    ciphertexts = [json.loads(r["encrypted_credential"])["ciphertext"] for r in rows]
    responses = [
        client.get(alice.base),
        client.get(bob.base),
        client.get(f"{bob.base}/relations"),
        client.get(f"{alice.base}/network"),
        client.get(f"{bob.base}/feed"),
        client.get("/api/explore/collections"),
        client.get("/api/explore/nfts"),
        client.get("/api/activity"),
        settings(alice, name="Alice2"),
        toggle(bob, "likes", alice),
        toggle(bob, "follows", alice),
    ]
    for resp in responses:
        assert resp.status_code == 200, resp.text
        assert not walk_keys(resp.json()) & SECRET_KEYS, resp.request.url
        for ciphertext in ciphertexts:
            assert ciphertext not in resp.text
        for profile in (alice, bob):
            assert profile.secret.hex() not in resp.text
