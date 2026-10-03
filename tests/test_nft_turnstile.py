"""Cloudflare Turnstile on NFT portfolio image uploads (cashu/nft/turnstile.py)."""

from typing import Iterator, List, Optional
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from cashu.nft.portfolio import create_portfolio_app
from cashu.nft.turnstile import (
    FAILED_MESSAGE,
    SITEVERIFY_URL,
    UNAVAILABLE_MESSAGE,
    configure_turnstile,
)
from tests.test_nft_moderation import art
from tests.test_nft_portfolio import Profile

SITEKEY = "1x00000000000000000000BB"
SECRET = "test-secret"


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    app = create_portfolio_app(
        str(tmp_path / "portfolio"),
        turnstile_sitekey=SITEKEY,
        turnstile_secret=SECRET,
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def siteverify() -> Iterator[List[dict]]:
    """Cloudflare's siteverify: the token "good" passes, anything else fails."""
    calls: List[dict] = []

    def answer(request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        calls.append(form)
        ok = form.get("secret") == SECRET and form.get("response") == "good"
        return httpx.Response(
            200,
            json={
                "success": ok,
                "error-codes": [] if ok else ["invalid-input-response"],
            },
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.post(SITEVERIFY_URL).mock(side_effect=answer)
        yield calls


def upload(profile: Profile, path: str, body: bytes, token: Optional[str]):
    challenge = profile.challenge(path, body)
    headers = profile.headers(challenge)
    if token is not None:
        headers["X-Turnstile-Token"] = token
    return profile.client.post(path, content=body, headers=headers)


def mint_path(profile: Profile) -> str:
    return f"{profile.base}/wallet/prepare?kind=mint&title=Art&issuance_version=3"


def test_config_and_csp_announce_turnstile(client):
    response = client.get("/api/config")
    assert response.json()["turnstile_sitekey"] == SITEKEY
    policy = response.headers["Content-Security-Policy"]
    assert "script-src 'self' https://challenges.cloudflare.com" in policy
    assert "frame-src 'self' https://challenges.cloudflare.com" in policy


def test_mint_needs_a_valid_token(client, siteverify):
    alice = Profile(client)
    alice.create()
    for token in (None, "", "x" * 2049):
        response = upload(alice, mint_path(alice), art(1), token)
        assert response.status_code == 403
        assert response.json()["detail"] == FAILED_MESSAGE
    assert siteverify == []
    assert upload(alice, mint_path(alice), art(1), "bad").status_code == 403
    assert upload(alice, mint_path(alice), art(1), "good").status_code == 200
    assert [call["response"] for call in siteverify] == ["bad", "good"]
    assert siteverify[-1]["remoteip"] == "testclient"


def test_receive_and_profile_picture_need_a_token(client, siteverify):
    alice = Profile(client)
    alice.create()
    receive = f"{alice.base}/wallet/prepare?kind=receive&title=Got"
    assert upload(alice, receive, art(2), None).status_code == 403
    assert upload(alice, f"{alice.base}/avatar", art(3), None).status_code == 403
    assert alice.get()["avatar"] is None
    assert upload(alice, f"{alice.base}/avatar", art(3), "good").status_code == 200


def test_unsigned_uploads_never_reach_cloudflare(client, siteverify):
    alice = Profile(client)
    alice.create()
    path = mint_path(alice)
    response = client.post(path, content=art(1), headers={"X-Turnstile-Token": "good"})
    assert response.status_code in (401, 403)
    assert siteverify == []


def test_cloudflare_outage_refuses_the_upload(client):
    alice = Profile(client)
    alice.create()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(SITEVERIFY_URL).mock(side_effect=httpx.ConnectError("down"))
        response = upload(alice, mint_path(alice), art(1), "good")
    assert response.status_code == 503
    assert response.json()["detail"] == UNAVAILABLE_MESSAGE


def test_turnstile_is_off_without_keys(tmp_path):
    with TestClient(create_portfolio_app(str(tmp_path / "p"))) as client:
        assert client.get("/api/config").json()["turnstile_sitekey"] is None
        alice = Profile(client)
        alice.create()
        assert upload(alice, mint_path(alice), art(1), None).status_code == 200


def test_both_keys_are_required():
    with pytest.raises(ValueError):
        configure_turnstile(SITEKEY, None)
    with pytest.raises(ValueError):
        configure_turnstile(None, SECRET)
    assert configure_turnstile(None, None) is None
