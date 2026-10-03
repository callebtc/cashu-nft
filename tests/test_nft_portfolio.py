"""Tests for the encrypted browser JPG portfolio app (cashu/nft/portfolio.py)."""

import hashlib
import io
import json
import secrets
import time
from typing import Dict, Iterator, Optional

import httpx
import pytest
from coincurve import PrivateKey, PublicKeyXOnly
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from PIL import Image

from cashu.core.crypto.bls import PublicKey, curve_order
from cashu.core.crypto.ps import (
    G_NULL,
    PS_BURN_BINDING,
    Credential,
    MintPublicKeyPS,
    blind_issue_commit,
    blind_issue_commit_v2,
    blind_transfer_commit,
    hash_asset,
    issue_commitment,
    present,
    present_private,
    present_showing,
    prove_owner_secret,
    unblind_issued,
    unblind_issued_v2,
)
from cashu.nft.imgmeta import embed_token, extract_token
from cashu.nft.portfolio import (
    auth_message,
    claim_digest,
    create_portfolio_app,
)
from cashu.nft.portfolio_jpg import normalize_jpg, split_transfer_jpg, validate_jpg
from cashu.nft.portfolio_og import profile_version
from cashu.nft.wallet import TOKEN_PREFIX, NFTClient


def make_jpg(
    width: int = 32,
    height: int = 16,
    color=(200, 30, 30),
    exif: Optional[Image.Exif] = None,
) -> bytes:
    image = Image.new("RGB", (width, height), color)
    # A non-uniform pixel keeps orientation observable after transposition.
    image.putpixel((0, 0), (0, 0, 255))
    out = io.BytesIO()
    if exif is not None:
        image.save(out, format="JPEG", quality=90, exif=exif)
    else:
        image.save(out, format="JPEG", quality=90)
    return out.getvalue()


