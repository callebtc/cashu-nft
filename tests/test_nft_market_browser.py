"""End-to-end marketplace flows driven by the real browser client.

The portfolio server (NFT mint, market, executor) runs in-process on a local
port; the ordinary ecash mint is the test Nutshell mint (FakeWallet, v2
keyset). Every browser step runs `tests/market.e2e.mjs` in a new Node process
with an empty IndexedDB, so each step starts like a fresh browser and must
recover from the profile key and the server's encrypted backups.
"""

import asyncio
import json
import os
import secrets
import shutil
import socket
import time
from pathlib import Path
from typing import Any, Dict

import httpx
import pytest
import pytest_asyncio
import uvicorn

from cashu.nft import market_protocol as mp
from cashu.nft.portfolio import create_portfolio_app
from tests.conftest import SERVER_ENDPOINT

WEB = Path(__file__).resolve().parents[1] / "cashu" / "nft" / "portfolio_web"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (WEB / "node_modules").exists(),
    reason="node frontend not installed",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, app: Any, url: str):
        self.app = app
        self.url = url
        self.market = app.state.market
        self.executor = app.state.executor
        self.db = app.state.portfolio.db
        self.mint = SERVER_ENDPOINT

    async def browser(self, phase: str, **args: Any) -> Dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            "node",
            "--import",
            "tsx",
            "tests/market.e2e.mjs",
            phase,
            json.dumps(args),
            cwd=WEB,
            env={
                "PATH": os.environ["PATH"],
                "PORTFOLIO_URL": self.url,
                "MINT_URL": self.mint,
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
        text = out.decode()
        lines = [line for line in text.splitlines() if line.startswith("RESULT ")]
        assert proc.returncode == 0 and lines, (
            f"{phase} failed:\n{text}\n{err.decode()}"
        )
        return json.loads(lines[-1][7:])

    async def settle(self) -> None:
        """Run due executor jobs now instead of waiting for its timer."""
        await self.db.execute("UPDATE market_jobs SET next_attempt=0")
        await self.executor.run_due()

    def shift_clock(self, offset: int) -> None:
        self.market.clock = lambda: int(time.time()) + offset


@pytest_asyncio.fixture
async def server(tmp_path):
    port = free_port()
    app = create_portfolio_app(
        str(tmp_path / "portfolio"),
        market_dev_mints=[SERVER_ENDPOINT],
        run_executor=False,
    )

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    yield Server(app, f"http://127.0.0.1:{port}")
    server.should_exit = True
    await task


def key() -> str:
    return secrets.token_hex(32)


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_reply", [False, True])
async def test_browser_one_request_issuance_and_recovery(
    server, monkeypatch, lose_reply
):
    async def forbidden_begin(*args, **kwargs):
        raise AssertionError("One-request issuance must not allocate a base")

    monkeypatch.setattr(
        server.app.state.portfolio.ledger, "issue_nft_begin", forbidden_begin
    )
    secret = key()
    result = await server.browser("nft_mint", secret=secret, drop_reply=lose_reply)
    assert result["nftIssueRequests"] == 1
    if lose_reply:
        assert result["interrupted"]
        recovered = await server.browser("nft_recover", secret=secret)
        assert recovered["recovered"]["operations"] == 1
        assert len(recovered["cards"]) == 1
        assert recovered["cards"][0]["signature"]
        assert recovered["nftIssueRequests"] == 1
    else:
        assert result["duplicateRejected"]
        assert result["minted"]["custody"] == "browser"
        assert result["minted"]["signature"]
    rows = await server.db.fetchall("SELECT * FROM ps_issue_sessions")
    assert len(rows) == 1 and rows[0]["session"].startswith("v3:")
    assert len(await server.db.fetchall("SELECT * FROM ps_asset_tags")) == 1


@pytest.mark.asyncio
async def test_browser_deletes_nft_and_voids_its_link(server):
    result = await server.browser("nft_delete", secret=key())
    assert result["cards"] == []
    assert result["image"] == 404
    assert result["spent"] == "SPENT"
    assert "deleted" in result["remint"]


@pytest.mark.asyncio
async def test_browser_refuses_to_delete_a_listed_nft(server):
    seller = key()
    listed = await server.browser("seller_list", secret=seller, price=100)
    card_id = listed["listing"]["card_id"]
    result = await server.browser("nft_delete_listed", secret=seller, card_id=card_id)
    assert "Unlist" in result["error"]
    profile = await server.db.fetchall("SELECT id FROM portfolio_cards")
    assert [row["id"] for row in profile] == [card_id]


@pytest.mark.asyncio
async def test_browser_offline_purchase_competing_refund_and_payout(server):
    seller, alice, bob = key(), key(), key()

    # Seller lists from the browser; listing rotates the credential first.
    listed = await server.browser("seller_list", secret=seller, price=100)
    listing = listed["listing"]
    assert listing["state"] == "active" and listing["price"] == 100
    assert listed["rotated"], "listing must rotate the NFT credential"

    # Bob funds an offer that expires a few seconds from now (market clock
    # shifted like the Python protocol tests), then closes his browser.
    offset = -(mp.MIN_LIFETIME - mp.CLOCK_SKEW) + 6
    server.shift_clock(offset)
    bob_offer = await server.browser(
        "buyer_offer",
        secret=bob,
        name="Bob",
        listing_id=listing["id"],
        topup=500,
        price=120,
        deadline_in=8,
        clock_offset=offset,
    )
    server.shift_clock(0)
    assert bob_offer["stage"] == "registered", bob_offer
    assert bob_offer["balance"]["offerLocked"] == bob_offer["amount"] == 120

    # Alice funds at the asking price. The mint processes her funding swap but
    # the reply is lost; the coordinator restores the exact saved outputs.
    alice_offer = await server.browser(
        "buyer_offer",
        secret=alice,
        name="Alice",
        listing_id=listing["id"],
        topup=300,
        price=100,
        drop_reply=True,
    )
    assert alice_offer["reply_dropped"] and alice_offer["stage"] == "registered"
    assert alice_offer["balance"]["available"] == 200
    assert alice_offer["balance"]["offerLocked"] == 100

    # Neither buyer is online. The seller (new device) accepts Alice's offer.
    accepted = await server.browser(
        "seller_accept", secret=seller, offer_id=alice_offer["offer_id"]
    )
    assert accepted["review"]["ok"], accepted["review"]
    assert accepted["nft_leg"] == "delivered"
    # Bob's offer for the same NFT can no longer be accepted.
    late = await server.browser(
        "seller_accept", secret=seller, offer_id=bob_offer["offer_id"], expect_fail=True
    )
    assert late["error"]

    # The executor claims the seller's payment with the escrowed preimage.
    await server.settle()
    seller_after = await server.browser("reconcile", secret=seller)
    sale = next(j for j in seller_after["journal"] if j["kind"] == "sale")
    assert sale["stage"] == "paid", seller_after
    assert seller_after["balance"]["available"] == 100
    assert seller_after["balance"]["pendingClaim"] == 0

    # Bob's deadline passes; the executor refunds to his fixed outputs.
    await asyncio.sleep(9)
    await server.settle()

    # Alice comes back on a new device: the purchase is recovered from the
    # signed receipt and her own s', verified, saved encrypted and published.
    alice_after = await server.browser("reconcile", secret=alice, nft=True)
    assert {"id": alice_offer["offer_id"], "kind": "offer", "stage": "purchased"} in (
        alice_after["journal"]
    ), alice_after
    owned = next(c for c in alice_after["cards"] if c["id"] == alice_offer["offer_id"])
    assert owned["status"] == "owned"
    assert alice_after["balance"]["available"] == 200
    assert alice_after["balance"]["offerLocked"] == 0
    # The recovered credential works like any other NFT in her wallet.
    used = await server.browser(
        "buyer_use_nft", secret=alice, card_id=alice_offer["offer_id"]
    )
    assert used["token_prefix"] == "psnft1"

    # Bob reopens: the refund is imported, nothing is left locked.
    bob_after = await server.browser("reconcile", secret=bob)
    assert {"id": bob_offer["offer_id"], "kind": "offer", "stage": "refunded"} in (
        bob_after["journal"]
    ), bob_after
    assert bob_after["balance"]["available"] == 500
    assert bob_after["balance"]["offerLocked"] == 0

    # Public sale activity shows NFT, buyer, seller, sale price and time;
    # the payment mint and settlement details stay private.
    sales = (await server.market.sales(10, None))[0]
    assert set(sales) == {
        "offer_id",
        "card_id",
        "h",
        "title",
        "seller",
        "buyer",
        "created",
        "price",
        "seller_name",
        "buyer_name",
    }
    assert sales["price"] == 100
    async with httpx.AsyncClient(base_url=server.url) as http:
        feed = (await http.get("/api/activity?limit=20")).json()
    sale = next(e for e in feed if e["kind"] == "sale")
    assert sale["price"] == 100 and "mint" not in sale


@pytest.mark.asyncio
async def test_browser_ordinary_wallet_token_round_trip_and_lease(server):
    a, b = key(), key()
    await server.browser("fund", secret=a, amount=100)
    result = await server.browser("ordinary", secret=a, other=b)
    assert result["afterSend"]["available"] == 79
    assert result["received"]["amount"] == 21
    assert result["bBalance"]["available"] == 21

    # Another device: read-only while the lease is held, writable after an
    # explicit takeover that first re-reads the latest backup.
    await server.browser("hold", secret=b)
    second = await server.browser("second_device", secret=b)
    assert second["readOnly"] is True and "another device" in second["blocked"]
    assert second["before"]["available"] == 21
    assert second["tookOver"] is True and second["after"]["available"] == 20


@pytest.mark.asyncio
async def test_browser_recovers_purchase_without_journal(server):
    """The journal is the normal recovery path, but s' is derived from the
    profile key and offer id, so a buyer whose encrypted journal is gone
    (locally and on the server) still recovers the NFT from the receipt."""
    seller, carol = key(), key()
    listing = (await server.browser("seller_list", secret=seller, price=40))["listing"]
    offer = await server.browser(
        "buyer_offer",
        secret=carol,
        name="Carol",
        listing_id=listing["id"],
        topup=100,
        price=40,
    )
    assert offer["stage"] == "registered"
    await server.browser("seller_accept", secret=seller, offer_id=offer["offer_id"])
    await server.settle()
    await server.db.execute("DELETE FROM market_recovery")
    after = await server.browser("reconcile", secret=carol, nft=True)
    assert after["journal"] == []
    owned = next(c for c in after["cards"] if c["id"] == offer["offer_id"])
    assert owned["status"] == "owned"
    used = await server.browser(
        "buyer_use_nft", secret=carol, card_id=offer["offer_id"]
    )
    assert used["token_prefix"] == "psnft1"


TESTNUT = "https://testnut.cashu.space"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("NFT_MARKET_TESTNUT") != "1",
    reason="live third-party test mint; run with NFT_MARKET_TESTNUT=1",
)
async def test_testnut_end_to_end(server):
    """The whole browser flow against testnut (fake-value test sats, real
    network, cdk-mintd, non-zero input fees): eligibility, funding with a
    lost reply, review/accept, executor claim, purchase recovery, and a
    competing offer refunded after its deadline."""
    server.mint = TESTNUT
    quote = await server.market.quote(TESTNUT, 21)
    assert quote["eligible"], quote
    assert quote["test_value"] is True and quote["fee_ppk"] > 0
    seller, alice, bob = key(), key(), key()
    listing = (await server.browser("seller_list", secret=seller, price=21))["listing"]

    # Bob's competing offer expires a few seconds from now (real time at the mint).
    offset = -(mp.MIN_LIFETIME - mp.CLOCK_SKEW) + 6
    server.shift_clock(offset)
    bob_offer = await server.browser(
        "buyer_offer",
        secret=bob,
        name="Bob",
        listing_id=listing["id"],
        topup=64,
        price=25,
        deadline_in=10,
        clock_offset=offset,
    )
    server.shift_clock(0)
    assert bob_offer["stage"] == "registered", bob_offer
    bob_fee = (await server.market.quote(TESTNUT, 25))["refund_fee"]
    assert bob_offer["balance"]["offerLocked"] == bob_offer["amount"] == 25 + bob_fee

    alice_offer = await server.browser(
        "buyer_offer",
        secret=alice,
        name="Alice",
        listing_id=listing["id"],
        topup=64,
        price=21,
        drop_reply=True,
    )
    assert alice_offer["reply_dropped"] and alice_offer["stage"] == "registered"
    assert alice_offer["amount"] == quote["amount"]
    # Funding pays its own swap fee; nothing else is lost.
    assert 0 < 64 - alice_offer["amount"] - alice_offer["balance"]["available"] <= 2

    accepted = await server.browser(
        "seller_accept", secret=seller, offer_id=alice_offer["offer_id"]
    )
    assert accepted["review"]["ok"], accepted["review"]
    assert accepted["nft_leg"] == "delivered"
    await server.settle()
    seller_after = await server.browser("reconcile", secret=seller)
    assert {"stage": "paid"}.items() <= next(
        j for j in seller_after["journal"] if j["kind"] == "sale"
    ).items(), seller_after
    assert seller_after["balance"]["available"] == 21  # exact net price

    alice_after = await server.browser("reconcile", secret=alice, nft=True)
    owned = next(c for c in alice_after["cards"] if c["id"] == alice_offer["offer_id"])
    assert owned["status"] == "owned"
    assert (
        await server.browser(
            "buyer_use_nft", secret=alice, card_id=alice_offer["offer_id"]
        )
    )["token_prefix"] == "psnft1"

    await asyncio.sleep(12)
    await server.settle()
    bob_after = await server.browser("reconcile", secret=bob)
    assert {"id": bob_offer["offer_id"], "kind": "offer", "stage": "refunded"} in (
        bob_after["journal"]
    ), bob_after
    assert (
        bob_after["balance"]["available"]
        == bob_offer["balance"]["available"] + bob_offer["amount"] - bob_fee
    )


