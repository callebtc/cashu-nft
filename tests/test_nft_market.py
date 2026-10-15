"""Marketplace acceptance tests (cashu/nft/MARKETPLACE_PLAN.md).

Everything goes through the portfolio's HTTP API, the real PS ledger and the
real test Nutshell mint (FakeWallet, v2 keyset). Browser roles are played by
Python with the same protocol helpers the frontend mirrors. The settlement
executor is driven step by step; the market clock is shifted only where a
test needs the payment mint's real locktime to have passed.
"""

import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest
import pytest_asyncio
from coincurve import PrivateKey as SecpKey
from coincurve import PublicKeyXOnly

import cashu.nft.market_net as net
from cashu.core.crypto.bls import PublicKey as G1Point
from cashu.core.crypto.ps import (
    G1,
    PS_BURN_BINDING,
    Credential,
    present,
    present_showing,
    prove_owner_secret,
    verify_presentation,
)
from cashu.core.migrations import migrate_databases
from cashu.core.split import amount_split
from cashu.nft import market_cash
from cashu.nft import market_protocol as mp
from cashu.nft.market import fee_for, funding_amount
from cashu.nft.market_net import (
    BlockedDestination,
    GuardedMintClient,
    MintNetPolicy,
    is_public_address,
)
from cashu.nft.portfolio import (
    CLAIM_DOMAIN,
    auth_message,
    create_portfolio_app,
    make_showing,
    showing_context,
)
from cashu.nft.wallet import SHOW_TOKEN_PREFIX
from cashu.wallet import migrations
from cashu.wallet.wallet import Wallet
from tests.conftest import SERVER_ENDPOINT
from tests.helpers import pay_if_regtest, use_v2_keyset
from tests.test_nft_market_protocol import htlc_lock


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Actor:
    """A profile driving the API like the browser does (signed requests)."""

    def __init__(self, env: "Env", name: str):
        self.env = env
        self.name = name
        self.secret = secrets.token_bytes(32)
        self.key = SecpKey(self.secret)
        self.pubkey = PublicKeyXOnly.from_secret(self.secret).format().hex()

    async def post(self, path: str, body: Any = None) -> httpx.Response:
        raw = b"" if body is None else json.dumps(body).encode()
        challenge = (
            await self.env.http.post(
                "/api/auth/challenge",
                json={
                    "pubkey": self.pubkey,
                    "method": "POST",
                    "path": path,
                    "body_hash": sha(raw),
                },
            )
        ).json()
        assert challenge["message"] == auth_message(
            self.pubkey,
            "POST",
            path,
            sha(raw),
            challenge["nonce"],
            challenge["expires"],
        )
        signature = self.key.sign_schnorr(
            hashlib.sha256(challenge["message"].encode()).digest()
        ).hex()
        return await self.env.http.post(
            path,
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Portfolio-Challenge": challenge["nonce"],
                "X-Portfolio-Signature": signature,
            },
        )

    def base(self) -> str:
        return f"/api/profiles/{self.pubkey}"


class Env:
    def __init__(self, app: Any, http: httpx.AsyncClient):
        self.app = app
        self.http = http
        self.market = app.state.market
        self.executor = app.state.executor
        self.ledger = app.state.portfolio.ledger
        self.db = app.state.portfolio.db

    async def actor(self, name: str) -> Actor:
        a = Actor(self, name)
        r = await a.post(f"/api/profiles/{a.pubkey}", {"name": name})
        assert r.status_code == 200, r.text
        return a

    def shift_clock(self, offset: int) -> None:
        self.market.clock = lambda: int(time.time()) + offset


@pytest_asyncio.fixture
async def env(tmp_path):
    app = create_portfolio_app(
        str(tmp_path / "portfolio"),
        market_dev_mints=[SERVER_ENDPOINT],
        run_executor=False,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://portfolio.test"
        ) as http:
            yield Env(app, http)


@pytest_asyncio.fixture
async def cash():
    wallet = await Wallet.with_db(
        SERVER_ENDPOINT, f"test_data/market_flow_{secrets.token_hex(4)}", "flow"
    )
    await migrate_databases(wallet.db, migrations)
    await wallet.load_mint()
    await use_v2_keyset(wallet)
    quote = await wallet.request_mint(2000)
    await pay_if_regtest(quote.request)
    await wallet.mint(2000, quote_id=quote.quote)
    yield wallet


class Seller:
    def __init__(
        self, actor: Actor, cred: Credential, card_id: str, claim_key: SecpKey
    ):
        self.actor = actor
        self.cred = cred
        self.card_id = card_id
        self.claim_key = claim_key
        self.claim_pub = claim_key.public_key.format().hex()


async def give_nft(env: Env, owner: Actor, title: str = "Sunset") -> Seller:
    """Fixture shortcut for an NFT already in the owner's browser wallet."""
    h = int.from_bytes(secrets.token_bytes(31), "big") + 1
    s = int.from_bytes(secrets.token_bytes(31), "big") + 1
    S, pok = prove_owner_secret(s)
    u, v = await env.ledger.issue_nft(h, S, pok)
    cred = Credential(u=u, v=v, h=h, s=s, keyset_id=env.ledger.keyset.keyset_id)
    showing = make_showing(owner.pubkey, cred)
    signature = owner.key.sign_schnorr(
        hashlib.sha256((CLAIM_DOMAIN + showing).encode()).digest()
    ).hex()
    card_id = secrets.token_hex(16)
    hx = h.to_bytes(32, "big").hex()
    async with env.db.get_connection() as conn:
        await conn.execute(
            "INSERT INTO portfolio_images(h,jpg) VALUES(:h,:j)",
            {"h": hx, "j": b"\xff\xd8\xff\xd9"},
        )
        await conn.execute(
            """INSERT INTO portfolio_cards(id,pubkey,h,title,showing,signature,status,created,encrypted_credential)
            VALUES(:id,:p,:h,:t,:s,:sig,'owned',:now,:enc)""",
            {
                "id": card_id,
                "p": owner.pubkey,
                "h": hx,
                "t": title,
                "s": showing,
                "sig": signature,
                "now": int(time.time()),
                "enc": json.dumps({"version": 1, "nonce": "00", "ciphertext": "00"}),
            },
        )
    return Seller(owner, cred, card_id, SecpKey(secrets.token_bytes(32)))