def noisy_jpg(size: int, quality: int = 95) -> bytes:
    image = Image.frombytes("RGB", (size, size), secrets.token_bytes(size * size * 3))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Profile:
    """Python mirror of portfolio_web/src/api.mjs + crypto.mjs signing."""

    def __init__(self, client: TestClient, secret: Optional[bytes] = None):
        self.client = client
        self.secret = secret or secrets.token_bytes(32)
        self.credentials: Dict[str, Credential] = {}
        self.key = PrivateKey(self.secret)
        self.pubkey = PublicKeyXOnly.from_secret(self.secret).format().hex()

    def sign(self, message: str, key: Optional[PrivateKey] = None) -> str:
        digest = hashlib.sha256(message.encode()).digest()
        return (key or self.key).sign_schnorr(digest).hex()

    def challenge(self, path: str, body: bytes = b"") -> dict:
        resp = self.client.post(
            "/api/auth/challenge",
            json={
                "pubkey": self.pubkey,
                "method": "POST",
                "path": path,
                "body_hash": sha(body),
            },
        )
        assert resp.status_code == 200, resp.text
        challenge = resp.json()
        expected = auth_message(
            self.pubkey,
            "POST",
            path,
            sha(body),
            challenge["nonce"],
            challenge["expires"],
        )
        assert challenge["message"] == expected
        return challenge

    def headers(self, challenge: dict, key: Optional[PrivateKey] = None) -> dict:
        return {
            "X-Portfolio-Challenge": challenge["nonce"],
            "X-Portfolio-Signature": self.sign(challenge["message"], key),
        }

    def post(self, path: str, body: bytes = b""):
        challenge = self.challenge(path, body)
        return self.client.post(path, content=body, headers=self.headers(challenge))

    @property
    def base(self) -> str:
        return f"/api/profiles/{self.pubkey}"

    def create(self, name: str = "Collector") -> dict:
        resp = self.post(self.base, f'{{"name": "{name}"}}'.encode())
        assert resp.status_code == 200, resp.text
        return resp.json()

    def json_post(self, path, value):
        return self.post(path, json.dumps(value).encode())

    def prepare(self, kind, jpg=b"", title="Art", card_id=None, issuance_version=3):
        suffix = f"&card_id={card_id}" if card_id else ""
        if kind == "mint":
            suffix += f"&issuance_version={issuance_version}"
        return self.post(
            f"{self.base}/wallet/prepare?kind={kind}&title={title}{suffix}", jpg
        )

    def encrypt(self, value):
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(hashlib.sha256(self.secret).digest()).encrypt(
            nonce, json.dumps(value).encode(), None
        )
        return {"version": 1, "nonce": nonce.hex(), "ciphertext": ciphertext.hex()}

    def finish(self, stage, s, t, u, request):
        root = f"{self.base}/wallet/operations/{stage['id']}"
        backup = self.json_post(
            root + "/backup", self.encrypt({"s": s, "t": t, "request": request})
        )
        assert backup.status_code == 200, backup.text
        response = self.json_post(root + "/finish", request)
        if response.status_code != 200:
            return response
        config = self.client.get("/api/config").json()
        keyset = MintPublicKeyPS.from_bytes(bytes.fromhex(config["public_key"]))
        raw = response.json()
        if request.get("version") in (2, 3):
            u = PublicKey(compressed=bytes.fromhex(raw["u"]), group="G1")
        raw_v = PublicKey(compressed=bytes.fromhex(raw["v"]), group="G1")
        cred = Credential(
            u,
            raw_v
            if request.get("version") == 3
            else unblind_issued_v2(raw_v, t, u)
            if request.get("version") == 2
            else unblind_issued(raw_v, t, keyset),
            int(stage["h"], 16),
            s,
            keyset.keyset_id,
        )
        context = f"Cashu_NFT_Portfolio_Show_v1\n{self.pubkey}\n{stage['h']}\n{keyset.keyset_id}".encode()
        showing = (
            "pshow1"
            + (
                len(context).to_bytes(2, "big")
                + context
                + present_showing(cred, context).to_bytes()
            ).hex()
        )
        published = self.json_post(
            root + "/publish",
            {
                "encrypted_credential": self.encrypt(
                    {"credential": cred.to_bytes().hex()}
                ),
                "showing": showing,
                "signature": self.key.sign_schnorr(claim_digest(showing)).hex(),
            },
        )
        if published.status_code == 200:
            self.credentials[published.json()["id"]] = cred
        return published

    def mint(self, jpg: bytes, title: str = "Art", issuance_version=3):
        response = self.prepare("mint", jpg, title, issuance_version=issuance_version)
        if response.status_code != 200:
            return response
        stage = response.json()
        config = self.client.get("/api/config").json()
        keyset = MintPublicKeyPS.from_bytes(bytes.fromhex(config["public_key"]))
        s = secrets.randbelow(curve_order - 1) + 1
        u = None
        if issuance_version == 3:
            assert stage["begin"] is None
            t = None
            D, B, proof = issue_commitment(
                keyset, int(stage["h"], 16), s, bytes.fromhex(stage["id"])
            )
        elif issuance_version == 2:
            assert stage["begin"] is None
            D, B, t, proof = blind_issue_commit_v2(
                keyset, int(stage["h"], 16), s, bytes.fromhex(stage["id"])
            )
        else:
            u = PublicKey(compressed=bytes.fromhex(stage["begin"]["u"]), group="G1")
            D, B, t, proof = blind_issue_commit(
                keyset, int(stage["h"], 16), s, u, bytes.fromhex(stage["id"])
            )
        S, _ = prove_owner_secret(s)
        return self.finish(
            stage,
            s,
            t,
            u,
            {
                "session": stage["id"],
                **({"version": issuance_version} if issuance_version != 1 else {}),
                "asset_tag": D.format().hex(),
                "b": B.format().hex(),
                "owner_commitment": S.format().hex(),
                "proof": proof.to_bytes().hex(),
            },
        )

    def swap(self, stage, cred):
        begin = self.client.post(
            "/v1/nft/transfer/private/begin",
            json={"nullifier": (G_NULL * cred.s).format().hex()},
        )
        if begin.status_code != 200:
            return begin
        config = self.client.get("/api/config").json()
        keyset = MintPublicKeyPS.from_bytes(bytes.fromhex(config["public_key"]))
        s = secrets.randbelow(curve_order - 1) + 1
        S, owner_proof = prove_owner_secret(s)
        pres, o = present_private(keyset, cred, binding=S.format())
        u = PublicKey(compressed=bytes.fromhex(begin.json()["u"]), group="G1")
        B, t, proof = blind_transfer_commit(
            keyset, cred.h, o, pres.kappa_h, u, binding=S.format()
        )
        return self.finish(
            stage,
            s,
            t,
            u,
            {
                "presentation": pres.to_bytes().hex(),
                "b": B.format().hex(),
                "proof": proof.to_bytes().hex(),
                "new_owner_commitment": S.format().hex(),
                "new_proof": owner_proof.to_bytes().hex(),
            },
        )

    def claim(self, card: dict):
        body = (
            '{"signature":"%s","showing":"%s"}'
            % (
                self.key.sign_schnorr(claim_digest(card["showing"])).hex(),
                card["showing"],
            )
        ).encode()
        return self.post(f"{self.base}/cards/{card['id']}/claim", body)

    def export(self, card_id: str):
        response = self.post(f"{self.base}/wallet/cards/{card_id}/ready")
        if response.status_code != 200:
            return response
        card = response.json()
        jpg = self.client.get(f"/api/images/{card['h']}.jpg").content
        return httpx.Response(
            200,
            content=embed_token(
                jpg, TOKEN_PREFIX + self.credentials[card_id].to_bytes().hex()
            ),
            headers={"content-type": "image/jpeg"},
        )

    def cancel(self, card_id: str):
        response = self.prepare("rotate", card_id=card_id)
        return (
            self.swap(response.json(), self.credentials[card_id])
            if response.status_code == 200
            else response
        )

    def receive(self, jpg: bytes, title: str = "Got"):
        try:
            clean, token = split_transfer_jpg(jpg)
            if token is None:
                raise ValueError("No transfer token")
            cred = NFTClient.decode_token(token)
            if cred.h != hash_asset(clean):
                raise ValueError("Wrong JPG. Nothing was redeemed")
        except ValueError as error:
            return httpx.Response(400, json={"detail": str(error)})
        state = self.client.post(
            "/v1/nft/checkstate",
            json={"nullifiers": [(G_NULL * cred.s).format().hex()]},
        ).json()
        if state["states"][0]["state"] != "UNSPENT":
            return httpx.Response(409, json={"detail": "Already spent"})
        response = self.prepare("receive", clean, title)
        return (
            self.swap(response.json(), cred)
            if response.status_code == 200
            else response
        )

    def get(self) -> dict:
        resp = self.client.get(self.base)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def mine(self) -> dict:
        """The owner's view: the public profile plus pending transfers."""
        data = self.get()
        resp = self.post(f"{self.base}/cards/pending")
        assert resp.status_code == 200, resp.text
        pending = set(resp.json()["ids"])
        for c in data["cards"]:
            if c["id"] in pending:
                c["status"] = "ready"
        return data


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    with TestClient(create_portfolio_app(str(tmp_path / "portfolio"))) as c:
        yield c


