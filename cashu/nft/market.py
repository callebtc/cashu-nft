"""NFT marketplace: listings, funded offers, NFT-mint-assisted settlement,
the restricted settlement executor, inbox events and encrypted recovery
records. Protocol: cashu/nft/MARKETPLACE_PLAN.md ("Protocol v1").

Boundaries kept separate even though they run in one process:

* ``Escrow`` (issuer role) opens buyer preimage envelopes, only to validate
  them at registration and to satisfy the NFT contract at delivery.
* ``Market`` owns listings, offers and the atomic delivery transaction.
* ``Executor`` runs pre-authorized fixed-output swaps against payment mints
  through the SSRF-guarded client; it never sees owner output secrets.

Every state dimension is its own column: listing visibility, offer
disposition, NFT leg, cash leg and buyer publication. Funded never means
paid, a deadline never means refunded, and delivery never by itself means
the seller was paid: cash outcomes come from the payment mint.

Experimental cryptography and schema: requires human review before use.
"""

import asyncio
import hashlib
import json
import logging
import math
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from ..core.crypto.b_dhke import carol_verify_dleq
from ..core.crypto.bls import PublicKey
from ..core.crypto.ps import DlogEqProof, Presentation, verify_showing
from ..core.crypto.secp import PrivateKey as SecpPrivate
from ..core.crypto.secp import PublicKey as SecpPublic
from ..core.db import Connection, LockOptions
from ..core.split import amount_split
from . import market_cash
from . import market_protocol as mp
from .ledger import NFTContract, NFTError
from .market_net import GuardedMintClient, MintNetPolicy
from .nostr_sig import PROFILE_SIG, verify_signature
from .wallet import NFTClient

log = logging.getLogger(__name__)

