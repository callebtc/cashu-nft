"""Nostr login end to end: the browser client (portfolio_web/tests/nostr.e2e.mjs,
with a fake NIP-07 extension and no relays) against a real portfolio server."""

import asyncio
import json
import os
import secrets
from typing import Any, Dict

import pytest

from tests.test_nft_market_browser import WEB, pytestmark, server  # noqa: F401


async def browser(server, phase: str, **args: Any) -> Dict[str, Any]:  # noqa: F811
    proc = await asyncio.create_subprocess_exec(
        "node",
        "--import",
        "tsx",
        "tests/nostr.e2e.mjs",
        phase,
        json.dumps(args),
        cwd=WEB,
        env={"PATH": os.environ["PATH"], "PORTFOLIO_URL": server.url},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    lines = [line for line in out.decode().splitlines() if line.startswith("RESULT ")]
    assert (
        proc.returncode == 0 and lines
    ), f"{phase} failed:\n{out.decode()}\n{err.decode()}"
    return json.loads(lines[-1][7:])


@pytest.mark.asyncio
async def test_nostr_login_signup_devices_deferral_and_restore(server):  # noqa: F811
    secret = secrets.token_hex(32)
    signup = await browser(server, "extension_signup", secret=secret, name="Nostr Nia")
    assert signup["before"] == {"existing": None, "vault": None}
    assert signup["profile"]["nostr"] is True
    assert signup["profile"]["name"] == "Nostr Nia"
    assert signup["published"] is False  # no relays in this test
    # Showings are extension-signed events; requests used the session key.
    assert signup["signature"].startswith("n1:")
    assert signup["calls"] == {
        "getPublicKey": 1,
        "signEvent": 2,
        "encrypt": 1,
        "decrypt": 0,
    }

    # A new browser: the extension decrypts the server's copy of the vault.
    device = await browser(server, "extension_new_device", secret=secret)
    assert device["nostr"] and device["onServer"] and device["check"] == signup["check"]
    assert device["calls"] == {
        "getPublicKey": 1,
        "signEvent": 1,
        "encrypt": 0,
        "decrypt": 1,
    }
    assert device["recovered"] == {"cards": 1, "operations": 0, "discarded": 0}

    # The same key pasted as an nsec opens the same wallet.
    nsec = await browser(server, "nsec_login", secret=secret)
    assert nsec == {"pubkey": signup["pubkey"], "nostr": True, "check": signup["check"]}

    # Background recovery never prompts; it waits for the user.
    root = signup["root"]
    assert (await browser(server, "interrupted_mint", secret=secret, root=root))[
        "interrupted"
    ]
    deferred = await browser(server, "deferred_recovery", secret=secret, root=root)
    assert deferred["background"] == "deferred" and deferred["waiting"] == 1
    assert deferred["promptsInBackground"] == 0 and deferred["prompts"] == 1
    assert deferred["recovered"]["operations"] == 1
    async with server.db.get_connection() as conn:
        rows = await conn.fetchall(
            "SELECT signature FROM portfolio_cards WHERE pubkey=:p",
            {"p": signup["pubkey"]},
        )
    assert len(rows) == 2 and all(r["signature"].startswith("n1:") for r in rows)

    # The server lost its copy: the backed-up wallet key restores it, a wrong one doesn't.
    await server.db.execute(
        "DELETE FROM portfolio_vaults WHERE pubkey=:p", {"p": signup["pubkey"]}
    )
    restore = await browser(server, "restore", secret=secret, root=root)
    assert restore == {"missing": True, "wrong": False, "right": True, "restored": True}


@pytest.mark.asyncio
async def test_extension_cannot_open_a_collection_that_uses_its_own_key(server):  # noqa: F811
    secret = secrets.token_hex(32)
    await browser(server, "key_collection", secret=secret)
    refused = await browser(server, "extension_on_key_collection", secret=secret)
    assert refused["refused"] and "uses its own key" in refused["message"]
    # Refused before any signature was asked for.
    assert refused["calls"]["signEvent"] == 0