def nullifier_of(cred: Credential) -> str:
    return present(cred).nullifier.format().hex()


async def list_nft(
    env: Env,
    seller: Seller,
    price: int,
    revision: int = 1,
    listing_id: Optional[str] = None,
) -> Dict[str, Any]:
    listing = {
        "v": mp.LISTING_PROTOCOL,
        "listing_id": listing_id or secrets.token_hex(16),
        "revision": revision,
        "card_id": seller.card_id,
        "seller": seller.actor.pubkey,
        "h": seller.cred.h.to_bytes(32, "big").hex(),
        "nullifier": nullifier_of(seller.cred),
        "nft_keyset": env.ledger.keyset.keyset_id,
        "price": price,
        "claim_pubkey": seller.claim_pub,
        "created": env.market.clock(),
    }
    sig = mp.sign_purpose(
        mp.LISTING_DOMAIN, mp.listing_hash(listing), seller.actor.secret
    )
    path = seller.actor.base() + (
        "/market/listings"
        if revision == 1
        else f"/market/listings/{listing['listing_id']}/revise"
    )
    r = await seller.actor.post(path, {"listing": listing, "signature": sig})
    assert r.status_code == 200, r.text
    return r.json()


class Offer:
    """Everything one buyer prepares and keeps privately for one offer."""

    def __init__(self) -> None:
        self.preimage = secrets.token_bytes(32)
        self.hashlock = sha(self.preimage)
        self.s_new = int.from_bytes(secrets.token_bytes(31), "big") + 1
        self.refund_key = SecpKey(secrets.token_bytes(32))
        self.body: Dict[str, Any] = {}
        self.refund_outputs: Optional[market_cash.OwnerOutputs] = None
        self.manifest_hash = b""


async def make_offer(
    env: Env,
    buyer: Actor,
    wallet: Wallet,
    listing: Dict[str, Any],
    price: Optional[int] = None,
    cash_deadline: Optional[int] = None,
    **overrides: Any,
) -> Offer:
    o = Offer()
    price = price or listing["price"]
    keyset_id = wallet.keyset_id
    fee = 0  # the test mint charges no input fees
    amount = price + fee
    now = env.market.clock()
    cash_deadline = cash_deadline or now + mp.DEFAULT_LIFETIME
    config = (await env.http.get("/api/market/config")).json()
    manifest: Dict[str, Any] = {
        "v": mp.PROTOCOL,
        "offer_id": secrets.token_hex(16),
        "listing_id": listing["id"],
        "listing_revision": listing["revision"],
        "nft": {
            "keyset_id": env.ledger.keyset.keyset_id,
            "h": listing["h"],
            "nullifier": listing["nullifier"],
        },
        "seller": listing["seller"],
        "buyer": buyer.pubkey,
        "price": price,
        "payment": {
            "mint": SERVER_ENDPOINT,
            "unit": "sat",
            "keyset_id": keyset_id,
            "amount": amount,
            "claim_fee": fee,
            "refund_fee": fee,
        },
        "hashlock": o.hashlock,
        "claim_pubkey": listing["claim_pubkey"],
        "refund_pubkey": o.refund_key.public_key.format().hex(),
        "cash_deadline": cash_deadline,
        "accept_deadline": cash_deadline - mp.ACCEPT_WINDOW,
        "nft_destination": "",
        "escrow_key": config["escrow"]["version"],
        "created": now,
    }
    manifest["nft_destination"] = (G1 * o.s_new).format().hex()
    manifest.update({k: v for k, v in overrides.items() if k in manifest})
    mhash = mp.manifest_hash(manifest)
    _, receive_proof = mp.prove_receive(o.s_new, mhash)
    lock = htlc_lock(
        o.hashlock, listing["claim_pubkey"], manifest["refund_pubkey"], cash_deadline
    )
    # Spend only ordinary, unreserved proofs (earlier offers left HTLC proofs behind).
    plain = [
        p for p in wallet.proofs if not p.secret.startswith("[") and not p.reserved
    ]
    _, sent = await wallet.swap_to_send(plain, amount, secret_lock=lock)
    proofs: List[Dict[str, Any]] = [
        {
            "amount": p.amount,
            "id": p.id,
            "secret": p.secret,
            "C": p.C,
            "dleq": {"e": p.dleq.e, "s": p.dleq.s, "r": p.dleq.r} if p.dleq else None,
        }
        for p in sent
    ]
    o.refund_outputs = market_cash.blind_outputs(amount - fee, keyset_id)
    refund_sig = o.refund_key.sign_schnorr(
        mp.sigall_digest(
            [(p["secret"], p["C"]) for p in proofs],
            [(x["amount"], x["B_"]) for x in o.refund_outputs.public()],
        )
    ).hex()
    o.body = {
        "manifest": manifest,
        "buyer_signature": mp.sign_purpose(mp.OFFER_SIG_DOMAIN, mhash, buyer.secret),
        "receive_proof": receive_proof.to_bytes().hex(),
        "escrow": mp.seal_preimage(
            config["escrow"]["public_key"],
            config["escrow"]["version"],
            o.preimage,
            mhash,
        ),
        "proofs": proofs,
        "refund": {"outputs": o.refund_outputs.public(), "signature": refund_sig},
    }
    o.manifest_hash = mhash
    return o


async def submit(buyer: Actor, offer: Offer, expect: int = 200) -> httpx.Response:
    r = await buyer.post(buyer.base() + "/market/offers", offer.body)
    assert r.status_code == expect, r.text
    return r


