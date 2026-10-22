"""Tests for portfolio transfer links (cashu/nft/portfolio_links.py)."""

import json
import secrets
from typing import Iterator, Optional

import pytest
from coincurve import PrivateKey
from fastapi.testclient import TestClient

from cashu.core.crypto.ps import present, prove_owner_secret
from cashu.nft.portfolio import create_portfolio_app
from cashu.nft.wallet import TOKEN_PREFIX, NFTClient
from tests.test_nft_portfolio import Profile, exported, make_jpg, minted_card

SECRET_KEYS = {
    "credential",
    "encrypted_credential",
    "secret",
    "s",
    "backup",
    "key",
    "token",
}


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with TestClient(create_portfolio_app(str(tmp_path / "portfolio"))) as c:
        yield c


def person(client: TestClient, name: str) -> Profile:
    profile = Profile(client)
    profile.create(name)
    return profile


def nullifier(card: dict) -> str:
    return NFTClient.decode_showing(card["showing"])[1].nullifier.format().hex()


def envelope(password: bool = False) -> dict:
    return {
        "version": 1,
        "kdf": (
            {
                "name": "PBKDF2-SHA256",
                "iterations": 210_000,
                "salt": secrets.token_hex(16),
            }
            if password
            else None
        ),
        "nonce": secrets.token_hex(12),
        "ciphertext": secrets.token_hex(200),
    }


def link_body(card: dict, **overrides) -> dict:
    body = {
        "id": secrets.token_hex(16),
        "card_id": card["id"],
        "nullifier": nullifier(card),
        "envelope": envelope(),
    }
    body.update(overrides)
    return body


def create_link(profile: Profile, body: dict):
    return profile.json_post(f"{profile.base}/links", body)


def ready_card(profile: Profile, jpg: Optional[bytes] = None) -> dict:
    """Mint a card and mark it 'ready' through the browser export path."""
    card, _ = exported(profile, jpg)
    current = next(c for c in profile.mine()["cards"] if c["id"] == card["id"])
    assert current["status"] == "ready"
    return current


def walk_keys(value) -> set:
    if isinstance(value, dict):
        keys = set(value)
        for item in value.values():
            keys |= walk_keys(item)
        return keys
    if isinstance(value, list):
        return set().union(*(walk_keys(item) for item in value)) if value else set()
    return set()


# --- create -----------------------------------------------------------------