def minted_card(profile: Profile, jpg: Optional[bytes] = None) -> dict:
    resp = profile.mint(jpg or make_jpg())
    assert resp.status_code == 200, resp.text
    return resp.json()


def exported(profile: Profile, jpg: Optional[bytes] = None) -> tuple:
    card = minted_card(profile, jpg)
    assert profile.claim(card).status_code == 200
    resp = profile.export(card["id"])
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/jpeg"
    return card, resp.content


# --- (1) signed auth ------------------------------------------------------


def test_auth_valid_signature_creates_profile(client):
    alice = Profile(client)
    profile = alice.create("Alice")
    assert profile["pubkey"] == alice.pubkey
    assert profile["name"] == "Alice"
    assert profile["cards"] == []


def test_auth_wrong_key_rejected(client):
    alice = Profile(client)
    body = b'{"name":"x"}'
    challenge = alice.challenge(alice.base, body)
    other = PrivateKey(secrets.token_bytes(32))
    resp = client.post(
        alice.base, content=body, headers=alice.headers(challenge, other)
    )
    assert resp.status_code == 403
    assert client.get(alice.base).status_code == 404


def test_auth_challenge_for_other_profile_rejected(client):
    alice, bob = Profile(client), Profile(client)
    resp = client.post(
        "/api/auth/challenge",
        json={
            "pubkey": bob.pubkey,
            "method": "POST",
            "path": alice.base,
            "body_hash": sha(b""),
        },
    )
    assert resp.status_code == 400


def test_auth_replayed_challenge_rejected(client):
    alice = Profile(client)
    body = b'{"name":"x"}'
    challenge = alice.challenge(alice.base, body)
    headers = alice.headers(challenge)
    assert client.post(alice.base, content=body, headers=headers).status_code == 200
    assert client.post(alice.base, content=body, headers=headers).status_code == 401


def test_auth_expired_challenge_rejected(client, monkeypatch):
    alice = Profile(client)
    body = b'{"name":"x"}'
    challenge = alice.challenge(alice.base, body)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 121)
    resp = client.post(alice.base, content=body, headers=alice.headers(challenge))
    assert resp.status_code == 401


def test_auth_altered_body_rejected(client):
    alice = Profile(client)
    challenge = alice.challenge(alice.base, b'{"name":"x"}')
    resp = client.post(
        alice.base, content=b'{"name":"y"}', headers=alice.headers(challenge)
    )
    assert resp.status_code == 403
    assert client.get(alice.base).status_code == 404


def test_auth_altered_path_rejected(client):
    alice = Profile(client)
    alice.create()
    jpg = make_jpg()
    # Signed for profile creation, replayed against the mint endpoint.
    challenge = alice.challenge(alice.base, jpg)
    resp = client.post(
        f"{alice.base}/mint", content=jpg, headers=alice.headers(challenge)
    )
    assert resp.status_code == 403
    # Query string is part of the signed path.
    challenge = alice.challenge(f"{alice.base}/mint?title=a", jpg)
    resp = client.post(
        f"{alice.base}/mint?title=b", content=jpg, headers=alice.headers(challenge)
    )
    assert resp.status_code == 403
    assert alice.get()["cards"] == []