async def accept(
    env: Env,
    seller: Seller,
    offer_id: str,
    expect: Optional[int] = 200,
    binding_to: Optional[G1Point] = None,
    claim_outputs: Optional[market_cash.OwnerOutputs] = None,
) -> httpx.Response:
    view = (
        await seller.actor.post(seller.actor.base() + f"/market/offers/{offer_id}")
    ).json()
    proofs = view["proofs"]
    outputs = claim_outputs or market_cash.blind_outputs(
        view["price"], view["keyset_id"]
    )
    claim_sig = seller.claim_key.sign_schnorr(
        mp.sigall_digest(
            [(p["secret"], p["C"]) for p in proofs],
            [(x["amount"], x["B_"]) for x in outputs.public()],
        )
    ).hex()
    destination = G1Point(
        compressed=bytes.fromhex(view["manifest"]["nft_destination"]), group="G1"
    )
    mhash = bytes.fromhex(view["manifest_hash"])
    pres = present(
        seller.cred, binding=mp.delivery_binding(mhash, binding_to or destination)
    )
    acceptance = {
        "v": mp.ACCEPT_PROTOCOL,
        "offer_id": offer_id,
        "manifest_hash": view["manifest_hash"],
        "claim_outputs": outputs.public(),
        "claim_signature": claim_sig,
        "accepted": env.market.clock(),
    }
    r = await seller.actor.post(
        seller.actor.base() + f"/market/offers/{offer_id}/accept",
        {
            "acceptance": acceptance,
            "seller_signature": mp.sign_purpose(
                mp.ACCEPT_DOMAIN, mp.acceptance_hash(acceptance), seller.actor.secret
            ),
            "presentation": pres.to_bytes().hex(),
        },
    )
    if expect is not None:
        assert r.status_code == expect, r.text
    r.claim_outputs = outputs  # type: ignore[attr-defined]
    return r


def keys_of(wallet: Wallet, keyset_id: str) -> Dict[int, str]:
    return {
        a: k.format().hex() for a, k in wallet.keysets[keyset_id].public_keys.items()
    }


async def spendable(proofs: List[Dict[str, Any]]) -> bool:
    async with httpx.AsyncClient() as client:
        states = await market_cash.proof_states(
            client, SERVER_ENDPOINT, [p["secret"] for p in proofs]
        )
    return all(s["state"] == "UNSPENT" for s in states)


async def recover_purchase(
    env: Env, buyer: Actor, offer: Offer, offer_id: str
) -> Credential:
    """What the buyer's browser does on return: fetch the signed receipt,
    check the pinned receipt key, combine (u, v) with s' and verify."""
    data = (
        await buyer.post(buyer.base() + f"/market/purchases/{offer_id}/receipt")
    ).json()
    config = (await env.http.get("/api/market/config")).json()
    assert data["receipt_key"] == config["receipt"]["public_key"]
    assert mp.verify_receipt(
        data["receipt"], data["signature"], config["receipt"]["public_key"]
    )
    receipt = data["receipt"]
    cred = Credential(
        u=G1Point(compressed=bytes.fromhex(receipt["u"]), group="G1"),
        v=G1Point(compressed=bytes.fromhex(receipt["v_"]), group="G1"),
        h=int(receipt["h"], 16),
        s=offer.s_new,
        keyset_id=env.ledger.keyset.keyset_id,
    )
    assert verify_presentation(
        env.ledger.keyset, present(cred, binding=b"check"), binding=b"check"
    )
    return cred


async def publish_purchase(
    env: Env, buyer: Actor, cred: Credential, offer_id: str
) -> httpx.Response:
    hx = cred.h.to_bytes(32, "big").hex()
    context = showing_context(buyer.pubkey, hx, cred.keyset_id)
    pres = present_showing(cred, context)
    showing = (
        SHOW_TOKEN_PREFIX
        + (len(context).to_bytes(2, "big") + context + pres.to_bytes()).hex()
    )
    signature = buyer.key.sign_schnorr(
        hashlib.sha256((CLAIM_DOMAIN + showing).encode()).digest()
    ).hex()
    return await buyer.post(
        buyer.base() + f"/market/purchases/{offer_id}/publish",
        {
            "encrypted_credential": {"version": 1, "nonce": "11", "ciphertext": "22"},
            "showing": showing,
            "signature": signature,
        },
    )


# --- the complete offline purchase ---------------------------------------------