def test_create_link_and_get_open(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    body = link_body(card, envelope=envelope(password=True))
    resp = create_link(alice, body)
    assert resp.status_code == 200, resp.text
    link = resp.json()
    assert link["id"] == body["id"] and link["status"] == "open"

    resp = client.get(f"/api/links/{body['id']}")
    assert resp.status_code == 200, resp.text
    link = resp.json()
    assert link["status"] == "open"
    assert link["claimed_by"] is None
    assert link["envelope"] == body["envelope"]
    assert link["protected"] is True
    assert link["sender"] == alice.pubkey
    assert link["sender_name"] == "Alice"
    assert link["title"] == card["title"]
    assert link["h"] == card["h"]
    assert link["card_id"] == card["id"]
    assert not walk_keys(link) & SECRET_KEYS
    cred = alice.credentials[card["id"]].to_bytes().hex()
    assert cred not in resp.text and TOKEN_PREFIX not in resp.text


def test_unprotected_link_flag(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    body = link_body(card)
    assert create_link(alice, body).status_code == 200
    link = client.get(f"/api/links/{body['id']}").json()
    assert link["protected"] is False
    assert link["envelope"]["kdf"] is None


def test_create_link_wrong_key_rejected(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    body = link_body(card)
    raw = json.dumps(body).encode()
    path = f"{alice.base}/links"
    challenge = alice.challenge(path, raw)
    other = PrivateKey(secrets.token_bytes(32))
    resp = client.post(path, content=raw, headers=alice.headers(challenge, other))
    assert resp.status_code == 403
    assert client.get(f"/api/links/{body['id']}").status_code == 404


def test_create_link_unsigned_rejected(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    resp = client.post(f"{alice.base}/links", json=link_body(card))
    assert resp.status_code == 401


def test_create_link_requires_ready_card(client):
    alice = person(client, "Alice")
    card = minted_card(alice)
    assert card["status"] == "owned"
    resp = create_link(alice, link_body(card))
    assert resp.status_code == 409


def test_create_link_requires_signer_owns_card(client):
    alice = person(client, "Alice")
    mallory = person(client, "Mallory")
    card = ready_card(alice)
    body = link_body(card)
    assert create_link(mallory, body).status_code == 409
    assert client.get(f"/api/links/{body['id']}").status_code == 404


def test_create_link_unknown_card(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    resp = create_link(alice, link_body(card, card_id="nope"))
    assert resp.status_code == 409


def test_create_link_nullifier_must_match_current_showing(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    other = ready_card(alice, make_jpg(color=(1, 2, 3)))
    resp = create_link(alice, link_body(card, nullifier=nullifier(other)))
    assert resp.status_code == 409
    # Well-formed but random nullifier.
    resp = create_link(alice, link_body(card, nullifier="02" + "ab" * 47))
    assert resp.status_code == 409


def test_create_link_stale_nullifier_after_cancel(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    stale = nullifier(card)
    assert alice.cancel(card["id"]).status_code == 200
    fresh = ready_card_again(alice, card["id"])
    assert nullifier(fresh) != stale
    assert create_link(alice, link_body(fresh, nullifier=stale)).status_code == 409
    assert create_link(alice, link_body(fresh)).status_code == 200


def ready_card_again(profile: Profile, card_id: str) -> dict:
    card = next(c for c in profile.get()["cards"] if c["id"] == card_id)
    assert profile.claim(card).status_code == 200
    assert profile.export(card_id).status_code == 200
    return next(c for c in profile.get()["cards"] if c["id"] == card_id)


def test_create_link_duplicate_id_rejected(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    body = link_body(card)
    assert create_link(alice, body).status_code == 200
    again = dict(body, envelope=envelope())
    assert create_link(alice, again).status_code == 409
    # The original envelope is unchanged.
    assert (
        client.get(f"/api/links/{body['id']}").json()["envelope"] == (body["envelope"])
    )


def test_multiple_links_per_card_allowed(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    for _ in range(3):
        assert create_link(alice, link_body(card)).status_code == 200


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(id="xyz"),
        lambda b: b.update(id=b["id"].upper()),
        lambda b: b.update(id=b["id"] + "00"),
        lambda b: b.update(nullifier=b["nullifier"][:-2]),
        lambda b: b.update(nullifier="zz" * 48),
        lambda b: b.update(card_id=""),
        lambda b: b.update(card_id="c" * 65),
        lambda b: b.pop("envelope"),
        lambda b: b["envelope"].update(version=2),
        lambda b: b["envelope"].update(nonce="00" * 11),
        lambda b: b["envelope"].update(ciphertext="zz" * 40),
        lambda b: b["envelope"].update(ciphertext="ab"),
        lambda b: b["envelope"].update(ciphertext="ab" * 1100),
        lambda b: b["envelope"].update(
            kdf={"name": "PBKDF2-SHA256", "iterations": 1000, "salt": "00" * 16}
        ),
        lambda b: b["envelope"].update(
            kdf={"name": "scrypt", "iterations": 200_000, "salt": "00" * 16}
        ),
        lambda b: b["envelope"].update(
            kdf={"name": "PBKDF2-SHA256", "iterations": 200_000, "salt": "00"}
        ),
    ],
)
def test_create_link_malformed_body_rejected(client, mutate):
    alice = person(client, "Alice")
    card = ready_card(alice)
    body = link_body(card)
    mutate(body)
    assert create_link(alice, body).status_code == 400


def test_create_link_non_json_rejected(client):
    alice = person(client, "Alice")
    resp = alice.post(f"{alice.base}/links", b"not json")
    assert resp.status_code == 400


# --- get --------------------------------------------------------------------


def test_get_unknown_and_malformed_link(client):
    assert client.get(f"/api/links/{secrets.token_hex(16)}").status_code == 404
    for bad in ("xyz", "A" * 32, "0" * 31, "0" * 33, "g" * 32):
        assert client.get(f"/api/links/{bad}").status_code == 404


def test_link_status_readable_cross_origin(client):
    """Other sites may read a link's public status, but nothing else."""
    origin = {"Origin": "https://example.com"}
    alice = person(client, "Alice")
    card = ready_card(alice)
    body = link_body(card)
    raw = json.dumps(body).encode()
    path = f"{alice.base}/links"
    challenge = alice.challenge(path, raw)
    resp = client.post(
        path, content=raw, headers={**alice.headers(challenge), **origin}
    )
    assert resp.status_code == 200, resp.text
    assert "access-control-allow-origin" not in resp.headers

    for link_id, status in ((body["id"], 200), (secrets.token_hex(16), 404)):
        resp = client.get(f"/api/links/{link_id}", headers=origin)
        assert resp.status_code == status
        assert resp.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in resp.headers


# --- lifecycle --------------------------------------------------------------


def test_link_claimed_after_receiver_redeems(client):
    alice = person(client, "Alice")
    bob = person(client, "Bob")
    card, transfer = exported(alice)
    current = next(c for c in alice.get()["cards"] if c["id"] == card["id"])
    body = link_body(current)
    assert create_link(alice, body).status_code == 200

    resp = bob.receive(transfer)
    assert resp.status_code == 200, resp.text

    link = client.get(f"/api/links/{body['id']}").json()
    assert link["status"] == "claimed"
    assert link["claimed_by"] == {"pubkey": bob.pubkey, "name": "Bob"}
    assert link["envelope"] is None
    # The sender's card is gone, so no new link can be created for it.
    assert create_link(alice, link_body(current)).status_code == 409


def test_link_void_after_sender_cancel(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    body = link_body(card)
    assert create_link(alice, body).status_code == 200
    assert alice.cancel(card["id"]).status_code == 200
    link = client.get(f"/api/links/{body['id']}").json()
    assert link["status"] == "void"
    assert link["claimed_by"] is None
    assert link["envelope"] is None


def test_void_link_stays_void_after_later_transfer(client):
    """Canceling and then sending the NFT another way must not claim the old link."""
    alice = person(client, "Alice")
    bob = person(client, "Bob")
    card = ready_card(alice)
    body = link_body(card)
    assert create_link(alice, body).status_code == 200
    assert alice.cancel(card["id"]).status_code == 200
    ready_card_again(alice, card["id"])
    resp = alice.export(card["id"])
    assert resp.status_code == 200
    assert bob.receive(resp.content).status_code == 200
    link = client.get(f"/api/links/{body['id']}").json()
    assert link["status"] == "void"
    assert link["claimed_by"] is None
    assert link["envelope"] is None


def test_create_link_for_spent_credential_rejected(client):
    alice = person(client, "Alice")
    card = ready_card(alice)
    # Redeem the bearer credential directly at the mint; the portfolio card
    # stays 'ready' because nothing reconciled it yet.
    cred = alice.credentials[card["id"]]
    commitment, proof = prove_owner_secret(12345)
    resp = client.post(
        "/v1/nft/transfer",
        json={
            "presentation": present(cred, binding=commitment.format()).to_bytes().hex(),
            "new_owner_commitment": commitment.format().hex(),
            "new_proof": proof.to_bytes().hex(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = link_body(card)
    assert create_link(alice, body).status_code == 409
    assert client.get(f"/api/links/{body['id']}").status_code == 404


# --- frontend route ---------------------------------------------------------


def test_claim_page_route(client):
    assert client.get(f"/claim/{secrets.token_hex(16)}").status_code in (200, 503)
    assert client.get("/claim/xyz").status_code == 404
    assert client.get(f"/claim/{'A' * 32}").status_code == 404