def test_auth_missing_or_garbage_signature_rejected(client):
    alice = Profile(client)
    challenge = alice.challenge(alice.base, b"{}")
    assert client.post(alice.base, content=b"{}").status_code == 401
    headers = {
        "X-Portfolio-Challenge": challenge["nonce"],
        "X-Portfolio-Signature": "zz",
    }
    assert client.post(alice.base, content=b"{}", headers=headers).status_code == 403


# --- claim ----------------------------------------------------------------


def test_claim_requires_matching_profile_signature(client):
    alice = Profile(client)
    alice.create()
    card = minted_card(alice)
    assert card["signature"]  # browser signs before publishing
    bad_sig = (
        PrivateKey(secrets.token_bytes(32))
        .sign_schnorr(claim_digest(card["showing"]))
        .hex()
    )
    body = ('{"signature":"%s","showing":"%s"}' % (bad_sig, card["showing"])).encode()
    assert alice.post(f"{alice.base}/cards/{card['id']}/claim", body).status_code == 403
    resp = alice.claim(card)
    assert resp.status_code == 200, resp.text
    assert resp.json()["signature"]


# --- (2) public endpoints leak no secrets ---------------------------------


def test_public_endpoints_never_expose_credentials(client):
    alice = Profile(client)
    alice.create()
    card, transfer = exported(alice)
    _, token = split_transfer_jpg(transfer)
    assert token is not None and token.startswith(TOKEN_PREFIX)
    cred_hex = token[len(TOKEN_PREFIX) :]
    resp = client.get(alice.base)
    text = resp.text
    assert TOKEN_PREFIX not in text and cred_hex not in text
    for c in resp.json()["cards"]:
        assert "credential" not in c
        assert set(c) == {
            "id",
            "pubkey",
            "h",
            "title",
            "showing",
            "signature",
            "status",
            "created",
            "sent",
            "custody",
        }
    image = client.get(f"/api/images/{card['h']}.jpg")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert TOKEN_PREFIX.encode() not in image.content
    assert extract_token(image.content) is None
    assert bytes.fromhex(cred_hex) not in image.content
    assert resp.headers["cache-control"] == "no-store"
    assert client.get("/api/images/" + "0" * 64 + ".jpg").status_code == 404
    assert client.get("/api/images/XYZ.jpg").status_code == 404


def test_minting_routes_not_exposed(client):
    assert client.post("/v1/nft/mint", json={}).status_code in (404, 405)
    assert client.post("/v1/nft/mint/quote", json={}).status_code in (404, 405)


# --- (3) JPG validation and normalization ---------------------------------