@pytest.mark.asyncio
async def test_browser_offer_with_ecash_spent_elsewhere(server):
    """A stale wallet copy bids with a proof another copy already spent: the
    funding swap fails, the offer is abandoned with a clear reason, the spent
    proof is dropped and the unspent input becomes spendable again."""
    listing = (await server.browser("seller_list", secret=key(), price=90))["listing"]
    # 100 sat arrive as 64 + 32 + 4; an offer of 96 must use the 64 and the 32.
    result = await server.browser(
        "stale_offer",
        secret=key(),
        listing_id=listing["id"],
        topup=100,
        price=96,
        spend_elsewhere=64,
    )
    assert result["error"] and "spent elsewhere" in result["error"], result
    assert result["journal"] == [{"stage": "abandoned", "error": result["error"]}]
    assert result["attention"] == []
    assert result["balance"]["available"] == 36
    assert result["balance"]["reserved"] == 0
    assert result["balance"]["offerLocked"] == 0


@pytest.mark.asyncio
async def test_browser_concurrent_offers_use_separate_ecash(server):
    listing = (await server.browser("seller_list", secret=key(), price=50))["listing"]
    result = await server.browser(
        "concurrent_offers",
        secret=key(),
        listing_id=listing["id"],
        topup=300,
        prices=[50, 70],
    )
    assert result["stages"] == ["registered", "registered"], result
    assert result["distinctInputs"] and result["attention"] == []
    locked = sum(result["amounts"])
    assert result["balance"]["offerLocked"] == locked
    assert result["balance"]["available"] == 300 - locked
    assert result["balance"]["reserved"] == 0