@pytest.mark.asyncio
async def test_offline_buyer_purchase_settles_and_recovers(env, cash):
    seller = await give_nft(env, await env.actor("Studio Ana"), "Harbour lights")
    buyer = await env.actor("Ben")
    listing = await list_nft(env, seller, 100)
    offer = await make_offer(env, buyer, cash, listing)
    r = await submit(buyer, offer)
    offer_id = r.json()["id"]
    assert r.json()["disposition"] == "funded" and r.json()["cash_leg"] == "locked"
    # The buyer is now offline. Nothing below is signed by the buyer.
    accepted = await accept(env, seller, offer_id)
    body = accepted.json()
    assert (
        body["offer"]["nft_leg"] == "delivered"
        and body["offer"]["cash_leg"] == "claim_pending"
    )
    # Delivery is a fact; payment is not until the payment mint confirms it.
    assert (await env.executor.run_due()) >= 1
    paid = (
        await seller.actor.post(
            seller.actor.base() + f"/market/offers/{offer_id}/payment/claim"
        )
    ).json()
    assert paid["state"] == "done" and paid["outcome"] == "claim"
    proofs = market_cash.unblind(
        accepted.claim_outputs, paid["signatures"], keys_of(cash, cash.keyset_id)
    )
    assert sum(p["amount"] for p in proofs) == 100 and await spendable(proofs)
    seller_view = (
        await seller.actor.post(seller.actor.base() + f"/market/offers/{offer_id}")
    ).json()
    assert seller_view["cash_leg"] == "claimed"
    # Seller's card is in Sent; the listing is sold.
    assert (await env.http.get(f"/api/market/listings/{listing['id']}")).json()[
        "state"
    ] == "sold"
    profile = (await env.http.get(f"/api/profiles/{seller.actor.pubkey}")).json()
    assert [c["status"] for c in profile["cards"] if c["id"] == seller.card_id] == [
        "sent"
    ]
    # The buyer returns: recover the exact usable credential, then publish.
    purchases = (await buyer.post(buyer.base() + "/market/purchases")).json()
    assert purchases[0]["publication"] == "awaiting_sync"
    cred = await recover_purchase(env, buyer, offer, offer_id)
    card = await publish_purchase(env, buyer, cred, offer_id)
    assert card.status_code == 200, card.text
    assert card.json()["status"] == "owned"
    buyer_profile = (await env.http.get(f"/api/profiles/{buyer.pubkey}")).json()
    assert [c["id"] for c in buyer_profile["cards"]] == [offer_id]
    # The seller's old credential is dead; the buyer's moves like any NFT.
    S2, pok2 = prove_owner_secret(31)
    await env.ledger.transfer(present(cred, binding=S2.format()), S2, pok2)
    # Inbox: both sides were told, privately.
    buyer_inbox = (
        await buyer.post(buyer.base() + "/market/inbox", {"after": 0, "wait": 0})
    ).json()
    seller_inbox = (
        await seller.actor.post(
            seller.actor.base() + "/market/inbox", {"after": 0, "wait": 0}
        )
    ).json()
    assert {"offer_funded", "purchased"} <= {e["kind"] for e in buyer_inbox["events"]}
    assert {"offer_received", "sold", "paid"} <= {
        e["kind"] for e in seller_inbox["events"]
    }
    # Public sale activity: NFT, buyer, seller, sale price, time. No mint or proofs.
    sales = (await env.http.get("/api/market/sales")).json()
    assert sales and set(sales[0]) == {
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
    activity = (await env.http.get("/api/activity")).json()
    assert any(e["kind"] == "sale" and e["actor"] == buyer.pubkey for e in activity)
    assert not any(
        e["kind"] == "receive" and e.get("card_id") == offer_id for e in activity
    )


# --- competing offers, refunds and races --------------------------------------


@pytest.mark.asyncio
async def test_competing_offers_one_wins_losers_refund_without_preimage(env, cash):
    seller = await give_nft(env, await env.actor("Ana"))
    alice, bob = await env.actor("Alice"), await env.actor("Bob")
    listing = await list_nft(env, seller, 50)
    # Bob's offer expires in real time a few seconds after funding.
    env.shift_clock(-(mp.MIN_LIFETIME - mp.CLOCK_SKEW) + 6)
    loser = await make_offer(
        env, bob, cash, listing, price=60, cash_deadline=int(time.time()) + 6
    )
    loser_id = (await submit(bob, loser)).json()["id"]
    env.shift_clock(0)
    winner = await make_offer(env, alice, cash, listing, price=55)
    winner_id = (await submit(alice, winner)).json()["id"]
    await accept(env, seller, winner_id)
    await accept(env, seller, loser_id, expect=409)
    bob_view = (await bob.post(bob.base() + f"/market/offers/{loser_id}")).json()
    assert bob_view["disposition"] == "superseded" and bob_view["cash_leg"] == "locked"
    # The losing offer's preimage was never released: no claim job, no witness.
    jobs = await env.db.fetchall(
        "SELECT kind, preimage FROM market_jobs WHERE offer_id=:o", {"o": loser_id}
    )
    assert [(j["kind"], j["preimage"]) for j in jobs] == [("refund", None)]
    # Before the deadline a refund isn't executable, and the executor knows it.
    await env.executor.run_due()
    assert (
        await bob.post(bob.base() + f"/market/offers/{loser_id}/payment/refund")
    ).json()["state"] == "pending"
    await asyncio.sleep(7)
    env.shift_clock(0)
    await env.db.execute(
        "UPDATE market_jobs SET next_attempt=0 WHERE offer_id=:o", {"o": loser_id}
    )
    await env.executor.run_due()
    refund = (
        await bob.post(bob.base() + f"/market/offers/{loser_id}/payment/refund")
    ).json()
    assert refund["state"] == "done" and refund["outcome"] == "refund"
    assert loser.refund_outputs is not None
    back = market_cash.unblind(
        loser.refund_outputs, refund["signatures"], keys_of(cash, cash.keyset_id)
    )
    assert sum(p["amount"] for p in back) == 60 and await spendable(back)
    bob_view = (await bob.post(bob.base() + f"/market/offers/{loser_id}")).json()
    assert bob_view["cash_leg"] == "refunded"
    assert "refunded" in {
        e["kind"]
        for e in (await bob.post(bob.base() + "/market/inbox", {"after": 0})).json()[
            "events"
        ]
    }


@pytest.mark.asyncio
async def test_simultaneous_accepts_deliver_exactly_once(env, cash):
    seller = await give_nft(env, await env.actor("Ana"))
    buyers = [await env.actor(f"Buyer {i}") for i in range(2)]
    listing = await list_nft(env, seller, 20)
    offers = [await make_offer(env, b, cash, listing) for b in buyers]
    ids = [(await submit(b, o)).json()["id"] for b, o in zip(buyers, offers)]
    results = await asyncio.gather(
        *(accept(env, seller, i, expect=None) for i in ids), return_exceptions=True
    )  # type: ignore[arg-type]
    codes = sorted(r.status_code for r in results if isinstance(r, httpx.Response))
    assert codes == [200, 409], results
    deliveries = await env.db.fetchall("SELECT offer_id FROM market_deliveries")
    assert len(deliveries) == 1
    claims = await env.db.fetchall(
        "SELECT offer_id FROM market_jobs WHERE kind='claim'"
    )
    assert [c["offer_id"] for c in claims] == [deliveries[0]["offer_id"]]


# --- rejection paths before anything irreversible ------------------------------


@pytest.mark.asyncio
async def test_offer_registration_rejects_bad_terms(env, cash):
    seller = await give_nft(env, await env.actor("Ana"))
    buyer = await env.actor("Ben")
    listing = await list_nft(env, seller, 40)
    below = await make_offer(env, buyer, cash, listing, price=39)
    await submit(buyer, below, expect=409)
    stale = await make_offer(env, buyer, cash, listing, listing_revision=2)
    await submit(buyer, stale, expect=409)
    # The escrow must open to this offer's hashlock.
    wrong_escrow = await make_offer(env, buyer, cash, listing)
    config = (await env.http.get("/api/market/config")).json()
    wrong_escrow.body["escrow"] = mp.seal_preimage(
        config["escrow"]["public_key"],
        "esc1",
        secrets.token_bytes(32),
        wrong_escrow.manifest_hash,
    )
    await submit(buyer, wrong_escrow, expect=400)
    # The refund authorization must cover exactly these proofs and outputs.
    bad_refund = await make_offer(env, buyer, cash, listing)
    bad_refund.body["refund"]["outputs"] = market_cash.blind_outputs(
        40, cash.keyset_id
    ).public()
    await submit(buyer, bad_refund, expect=403)
    # Forged proofs fail the DLEQ check.
    forged = await make_offer(env, buyer, cash, listing)
    forged.body["proofs"][0]["dleq"]["s"] = "11" * 32
    await submit(buyer, forged, expect=400)
    # A destination can only ever be used once.
    first = await make_offer(env, buyer, cash, listing)
    await submit(buyer, first)
    replay = await make_offer(env, buyer, cash, listing)
    replay.body = json.loads(json.dumps(first.body))
    replay.body["manifest"]["offer_id"] = secrets.token_hex(16)
    await submit(
        buyer, replay, expect=403
    )  # buyer signature and receive proof no longer match
    # Sellers can't buy their own NFT; buyers can't sign for someone else.
    own = await make_offer(env, seller.actor, cash, listing)
    await submit(seller.actor, own, expect=400)
    spoof = await make_offer(env, buyer, cash, listing)
    r = await seller.actor.post(seller.actor.base() + "/market/offers", spoof.body)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_acceptance_rejects_wrong_destination_signature_and_late_accept(
    env, cash
):
    seller = await give_nft(env, await env.actor("Ana"))
    buyer = await env.actor("Ben")
    listing = await list_nft(env, seller, 30)
    offer = await make_offer(env, buyer, cash, listing)
    offer_id = (await submit(buyer, offer)).json()["id"]
    # A presentation bound to another destination cannot deliver this offer.
    await accept(env, seller, offer_id, expect=409, binding_to=G1 * 777)
    # A claim signature over different outputs is refused before delivery.
    view = (
        await seller.actor.post(seller.actor.base() + f"/market/offers/{offer_id}")
    ).json()
    pres = present(
        seller.cred,
        binding=mp.delivery_binding(
            bytes.fromhex(view["manifest_hash"]),
            G1Point(
                compressed=bytes.fromhex(view["manifest"]["nft_destination"]),
                group="G1",
            ),
        ),
    )
    outputs = market_cash.blind_outputs(30, view["keyset_id"]).public()
    other = market_cash.blind_outputs(30, view["keyset_id"]).public()
    bad_sig = seller.claim_key.sign_schnorr(
        mp.sigall_digest(
            [(p["secret"], p["C"]) for p in view["proofs"]],
            [(x["amount"], x["B_"]) for x in other],
        )
    ).hex()
    acceptance = {
        "v": mp.ACCEPT_PROTOCOL,
        "offer_id": offer_id,
        "manifest_hash": view["manifest_hash"],
        "claim_outputs": outputs,
        "claim_signature": bad_sig,
        "accepted": env.market.clock(),
    }
    r = await seller.actor.post(
        seller.actor.base() + f"/market/offers/{offer_id}/accept",
        {
            "acceptance": acceptance,
            "seller_signature": mp.sign_purpose(
                mp.ACCEPT_DOMAIN, mp.acceptance_hash(acceptance), seller.actor.secret
            ),
            "presentation": pres.to_bytes().hex(),
        },
    )
    assert r.status_code == 403
    # Nothing irreversible happened.
    assert not await env.ledger.is_spent(bytes.fromhex(listing["nullifier"]))
    assert (await env.http.get(f"/api/market/listings/{listing['id']}")).json()[
        "state"
    ] == "active"
    assert (await env.db.fetchone("SELECT COUNT(*) AS n FROM market_deliveries"))[
        "n"
    ] == 0
    # After the acceptance cutoff, even a correct acceptance is refused.
    env.shift_clock(mp.DEFAULT_LIFETIME - mp.ACCEPT_WINDOW)
    await accept(env, seller, offer_id, expect=409)


@pytest.mark.asyncio
async def test_preimage_never_stored_or_returned_before_delivery(env, cash):
    seller = await give_nft(env, await env.actor("Ana"))
    buyer = await env.actor("Ben")
    listing = await list_nft(env, seller, 25)
    offer = await make_offer(env, buyer, cash, listing)
    offer_id = (await submit(buyer, offer)).json()["id"]
    secret_hex = offer.preimage.hex()
    seen = json.dumps(
        [
            (await buyer.post(buyer.base() + f"/market/offers/{offer_id}")).json(),
            (
                await seller.actor.post(
                    seller.actor.base() + f"/market/offers/{offer_id}"
                )
            ).json(),
            (
                await seller.actor.post(seller.actor.base() + "/market/offers/list")
            ).json(),
            (await env.http.get("/api/market/listings")).json(),
        ]
    )
    assert secret_hex not in seen
    db_file = Path(env.db.db_location) / "portfolio.sqlite3"
    assert (
        secret_hex.encode() not in db_file.read_bytes()
        and offer.preimage not in db_file.read_bytes()
    )
    # Declining doesn't release it either.
    await seller.actor.post(seller.actor.base() + f"/market/offers/{offer_id}/decline")
    assert secret_hex.encode() not in db_file.read_bytes()


# --- listings -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_guards_revisions_and_unlisting(env, cash):
    seller = await give_nft(env, await env.actor("Ana"))
    buyer = await env.actor("Ben")
    listing = await list_nft(env, seller, 10)
    # Listed NFTs can't be exported or linked until unlisted.
    r = await seller.actor.post(
        seller.actor.base() + f"/wallet/cards/{seller.card_id}/ready"
    )
    assert r.status_code == 409
    early = await make_offer(env, buyer, cash, listing)
    early_id = (await submit(buyer, early)).json()["id"]
    # A price edit applies to new offers; the existing offer keeps its terms.
    revised = await list_nft(env, seller, 15, revision=2, listing_id=listing["id"])
    assert revised["price"] == 15 and revised["revision"] == 2
    late_low = await make_offer(env, buyer, cash, revised, price=12)
    await submit(buyer, late_low, expect=409)
    assert (await buyer.post(buyer.base() + f"/market/offers/{early_id}")).json()[
        "price"
    ] == 10
    # Unlisting declines pending offers; their refunds still follow the schedule.
    r = await seller.actor.post(
        seller.actor.base() + f"/market/listings/{listing['id']}/unlist"
    )
    assert r.json()["state"] == "unlisted"
    view = (await buyer.post(buyer.base() + f"/market/offers/{early_id}")).json()
    assert view["disposition"] == "declined" and [j["kind"] for j in view["jobs"]] == [
        "refund"
    ]
    assert view["cash_leg"] == "locked"  # declined is not refunded
    r = await seller.actor.post(
        seller.actor.base() + f"/wallet/cards/{seller.card_id}/ready"
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_bids_are_public_highest_first_and_in_activity(env, cash):
    seller = await give_nft(env, await env.actor("Ana"), title="Dusk")
    ben, cy = await env.actor("Ben"), await env.actor("Cy")
    listing = await list_nft(env, seller, 10)
    low = (
        await submit(ben, await make_offer(env, ben, cash, listing, price=12))
    ).json()
    await submit(cy, await make_offer(env, cy, cash, listing, price=20))

    bids = (await env.http.get(f"/api/market/listings/{listing['id']}/bids")).json()
    assert [(b["buyer"], b["price"], b["status"]) for b in bids["items"]] == [
        (cy.pubkey, 20, "open"),
        (ben.pubkey, 12, "open"),
    ]
    assert (bids["count"], bids["bidders"], bids["top"]) == (2, 2, 20)
    assert bids["items"][1]["buyer_name"] == "Ben"
    # Settlement details stay with the participants.
    assert not {"mint", "proofs", "amount", "manifest", "escrow"} & set(
        bids["items"][0]
    )
    summary = {"count": 2, "bidders": 2, "top": 20}
    one = (await env.http.get(f"/api/market/listings/{listing['id']}")).json()
    assert one["bids"] == summary
    browse = (await env.http.get("/api/market/listings")).json()["items"]
    assert [i["bids"] for i in browse if i["id"] == listing["id"]] == [summary]

    events = (await env.http.get("/api/activity")).json()
    bid_events = {
        (e["actor"], e["price"], e["title"]) for e in events if e["kind"] == "bid"
    }
    assert bid_events == {(cy.pubkey, 20, "Dusk"), (ben.pubkey, 12, "Dusk")}
    mine = (await env.http.get(f"/api/activity?actor={ben.pubkey}")).json()
    assert [e["price"] for e in mine if e["kind"] == "bid"] == [12]

    # A declined bid stays visible but no longer counts.
    await seller.actor.post(seller.actor.base() + f"/market/offers/{low['id']}/decline")
    bids = (await env.http.get(f"/api/market/listings/{listing['id']}/bids")).json()
    assert [b["status"] for b in bids["items"]] == ["open", "declined"]
    assert (bids["count"], bids["top"]) == (1, 20)


@pytest.mark.asyncio
async def test_browse_filters_sorts_and_facets(env, cash):
    ana, ben, cy = await env.actor("Ana"), await env.actor("Ben"), await env.actor("Cy")
    env.shift_clock(-3 * 86400)
    await list_nft(env, await give_nft(env, ana, title="Dawn"), 3)
    env.shift_clock(-600)
    noon = await list_nft(env, await give_nft(env, ana, title="Noon"), 40)
    env.shift_clock(0)
    dusk = await list_nft(env, await give_nft(env, ben, title="Dusk"), 900)
    await submit(cy, await make_offer(env, cy, cash, noon, price=50))

    async def browse(query: str = "") -> Dict[str, Any]:
        r = await env.http.get(f"/api/market/listings?{query}")
        assert r.status_code == 200, r.text
        return r.json()

    def titles(page: Dict[str, Any]) -> List[str]:
        return [i["title"] for i in page["items"]]

    def sellers(page: Dict[str, Any]) -> Dict[str, int]:
        return {s["name"]: s["count"] for s in page["facets"]["sellers"]}

    everything = await browse()
    assert titles(everything) == ["Dusk", "Noon", "Dawn"] and everything["total"] == 3
    price = everything["facets"]["price"]
    assert (price["min"], price["max"]) == (3, 900)
    assert len(price["buckets"]) == 28 and sum(price["buckets"]) == 3
    assert price["buckets"][0] == price["buckets"][-1] == 1
    assert sellers(everything) == {"Ana": 2, "Ben": 1}

    # Sorts.
    assert titles(await browse("sort=old")) == ["Dawn", "Noon", "Dusk"]
    assert titles(await browse("sort=price_asc")) == ["Dawn", "Noon", "Dusk"]
    assert titles(await browse("sort=price_desc")) == ["Dusk", "Noon", "Dawn"]
    assert titles(await browse("sort=bids"))[0] == "Noon"
    assert titles(await browse("sort=top_bid")) == ["Noon", "Dusk", "Dawn"]

    # A facet ignores its own filter, so the other choices stay visible.
    ranged = await browse("min_price=10&max_price=100")
    assert titles(ranged) == ["Noon"] and ranged["total"] == 1
    assert sum(ranged["facets"]["price"]["buckets"]) == 3
    assert sellers(ranged) == {"Ana": 1}
    by_ben = await browse(f"sellers={ben.pubkey}")
    assert titles(by_ben) == ["Dusk"] and sellers(by_ben) == {"Ana": 2, "Ben": 1}
    both = await browse(f"sellers={ben.pubkey},{ana.pubkey}&sort=price_asc")
    assert titles(both) == ["Dawn", "Noon", "Dusk"]

    assert titles(await browse("bids=some")) == ["Noon"]
    assert titles(await browse("bids=none")) == ["Dusk", "Dawn"]
    assert titles(await browse("since=86400")) == ["Dusk", "Noon"]
    assert titles(await browse("q=ben")) == ["Dusk"]

    # Sales in progress are listed unless only buyable NFTs are asked for.
    async with env.db.get_connection() as conn:
        await conn.execute(
            "UPDATE market_listings SET state='reserved' WHERE id=:id",
            {"id": dusk["id"]},
        )
    assert titles(await browse()) == ["Dusk", "Noon", "Dawn"]
    available = await browse("available=1")
    assert titles(available) == ["Noon", "Dawn"] and available["total"] == 2

    paged = await browse("sort=price_asc&limit=1&offset=1")
    assert titles(paged) == ["Noon"] and paged["more"] and paged["total"] == 3

    for bad in ("sellers=abc", "bids=maybe", "min_price=-1", "since=1", "sort=hot"):
        assert (await env.http.get(f"/api/market/listings?{bad}")).status_code == 422


@pytest.mark.asyncio
async def test_listed_nft_cannot_be_deleted_until_unlisted(env):
    seller = await give_nft(env, await env.actor("Ana"))
    listing = await list_nft(env, seller, 10)
    path = seller.actor.base() + f"/wallet/cards/{seller.card_id}/delete"
    body = {
        "presentation": present(seller.cred, binding=PS_BURN_BINDING).to_bytes().hex()
    }
    r = await seller.actor.post(path, body)
    assert r.status_code == 409 and "Unlist" in r.json()["detail"]
    r = await seller.actor.post(
        seller.actor.base() + f"/market/listings/{listing['id']}/unlist"
    )
    assert r.json()["state"] == "unlisted"
    r = await seller.actor.post(path, body)
    assert r.status_code == 200, r.text
    assert await env.ledger.asset_status(seller.cred.h) == "burned"
    h = seller.cred.h.to_bytes(32, "big").hex()
    assert (await env.http.get(f"/api/images/{h}.jpg")).status_code == 404


@pytest.mark.asyncio
async def test_listing_requires_current_credential_and_owner(env):
    seller = await give_nft(env, await env.actor("Ana"))
    other = await env.actor("Mallory")
    listing = {
        "v": mp.LISTING_PROTOCOL,
        "listing_id": secrets.token_hex(16),
        "revision": 1,
        "card_id": seller.card_id,
        "seller": other.pubkey,
        "h": seller.cred.h.to_bytes(32, "big").hex(),
        "nullifier": nullifier_of(seller.cred),
        "nft_keyset": env.ledger.keyset.keyset_id,
        "price": 5,
        "claim_pubkey": seller.claim_pub,
        "created": env.market.clock(),
    }
    r = await other.post(
        other.base() + "/market/listings",
        {
            "listing": listing,
            "signature": mp.sign_purpose(
                mp.LISTING_DOMAIN, mp.listing_hash(listing), other.secret
            ),
        },
    )
    assert r.status_code == 404
    listing["seller"] = seller.actor.pubkey
    listing["nullifier"] = "00" * 48
    r = await seller.actor.post(
        seller.actor.base() + "/market/listings",
        {
            "listing": listing,
            "signature": mp.sign_purpose(
                mp.LISTING_DOMAIN, mp.listing_hash(listing), seller.actor.secret
            ),
        },
    )
    assert r.status_code == 409


# --- executor recovery ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_recovers_a_claim_whose_reply_was_lost(env, cash):
    seller = await give_nft(env, await env.actor("Ana"))
    buyer = await env.actor("Ben")
    listing = await list_nft(env, seller, 33)
    offer = await make_offer(env, buyer, cash, listing)
    offer_id = (await submit(buyer, offer)).json()["id"]
    accepted = await accept(env, seller, offer_id)
    job = await env.db.fetchone(
        "SELECT * FROM market_jobs WHERE offer_id=:o AND kind='claim'", {"o": offer_id}
    )
    # The swap reaches the mint but the executor never sees the reply.
    async with httpx.AsyncClient() as client:
        inputs = market_cash.swap_inputs(
            json.loads(job["inputs"]), job["signature"], job["preimage"]
        )
        await market_cash.submit_swap(
            client, SERVER_ENDPOINT, inputs, json.loads(job["outputs"])
        )
    await env.executor.run_due()
    paid = (
        await seller.actor.post(
            seller.actor.base() + f"/market/offers/{offer_id}/payment/claim"
        )
    ).json()
    assert paid["state"] == "done" and paid["outcome"] == "claim"
    proofs = market_cash.unblind(
        accepted.claim_outputs, paid["signatures"], keys_of(cash, cash.keyset_id)
    )
    assert sum(p["amount"] for p in proofs) == 33 and await spendable(proofs)


@pytest.mark.asyncio
async def test_executor_keeps_jobs_pending_while_the_mint_is_down(env, cash):
    seller = await give_nft(env, await env.actor("Ana"))
    buyer = await env.actor("Ben")
    listing = await list_nft(env, seller, 12)
    offer = await make_offer(env, buyer, cash, listing)
    offer_id = (await submit(buyer, offer)).json()["id"]
    await accept(env, seller, offer_id)
    dead = "http://127.0.0.1:9"
    env.market.policy.dev_mints = frozenset({SERVER_ENDPOINT, dead})
    await env.db.execute(
        "UPDATE market_jobs SET mint=:m WHERE offer_id=:o", {"m": dead, "o": offer_id}
    )
    await env.executor.run_due()
    job = await env.db.fetchone(
        "SELECT * FROM market_jobs WHERE offer_id=:o AND kind='claim'", {"o": offer_id}
    )
    assert (
        job["state"] == "pending"
        and job["attempts"] == 1
        and "unreachable" in job["last_error"]
    )
    view = (
        await seller.actor.post(seller.actor.base() + f"/market/offers/{offer_id}")
    ).json()
    assert view["cash_leg"] == "claim_pending"  # never fabricated as paid
    # The mint returns; the job completes from durable state.
    await env.db.execute(
        "UPDATE market_jobs SET mint=:m, next_attempt=0 WHERE offer_id=:o",
        {"m": SERVER_ENDPOINT, "o": offer_id},
    )
    await env.executor.run_due()
    assert (
        await seller.actor.post(seller.actor.base() + f"/market/offers/{offer_id}")
    ).json()["cash_leg"] == "claimed"


# --- wallet backups, leases and recovery records --------------------------------


@pytest.mark.asyncio
async def test_wallet_backup_lease_and_revisions(env):
    owner = await env.actor("Ana")
    phone, laptop = secrets.token_hex(16), secrets.token_hex(16)
    assert (await owner.post(owner.base() + "/money/lease", {"device": phone})).json()[
        "granted"
    ]
    assert not (
        await owner.post(owner.base() + "/money/lease", {"device": laptop})
    ).json()["granted"]
    env_a = {"version": "1", "nonce": "aa", "ciphertext": "bb"}
    r = await owner.post(
        owner.base() + "/money/backup/put",
        {"device": laptop, "base_revision": 0, "revision": 1, "envelope": env_a},
    )
    assert r.status_code == 423  # not the lease holder
    assert (
        await owner.post(
            owner.base() + "/money/backup/put",
            {"device": phone, "base_revision": 0, "revision": 7, "envelope": env_a},
        )
    ).json()["revision"] == 7
    stale = await owner.post(
        owner.base() + "/money/backup/put",
        {"device": phone, "base_revision": 0, "revision": 8, "envelope": env_a},
    )
    assert stale.status_code == 409  # compare-and-swap on revisions
    # Takeover after reading the latest revision.
    assert (await owner.post(owner.base() + "/money/backup/get")).json()[
        "revision"
    ] == 7
    assert (
        await owner.post(
            owner.base() + "/money/lease", {"device": laptop, "takeover": True}
        )
    ).json()["granted"]
    assert (
        await owner.post(
            owner.base() + "/money/backup/put",
            {"device": phone, "base_revision": 7, "revision": 8, "envelope": env_a},
        )
    ).status_code == 423
    rec = {
        "id": "offer:abc",
        "kind": "offer",
        "envelope": {"version": "1", "nonce": "cc", "ciphertext": "dd"},
    }
    assert (
        await owner.post(owner.base() + "/market/recovery/put", rec)
    ).status_code == 200
    assert (await owner.post(owner.base() + "/market/recovery/list")).json()[0][
        "id"
    ] == "offer:abc"
    other = await env.actor("Mallory")
    assert (await other.post(other.base() + "/market/recovery/list")).json() == []


# --- network boundary -------------------------------------------------------------


def test_public_address_classification():
    for blocked in (
        "127.0.0.1",
        "10.1.2.3",
        "192.168.1.1",
        "172.16.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "224.0.0.1",
    ):
        assert not is_public_address(blocked), blocked
    for allowed in ("1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"):
        assert is_public_address(allowed), allowed


@pytest.mark.asyncio
async def test_guarded_client_blocks_internal_targets_and_rebinding(monkeypatch):
    client = GuardedMintClient(MintNetPolicy())
    try:
        with pytest.raises(mp.ProtocolError):
            await client.get("http://mint.example/v1/info")  # not https

        async def fake_resolve(host: str, port: int) -> List[str]:
            return {
                "internal.example": ["10.0.0.5"],
                "rebind.example": ["93.184.216.34", "127.0.0.1"],
                "meta.example": ["169.254.169.254"],
            }[host]

        monkeypatch.setattr("cashu.nft.market_net._resolve", fake_resolve)
        for host in ("internal.example", "rebind.example", "meta.example"):
            with pytest.raises(BlockedDestination):
                await client.get(f"https://{host}/v1/info")
        with pytest.raises(mp.ProtocolError):
            await client.get("https://user:pw@mint.example/v1/info")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_guarded_client_does_not_follow_redirects(env):
    async def app(scope, receive, send):  # minimal ASGI redirector
        await send(
            {
                "type": "http.response.start",
                "status": 302,
                "headers": [(b"location", b"http://169.254.169.254/")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    client = GuardedMintClient(
        MintNetPolicy(dev_mints=frozenset({"http://redirect.test"}))
    )
    client._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))

    async def resolve(host: str, port: int) -> List[str]:
        return ["127.0.0.1"]

    original = net._resolve
    net._resolve = resolve  # type: ignore[assignment]
    try:
        with pytest.raises(BlockedDestination):
            await client.get("http://redirect.test/v1/info")
    finally:
        net._resolve = original  # type: ignore[assignment]
        await client.aclose()


@pytest.mark.asyncio
async def test_market_config_and_mint_eligibility(env):
    config = (await env.http.get("/api/market/config")).json()
    assert (
        config["escrow"]["version"] == "esc1"
        and len(config["escrow"]["public_key"]) == 130
    )
    assert config["accept_window"] == 3600 and config["default_lifetime"] == 24 * 3600
    assert [m["url"] for m in config["mint_shortcuts"]][
        0
    ] == "https://testnut.cashu.space"
    check = (
        await env.http.get("/api/market/mints/check", params={"url": SERVER_ENDPOINT})
    ).json()
    assert check["eligible"], check["reasons"]
    assert check["keyset_id"].startswith(("00", "01"))
    quote = (
        await env.http.get(
            "/api/market/quote", params={"mint": SERVER_ENDPOINT, "price": 21}
        )
    ).json()
    assert quote["amount"] == 21 and quote["claim_fee"] == 0
    # Custom URLs that aren't configured dev mints must be public HTTPS.
    r = await env.http.get(
        "/api/market/mints/check", params={"url": "http://127.0.0.1:3338"}
    )
    assert r.status_code == 400


def test_fee_math_pays_exact_net_price():
    assert funding_amount(100, 0) == (100, 0)
    amount, fee = funding_amount(100, 100)  # 100 ppk per input
    assert amount - fee == 100 and fee == fee_for(len(amount_split(amount)), 100)
    amount, fee = funding_amount(1023, 1000)  # one sat per input
    assert amount - fee == 1023 and fee >= 1