def test_rejects_non_jpg_and_malformed(client):
    alice = Profile(client)
    alice.create()
    png = io.BytesIO()
    Image.new("RGB", (8, 8)).save(png, format="PNG")
    assert alice.mint(png.getvalue()).status_code == 400
    assert alice.mint(b"\xff\xd8not really a jpeg").status_code == 400
    assert alice.mint(b"").status_code == 400
    # Header intact, scan data truncated: verify() passes, decoding fails.
    jpg = noisy_jpg(128)
    assert alice.mint(jpg[: len(jpg) // 2]).status_code == 400
    assert alice.mint(jpg[:-2]).status_code == 400
    assert alice.get()["cards"] == []


def test_validate_jpg_rejects_png_directly():
    png = io.BytesIO()
    Image.new("RGB", (8, 8)).save(png, format="PNG")
    with pytest.raises(ValueError):
        validate_jpg(png.getvalue())


def test_normalize_applies_orientation_and_strips_metadata():
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90 CW
    exif[0x010F] = "SecretCamMaker"  # Make
    exif[0x0131] = "leaky-software"  # Software
    src = make_jpg(40, 20, exif=exif)
    with Image.open(io.BytesIO(src)) as check:
        assert check.getexif().get(0x0112) == 6
    out = normalize_jpg(src)
    with Image.open(io.BytesIO(out)) as image:
        assert image.size == (20, 40)
        assert not image.getexif()
        assert "exif" not in image.info
    assert b"SecretCamMaker" not in out and b"leaky-software" not in out
    assert normalize_jpg(src) == out  # deterministic


def test_normalize_rejects_transfer_jpg():
    base = normalize_jpg(make_jpg())
    with pytest.raises(ValueError):
        normalize_jpg(embed_token(base, TOKEN_PREFIX + "00" * 10))


# --- (4) transfer envelope byte identity -----------------------------------


def test_envelope_embed_and_remove_preserves_bytes():
    exif = Image.Exif()
    exif[0x010F] = "UserMeta"
    for base in (normalize_jpg(make_jpg()), make_jpg(exif=exif)):
        token = TOKEN_PREFIX + "ab" * 50
        wrapped = embed_token(base, token)
        assert wrapped != base
        stripped, found = split_transfer_jpg(wrapped)
        assert found == token
        assert stripped == base
        assert split_transfer_jpg(base) == (base, None)


def test_envelope_duplicate_rejected():
    base = normalize_jpg(make_jpg())
    token = TOKEN_PREFIX + "cd" * 20
    once = embed_token(base, token)
    segment = once[2 : len(once) - len(base) + 2]
    twice = once[:2] + segment + once[2:]
    with pytest.raises(ValueError):
        split_transfer_jpg(twice)


def test_exported_jpg_strips_to_public_image(client):
    alice = Profile(client)
    alice.create()
    card, transfer = exported(alice)
    public = client.get(f"/api/images/{card['h']}.jpg").content
    stripped, token = split_transfer_jpg(transfer)
    assert stripped == public
    assert token is not None
    assert hash_asset(public).to_bytes(32, "big").hex() == card["h"]
    assert NFTClient.decode_token(token).h == hash_asset(public)


# --- (5) duplicate mint ------------------------------------------------------


def test_duplicate_mint_rejected(client):
    alice, bob = Profile(client), Profile(client)
    alice.create()
    bob.create()
    jpg = make_jpg()
    minted_card(alice, jpg)
    assert alice.mint(jpg).status_code == 409
    assert bob.mint(jpg).status_code == 409
    assert len(alice.get()["cards"]) == 1
    assert bob.get()["cards"] == []


# --- (6)(7) full flow and double redemption ----------------------------------


def test_full_transfer_flow_and_double_redeem(client):
    alice, bob, carol = Profile(client), Profile(client), Profile(client)
    for p in (alice, bob, carol):
        p.create()
    card, transfer = exported(alice)
    assert alice.mine()["cards"][0]["status"] == "ready"
    # A pending transfer is the owner's business: the public profile shows it as owned.
    assert alice.get()["cards"][0]["status"] == "owned"
    assert alice.client.post(f"{alice.base}/cards/pending").status_code >= 400
    explore = alice.client.get("/api/explore/nfts").json()["items"]
    assert {c["status"] for c in explore if c["pubkey"] == alice.pubkey} == {"owned"}

    resp = bob.receive(transfer)
    assert resp.status_code == 200, resp.text
    received = resp.json()
    assert received["h"] == card["h"] and received["status"] == "owned"
    assert received["pubkey"] == bob.pubkey

    sender = alice.get()["cards"]
    assert [c["status"] for c in sender] == ["sent"]
    assert sender[0]["sent"] is not None
    receiver = bob.get()["cards"]
    assert [c["id"] for c in receiver] == [received["id"]]

    # Second redemption (same or other receiver) fails and changes nothing.
    assert bob.receive(transfer).status_code == 409
    assert carol.receive(transfer).status_code == 409
    assert carol.get()["cards"] == []
    assert len(bob.get()["cards"]) == 1

    # Old owner can no longer act on the card.
    assert alice.export(card["id"]).status_code == 409
    assert alice.cancel(card["id"]).status_code == 409

    # Bob can pass it on.
    assert bob.claim(received).status_code == 200
    resp = bob.export(received["id"])
    assert resp.status_code == 200
    assert carol.receive(resp.content).status_code == 200
    assert [c["status"] for c in bob.get()["cards"]] == ["sent"]
    assert [c["status"] for c in carol.get()["cards"]] == ["owned"]


def test_receive_plain_jpg_without_token_rejected(client):
    alice, bob = Profile(client), Profile(client)
    alice.create()
    bob.create()
    card = minted_card(alice)
    public = client.get(f"/api/images/{card['h']}.jpg").content
    assert bob.receive(public).status_code == 400


def test_receive_requires_existing_profile(client):
    alice, bob = Profile(client), Profile(client)
    alice.create()
    card, transfer = exported(alice)
    assert bob.receive(transfer).status_code == 404
    # Nothing spent: alice's export remains redeemable after bob signs up.
    bob.create()
    assert bob.receive(transfer).status_code == 200


def test_bearer_redemption_reconciles_sender(client):
    """A transfer JPG redeemed via the public /v1/nft/transfer route marks the card sent."""
    alice = Profile(client)
    alice.create()
    card, transfer = exported(alice)
    _, token = split_transfer_jpg(transfer)
    assert token is not None
    cred = NFTClient.decode_token(token)
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
    assert [c["status"] for c in alice.get()["cards"]] == ["sent"]
    assert alice.cancel(card["id"]).status_code == 409


# --- (8) cancel ------------------------------------------------------------


def test_cancel_invalidates_exported_jpg(client):
    alice, bob = Profile(client), Profile(client)
    alice.create()
    bob.create()
    card, transfer = exported(alice)
    resp = alice.cancel(card["id"])
    assert resp.status_code == 200, resp.text
    cancelled = resp.json()
    assert cancelled["status"] == "owned" and cancelled["signature"] is not None
    assert cancelled["showing"] != card["showing"]
    assert alice.cancel(card["id"]).status_code == 409

    assert bob.receive(transfer).status_code == 409
    assert bob.get()["cards"] == []
    assert [c["status"] for c in alice.get()["cards"]] == ["owned"]

    # Alice can re-claim and export a fresh, valid transfer.
    assert alice.claim(cancelled).status_code == 200
    fresh = alice.export(card["id"])
    assert fresh.status_code == 200
    assert fresh.content != transfer
    assert bob.receive(fresh.content).status_code == 200


def burn(
    profile: Profile,
    card_id: str,
    binding: bytes = PS_BURN_BINDING,
    cred: Optional[Credential] = None,
):
    pres = present(cred or profile.credentials[card_id], binding=binding)
    return profile.json_post(
        f"{profile.base}/wallet/cards/{card_id}/delete",
        {"presentation": pres.to_bytes().hex()},
    )


def test_delete_burns_nft_and_erases_jpg(client):
    alice, bob = Profile(client), Profile(client)
    alice.create()
    bob.create()
    jpg = make_jpg()
    card, transfer = exported(alice, jpg)
    keep = minted_card(alice, make_jpg(color=(20, 160, 40)))
    # Only the burn binding works, and only the owner can ask.
    assert burn(alice, card["id"], binding=b"not a burn").status_code != 200
    stolen = alice.credentials[card["id"]]
    assert burn(bob, card["id"], cred=stolen).status_code == 404
    assert [c["id"] for c in alice.get()["cards"]] == [keep["id"], card["id"]]

    resp = burn(alice, card["id"])
    assert resp.status_code == 200, resp.text
    assert [c["id"] for c in alice.get()["cards"]] == [keep["id"]]
    assert client.get(f"/api/images/{card['h']}.jpg").status_code == 404
    assert client.get(f"/api/images/{keep['h']}.jpg").status_code == 200
    # The pending transfer JPG died with it, and the JPG can't come back.
    assert bob.receive(transfer).status_code != 200
    assert bob.get()["cards"] == []
    remint = alice.mint(jpg)
    assert remint.status_code == 409 and "deleted" in remint.json()["detail"]
    assert burn(alice, card["id"]).status_code == 404


def test_delete_needs_the_current_credential(client):
    alice = Profile(client)
    alice.create()
    card = minted_card(alice)
    other = minted_card(alice, make_jpg(color=(20, 160, 40)))
    alice.credentials[card["id"]] = alice.credentials[other["id"]]
    assert burn(alice, card["id"]).status_code == 403
    assert len(alice.get()["cards"]) == 2


# --- (9) image/token mismatch -------------------------------------------------


def test_image_token_mismatch_rejected_before_spending(client):
    alice, bob = Profile(client), Profile(client)
    alice.create()
    bob.create()
    card, transfer = exported(alice)
    _, token = split_transfer_jpg(transfer)
    assert token is not None
    other = normalize_jpg(make_jpg(color=(10, 200, 10)))
    forged = embed_token(other, token)
    resp = bob.receive(forged)
    assert resp.status_code == 400
    assert "Nothing was redeemed" in resp.json()["detail"]
    assert bob.get()["cards"] == []
    assert [c["status"] for c in alice.mine()["cards"]] == ["ready"]
    # The genuine transfer JPG is still redeemable.
    assert bob.receive(transfer).status_code == 200


def test_receive_garbage_token_rejected(client):
    alice = Profile(client)
    alice.create()
    base = normalize_jpg(make_jpg())
    assert alice.receive(embed_token(base, TOKEN_PREFIX + "zz")).status_code == 400
    assert alice.receive(embed_token(base, TOKEN_PREFIX + "00" * 8)).status_code == 400


# --- (10) quotas -----------------------------------------------------------------


def test_collections_are_unlimited_by_default(client):
    assert client.get("/api/config").json()["max_cards"] is None
    alice = Profile(client)
    alice.create()
    for i in range(3):
        minted_card(alice, make_jpg(color=(10 + i, 20, 30)))
    assert len(alice.get()["cards"]) == 3


def test_max_cards_limit(tmp_path):
    app = create_portfolio_app(str(tmp_path / "p"), max_cards=1)
    with TestClient(app) as client:
        alice, bob = Profile(client), Profile(client)
        alice.create()
        bob.create()
        minted_card(alice, make_jpg(color=(1, 2, 3)))
        assert alice.mint(make_jpg(color=(4, 5, 6))).status_code == 409
        assert len(alice.get()["cards"]) == 1
        # Receiving also counts against the limit, and must not spend.
        card, transfer = exported(bob, make_jpg(color=(7, 8, 9)))
        assert alice.receive(transfer).status_code == 409
        assert [c["status"] for c in bob.mine()["cards"]] == ["ready"]


def test_max_jpg_bytes_limit(tmp_path):
    small = make_jpg(8, 8)
    app = create_portfolio_app(str(tmp_path / "p"), max_jpg_bytes=len(small) + 10)
    with TestClient(app) as client:
        alice = Profile(client)
        alice.create()
        assert alice.mint(noisy_jpg(128)).status_code == 413
        assert alice.get()["cards"] == []


def test_normalized_jpg_over_limit_rejected(tmp_path):
    # Upload fits, but the q95 re-encode grows beyond the limit.
    src = noisy_jpg(64, quality=30)
    assert len(normalize_jpg(src)) > len(src)
    app = create_portfolio_app(str(tmp_path / "p"), max_jpg_bytes=len(src))
    with TestClient(app) as client:
        alice = Profile(client)
        alice.create()
        assert alice.mint(src).status_code == 413


def test_storage_limit(tmp_path):
    app = create_portfolio_app(str(tmp_path / "p"), max_storage_bytes=1)
    with TestClient(app) as client:
        alice = Profile(client)
        alice.create()
        assert alice.mint(make_jpg()).status_code == 507
        assert alice.get()["cards"] == []


def test_mint_without_profile_rejected(client):
    alice = Profile(client)
    assert alice.mint(make_jpg()).status_code == 404


# --- (11) persistence ------------------------------------------------------------


def test_restart_persistence(tmp_path):
    data_dir = str(tmp_path / "p")
    with TestClient(create_portfolio_app(data_dir)) as client:
        keyset = client.get("/api/config").json()["keyset_id"]
        alice = Profile(client)
        alice.create("Persisted")
        card, transfer = exported(alice)
        secret = alice.secret
    with TestClient(create_portfolio_app(data_dir)) as client:
        assert client.get("/api/config").json()["keyset_id"] == keyset
        alice = Profile(client, secret)
        profile = alice.get()
        assert profile["name"] == "Persisted"
        assert [c["id"] for c in profile["cards"]] == [card["id"]]
        assert client.get(f"/api/images/{card['h']}.jpg").status_code == 200
        bob = Profile(client)
        bob.create()
        assert bob.receive(transfer).status_code == 200
    with TestClient(create_portfolio_app(str(tmp_path / "other"))) as client:
        assert client.get("/api/config").json()["keyset_id"] != keyset


def test_new_cards_persist_only_encrypted_credentials(client):
    alice = Profile(client)
    alice.create()
    card = minted_card(alice)
    rows = client.portal.call(
        client.app.state.portfolio.db.fetchall, "SELECT * FROM portfolio_cards"
    )
    assert rows[0]["credential"] is None
    assert rows[0]["encrypted_credential"]
    raw = alice.credentials[card["id"]].to_bytes().hex()
    assert raw not in rows[0]["encrypted_credential"]
    assert card["custody"] == "browser"
    assert "encrypted_credential" not in card
    backups = alice.post(alice.base + "/wallet/recover").json()
    assert backups["cards"][0]["id"] == card["id"]
    assert backups["operations"] == []
    bob = Profile(client)
    bob.create()
    assert bob.post(bob.base + "/wallet/recover").json()["cards"] == []


@pytest.mark.parametrize("issuance_version", [1, 2, 3])
def test_interrupted_publication_recovers_exact_issued_response(
    client, monkeypatch, issuance_version
):
    alice = Profile(client)
    alice.create()
    original = alice.json_post
    saved = {}

    def interrupt(path, body):
        if path.endswith("/finish"):
            saved["finish_path"], saved["finish_body"] = path, body
            response = original(path, body)
            saved["response"] = response.json()
            return response
        if path.endswith("/publish"):
            saved["publish_path"], saved["publish_body"] = path, body
            return httpx.Response(503, json={"detail": "simulated connection loss"})
        return original(path, body)

    monkeypatch.setattr(alice, "json_post", interrupt)
    assert alice.mint(make_jpg(), issuance_version=issuance_version).status_code == 503
    assert alice.get()["cards"] == []
    backups = alice.post(alice.base + "/wallet/recover").json()
    assert len(backups["operations"]) == 1
    assert backups["operations"][0]["state"] == "issued"
    root = saved["finish_path"].removesuffix("/finish")
    assert alice.post(root + "/discard").status_code == 409
    retry = original(saved["finish_path"], saved["finish_body"])
    assert retry.json() == saved["response"]
    altered = {**saved["finish_body"], "proof": "00" * 128}
    assert original(saved["finish_path"], altered).status_code == 409
    published = original(saved["publish_path"], saved["publish_body"])
    assert published.status_code == 200
    assert (
        original(saved["publish_path"], saved["publish_body"]).json()
        == published.json()
    )
    assert alice.post(alice.base + "/wallet/recover").json()["operations"] == []


def test_legacy_migration_rotates_away_backend_known_secret(client):
    alice = Profile(client)
    alice.create()
    portfolio = client.app.state.portfolio
    legacy = client.portal.call(portfolio.mint, alice.pubkey, make_jpg(), "Legacy")
    stage = alice.prepare("migrate", card_id=legacy["id"])
    assert stage.status_code == 200
    old = NFTClient.decode_token(stage.json()["legacy_token"])
    migrated = alice.swap(stage.json(), old)
    assert migrated.status_code == 200, migrated.text
    assert migrated.json()["id"] == legacy["id"]
    assert migrated.json()["custody"] == "browser"
    assert migrated.json()["signature"]
    states = client.post(
        "/v1/nft/checkstate", json={"nullifiers": [(G_NULL * old.s).format().hex()]}
    ).json()
    assert states["states"][0]["state"] == "SPENT"
    rows = client.portal.call(portfolio.db.fetchall, "SELECT * FROM portfolio_cards")
    assert len(rows) == 1 and rows[0]["credential"] is None
    assert alice.prepare("migrate", card_id=legacy["id"]).status_code == 409


def test_server_refuses_transfer_tokens_and_custodial_wallet_routes(client):
    alice = Profile(client)
    alice.create()
    card, transfer = exported(alice)
    assert alice.prepare("receive", transfer).status_code == 400
    for suffix in (
        "mint",
        "receive",
        f"cards/{card['id']}/export",
        f"cards/{card['id']}/cancel",
    ):
        assert alice.post(alice.base + "/" + suffix).status_code == 410


def test_proofs_require_encrypted_backup_and_operation_owner(client):
    alice, bob = Profile(client), Profile(client)
    alice.create()
    bob.create()
    stage = alice.prepare("mint", make_jpg()).json()
    path = "/wallet/operations/" + stage["id"]
    proof = {"b": "invalid", "proof": "invalid"}
    assert alice.json_post(alice.base + path + "/finish", proof).status_code == 409
    assert alice.post(alice.base + path + "/discard").status_code == 200
    assert alice.json_post(alice.base + path + "/finish", proof).status_code == 404
    stage = alice.prepare("mint", make_jpg()).json()
    path = "/wallet/operations/" + stage["id"]
    envelope = alice.encrypt({"recovery": "data"})
    assert bob.json_post(bob.base + path + "/backup", envelope).status_code == 404
    assert (
        alice.json_post(
            alice.base + path + "/backup", {**envelope, "s": "secret"}
        ).status_code
        == 400
    )


# --- profile pictures ---------------------------------------------------------------


def test_profile_picture_is_scaled_stripped_and_owner_only(client):
    alice, bob = Profile(client), Profile(client)
    alice.create()
    bob.create()
    big = io.BytesIO()
    Image.new("RGB", (2000, 1200), (200, 40, 40)).save(big, "PNG", pnginfo=None)
    resp = alice.post(f"{alice.base}/avatar", big.getvalue())
    assert resp.status_code == 200, resp.text
    version = resp.json()["avatar"]
    assert version
    stored = client.get(f"/api/avatars/{alice.pubkey}.jpg")
    assert stored.status_code == 200 and stored.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(stored.content)) as image:
        assert image.size == (256, 256) and image.format == "JPEG"
        assert not image.getexif()
    lookup = client.get(f"/api/avatars?pubkeys={alice.pubkey},{bob.pubkey}").json()
    assert lookup == {alice.pubkey: version}
    # Over 5 MB is refused before decoding; non-images are refused cleanly.
    assert (
        alice.post(f"{alice.base}/avatar", b"\0" * (5 * 1024 * 1024 + 1)).status_code
        == 413
    )
    assert alice.post(f"{alice.base}/avatar", b"not an image").status_code == 400
    # Only the owner can set or remove it.
    assert (
        client.post(f"{alice.base}/avatar", content=big.getvalue()).status_code >= 400
    )
    assert alice.post(f"{alice.base}/avatar/remove").json()["avatar"] is None
    assert client.get(f"/api/avatars/{alice.pubkey}.jpg").status_code == 404


def test_collection_preview_image(client):
    alice = Profile(client)
    alice.create()
    minted_card(alice, make_jpg(color=(30, 120, 200)))
    assert client.get(f"/api/og/p/{'ab' * 32}.jpg").status_code == 404

    stale = client.get(f"/api/og/p/{alice.pubkey}.jpg?v=old")
    assert stale.status_code == 200
    assert stale.headers["content-type"] == "image/jpeg"
    assert stale.headers["cache-control"] == "public, max-age=300"
    with Image.open(io.BytesIO(stale.content)) as image:
        assert (image.format, image.size) == ("JPEG", (1200, 630))

    version = profile_version(client.get(f"/api/profiles/{alice.pubkey}").json())
    current = client.get(f"/api/og/p/{alice.pubkey}.jpg?v={version}")
    assert current.content == stale.content  # served from the render cache
    assert "immutable" in current.headers["cache-control"]