HEX32 = r"^[0-9a-f]{32}$"
HEX64 = r"^[0-9a-f]{64}$"
G1HEX = r"^[0-9a-f]{96}$"
SECP = r"^0[23][0-9a-f]{64}$"
SIG = r"^[0-9a-f]{128}$"
CLAIM_DOMAIN = "Cashu_NFT_Portfolio_Claim_v1\n"
SHOW_DOMAIN = "Cashu_NFT_Portfolio_Show_v1"
COMPATIBLE_MINTS = {
    # Implementations whose SIG_ALL transcript is the unframed v0 digest
    # (cashu-ts SigAll docs: "CDK >= 0.14.0, Nutshell > 0.20.2").
    "nutshell": (0, 20, 3),
    "cdk-mintd": (0, 14, 0),
    "cdk": (0, 14, 0),
}
MINT_SHORTCUTS = [
    {
        "name": "Testnut (test sats)",
        "url": "https://testnut.cashu.space",
        "test_value": True,
    },
    {
        "name": "Minibits",
        "url": "https://mint.minibits.cash/Bitcoin",
        "test_value": False,
    },
    {"name": "Coinos", "url": "https://mint.coinos.io", "test_value": False},
    {"name": "Macadamia", "url": "https://mint.macadamia.cash", "test_value": False},
]
TEST_VALUE_MINTS = {"https://testnut.cashu.space"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NftRef(Strict):
    keyset_id: StrictStr = Field(max_length=80)
    h: StrictStr = Field(pattern=HEX64)
    nullifier: StrictStr = Field(pattern=G1HEX)


class Payment(Strict):
    mint: StrictStr = Field(max_length=512)
    unit: Literal["sat"]
    keyset_id: StrictStr = Field(pattern=r"^0[01][0-9a-f]{14,64}$")
    amount: StrictInt = Field(gt=0, le=mp.MAX_PRICE)
    claim_fee: StrictInt = Field(ge=0, le=mp.MAX_PRICE)
    refund_fee: StrictInt = Field(ge=0, le=mp.MAX_PRICE)


class OfferManifest(Strict):
    v: Literal["cashu-nft-offer-1"]
    offer_id: StrictStr = Field(pattern=HEX32)
    listing_id: StrictStr = Field(pattern=HEX32)
    listing_revision: StrictInt = Field(ge=1)
    nft: NftRef
    seller: StrictStr = Field(pattern=HEX64)
    buyer: StrictStr = Field(pattern=HEX64)
    price: StrictInt = Field(gt=0, le=mp.MAX_PRICE)
    payment: Payment
    hashlock: StrictStr = Field(pattern=HEX64)
    claim_pubkey: StrictStr = Field(pattern=SECP)
    refund_pubkey: StrictStr = Field(pattern=SECP)
    cash_deadline: StrictInt
    accept_deadline: StrictInt
    nft_destination: StrictStr = Field(pattern=G1HEX)
    escrow_key: StrictStr = Field(max_length=16)
    created: StrictInt


class ListingManifest(Strict):
    v: Literal["cashu-nft-listing-1"]
    listing_id: StrictStr = Field(pattern=HEX32)
    revision: StrictInt = Field(ge=1)
    card_id: StrictStr = Field(min_length=1, max_length=64)
    seller: StrictStr = Field(pattern=HEX64)
    h: StrictStr = Field(pattern=HEX64)
    nullifier: StrictStr = Field(pattern=G1HEX)
    nft_keyset: StrictStr = Field(max_length=80)
    price: StrictInt = Field(gt=0, le=mp.MAX_PRICE)
    claim_pubkey: StrictStr = Field(pattern=SECP)
    created: StrictInt


class BlindedOutput(Strict):
    amount: StrictInt = Field(gt=0, le=mp.MAX_PRICE)
    id: StrictStr = Field(pattern=r"^0[01][0-9a-f]{14,64}$")
    B_: StrictStr = Field(pattern=SECP)


class HtlcProof(BaseModel):
    model_config = ConfigDict(extra="ignore")
    amount: StrictInt = Field(gt=0, le=mp.MAX_PRICE)
    id: StrictStr = Field(max_length=80)
    secret: StrictStr = Field(max_length=2048)
    C: StrictStr = Field(pattern=SECP)
    dleq: Optional[Dict[str, StrictStr]] = None


class ListingRequest(Strict):
    listing: ListingManifest
    signature: StrictStr = Field(pattern=PROFILE_SIG)


class RefundAuthorization(Strict):
    outputs: List[BlindedOutput] = Field(min_length=1, max_length=64)
    signature: StrictStr = Field(pattern=SIG)


class OfferRequest(Strict):
    manifest: OfferManifest
    buyer_signature: StrictStr = Field(pattern=PROFILE_SIG)
    receive_proof: StrictStr = Field(pattern=r"^[0-9a-f]{128}$")
    escrow: Dict[str, StrictStr]
    proofs: List[HtlcProof] = Field(min_length=1, max_length=64)
    refund: RefundAuthorization


class Acceptance(Strict):
    v: Literal["cashu-nft-accept-1"]
    offer_id: StrictStr = Field(pattern=HEX32)
    manifest_hash: StrictStr = Field(pattern=HEX64)
    claim_outputs: List[BlindedOutput] = Field(min_length=1, max_length=64)
    claim_signature: StrictStr = Field(pattern=SIG)
    accepted: StrictInt


class AcceptRequest(Strict):
    acceptance: Acceptance
    seller_signature: StrictStr = Field(pattern=PROFILE_SIG)
    presentation: StrictStr = Field(pattern=r"^[0-9a-f]{642}$")


class RecoveryRecord(Strict):
    id: StrictStr = Field(pattern=r"^[a-z0-9:_-]{1,96}$")
    kind: StrictStr = Field(pattern=r"^[a-z_]{1,32}$")
    envelope: Dict[str, StrictStr]


class BackupRequest(Strict):
    device: StrictStr = Field(pattern=HEX32)
    base_revision: StrictInt = Field(ge=0)
    revision: StrictInt = Field(ge=1)  # client-numbered; must increase
    envelope: Dict[str, StrictStr]


class LeaseRequest(Strict):
    device: StrictStr = Field(pattern=HEX32)
    takeover: bool = False
    release: bool = False  # the holder closes the wallet on this device


class PublishPurchase(Strict):
    encrypted_credential: Dict[str, Any]
    showing: StrictStr = Field(max_length=4096)
    signature: StrictStr = Field(pattern=PROFILE_SIG)


def _now() -> int:
    return int(time.time())


def _parse(model: Any, raw: bytes) -> Any:
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        errors = exc.errors()
        where = ".".join(str(p) for p in errors[0]["loc"]) if errors else ""
        raise HTTPException(400, f"Invalid request ({where or 'body'}).")


def bid_status(disposition: str, accept_deadline: int, now: int) -> str:
    """Public status of an offer, as a bid on its listing."""
    if disposition == "funded":
        return "open" if accept_deadline > now else "expired"
    return {"accepted": "accepted", "declined": "declined"}.get(disposition, "closed")


def fee_for(n_inputs: int, fee_ppk: int) -> int:
    """NUT-02 input fee for n inputs of one keyset."""
    return math.ceil(n_inputs * fee_ppk / 1000)


def funding_amount(price: int, fee_ppk: int) -> Tuple[int, int]:
    """(locked amount, claim/refund fee) so the claim pays exactly ``price``
    net. The HTLC splits into amount_split(amount) proofs."""
    fee = 0
    for _ in range(16):
        amount = price + fee
        needed = fee_for(len(amount_split(amount)), fee_ppk)
        if needed == fee:
            return amount, fee
        fee = needed
    raise mp.ProtocolError("fee calculation does not converge")


def parse_version(info: Dict[str, Any]) -> Tuple[str, Tuple[int, ...]]:
    raw = str(info.get("version", ""))
    name, _, ver = raw.partition("/")
    parts: List[int] = []
    for piece in ver.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return name.strip().lower(), tuple(parts)


class PaymentMints:
    """Capability, keyset and authenticity checks for buyer-chosen mints,
    fetched through the SSRF guard and cached briefly."""

    def __init__(self, client: GuardedMintClient, ttl: int = 300):
        self.client = client
        self.ttl = ttl
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    async def _get(self, url: str) -> Dict[str, Any]:
        hit = self._cache.get(url)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            response = await self.client.get(url)
        except mp.ProtocolError:
            raise
        except Exception as exc:
            raise market_cash.MintUnavailable(str(exc)) from exc
        if response.status_code != 200:
            raise market_cash.MintUnavailable(f"mint returned {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise market_cash.MintUnavailable("mint returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise market_cash.MintUnavailable("mint returned unexpected data")
        self._cache[url] = (time.monotonic() + self.ttl, data)
        return data

    async def eligibility(self, mint: str) -> Dict[str, Any]:
        """Fail-closed marketplace eligibility. Ordinary wallet use of a mint
        does not depend on this."""
        reasons: List[str] = []
        info = await self._get(f"{mint}/v1/info")
        raw_nuts = info.get("nuts")
        nuts: Dict[str, Any] = raw_nuts if isinstance(raw_nuts, dict) else {}
        for nut in ("7", "9", "10", "11", "12", "14"):
            entry = nuts.get(nut)
            if not isinstance(entry, dict) or entry.get("supported") is not True:
                reasons.append(f"NUT-{nut} not advertised")
        name, version = parse_version(info)
        minimum = COMPATIBLE_MINTS.get(name)
        if minimum is None:
            reasons.append(
                f"unknown SIG_ALL implementation ({info.get('version', 'no version')})"
            )
        elif version < minimum:
            reasons.append(
                f"{name} {'.'.join(map(str, version))} predates the v0 SIG_ALL transcript"
            )
        keysets = (await self._get(f"{mint}/v1/keysets")).get("keysets", [])
        usable = [
            k
            for k in keysets
            if isinstance(k, dict)
            and k.get("unit") == "sat"
            and k.get("active") is True
            and str(k.get("id", "")).startswith(("00", "01"))
        ]
        if not usable:
            reasons.append("no active pre-v3 sat keyset (HTLC secrets need one)")
        usable.sort(key=lambda k: int(k.get("input_fee_ppk", 0) or 0))
        keyset = usable[0] if usable else None
        return {
            "mint": mint,
            "eligible": not reasons,
            "reasons": reasons,
            "version": info.get("version"),
            "keyset_id": keyset["id"] if keyset else None,
            "fee_ppk": int(keyset.get("input_fee_ppk", 0) or 0) if keyset else None,
            "test_value": mint in TEST_VALUE_MINTS,
        }

    async def keyset(self, mint: str, keyset_id: str) -> Dict[str, Any]:
        keysets = (await self._get(f"{mint}/v1/keysets")).get("keysets", [])
        meta = next(
            (k for k in keysets if isinstance(k, dict) and k.get("id") == keyset_id),
            None,
        )
        if meta is None or meta.get("unit") != "sat" or meta.get("active") is not True:
            raise mp.ProtocolError(
                "payment keyset is not an active sat keyset of this mint"
            )
        keys_doc = await self._get(f"{mint}/v1/keys/{keyset_id}")
        entries = [k for k in keys_doc.get("keysets", []) if k.get("id") == keyset_id]
        if not entries:
            raise mp.ProtocolError("mint did not return keys for the payment keyset")
        keys = {int(a): str(k) for a, k in entries[0].get("keys", {}).items()}
        return {"fee_ppk": int(meta.get("input_fee_ppk", 0) or 0), "keys": keys}

    @staticmethod
    def verify_dleq(proofs: List[HtlcProof], keys: Dict[int, str]) -> None:
        """NUT-12: the mint really signed these proofs (state lookup alone
        does not prove a supplied signature is genuine)."""
        for p in proofs:
            dleq = p.dleq or {}
            if p.amount not in keys or not all(k in dleq for k in ("e", "s", "r")):
                raise mp.ProtocolError("payment proofs need DLEQ proofs from the mint")
            try:
                ok = carol_verify_dleq(
                    secret_msg=p.secret,
                    r=SecpPrivate(bytes.fromhex(dleq["r"])),
                    C=SecpPublic(bytes.fromhex(p.C)),
                    e=SecpPrivate(bytes.fromhex(dleq["e"])),
                    s=SecpPrivate(bytes.fromhex(dleq["s"])),
                    A=SecpPublic(bytes.fromhex(keys[p.amount])),
                )
            except ValueError:
                ok = False
            if not ok:
                raise mp.ProtocolError("a payment proof has an invalid DLEQ proof")


class Market:
    def __init__(
        self,
        portfolio: Any,
        data_dir: str,
        policy: Optional[MintNetPolicy] = None,
        clock: Callable[[], int] = _now,
    ):
        self.portfolio = portfolio
        self.db = portfolio.db
        self.ledger = portfolio.ledger
        seed = (Path(data_dir) / "mint.seed").read_bytes()
        self.escrow = mp.EscrowKey(seed)
        self.receipts = mp.ReceiptKey(seed)
        self.policy = policy or MintNetPolicy()
        self.client = GuardedMintClient(self.policy)
        self.mints = PaymentMints(self.client)
        self.clock = clock
        self.waiters: Dict[str, asyncio.Event] = {}

    # --- schema -------------------------------------------------------------

    async def migrate(self) -> None:
        async with self.db.get_connection() as conn:
            for statement in (
                """CREATE TABLE IF NOT EXISTS market_listings (
                    id TEXT PRIMARY KEY, card_id TEXT NOT NULL, seller TEXT NOT NULL,
                    h TEXT NOT NULL, nullifier TEXT NOT NULL, price INTEGER NOT NULL,
                    claim_pubkey TEXT NOT NULL, revision INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','reserved','sold','unlisted','stale')),
                    manifest TEXT NOT NULL, signature TEXT NOT NULL,
                    created INTEGER NOT NULL, updated INTEGER NOT NULL)""",
                "CREATE UNIQUE INDEX IF NOT EXISTS market_listing_live ON market_listings(card_id) WHERE state IN ('active','reserved')",
                "CREATE INDEX IF NOT EXISTS market_listing_state ON market_listings(state, updated)",
                """CREATE TABLE IF NOT EXISTS market_listing_revisions (
                    listing_id TEXT NOT NULL, revision INTEGER NOT NULL, price INTEGER NOT NULL,
                    manifest TEXT NOT NULL, signature TEXT NOT NULL, created INTEGER NOT NULL,
                    PRIMARY KEY (listing_id, revision))""",
                """CREATE TABLE IF NOT EXISTS market_offers (
                    id TEXT PRIMARY KEY, listing_id TEXT NOT NULL, listing_revision INTEGER NOT NULL,
                    seller TEXT NOT NULL, buyer TEXT NOT NULL, h TEXT NOT NULL, nullifier TEXT NOT NULL,
                    price INTEGER NOT NULL, amount INTEGER NOT NULL, mint TEXT NOT NULL,
                    keyset_id TEXT NOT NULL, test_value INTEGER NOT NULL,
                    cash_deadline INTEGER NOT NULL, accept_deadline INTEGER NOT NULL,
                    destination TEXT NOT NULL UNIQUE, manifest TEXT NOT NULL, manifest_hash TEXT NOT NULL,
                    buyer_signature TEXT NOT NULL, receive_proof TEXT NOT NULL, escrow TEXT NOT NULL,
                    proofs TEXT NOT NULL,
                    disposition TEXT NOT NULL CHECK(disposition IN ('funded','accepted','declined','superseded','closed')),
                    nft_leg TEXT NOT NULL CHECK(nft_leg IN ('unlocked','delivered')),
                    cash_leg TEXT NOT NULL CHECK(cash_leg IN ('locked','claim_pending','claimed','refund_pending','refunded','needs_attention')),
                    publication TEXT NOT NULL CHECK(publication IN ('none','awaiting_sync','published')),
                    created INTEGER NOT NULL, updated INTEGER NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS market_offer_listing ON market_offers(listing_id)",
                "CREATE INDEX IF NOT EXISTS market_offer_buyer ON market_offers(buyer)",
                "CREATE INDEX IF NOT EXISTS market_offer_seller ON market_offers(seller)",
                """CREATE TABLE IF NOT EXISTS market_jobs (
                    id TEXT PRIMARY KEY, offer_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('claim','refund')),
                    mint TEXT NOT NULL, inputs TEXT NOT NULL, outputs TEXT NOT NULL,
                    signature TEXT NOT NULL, preimage TEXT, not_before INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','done','lost','needs_attention')),
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL,
                    lease_owner TEXT, lease_until INTEGER, result TEXT, outcome TEXT, last_error TEXT,
                    created INTEGER NOT NULL, updated INTEGER NOT NULL,
                    UNIQUE (offer_id, kind))""",
                "CREATE INDEX IF NOT EXISTS market_job_due ON market_jobs(state, next_attempt)",
                """CREATE TABLE IF NOT EXISTS market_deliveries (
                    offer_id TEXT PRIMARY KEY, contract_id TEXT NOT NULL, nullifier TEXT NOT NULL,
                    destination TEXT NOT NULL, u TEXT NOT NULL, v TEXT NOT NULL,
                    receipt TEXT NOT NULL, receipt_signature TEXT NOT NULL, delivered INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS market_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT NOT NULL, kind TEXT NOT NULL,
                    offer_id TEXT, listing_id TEXT, payload TEXT NOT NULL, created INTEGER NOT NULL,
                    read INTEGER NOT NULL DEFAULT 0, dedupe TEXT UNIQUE)""",
                "CREATE INDEX IF NOT EXISTS market_event_inbox ON market_events(recipient, id)",
                """CREATE TABLE IF NOT EXISTS market_sales (
                    offer_id TEXT PRIMARY KEY, listing_id TEXT NOT NULL, card_id TEXT NOT NULL,
                    h TEXT NOT NULL, title TEXT NOT NULL, seller TEXT NOT NULL, buyer TEXT NOT NULL,
                    created INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS market_recovery (
                    pubkey TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, envelope TEXT NOT NULL,
                    updated INTEGER NOT NULL, PRIMARY KEY (pubkey, id))""",
                """CREATE TABLE IF NOT EXISTS money_backups (
                    pubkey TEXT PRIMARY KEY, revision INTEGER NOT NULL, envelope TEXT NOT NULL,
                    device TEXT NOT NULL, updated INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS money_leases (
                    pubkey TEXT PRIMARY KEY, device TEXT NOT NULL, until INTEGER NOT NULL)""",
            ):
                await conn.execute(statement)

    # --- config ---------------------------------------------------------------

    def public_config(self) -> Dict[str, Any]:
        return {
            "protocol": mp.PROTOCOL,
            "nft_keyset_id": self.ledger.keyset.keyset_id,
            "escrow": {
                "version": self.escrow.version,
                "public_key": self.escrow.public_key,
            },
            "receipt": {
                "version": self.receipts.version,
                "public_key": self.receipts.public_key,
            },
            "accept_window": mp.ACCEPT_WINDOW,
            "clock_skew": mp.CLOCK_SKEW,
            "min_lifetime": mp.MIN_LIFETIME,
            "max_lifetime": mp.MAX_LIFETIME,
            "default_lifetime": mp.DEFAULT_LIFETIME,
            "mint_shortcuts": MINT_SHORTCUTS,
            "dev_mints": sorted(self.policy.dev_mints),
            "now": self.clock(),
        }

    def mint_url(self, url: str) -> str:
        try:
            return self.policy.check_url(url)
        except mp.ProtocolError as exc:
            raise HTTPException(400, str(exc))

    # --- events ---------------------------------------------------------------

    async def emit(
        self,
        conn: Connection,
        recipient: str,
        kind: str,
        payload: Dict[str, Any],
        offer_id: Optional[str] = None,
        listing_id: Optional[str] = None,
        dedupe: Optional[str] = None,
    ) -> None:
        await conn.execute(
            """INSERT INTO market_events(recipient,kind,offer_id,listing_id,payload,created,dedupe)
            VALUES(:r,:k,:o,:l,:p,:t,:d) ON CONFLICT(dedupe) DO NOTHING""",
            {
                "r": recipient,
                "k": kind,
                "o": offer_id,
                "l": listing_id,
                "p": json.dumps(payload, separators=(",", ":")),
                "t": self.clock(),
                "d": dedupe
                or f"{recipient}:{kind}:{offer_id or listing_id or secrets.token_hex(8)}",
            },
        )
        waiter = self.waiters.get(recipient)
        if waiter:
            waiter.set()

    async def inbox(self, pubkey: str, after: int, wait: float) -> Dict[str, Any]:
        """Authenticated long-poll with a durable cursor; clients reconnect
        from their stored cursor and re-read authoritative offer state."""
        deadline = time.monotonic() + max(0.0, min(wait, 25.0))
        while True:
            rows = await self.db.fetchall(
                "SELECT * FROM market_events WHERE recipient=:p AND id>:a ORDER BY id LIMIT 100",
                {"p": pubkey, "a": after},
            )
            remaining = deadline - time.monotonic()
            if rows or remaining <= 0:
                unread = await self.db.fetchone(
                    "SELECT COUNT(*) AS n FROM market_events WHERE recipient=:p AND read=0",
                    {"p": pubkey},
                )
                return {
                    "events": [
                        {
                            "id": r["id"],
                            "kind": r["kind"],
                            "offer_id": r["offer_id"],
                            "listing_id": r["listing_id"],
                            "payload": json.loads(r["payload"]),
                            "created": r["created"],
                            "read": bool(r["read"]),
                        }
                        for r in rows
                    ],
                    "cursor": rows[-1]["id"] if rows else after,
                    "unread": unread["n"] if unread else 0,
                }
            event = self.waiters.setdefault(pubkey, asyncio.Event())
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

    async def mark_read(self, pubkey: str, upto: int) -> None:
        await self.db.execute(
            "UPDATE market_events SET read=1 WHERE recipient=:p AND id<=:u",
            {"p": pubkey, "u": upto},
        )

    # --- listings -------------------------------------------------------------

    async def card_listed(self, conn: Connection, card_id: str) -> bool:
        row = await conn.fetchone(
            "SELECT id FROM market_listings WHERE card_id=:c AND state IN ('active','reserved')",
            {"c": card_id},
        )
        return row is not None

    async def create_listing(self, seller: str, body: ListingRequest) -> Dict[str, Any]:
        listing = body.listing
        if listing.seller != seller or listing.revision != 1:
            raise HTTPException(
                400, "A new listing starts at revision 1 and is signed by its owner."
            )
        doc = listing.model_dump()
        if not mp.verify_purpose(
            mp.LISTING_DOMAIN, mp.listing_hash(doc), body.signature, seller
        ):
            raise HTTPException(403, "The listing needs your profile signature.")
        now = self.clock()
        if abs(listing.created - now) > mp.CLOCK_SKEW:
            raise HTTPException(400, "Check your device clock and try again.")
        if listing.nft_keyset != self.ledger.keyset.keyset_id:
            raise HTTPException(400, "This NFT belongs to another mint.")
        async with self.db.get_connection(
            locks=[
                LockOptions(table="market_listings"),
                LockOptions(table="portfolio_cards"),
            ]
        ) as conn:
            card = await conn.fetchone(
                "SELECT * FROM portfolio_cards WHERE id=:id AND pubkey=:p",
                {"id": listing.card_id, "p": seller},
            )
            if card is None or card["status"] == "sent":
                raise HTTPException(404, "That NFT is not in your collection.")
            if card["status"] != "owned":
                # 'ready' means a transfer JPG or link exists: rotate first.
                raise HTTPException(
                    409,
                    "Cancel the pending transfer before listing; that rotates the credential.",
                )
            if not card["encrypted_credential"]:
                raise HTTPException(
                    409, "Move this NFT into your browser wallet first."
                )
            current = NFTClient.decode_showing(card["showing"])[1].nullifier.format()
            if current.hex() != listing.nullifier or card["h"] != listing.h:
                raise HTTPException(
                    409, "The listing doesn't match this NFT's current credential."
                )
            if await self.ledger.is_spent(current):
                raise HTTPException(409, "This NFT was already transferred.")
            if await self.card_listed(conn, listing.card_id):
                raise HTTPException(409, "This NFT is already listed.")
            try:
                await conn.execute(
                    """INSERT INTO market_listings(id,card_id,seller,h,nullifier,price,claim_pubkey,revision,state,
                    manifest,signature,created,updated)
                    VALUES(:id,:card,:seller,:h,:n,:price,:claim,1,'active',:m,:sig,:t,:t)""",
                    {
                        "id": listing.listing_id,
                        "card": listing.card_id,
                        "seller": seller,
                        "h": listing.h,
                        "n": listing.nullifier,
                        "price": listing.price,
                        "claim": listing.claim_pubkey,
                        "m": json.dumps(doc, separators=(",", ":")),
                        "sig": body.signature,
                        "t": now,
                    },
                )
            except Exception:
                raise HTTPException(409, "This listing already exists.")
            await conn.execute(
                """INSERT INTO market_listing_revisions(listing_id,revision,price,manifest,signature,created)
                VALUES(:id,1,:price,:m,:sig,:t)""",
                {
                    "id": listing.listing_id,
                    "price": listing.price,
                    "m": json.dumps(doc, separators=(",", ":")),
                    "sig": body.signature,
                    "t": now,
                },
            )
        return await self.listing(listing.listing_id)

    async def revise_listing(
        self, seller: str, listing_id: str, body: ListingRequest
    ) -> Dict[str, Any]:
        new = body.listing
        doc = new.model_dump()
        if new.seller != seller or new.listing_id != listing_id:
            raise HTTPException(400, "Revise your own listing.")
        if not mp.verify_purpose(
            mp.LISTING_DOMAIN, mp.listing_hash(doc), body.signature, seller
        ):
            raise HTTPException(403, "The revision needs your profile signature.")
        async with self.db.get_connection(
            locks=[LockOptions(table="market_listings")]
        ) as conn:
            row = await conn.fetchone(
                "SELECT * FROM market_listings WHERE id=:id AND seller=:s",
                {"id": listing_id, "s": seller},
            )
            if row is None:
                raise HTTPException(404, "Listing not found.")
            if row["state"] != "active":
                raise HTTPException(409, "This listing can't be edited right now.")
            if new.revision != row["revision"] + 1:
                raise HTTPException(409, "This listing changed. Reload and try again.")
            revised, current = new.model_dump(), json.loads(row["manifest"])
            for field_name in (
                "card_id",
                "h",
                "nullifier",
                "claim_pubkey",
                "nft_keyset",
            ):
                if revised[field_name] != current[field_name]:
                    raise HTTPException(400, "Only the price can change in a revision.")
            await conn.execute(
                """UPDATE market_listings SET price=:p, revision=:r, manifest=:m, signature=:sig, updated=:t
                WHERE id=:id AND revision=:old AND state='active'""",
                {
                    "p": new.price,
                    "r": new.revision,
                    "m": json.dumps(doc, separators=(",", ":")),
                    "sig": body.signature,
                    "t": self.clock(),
                    "id": listing_id,
                    "old": row["revision"],
                },
            )
            await conn.execute(
                """INSERT INTO market_listing_revisions(listing_id,revision,price,manifest,signature,created)
                VALUES(:id,:r,:p,:m,:sig,:t)""",
                {
                    "id": listing_id,
                    "r": new.revision,
                    "p": new.price,
                    "m": json.dumps(doc, separators=(",", ":")),
                    "sig": body.signature,
                    "t": self.clock(),
                },
            )
        return await self.listing(listing_id)

    async def unlist(self, seller: str, listing_id: str) -> Dict[str, Any]:
        async with self.db.get_connection(
            locks=[
                LockOptions(table="market_listings"),
                LockOptions(table="market_offers"),
            ]
        ) as conn:
            row = await conn.fetchone(
                "SELECT * FROM market_listings WHERE id=:id AND seller=:s",
                {"id": listing_id, "s": seller},
            )
            if row is None:
                raise HTTPException(404, "Listing not found.")
            if row["state"] == "reserved":
                raise HTTPException(409, "A sale is settling. Wait until it completes.")
            if row["state"] in ("active", "stale"):
                await conn.execute(
                    "UPDATE market_listings SET state='unlisted', updated=:t WHERE id=:id",
                    {"t": self.clock(), "id": listing_id},
                )
                await self._close_offers(conn, listing_id, "declined", "offer_declined")
        return await self.listing(listing_id)

    async def _close_offers(
        self,
        conn: Connection,
        listing_id: str,
        disposition: str,
        kind: str,
        keep: str = "",
    ) -> None:
        rows = await conn.fetchall(
            "SELECT id, buyer, cash_deadline FROM market_offers WHERE listing_id=:l AND disposition='funded' AND id!=:keep",
            {"l": listing_id, "keep": keep},
        )
        for r in rows:
            await conn.execute(
                "UPDATE market_offers SET disposition=:d, updated=:t WHERE id=:id AND disposition='funded'",
                {"d": disposition, "t": self.clock(), "id": r["id"]},
            )
            await self.emit(
                conn,
                r["buyer"],
                kind,
                {"refund_after": r["cash_deadline"]},
                offer_id=r["id"],
                listing_id=listing_id,
            )

    async def _refresh_stale(self, rows: List[Any]) -> None:
        for r in rows:
            if r["state"] == "active" and await self.ledger.is_spent(
                bytes.fromhex(r["nullifier"])
            ):
                async with self.db.get_connection(
                    locks=[LockOptions(table="market_listings")]
                ) as conn:
                    await conn.execute(
                        "UPDATE market_listings SET state='stale', updated=:t WHERE id=:id AND state='active'",
                        {"t": self.clock(), "id": r["id"]},
                    )

    async def _bid_summaries(self, listing_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Open bids per listing: how many, from how many bidders, the top one."""
        if not listing_ids:
            return {}
        params: Dict[str, Any] = {f"l{i}": v for i, v in enumerate(listing_ids)}
        clause = ",".join(f":l{i}" for i in range(len(listing_ids)))
        rows = await self.db.fetchall(
            f"""SELECT listing_id, COUNT(*) AS n, COUNT(DISTINCT buyer) AS bidders,
            MAX(price) AS top FROM market_offers
            WHERE disposition='funded' AND accept_deadline>:now AND listing_id IN ({clause})
            GROUP BY listing_id""",
            {**params, "now": self.clock()},
        )
        return {
            r["listing_id"]: {"count": r["n"], "bidders": r["bidders"], "top": r["top"]}
            for r in rows
        }

    async def bids(self, listing_id: str) -> Dict[str, Any]:
        """Every offer on a listing as a public bid, highest first. Bidder,
        amount, time and status are public; the payment mint, proofs and
        settlement details stay participant-only."""
        await self.listing(listing_id)
        rows = await self.db.fetchall(
            """SELECT o.id, o.buyer, p.name AS buyer_name, o.price, o.test_value,
            o.disposition, o.accept_deadline, o.created FROM market_offers o
            LEFT JOIN portfolio_profiles p ON p.pubkey=o.buyer WHERE o.listing_id=:l
            ORDER BY o.price DESC, o.created ASC""",
            {"l": listing_id},
        )
        now = self.clock()
        items = [
            {
                "id": r["id"],
                "buyer": r["buyer"],
                "buyer_name": r["buyer_name"],
                "price": r["price"],
                "test_value": bool(r["test_value"]),
                "status": bid_status(r["disposition"], r["accept_deadline"], now),
                "expires": r["accept_deadline"],
                "created": r["created"],
            }
            for r in rows
        ]
        live = [i for i in items if i["status"] == "open"]
        return {
            "items": items,
            "count": len(live),
            "bidders": len({i["buyer"] for i in live}),
            "top": live[0]["price"] if live else None,
        }

    def _public_listing(
        self, row: Any, bids: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        r = dict(row)
        return {
            "id": r["id"],
            "card_id": r["card_id"],
            "seller": r["seller"],
            "seller_name": r.get("seller_name"),
            "title": r.get("title"),
            "h": r["h"],
            "nullifier": r["nullifier"],
            "price": r["price"],
            "claim_pubkey": r["claim_pubkey"],
            "revision": r["revision"],
            "state": r["state"],
            "created": r["created"],
            "updated": r["updated"],
            "bids": bids or {"count": 0, "bidders": 0, "top": None},
        }

    async def listing(self, listing_id: str) -> Dict[str, Any]:
        row = await self.db.fetchone(
            """SELECT l.*, p.name AS seller_name, c.title AS title FROM market_listings l
            LEFT JOIN portfolio_profiles p ON p.pubkey=l.seller
            LEFT JOIN portfolio_cards c ON c.id=l.card_id WHERE l.id=:id""",
            {"id": listing_id},
        )
        if row is None:
            raise HTTPException(404, "Listing not found.")
        await self._refresh_stale([row])
        row = await self.db.fetchone(
            """SELECT l.*, p.name AS seller_name, c.title AS title FROM market_listings l
            LEFT JOIN portfolio_profiles p ON p.pubkey=l.seller
            LEFT JOIN portfolio_cards c ON c.id=l.card_id WHERE l.id=:id""",
            {"id": listing_id},
        )
        summary = await self._bid_summaries([listing_id])
        return self._public_listing(row, summary.get(listing_id))

    async def listings(
        self, sort: str, q: str, seller: Optional[str], limit: int, offset: int
    ) -> Dict[str, Any]:
        order = {
            "new": "l.updated DESC",
            "price_asc": "l.price ASC, l.updated DESC",
            "price_desc": "l.price DESC, l.updated DESC",
        }[sort]
        rows = await self.db.fetchall(
            f"""SELECT l.*, p.name AS seller_name, c.title AS title FROM market_listings l
            LEFT JOIN portfolio_profiles p ON p.pubkey=l.seller
            LEFT JOIN portfolio_cards c ON c.id=l.card_id
            WHERE l.state IN ('active','reserved') AND (:seller='' OR l.seller=:seller)
            AND (:q='' OR lower(c.title) LIKE :like OR lower(p.name) LIKE :like)
            ORDER BY {order} LIMIT :limit OFFSET :offset""",
            {
                "seller": seller or "",
                "q": q,
                "like": f"%{q.lower()}%",
                "limit": limit + 1,
                "offset": offset,
            },
        )
        await self._refresh_stale(rows[:limit])
        summaries = await self._bid_summaries([r["id"] for r in rows[:limit]])
        live = []
        for r in rows[:limit]:
            current = await self.db.fetchone(
                "SELECT state FROM market_listings WHERE id=:id", {"id": r["id"]}
            )
            if current and current["state"] in ("active", "reserved"):
                live.append(self._public_listing(r, summaries.get(r["id"])))
        return {"items": live, "more": len(rows) > limit}

    async def listing_for_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        row = await self.db.fetchone(
            "SELECT id FROM market_listings WHERE card_id=:c AND state IN ('active','reserved')",
            {"c": card_id},
        )
        return await self.listing(row["id"]) if row else None

    # --- offers -----------------------------------------------------------------

    async def quote(self, mint: str, price: int) -> Dict[str, Any]:
        """Fees and eligibility shown to a buyer before funding."""
        mint = self.mint_url(mint)
        try:
            info = await self.mints.eligibility(mint)
        except market_cash.MintUnavailable as exc:
            raise HTTPException(502, f"The mint is unreachable: {exc}")
        if not info["eligible"]:
            return {**info, "amount": None, "claim_fee": None}
        amount, fee = funding_amount(price, info["fee_ppk"])
        return {
            **info,
            "price": price,
            "amount": amount,
            "claim_fee": fee,
            "refund_fee": fee,
            "refund_amount": amount - fee,
        }

    async def register_offer(self, buyer: str, body: OfferRequest) -> Dict[str, Any]:
        m = body.manifest
        doc = m.model_dump()
        mhash = mp.manifest_hash(doc)
        now = self.clock()
        if m.buyer != buyer:
            raise HTTPException(403, "Offers are signed by the buying profile.")
        if m.buyer == m.seller:
            raise HTTPException(400, "You can't buy your own NFT.")
        if not mp.verify_purpose(
            mp.OFFER_SIG_DOMAIN, mhash, body.buyer_signature, buyer
        ):
            raise HTTPException(403, "The offer needs your profile signature.")
        if abs(m.created - now) > mp.CLOCK_SKEW:
            raise HTTPException(400, "Check your device clock and try again.")
        if m.accept_deadline != m.cash_deadline - mp.ACCEPT_WINDOW:
            raise HTTPException(
                400, "Acceptance must close one hour before the refund deadline."
            )
        if (
            not now + mp.MIN_LIFETIME - mp.CLOCK_SKEW
            <= m.cash_deadline
            <= now + mp.MAX_LIFETIME
        ):
            raise HTTPException(400, "Offer lifetime out of range.")
        if m.escrow_key != self.escrow.version:
            raise HTTPException(
                409, "The marketplace escrow key changed. Reload and try again."
            )
        if m.nft.keyset_id != self.ledger.keyset.keyset_id:
            raise HTTPException(400, "This NFT belongs to another mint.")
        mint = self.mint_url(m.payment.mint)
        if mint != m.payment.mint:
            raise HTTPException(400, "Use the normalized mint URL.")
        try:
            destination = PublicKey(
                compressed=bytes.fromhex(m.nft_destination), group="G1"
            )
            receive_proof = DlogEqProof.from_bytes(bytes.fromhex(body.receive_proof))
        except ValueError:
            raise HTTPException(400, "Invalid NFT destination.")
        if not mp.verify_receive(destination, receive_proof, mhash):
            raise HTTPException(
                403, "The NFT destination isn't authorized for this offer."
            )
        proofs = [p.model_dump() for p in body.proofs]
        try:
            locked = mp.check_payment_proofs(proofs, doc)
        except mp.ProtocolError as exc:
            raise HTTPException(400, str(exc))
        refund_outputs = [o.model_dump() for o in body.refund.outputs]
        inputs = [(p["secret"], p["C"]) for p in proofs]
        outs = [(o["amount"], o["B_"]) for o in refund_outputs]
        if not mp.verify_sigall(inputs, outs, body.refund.signature, m.refund_pubkey):
            raise HTTPException(
                403, "The refund authorization doesn't match these proofs and outputs."
            )
        if any(o["id"] != m.payment.keyset_id for o in refund_outputs):
            raise HTTPException(400, "Refund outputs must use the payment keyset.")
        # Fees and amounts against the mint's actual keyset.
        try:
            info = await self.mints.eligibility(mint)
            keyset = await self.mints.keyset(mint, m.payment.keyset_id)
        except mp.ProtocolError as exc:
            raise HTTPException(400, str(exc))
        except market_cash.MintUnavailable as exc:
            raise HTTPException(502, f"The payment mint is unreachable: {exc}")
        if not info["eligible"]:
            raise HTTPException(
                400, "This mint can't be used for offers: " + "; ".join(info["reasons"])
            )
        fee = fee_for(len(proofs), keyset["fee_ppk"])
        if m.payment.claim_fee != fee or m.payment.refund_fee != fee:
            raise HTTPException(400, f"Fees must match the mint ({fee} sat).")
        if (
            locked - fee != m.price
            or sum(o["amount"] for o in refund_outputs) != locked - fee
        ):
            raise HTTPException(400, "Amounts don't add up to the price plus fees.")
        try:
            PaymentMints.verify_dleq(body.proofs, keyset["keys"])
        except mp.ProtocolError as exc:
            raise HTTPException(400, str(exc))
        # Escrow: open in memory only to check it unlocks this hashlock.
        try:
            preimage = self.escrow.open(body.escrow, mhash)
        except mp.ProtocolError:
            raise HTTPException(400, "The preimage escrow doesn't open for this offer.")
        valid = mp.preimage_matches(preimage, m.hashlock)
        del preimage
        if not valid:
            raise HTTPException(400, "The preimage escrow doesn't match the hashlock.")
        try:
            states = await market_cash.proof_states(
                self.client, mint, [p["secret"] for p in proofs]
            )
        except (market_cash.MintUnavailable, market_cash.MintRejected) as exc:
            raise HTTPException(502, f"Couldn't check the payment proofs: {exc}")
        if any(s.get("state") != "UNSPENT" for s in states):
            raise HTTPException(409, "These payment proofs are already spent.")
        async with self.db.get_connection(
            locks=[
                LockOptions(table="market_offers"),
                LockOptions(table="market_listings"),
                LockOptions(table="market_jobs"),
            ]
        ) as conn:
            listing = await conn.fetchone(
                "SELECT * FROM market_listings WHERE id=:id", {"id": m.listing_id}
            )
            if listing is None or listing["state"] != "active":
                raise HTTPException(409, "This NFT isn't accepting offers.")
            if (
                listing["revision"] != m.listing_revision
                or listing["seller"] != m.seller
            ):
                raise HTTPException(409, "The listing changed. Review the new terms.")
            if (
                listing["h"] != m.nft.h
                or listing["nullifier"] != m.nft.nullifier
                or listing["claim_pubkey"] != m.claim_pubkey
            ):
                raise HTTPException(409, "The offer doesn't match the listing.")
            if m.price < listing["price"]:
                raise HTTPException(409, "Offers must meet the asking price.")
            existing = await conn.fetchone(
                "SELECT id, manifest_hash FROM market_offers WHERE id=:id",
                {"id": m.offer_id},
            )
            if existing is not None:
                if existing["manifest_hash"] == mhash.hex():
                    return await self._offer_view(
                        conn, m.offer_id, buyer
                    )  # idempotent retry
                raise HTTPException(409, "Offer ID already used.")
            if await conn.fetchone(
                "SELECT id FROM market_offers WHERE destination=:d",
                {"d": m.nft_destination},
            ):
                raise HTTPException(409, "Use a fresh NFT destination for every offer.")
            await conn.execute(
                """INSERT INTO market_offers(id,listing_id,listing_revision,seller,buyer,h,nullifier,price,amount,mint,
                keyset_id,test_value,cash_deadline,accept_deadline,destination,manifest,manifest_hash,buyer_signature,
                receive_proof,escrow,proofs,disposition,nft_leg,cash_leg,publication,created,updated)
                VALUES(:id,:l,:rev,:seller,:buyer,:h,:n,:price,:amount,:mint,:ks,:test,:cash,:accept,:dest,:m,:mh,
                :bsig,:rp,:esc,:proofs,'funded','unlocked','locked','none',:t,:t)""",
                {
                    "id": m.offer_id,
                    "l": m.listing_id,
                    "rev": m.listing_revision,
                    "seller": m.seller,
                    "buyer": buyer,
                    "h": m.nft.h,
                    "n": m.nft.nullifier,
                    "price": m.price,
                    "amount": locked,
                    "mint": mint,
                    "ks": m.payment.keyset_id,
                    "test": 1 if info["test_value"] else 0,
                    "cash": m.cash_deadline,
                    "accept": m.accept_deadline,
                    "dest": m.nft_destination,
                    "m": json.dumps(doc, separators=(",", ":")),
                    "mh": mhash.hex(),
                    "bsig": body.buyer_signature,
                    "rp": body.receive_proof,
                    "esc": json.dumps(body.escrow, separators=(",", ":")),
                    "proofs": json.dumps(
                        [
                            {k: p[k] for k in ("amount", "id", "secret", "C", "dleq")}
                            for p in proofs
                        ],
                        separators=(",", ":"),
                    ),
                    "t": now,
                },
            )
            await self._add_job(
                conn,
                m.offer_id,
                "refund",
                mint,
                proofs,
                refund_outputs,
                body.refund.signature,
                None,
                m.cash_deadline,
            )
            await self.emit(
                conn,
                m.seller,
                "offer_received",
                {"price": m.price},
                offer_id=m.offer_id,
                listing_id=m.listing_id,
            )
            await self.emit(
                conn,
                buyer,
                "offer_funded",
                {"price": m.price},
                offer_id=m.offer_id,
                listing_id=m.listing_id,
            )
            return await self._offer_view(conn, m.offer_id, buyer)

    async def _add_job(
        self,
        conn: Connection,
        offer_id: str,
        kind: str,
        mint: str,
        proofs: List[Dict[str, Any]],
        outputs: List[Dict[str, Any]],
        signature: str,
        preimage: Optional[str],
        not_before: int,
    ) -> None:
        now = self.clock()
        await conn.execute(
            """INSERT INTO market_jobs(id,offer_id,kind,mint,inputs,outputs,signature,preimage,not_before,state,
            next_attempt,created,updated)
            VALUES(:id,:o,:k,:mint,:in,:out,:sig,:pre,:nb,'pending',:nb,:t,:t)
            ON CONFLICT(offer_id,kind) DO NOTHING""",
            {
                "id": f"{offer_id}:{kind}",
                "o": offer_id,
                "k": kind,
                "mint": mint,
                "in": json.dumps(
                    [
                        {k: p[k] for k in ("amount", "id", "secret", "C")}
                        for p in proofs
                    ],
                    separators=(",", ":"),
                ),
                "out": json.dumps(outputs, separators=(",", ":")),
                "sig": signature,
                "pre": preimage,
                "nb": not_before,
                "t": now,
            },
        )

    async def decline(self, seller: str, offer_id: str) -> Dict[str, Any]:
        async with self.db.get_connection(
            locks=[LockOptions(table="market_offers")]
        ) as conn:
            row = await conn.fetchone(
                "SELECT * FROM market_offers WHERE id=:id AND seller=:s",
                {"id": offer_id, "s": seller},
            )
            if row is None:
                raise HTTPException(404, "Offer not found.")
            if row["disposition"] == "funded":
                await conn.execute(
                    "UPDATE market_offers SET disposition='declined', updated=:t WHERE id=:id AND disposition='funded'",
                    {"t": self.clock(), "id": offer_id},
                )
                await self.emit(
                    conn,
                    row["buyer"],
                    "offer_declined",
                    {"refund_after": row["cash_deadline"]},
                    offer_id=offer_id,
                    listing_id=row["listing_id"],
                )
            return await self._offer_view(conn, offer_id, seller)

    async def accept(
        self, seller: str, offer_id: str, body: AcceptRequest
    ) -> Dict[str, Any]:
        a = body.acceptance
        if a.offer_id != offer_id:
            raise HTTPException(400, "Acceptance is for another offer.")
        offer = await self.db.fetchone(
            "SELECT * FROM market_offers WHERE id=:id", {"id": offer_id}
        )
        if offer is None or offer["seller"] != seller:
            raise HTTPException(404, "Offer not found.")
        if a.manifest_hash != offer["manifest_hash"]:
            raise HTTPException(400, "Acceptance is for different terms.")
        if not mp.verify_purpose(
            mp.ACCEPT_DOMAIN,
            mp.acceptance_hash(a.model_dump()),
            body.seller_signature,
            seller,
        ):
            raise HTTPException(403, "The acceptance needs your profile signature.")
        now = self.clock()
        if offer["disposition"] != "funded":
            raise HTTPException(409, "This offer can no longer be accepted.")
        if now > offer["accept_deadline"] - mp.CLOCK_SKEW:
            raise HTTPException(409, "Acceptance has closed for this offer.")
        manifest = json.loads(offer["manifest"])
        proofs = json.loads(offer["proofs"])
        claim_outputs = [o.model_dump() for o in a.claim_outputs]
        if any(o["id"] != offer["keyset_id"] for o in claim_outputs):
            raise HTTPException(400, "Claim outputs must use the payment keyset.")
        if (
            sum(o["amount"] for o in claim_outputs)
            != offer["amount"] - manifest["payment"]["claim_fee"]
        ):
            raise HTTPException(400, "Claim outputs must total the price.")
        if not mp.verify_sigall(
            [(p["secret"], p["C"]) for p in proofs],
            [(o["amount"], o["B_"]) for o in claim_outputs],
            a.claim_signature,
            manifest["claim_pubkey"],
        ):
            raise HTTPException(403, "The claim signature doesn't cover these outputs.")
        try:
            pres = Presentation.from_bytes(bytes.fromhex(body.presentation))
        except ValueError:
            raise HTTPException(400, "Invalid NFT presentation.")
        destination = PublicKey(
            compressed=bytes.fromhex(offer["destination"]), group="G1"
        )
        mhash = bytes.fromhex(offer["manifest_hash"])
        # Live cash check before committing anything irreversible.
        try:
            states = await market_cash.proof_states(
                self.client, offer["mint"], [p["secret"] for p in proofs]
            )
        except (
            market_cash.MintUnavailable,
            market_cash.MintRejected,
            mp.ProtocolError,
        ) as exc:
            raise HTTPException(502, f"Couldn't reach the payment mint: {exc}")
        if any(s.get("state") != "UNSPENT" for s in states):
            raise HTTPException(409, "The buyer's payment is no longer locked.")
        contract = NFTContract(
            contract_id=offer_id,
            nullifier=bytes.fromhex(offer["nullifier"]),
            h=int(offer["h"], 16),
            hashlock=manifest["hashlock"],
            destination=destination,
            deadline=offer["accept_deadline"],
        )
        locks = [
            LockOptions(table=t)
            for t in (
                "market_offers",
                "market_listings",
                "market_jobs",
                "market_deliveries",
                "ps_nullifiers",
                "portfolio_cards",
            )
        ]
        async with self.db.get_connection(locks=locks) as conn:
            fresh = await conn.fetchone(
                "SELECT disposition FROM market_offers WHERE id=:id", {"id": offer_id}
            )
            listing = await conn.fetchone(
                "SELECT * FROM market_listings WHERE id=:id",
                {"id": offer["listing_id"]},
            )
            if fresh is None or fresh["disposition"] != "funded":
                raise HTTPException(409, "This offer can no longer be accepted.")
            if (
                listing is None
                or listing["state"] != "active"
                or listing["nullifier"] != offer["nullifier"]
            ):
                raise HTTPException(409, "This listing is no longer available.")
            reserved = await conn.execute(
                "UPDATE market_listings SET state='sold', updated=:t WHERE id=:id AND state='active'",
                {"t": now, "id": listing["id"]},
            )
            accepted = await conn.execute(
                """UPDATE market_offers SET disposition='accepted', nft_leg='delivered', cash_leg='claim_pending',
                publication='awaiting_sync', updated=:t WHERE id=:id AND disposition='funded'""",
                {"t": now, "id": offer_id},
            )
            if reserved.rowcount != 1 or accepted.rowcount != 1:
                raise HTTPException(409, "Another acceptance won.")
            # Issuer escrow: the plaintext preimage exists only inside this
            # transaction; it is persisted only together with the delivery.
            try:
                preimage = self.escrow.open(json.loads(offer["escrow"]), mhash)
                await self.ledger.install_lock(
                    conn, pres, contract, mp.delivery_binding(mhash, destination)
                )
                u, v = await self.ledger.claim_lock(conn, offer_id, preimage)
            except mp.ProtocolError:
                raise HTTPException(500, "Escrow failure; nothing was delivered.")
            except NFTError as exc:
                raise HTTPException(409, f"NFT delivery failed: {exc}")
            await self._add_job(
                conn,
                offer_id,
                "claim",
                offer["mint"],
                proofs,
                claim_outputs,
                a.claim_signature,
                preimage.hex(),
                now,
            )
            receipt = {
                "v": mp.RECEIPT_PROTOCOL,
                "offer_id": offer_id,
                "manifest_hash": offer["manifest_hash"],
                "nft_keyset": self.ledger.keyset.keyset_id,
                "h": offer["h"],
                "spent_nullifier": offer["nullifier"],
                "destination": offer["destination"],
                "u": u.format().hex(),
                "v_": v.format().hex(),
                "delivered": now,
            }
            receipt_sig = self.receipts.sign(receipt)
            await conn.execute(
                """INSERT INTO market_deliveries(offer_id,contract_id,nullifier,destination,u,v,receipt,receipt_signature,delivered)
                VALUES(:o,:c,:n,:d,:u,:v,:r,:s,:t)""",
                {
                    "o": offer_id,
                    "c": offer_id,
                    "n": offer["nullifier"],
                    "d": offer["destination"],
                    "u": receipt["u"],
                    "v": receipt["v_"],
                    "r": json.dumps(receipt, separators=(",", ":")),
                    "s": receipt_sig,
                    "t": now,
                },
            )
            await conn.execute(
                """UPDATE portfolio_cards SET status='sent', sent=:t, credential=NULL, encrypted_credential=NULL
                WHERE id=:id AND pubkey=:p""",
                {"t": now, "id": listing["card_id"], "p": seller},
            )
            await self._close_offers(
                conn, listing["id"], "superseded", "offer_lost", keep=offer_id
            )
            card = await conn.fetchone(
                "SELECT title FROM portfolio_cards WHERE id=:id",
                {"id": listing["card_id"]},
            )
            await conn.execute(
                """INSERT INTO market_sales(offer_id,listing_id,card_id,h,title,seller,buyer,created)
                VALUES(:o,:l,:c,:h,:title,:s,:b,:t) ON CONFLICT(offer_id) DO NOTHING""",
                {
                    "o": offer_id,
                    "l": listing["id"],
                    "c": listing["card_id"],
                    "h": offer["h"],
                    "title": card["title"] if card else "NFT",
                    "s": seller,
                    "b": offer["buyer"],
                    "t": now,
                },
            )
            await self.emit(
                conn,
                offer["buyer"],
                "purchased",
                {"title": card["title"] if card else "NFT"},
                offer_id=offer_id,
                listing_id=listing["id"],
            )
            await self.emit(
                conn,
                seller,
                "sold",
                {"price": offer["price"]},
                offer_id=offer_id,
                listing_id=listing["id"],
            )
        return {
            "receipt": receipt,
            "signature": receipt_sig,
            "offer": await self.offer(offer_id, seller),
        }

    async def _offer_view(
        self, conn: Optional[Connection], offer_id: str, viewer: str
    ) -> Dict[str, Any]:
        query = """SELECT o.*, l.card_id AS card_id, c.title AS title, sp.name AS seller_name, bp.name AS buyer_name
            FROM market_offers o LEFT JOIN market_listings l ON l.id=o.listing_id
            LEFT JOIN portfolio_cards c ON c.id=l.card_id
            LEFT JOIN portfolio_profiles sp ON sp.pubkey=o.seller LEFT JOIN portfolio_profiles bp ON bp.pubkey=o.buyer
            WHERE o.id=:id"""
        row = await (
            conn.fetchone(query, {"id": offer_id})
            if conn
            else self.db.fetchone(query, {"id": offer_id})
        )
        if row is None or viewer not in (row["buyer"], row["seller"]):
            raise HTTPException(404, "Offer not found.")
        manifest = json.loads(row["manifest"])
        jobs = await (conn.fetchall if conn else self.db.fetchall)(
            "SELECT kind, state, outcome, attempts, last_error, not_before FROM market_jobs WHERE offer_id=:id",
            {"id": offer_id},
        )
        view = {
            "id": row["id"],
            "listing_id": row["listing_id"],
            "card_id": row["card_id"],
            "title": row["title"],
            "h": row["h"],
            "seller": row["seller"],
            "seller_name": row["seller_name"],
            "buyer": row["buyer"],
            "buyer_name": row["buyer_name"],
            "role": "buyer" if viewer == row["buyer"] else "seller",
            "price": row["price"],
            "amount": row["amount"],
            "claim_fee": manifest["payment"]["claim_fee"],
            "mint": row["mint"],
            "keyset_id": row["keyset_id"],
            "test_value": bool(row["test_value"]),
            "cash_deadline": row["cash_deadline"],
            "accept_deadline": row["accept_deadline"],
            "disposition": row["disposition"],
            "nft_leg": row["nft_leg"],
            "cash_leg": row["cash_leg"],
            "publication": row["publication"],
            "manifest": manifest,
            "manifest_hash": row["manifest_hash"],
            "created": row["created"],
            "updated": row["updated"],
            "jobs": [dict(j) for j in jobs],
        }
        if viewer == row["seller"]:
            # The seller needs the exact proofs to verify DLEQ/state and sign the claim.
            view["proofs"] = json.loads(row["proofs"])
        return view

    async def offer(self, offer_id: str, viewer: str) -> Dict[str, Any]:
        return await self._offer_view(None, offer_id, viewer)

    async def offers(self, viewer: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id FROM market_offers WHERE buyer=:p OR seller=:p ORDER BY updated DESC LIMIT 200",
            {"p": viewer},
        )
        return [await self.offer(r["id"], viewer) for r in rows]

    async def job_result(self, viewer: str, offer_id: str, kind: str) -> Dict[str, Any]:
        """Owner-only: blind signatures for the owner's fixed outputs (the
        owner unblinds them with locally saved material)."""
        offer = await self.offer(offer_id, viewer)
        if (kind == "claim" and viewer != offer["seller"]) or (
            kind == "refund" and viewer != offer["buyer"]
        ):
            raise HTTPException(404, "Not your payment.")
        row = await self.db.fetchone(
            "SELECT * FROM market_jobs WHERE offer_id=:o AND kind=:k",
            {"o": offer_id, "k": kind},
        )
        if row is None:
            raise HTTPException(404, "No such payment.")
        return {
            "state": row["state"],
            "outcome": row["outcome"],
            "mint": row["mint"],
            "outputs": json.loads(row["outputs"]),
            "signatures": json.loads(row["result"]) if row["result"] else None,
            "last_error": row["last_error"],
        }

    # --- purchases ------------------------------------------------------------

    async def purchase_receipt(self, buyer: str, offer_id: str) -> Dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT d.*, o.buyer FROM market_deliveries d JOIN market_offers o ON o.id=d.offer_id WHERE d.offer_id=:o",
            {"o": offer_id},
        )
        if row is None or row["buyer"] != buyer:
            raise HTTPException(404, "No delivery for this offer.")
        return {
            "receipt": json.loads(row["receipt"]),
            "signature": row["receipt_signature"],
            "receipt_key": self.receipts.public_key,
        }

    async def purchases(self, buyer: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            """SELECT o.id, o.h, o.price, o.publication, d.delivered, c.title FROM market_offers o
            JOIN market_deliveries d ON d.offer_id=o.id LEFT JOIN market_listings l ON l.id=o.listing_id
            LEFT JOIN portfolio_cards c ON c.id=l.card_id WHERE o.buyer=:b ORDER BY d.delivered DESC""",
            {"b": buyer},
        )
        return [dict(r) for r in rows]

    async def publish_purchase(
        self, buyer: str, offer_id: str, body: PublishPurchase
    ) -> Dict[str, Any]:
        """The buyer's wallet recovered (u, v) + s', verified it, and now
        publishes the ordinary signed showing. Only then is it a normal card."""
        offer = await self.db.fetchone(
            "SELECT * FROM market_offers WHERE id=:id AND buyer=:b",
            {"id": offer_id, "b": buyer},
        )
        if offer is None or offer["nft_leg"] != "delivered":
            raise HTTPException(404, "No delivered purchase for this offer.")
        if offer["publication"] == "published":
            row = await self.db.fetchone(
                "SELECT * FROM portfolio_cards WHERE id=:id", {"id": offer_id}
            )
            if row:
                return self.portfolio.public_card(dict(row))
        try:
            context, pres = NFTClient.decode_showing(body.showing)
        except Exception:
            raise HTTPException(400, "Invalid ownership showing.")
        expected = f"{SHOW_DOMAIN}\n{buyer}\n{offer['h']}\n{self.ledger.keyset.keyset_id}".encode()
        if (
            context != expected
            or pres.h.to_bytes(32, "big").hex() != offer["h"]
            or not verify_showing(self.ledger.keyset, pres, context)
        ):
            raise HTTPException(403, "Invalid ownership showing.")
        if not verify_signature(
            buyer,
            "claim",
            hashlib.sha256((CLAIM_DOMAIN + body.showing).encode()).digest(),
            body.signature,
        ):
            raise HTTPException(403, "Invalid profile signature.")
        if await self.ledger.is_spent(pres.nullifier.format()):
            raise HTTPException(409, "This credential was already spent.")
        listing = await self.db.fetchone(
            "SELECT card_id FROM market_listings WHERE id=:id",
            {"id": offer["listing_id"]},
        )
        source = (
            await self.db.fetchone(
                "SELECT title FROM portfolio_cards WHERE id=:id",
                {"id": listing["card_id"]},
            )
            if listing
            else None
        )
        now = self.clock()
        async with self.db.get_connection(
            locks=[
                LockOptions(table="portfolio_cards"),
                LockOptions(table="market_offers"),
            ]
        ) as conn:
            await conn.execute(
                """INSERT INTO portfolio_cards(id,pubkey,h,title,showing,signature,status,created,encrypted_credential)
                VALUES(:id,:p,:h,:title,:showing,:sig,'owned',:t,:enc)
                ON CONFLICT(id) DO UPDATE SET showing=:showing, signature=:sig, encrypted_credential=:enc""",
                {
                    "id": offer_id,
                    "p": buyer,
                    "h": offer["h"],
                    "title": source["title"] if source else "Purchased NFT",
                    "showing": body.showing,
                    "sig": body.signature,
                    "t": now,
                    "enc": json.dumps(body.encrypted_credential, separators=(",", ":")),
                },
            )
            await conn.execute(
                "UPDATE market_offers SET publication='published', updated=:t WHERE id=:id",
                {"t": now, "id": offer_id},
            )
            row = await conn.fetchone(
                "SELECT * FROM portfolio_cards WHERE id=:id", {"id": offer_id}
            )
        return self.portfolio.public_card(dict(row))

    # --- recovery records and wallet backups -----------------------------------

    async def put_recovery(self, pubkey: str, record: RecoveryRecord) -> None:
        if len(json.dumps(record.envelope)) > 64 * 1024:
            raise HTTPException(413, "Recovery record too large.")
        count = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM market_recovery WHERE pubkey=:p", {"p": pubkey}
        )
        if count and count["n"] >= 2000:
            raise HTTPException(429, "Too many recovery records.")
        await self.db.execute(
            """INSERT INTO market_recovery(pubkey,id,kind,envelope,updated) VALUES(:p,:id,:k,:e,:t)
            ON CONFLICT(pubkey,id) DO UPDATE SET envelope=:e, kind=:k, updated=:t""",
            {
                "p": pubkey,
                "id": record.id,
                "k": record.kind,
                "e": json.dumps(record.envelope, separators=(",", ":")),
                "t": self.clock(),
            },
        )

    async def list_recovery(self, pubkey: str) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id, kind, envelope, updated FROM market_recovery WHERE pubkey=:p",
            {"p": pubkey},
        )
        return [
            {
                "id": r["id"],
                "kind": r["kind"],
                "envelope": json.loads(r["envelope"]),
                "updated": r["updated"],
            }
            for r in rows
        ]

    async def lease(
        self, pubkey: str, body: LeaseRequest, ttl: int = 120
    ) -> Dict[str, Any]:
        """Single-writer lease per wallet: only the lease holder may write a
        new backup revision, so two devices never allocate from the same
        proofs or counters unknowingly. Takeover must follow a fresh read."""
        now = self.clock()
        async with self.db.get_connection(
            locks=[LockOptions(table="money_leases")]
        ) as conn:
            row = await conn.fetchone(
                "SELECT * FROM money_leases WHERE pubkey=:p", {"p": pubkey}
            )
            if body.release:
                if row and row["device"] == body.device:
                    await conn.execute(
                        "UPDATE money_leases SET until=0 WHERE pubkey=:p", {"p": pubkey}
                    )
                return {"granted": False, "holder": body.device, "until": 0}
            if (
                row
                and row["device"] != body.device
                and row["until"] > now
                and not body.takeover
            ):
                return {
                    "granted": False,
                    "holder": row["device"],
                    "until": row["until"],
                }
            await conn.execute(
                """INSERT INTO money_leases(pubkey,device,until) VALUES(:p,:d,:u)
                ON CONFLICT(pubkey) DO UPDATE SET device=:d, until=:u""",
                {"p": pubkey, "d": body.device, "u": now + ttl},
            )
        return {"granted": True, "holder": body.device, "until": now + ttl}

    async def put_backup(self, pubkey: str, body: BackupRequest) -> Dict[str, Any]:
        if len(json.dumps(body.envelope)) > 4 * 1024 * 1024:
            raise HTTPException(413, "Wallet backup too large.")
        now = self.clock()
        async with self.db.get_connection(
            locks=[
                LockOptions(table="money_backups"),
                LockOptions(table="money_leases"),
            ]
        ) as conn:
            lease = await conn.fetchone(
                "SELECT * FROM money_leases WHERE pubkey=:p", {"p": pubkey}
            )
            if lease is None or lease["device"] != body.device or lease["until"] < now:
                raise HTTPException(423, "Another device is using this wallet.")
            row = await conn.fetchone(
                "SELECT revision FROM money_backups WHERE pubkey=:p", {"p": pubkey}
            )
            current = row["revision"] if row else 0
            if body.base_revision != current or body.revision <= current:
                raise HTTPException(
                    409, "The wallet changed on another device. Reload it first."
                )
            await conn.execute(
                """INSERT INTO money_backups(pubkey,revision,envelope,device,updated) VALUES(:p,:r,:e,:d,:t)
                ON CONFLICT(pubkey) DO UPDATE SET revision=:r, envelope=:e, device=:d, updated=:t""",
                {
                    "p": pubkey,
                    "r": body.revision,
                    "e": json.dumps(body.envelope, separators=(",", ":")),
                    "d": body.device,
                    "t": now,
                },
            )
        return {"revision": body.revision}

    async def get_backup(self, pubkey: str) -> Dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT * FROM money_backups WHERE pubkey=:p", {"p": pubkey}
        )
        if row is None:
            return {"revision": 0, "envelope": None}
        return {
            "revision": row["revision"],
            "envelope": json.loads(row["envelope"]),
            "device": row["device"],
            "updated": row["updated"],
        }

    # --- public projections ---------------------------------------------------

    async def sales(self, limit: int, before: Optional[int]) -> List[Dict[str, Any]]:
        """Completed sales for public activity: NFT, buyer, seller, sale price
        and time. Mints and settlement details are never part of this."""
        rows = await self.db.fetchall(
            """SELECT s.offer_id, s.card_id, s.h, s.title, s.seller, s.buyer, s.created, o.price,
            sp.name AS seller_name, bp.name AS buyer_name FROM market_sales s
            LEFT JOIN market_offers o ON o.id=s.offer_id
            LEFT JOIN portfolio_profiles sp ON sp.pubkey=s.seller LEFT JOIN portfolio_profiles bp ON bp.pubkey=s.buyer
            WHERE s.created < :before ORDER BY s.created DESC LIMIT :limit""",
            {
                "before": before if before is not None else self.clock() + 1,
                "limit": limit,
            },
        )
        return [dict(r) for r in rows]


class Executor:
    """Restricted settlement executor: submits pre-authorized fixed-output
    swaps, recovers lost replies with NUT-09 and reads outcomes from NUT-07.
    It cannot change inputs, outputs or keysets; anything that would need
    that becomes ``needs_attention`` for the owner."""

    MAX_ATTEMPTS = 40

    def __init__(self, market: Market, interval: float = 5.0, lease_seconds: int = 60):
        self.market = market
        self.db = market.db
        self.interval = interval
        self.lease_seconds = lease_seconds
        self.owner = secrets.token_hex(8)
        self._task: Optional["asyncio.Task[None]"] = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except Exception:  # keep the executor alive; jobs are durable
                log.exception("settlement executor pass failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    async def run_due(self) -> int:
        now = self.market.clock()
        rows = await self.db.fetchall(
            "SELECT id FROM market_jobs WHERE state='pending' AND next_attempt<=:now ORDER BY next_attempt LIMIT 20",
            {"now": now},
        )
        done = 0
        for r in rows:
            if await self._lease(r["id"]):
                await self._run(r["id"])
                done += 1
        await self._mark_refund_pending()
        return done

    async def _lease(self, job_id: str) -> bool:
        now = self.market.clock()
        async with self.db.get_connection(
            locks=[LockOptions(table="market_jobs")]
        ) as conn:
            result = await conn.execute(
                """UPDATE market_jobs SET lease_owner=:o, lease_until=:u WHERE id=:id AND state='pending'
                AND (lease_until IS NULL OR lease_until<:now)""",
                {
                    "o": self.owner,
                    "u": now + self.lease_seconds,
                    "id": job_id,
                    "now": now,
                },
            )
            return result.rowcount == 1

    async def _mark_refund_pending(self) -> None:
        now = self.market.clock()
        await self.db.execute(
            """UPDATE market_offers SET cash_leg='refund_pending', updated=:t
            WHERE cash_leg='locked' AND cash_deadline<=:t""",
            {"t": now},
        )

    async def _finish(
        self,
        job: Any,
        state: str,
        outcome: Optional[str],
        result: Optional[List[Dict[str, Any]]],
        error: Optional[str],
    ) -> None:
        now = self.market.clock()
        async with self.db.get_connection(
            locks=[
                LockOptions(table="market_jobs"),
                LockOptions(table="market_offers"),
                LockOptions(table="market_events"),
            ]
        ) as conn:
            await conn.execute(
                """UPDATE market_jobs SET state=:s, outcome=:o, result=:r, last_error=:e, lease_owner=NULL,
                lease_until=NULL, updated=:t WHERE id=:id AND lease_owner=:me""",
                {
                    "s": state,
                    "o": outcome,
                    "r": json.dumps(result) if result is not None else None,
                    "e": error,
                    "t": now,
                    "id": job["id"],
                    "me": self.owner,
                },
            )
            offer = await conn.fetchone(
                "SELECT * FROM market_offers WHERE id=:id", {"id": job["offer_id"]}
            )
            if offer is None:
                return
            cash_leg = offer["cash_leg"]
            if outcome == "claim":
                cash_leg = "claimed"
            elif outcome == "refund":
                cash_leg = "refunded"
            elif state == "needs_attention":
                cash_leg = "needs_attention"
            await conn.execute(
                "UPDATE market_offers SET cash_leg=:c, updated=:t WHERE id=:id",
                {"c": cash_leg, "t": now, "id": offer["id"]},
            )
            if outcome == "claim" and state == "done":
                await self.market.emit(
                    conn,
                    offer["seller"],
                    "paid",
                    {"price": offer["price"]},
                    offer_id=offer["id"],
                )
            if outcome == "refund" and state == "done":
                await self.market.emit(
                    conn,
                    offer["buyer"],
                    "refunded",
                    {"amount": offer["amount"]},
                    offer_id=offer["id"],
                )
            if state == "needs_attention":
                who = offer["seller"] if job["kind"] == "claim" else offer["buyer"]
                await self.market.emit(
                    conn,
                    who,
                    "payment_attention",
                    {"kind": job["kind"], "error": error or ""},
                    offer_id=offer["id"],
                )
            if state == "lost":
                who = offer["seller"] if job["kind"] == "claim" else offer["buyer"]
                await self.market.emit(
                    conn,
                    who,
                    "payment_lost_race",
                    {"kind": job["kind"]},
                    offer_id=offer["id"],
                )

    async def _retry(self, job: Any, error: str, delay: int) -> None:
        now = self.market.clock()
        attempts = job["attempts"] + 1
        if attempts >= self.MAX_ATTEMPTS:
            await self._finish(job, "needs_attention", None, None, error)
            return
        await self.db.execute(
            """UPDATE market_jobs SET attempts=:a, next_attempt=:n, last_error=:e, lease_owner=NULL, lease_until=NULL,
            updated=:t WHERE id=:id AND lease_owner=:me""",
            {
                "a": attempts,
                "n": now + delay,
                "e": error,
                "t": now,
                "id": job["id"],
                "me": self.owner,
            },
        )

    async def _run(self, job_id: str) -> None:
        job = await self.db.fetchone(
            "SELECT * FROM market_jobs WHERE id=:id", {"id": job_id}
        )
        if job is None:
            return
        inputs = json.loads(job["inputs"])
        outputs = json.loads(job["outputs"])
        client = self.market.client
        backoff = min(3600, 10 * 2 ** min(job["attempts"], 8))
        try:
            states = await market_cash.proof_states(
                client, job["mint"], [p["secret"] for p in inputs]
            )
        except (market_cash.MintUnavailable, market_cash.MintRejected) as exc:
            await self._retry(job, f"mint unreachable: {exc}", backoff)
            return
        except mp.ProtocolError as exc:
            await self._finish(job, "needs_attention", None, None, str(exc))
            return
        branch = market_cash.spent_by(states)
        if branch == "pending":
            await self._retry(job, "inputs pending at the mint", 30)
            return
        if branch is not None:
            if branch == job["kind"]:
                # Our swap went through but the reply was lost: restore it.
                try:
                    sigs = await market_cash.restore(client, job["mint"], outputs)
                except (market_cash.MintUnavailable, market_cash.MintRejected) as exc:
                    await self._retry(job, f"restore failed: {exc}", backoff)
                    return
                if sigs:
                    await self._finish(job, "done", branch, sigs, None)
                else:
                    await self._finish(
                        job,
                        "needs_attention",
                        branch,
                        None,
                        "inputs spent but outputs not restorable",
                    )
                return
            await self._finish(
                job, "lost", branch, None, f"inputs were spent by the {branch} path"
            )
            return
        if job["kind"] == "claim" and not job["preimage"]:
            await self._retry(job, "waiting for delivery", 30)
            return
        if job["kind"] == "refund" and self.market.clock() < job["not_before"]:
            await self._retry(
                job, "refund not open yet", job["not_before"] - self.market.clock()
            )
            return
        swap = market_cash.swap_inputs(inputs, job["signature"], job["preimage"])
        try:
            sigs = await market_cash.submit_swap(client, job["mint"], swap, outputs)
        except market_cash.MintUnavailable as exc:
            await self._retry(job, f"mint unavailable: {exc}", backoff)
            return
        except market_cash.MintRejected as exc:
            # A refund racing the mint's clock or a concurrent claim: re-read
            # state on the next pass rather than guessing.
            await self._retry(
                job, f"mint rejected: {exc}", 60 if job["kind"] == "refund" else backoff
            )
            return
        await self._finish(job, "done", job["kind"], sigs, None)


ListingSort = Literal["new", "price_asc", "price_desc"]


def market_router(
    market: Market, executor: Executor, authorize: Any, read_body: Any
) -> APIRouter:
    """HTTP layer only: parsing, authorization and limits. All state
    changes live in Market/Executor."""
    router = APIRouter()

    async def signed(request: Request, pubkey: str, limit: int) -> bytes:
        raw: bytes = await read_body(request, limit)
        await authorize(request, pubkey, raw)
        return raw

    @router.get("/api/market/config")
    async def config():
        return market.public_config()

    @router.get("/api/market/mints/check")
    async def check_mint(url: str = Query(max_length=512)):
        mint = market.mint_url(url)
        try:
            return await market.mints.eligibility(mint)
        except market_cash.MintUnavailable as exc:
            raise HTTPException(502, f"The mint is unreachable: {exc}")
        except mp.ProtocolError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/api/market/quote")
    async def quote(
        mint: str = Query(max_length=512), price: int = Query(gt=0, le=mp.MAX_PRICE)
    ):
        try:
            return await market.quote(mint, price)
        except mp.ProtocolError as exc:
            raise HTTPException(400, str(exc))

    @router.get("/api/market/listings")
    async def listings(
        sort: ListingSort = "new",
        q: str = Query(default="", max_length=80),
        seller: str = Query(default="", pattern=r"^([0-9a-f]{64})?$"),
        limit: int = Query(default=24, ge=1, le=60),
        offset: int = Query(default=0, ge=0, le=10000),
    ):
        return await market.listings(sort, q.strip(), seller or None, limit, offset)

    @router.get("/api/market/listings/{listing_id}")
    async def listing(listing_id: str):
        if len(listing_id) != 32:
            raise HTTPException(404, "Listing not found.")
        return await market.listing(listing_id)

    @router.get("/api/market/listings/{listing_id}/bids")
    async def bids(listing_id: str):
        if len(listing_id) != 32:
            raise HTTPException(404, "Listing not found.")
        return await market.bids(listing_id)

    @router.get("/api/market/cards/{card_id}/listing")
    async def card_listing(card_id: str):
        return {"listing": await market.listing_for_card(card_id[:64])}

    @router.get("/api/market/sales")
    async def sales(
        limit: int = Query(default=30, ge=1, le=100),
        before: Optional[int] = Query(default=None, ge=0),
    ):
        return await market.sales(limit, before)

    @router.post("/api/profiles/{pubkey}/market/listings")
    async def create_listing(pubkey: str, request: Request):
        return await market.create_listing(
            pubkey, _parse(ListingRequest, await signed(request, pubkey, 8192))
        )

    @router.post("/api/profiles/{pubkey}/market/listings/{listing_id}/revise")
    async def revise(pubkey: str, listing_id: str, request: Request):
        return await market.revise_listing(
            pubkey,
            listing_id,
            _parse(ListingRequest, await signed(request, pubkey, 8192)),
        )

    @router.post("/api/profiles/{pubkey}/market/listings/{listing_id}/unlist")
    async def unlist(pubkey: str, listing_id: str, request: Request):
        await signed(request, pubkey, 0)
        return await market.unlist(pubkey, listing_id)

    @router.post("/api/profiles/{pubkey}/market/offers")
    async def offer(pubkey: str, request: Request):
        result = await market.register_offer(
            pubkey, _parse(OfferRequest, await signed(request, pubkey, 256 * 1024))
        )
        executor.wake()
        return result

    @router.post("/api/profiles/{pubkey}/market/offers/list")
    async def offers(pubkey: str, request: Request):
        await signed(request, pubkey, 0)
        return await market.offers(pubkey)

    @router.post("/api/profiles/{pubkey}/market/offers/{offer_id}")
    async def one_offer(pubkey: str, offer_id: str, request: Request):
        await signed(request, pubkey, 0)
        return await market.offer(offer_id, pubkey)

    @router.post("/api/profiles/{pubkey}/market/offers/{offer_id}/accept")
    async def accept(pubkey: str, offer_id: str, request: Request):
        result = await market.accept(
            pubkey,
            offer_id,
            _parse(AcceptRequest, await signed(request, pubkey, 64 * 1024)),
        )
        executor.wake()
        return result

    @router.post("/api/profiles/{pubkey}/market/offers/{offer_id}/decline")
    async def decline(pubkey: str, offer_id: str, request: Request):
        await signed(request, pubkey, 0)
        return await market.decline(pubkey, offer_id)

    @router.post("/api/profiles/{pubkey}/market/offers/{offer_id}/payment/{kind}")
    async def payment(
        pubkey: str, offer_id: str, kind: Literal["claim", "refund"], request: Request
    ):
        await signed(request, pubkey, 0)
        return await market.job_result(pubkey, offer_id, kind)

    @router.post("/api/profiles/{pubkey}/market/purchases")
    async def purchases(pubkey: str, request: Request):
        await signed(request, pubkey, 0)
        return await market.purchases(pubkey)

    @router.post("/api/profiles/{pubkey}/market/purchases/{offer_id}/receipt")
    async def receipt(pubkey: str, offer_id: str, request: Request):
        await signed(request, pubkey, 0)
        return await market.purchase_receipt(pubkey, offer_id)

    @router.post("/api/profiles/{pubkey}/market/purchases/{offer_id}/publish")
    async def publish(pubkey: str, offer_id: str, request: Request):
        return await market.publish_purchase(
            pubkey,
            offer_id,
            _parse(PublishPurchase, await signed(request, pubkey, 16 * 1024)),
        )

    @router.post("/api/profiles/{pubkey}/market/inbox")
    async def inbox(pubkey: str, request: Request):
        raw = await signed(request, pubkey, 256)
        try:
            body = json.loads(raw or b"{}")
            after, wait = int(body.get("after", 0)), float(body.get("wait", 0))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(400, "Invalid inbox request.")
        return await market.inbox(pubkey, max(0, after), wait)

    @router.post("/api/profiles/{pubkey}/market/inbox/read")
    async def inbox_read(pubkey: str, request: Request):
        raw = await signed(request, pubkey, 128)
        try:
            upto = int(json.loads(raw)["upto"])
        except (ValueError, TypeError, KeyError):
            raise HTTPException(400, "Invalid request.")
        await market.mark_read(pubkey, upto)
        return {"ok": True}

    @router.post("/api/profiles/{pubkey}/market/recovery/put")
    async def recovery_put(pubkey: str, request: Request):
        await market.put_recovery(
            pubkey, _parse(RecoveryRecord, await signed(request, pubkey, 96 * 1024))
        )
        return {"ok": True}

    @router.post("/api/profiles/{pubkey}/market/recovery/list")
    async def recovery_list(pubkey: str, request: Request):
        await signed(request, pubkey, 0)
        return await market.list_recovery(pubkey)

    @router.post("/api/profiles/{pubkey}/money/lease")
    async def lease(pubkey: str, request: Request):
        return await market.lease(
            pubkey, _parse(LeaseRequest, await signed(request, pubkey, 256))
        )

    @router.post("/api/profiles/{pubkey}/money/backup/put")
    async def backup_put(pubkey: str, request: Request):
        return await market.put_backup(
            pubkey,
            _parse(BackupRequest, await signed(request, pubkey, 6 * 1024 * 1024)),
        )

    @router.post("/api/profiles/{pubkey}/money/backup/get")
    async def backup_get(pubkey: str, request: Request):
        await signed(request, pubkey, 0)
        return await market.get_backup(pubkey)

    return router
