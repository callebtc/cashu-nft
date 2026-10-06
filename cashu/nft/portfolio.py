"""Public image portfolios with encrypted browser-wallet recovery backups.

Build portfolio_web first, then: poetry run python -m cashu.nft.portfolio
The ledger, images and collection changes share a single SQLite transaction.
Profile private keys never enter this process.
"""

import hashlib
import os
import re
import secrets
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Deque, Dict, List, Literal, Optional, cast

import uvicorn
from coincurve import PublicKeyXOnly
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from ..core.crypto.bls import PublicKey, curve_order
from ..core.crypto.ps import (
    Credential,
    MintPrivateKeyPS,
    blind_issue_commit,
    hash_asset,
    present,
    present_showing,
    prove_owner_secret,
    unblind_issued,
)
from ..core.db import Connection, Database, LockOptions
from .api import create_router
from .imgmeta import embed_token
from .ledger import AlreadyMintedError, AlreadySpentError, NFTError, PSLedger
from .market import Executor, Market, market_router
from .market_net import MintNetPolicy
from .moderation import DEFAULT_THRESHOLD, Classifier, Moderation, NSFWClassifier
from .nostr_sig import PROFILE_SIG, verify_signature
from .portfolio_image import (
    avatar_jpg,
    image_format,
    normalize_image,
    split_transfer,
    validate_image,
)
from .portfolio_links import LinkRequest, Links
from .portfolio_og import (
    collection_meta,
    fan,
    link_meta,
    link_version,
    listing_meta,
    listing_version,
    nft_meta,
    nft_version,
    owned_count,
    profile_version,
    site_base,
    with_meta,
)
from .portfolio_og_image import (
    Card,
    CollectionPreview,
    LinkPreview,
    ListingPreview,
    NFTPreview,
    collection_image,
    link_image,
    listing_image,
    nft_image,
)
from .portfolio_social import (
    CollectionSort,
    NFTSort,
    SettingsRequest,
    Social,
    ToggleRequest,
)
from .portfolio_wallet import (
    BrowserPortfolio,
    DeleteRequest,
    Envelope,
    PublishRequest,
    WalletProofRequest,
)
from .turnstile import configure_turnstile
from .wallet import SHOW_TOKEN_PREFIX, TOKEN_PREFIX, NFTClient

AUTH_DOMAIN = "Cashu_NFT_Portfolio_Auth_v1"
CLAIM_DOMAIN = "Cashu_NFT_Portfolio_Claim_v1\n"
SHOW_DOMAIN = "Cashu_NFT_Portfolio_Show_v1"
MAX_AVATAR_BYTES = 5 * 1024 * 1024  # upload limit; stored pictures are 256 px
SESSION_TTL = 30 * 86400  # longest a signing extension's session key may last
MAX_SESSIONS = 20  # live session keys per profile
WEB_DIR = Path(__file__).parent / "portfolio_web" / "dist"
CARD_LOCKS = [
    LockOptions(table="ps_assets"),
    LockOptions(table="ps_nullifiers"),
    LockOptions(table="portfolio_cards"),
]


class ChallengeRequest(BaseModel):
    pubkey: str = Field(pattern=r"^[0-9a-f]{64}$")
    method: Literal["POST"]
    path: str = Field(max_length=1024, pattern=r"^/api/profiles/[0-9a-f]{64}")
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class VaultRequest(BaseModel):
    """A Nostr collection's root secret, NIP-44-encrypted to its own key."""

    ciphertext: str = Field(
        min_length=24, max_length=1024, pattern=r"^[A-Za-z0-9+/]+={0,2}$"
    )
    check: str = Field(pattern=r"^[0-9a-f]{32}$")


class ProfileRequest(BaseModel):
    name: str = Field(default="Collector", min_length=1, max_length=40)
    # Only Nostr collections have a vault (NOSTR_LOGIN_PLAN.md).
    vault: Optional[VaultRequest] = None


class SessionRequest(BaseModel):
    session_pubkey: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires: int


class RevokeRequest(BaseModel):
    session_pubkey: str = Field(pattern=r"^[0-9a-f]{64}$")


class TitleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=80)


class ClaimRequest(BaseModel):
    signature: str = Field(pattern=PROFILE_SIG)
    showing: str = Field(max_length=4096)


def validate_pubkey(pubkey: str) -> PublicKeyXOnly:
    try:
        if len(pubkey) != 64 or pubkey != pubkey.lower():
            raise ValueError("invalid key length")
        return PublicKeyXOnly(bytes.fromhex(pubkey))
    except ValueError:
        raise HTTPException(400, "Use a valid 64-character public key.")


def showing_context(pubkey: str, h: str, keyset_id: str) -> bytes:
    return f"{SHOW_DOMAIN}\n{pubkey}\n{h}\n{keyset_id}".encode()


def make_showing(pubkey: str, cred: Credential) -> str:
    h = cred.h.to_bytes(32, "big").hex()
    context = showing_context(pubkey, h, cred.keyset_id)
    presentation = present_showing(cred, context)
    return (
        SHOW_TOKEN_PREFIX
        + (len(context).to_bytes(2, "big") + context + presentation.to_bytes()).hex()
    )


def claim_digest(showing: str) -> bytes:
    return hashlib.sha256((CLAIM_DOMAIN + showing).encode()).digest()


def auth_message(
    pubkey: str, method: str, path: str, body_hash: str, nonce: str, expires: int
) -> str:
    return f"{AUTH_DOMAIN}\n{pubkey}\n{method}\n{path}\n{body_hash}\n{nonce}\n{expires}"


def load_mint_key(data_dir: Path) -> MintPrivateKeyPS:
    """Persist a randomly generated mint identity, never a demo/default seed."""
    seed_file = data_dir / "mint.seed"
    try:
        fd = os.open(seed_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        seed = seed_file.read_bytes()
    else:
        seed = secrets.token_bytes(32)
        with os.fdopen(fd, "wb") as stream:
            stream.write(seed)
    if len(seed) != 32:
        raise ValueError("Invalid portfolio mint seed; restore its backup.")
    return MintPrivateKeyPS.from_seed(seed)


class Portfolio:
    def __init__(
        self,
        data_dir: str,
        max_image_bytes: int = 10 * 1024 * 1024,
        max_cards: Optional[int] = None,
        max_storage_bytes: int = 1024**3,
        classifier: Optional[Classifier] = None,
        nsfw_threshold: float = DEFAULT_THRESHOLD,
    ):
        directory = Path(data_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = Database("portfolio", str(directory))
        self.ledger = PSLedger(self.db, load_mint_key(directory))
        self.max_image_bytes = max_image_bytes
        self.max_cards = max_cards
        self.max_storage_bytes = max_storage_bytes
        self.moderation = Moderation(self.db, classifier, nsfw_threshold)

    async def migrate(self) -> None:
        await self.ledger.migrate()
        await self.moderation.migrate()
        async with self.db.get_connection() as conn:
            for statement in (
                """CREATE TABLE IF NOT EXISTS portfolio_profiles (
                    pubkey TEXT PRIMARY KEY, name TEXT NOT NULL, created INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS portfolio_avatars (
                    pubkey TEXT PRIMARY KEY, jpg BLOB NOT NULL, updated INTEGER NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS portfolio_images (
                    h TEXT PRIMARY KEY, jpg BLOB NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS portfolio_cards (
                    id TEXT PRIMARY KEY, pubkey TEXT NOT NULL REFERENCES portfolio_profiles(pubkey),
                    h TEXT NOT NULL REFERENCES portfolio_images(h), title TEXT NOT NULL,
                    credential BLOB, showing TEXT NOT NULL, signature TEXT,
                    status TEXT NOT NULL CHECK(status IN ('owned','ready','sent')),
                    created INTEGER NOT NULL, sent INTEGER)""",
                "CREATE UNIQUE INDEX IF NOT EXISTS portfolio_active_asset ON portfolio_cards(h) WHERE status != 'sent'",
                "CREATE INDEX IF NOT EXISTS portfolio_owner ON portfolio_cards(pubkey)",
                """CREATE TABLE IF NOT EXISTS portfolio_challenges (
                    nonce TEXT PRIMARY KEY, pubkey TEXT NOT NULL, message TEXT NOT NULL,
                    expires INTEGER NOT NULL)""",
                # Keys a signing extension authorized to sign owner requests.
                """CREATE TABLE IF NOT EXISTS portfolio_sessions (
                    session_pubkey TEXT PRIMARY KEY, pubkey TEXT NOT NULL,
                    expires INTEGER NOT NULL, created INTEGER NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS portfolio_sessions_owner ON portfolio_sessions(pubkey)",
                # A Nostr collection's NIP-44-encrypted root secret; write-once
                # per check value (a hash of the secret).
                """CREATE TABLE IF NOT EXISTS portfolio_vaults (
                    pubkey TEXT PRIMARY KEY, ciphertext TEXT NOT NULL,
                    vault_check TEXT NOT NULL, updated INTEGER NOT NULL)""",
            ):
                await conn.execute(statement)
            columns = await conn.fetchall("PRAGMA table_info(portfolio_profiles)")
            if "nostr" not in {c["name"] for c in columns}:
                await conn.execute(
                    "ALTER TABLE portfolio_profiles ADD COLUMN nostr INTEGER NOT NULL DEFAULT 0"
                )

    async def capacity(self, conn: Connection, pubkey: str, image_size: int) -> None:
        profile = await conn.fetchone(
            "SELECT pubkey FROM portfolio_profiles WHERE pubkey=:p", {"p": pubkey}
        )
        if profile is None:
            raise HTTPException(404, "Create this portfolio first.")
        count = await conn.fetchone(
            "SELECT COUNT(*) AS n FROM portfolio_cards WHERE pubkey=:p AND status!='sent'",
            {"p": pubkey},
        )
        if count is None or (
            self.max_cards is not None and count["n"] >= self.max_cards
        ):
            raise HTTPException(409, "This portfolio has reached its collection limit.")
        total = await conn.fetchone(
            "SELECT COALESCE(SUM(length(jpg)),0) AS n FROM portfolio_images"
        )
        if total is None or total["n"] + image_size > self.max_storage_bytes:
            raise HTTPException(507, "The mint has reached its image storage limit.")

    @staticmethod
    def public_card(row: dict, owner: bool = True) -> dict:
        # Deliberately whitelist fields: never serialize a credential or owner secret.
        # A pending transfer ('ready': a transfer file or link exists) is the
        # owner's business; public views show such a card as plainly owned.
        public = {
            key: row[key]
            for key in (
                "id",
                "pubkey",
                "h",
                "title",
                "showing",
                "signature",
                "status",
                "created",
                "sent",
            )
        }
        if not owner and public["status"] == "ready":
            public["status"] = "owned"
        public["custody"] = "browser" if row.get("encrypted_credential") else "legacy"
        return public

    async def store_card(
        self, conn: Connection, pubkey: str, cred: Credential, jpg: bytes, title: str
    ) -> dict:
        h = cred.h.to_bytes(32, "big").hex()
        await conn.execute(
            "INSERT INTO portfolio_images(h,jpg) VALUES(:h,:jpg) ON CONFLICT(h) DO NOTHING",
            {"h": h, "jpg": jpg},
        )
        row = dict(
            id=uuid.uuid4().hex,
            pubkey=pubkey,
            h=h,
            title=title,
            showing=make_showing(pubkey, cred),
            signature=None,
            status="owned",
            created=int(time.time()),
            sent=None,
        )
        await conn.execute(
            """INSERT INTO portfolio_cards
            (id,pubkey,h,title,credential,showing,status,created)
            VALUES(:id,:pubkey,:h,:title,:credential,:showing,:status,:created)""",
            {**row, "credential": cred.to_bytes()},
        )
        return row

    async def mint(self, pubkey: str, data: bytes, title: str) -> dict:
        jpg = await run_in_threadpool(normalize_image, data)
        if len(jpg) > self.max_image_bytes:
            raise HTTPException(
                413, "The normalized image is too large. Use a smaller image."
            )
        await self.moderation.check(pubkey, jpg)
        h = hash_asset(jpg)
        secret = secrets.randbelow(curve_order - 1) + 1
        commitment, _ = prove_owner_secret(secret)
        async with self.db.get_connection(locks=CARD_LOCKS) as conn:
            await self.capacity(conn, pubkey, len(jpg))
            begin = await self.ledger.issue_nft_begin(conn=conn)
            # This server is still the custodial wallet. Its call into the
            # ledger uses a hidden scalar, but the uploaded image is public.
            base = PublicKey(compressed=bytes.fromhex(begin["u"]), group="G1")
            tag, B, t, blind_proof = blind_issue_commit(
                self.ledger.keyset, h, secret, base, bytes.fromhex(begin["session"])
            )
            u, v_raw = await self.ledger.issue_nft_blind(
                begin["session"], tag, B, commitment, blind_proof, conn=conn
            )
            v = unblind_issued(v_raw, t, self.ledger.keyset)
            cred = Credential(
                u=u, v=v, h=h, s=secret, keyset_id=self.ledger.keyset.keyset_id
            )
            return await self.store_card(conn, pubkey, cred, jpg, title)

    async def receive(self, pubkey: str, data: bytes, title: str) -> dict:
        jpg, token = split_transfer(data)
        await run_in_threadpool(validate_image, jpg)
        if token is None:
            raise HTTPException(
                400,
                "This file has no transfer token. Ask for the original transfer file.",
            )
        old = NFTClient.decode_token(token)
        if old.h != hash_asset(jpg):
            raise HTTPException(
                400,
                "The embedded token does not belong to this picture. Nothing was redeemed.",
            )
        await self.moderation.check(pubkey, jpg)
        if old.keyset_id != self.ledger.keyset.keyset_id:
            raise HTTPException(400, "This NFT belongs to another mint.")
        secret = secrets.randbelow(curve_order - 1) + 1
        commitment, proof = prove_owner_secret(secret)
        presentation = present(old, binding=commitment.format())
        async with self.db.get_connection(locks=CARD_LOCKS) as conn:
            h = old.h.to_bytes(32, "big").hex()
            existing = await conn.fetchone(
                "SELECT h FROM portfolio_images WHERE h=:h", {"h": h}
            )
            await self.capacity(conn, pubkey, 0 if existing else len(jpg))
            u, v = await self.ledger.transfer(
                presentation, commitment, proof, conn=conn
            )
            await conn.execute(
                """UPDATE portfolio_cards SET status='sent',sent=:now,credential=NULL
                WHERE h=:h AND status!='sent'""",
                {"h": h, "now": int(time.time())},
            )
            cred = Credential(u=u, v=v, h=old.h, s=secret, keyset_id=old.keyset_id)
            return await self.store_card(conn, pubkey, cred, jpg, title)

    async def reconcile(self, conn: Connection, pubkey: str) -> None:
        rows = await conn.fetchall(
            "SELECT id,showing FROM portfolio_cards WHERE pubkey=:p AND status!='sent'",
            {"p": pubkey},
        )
        for row in rows:
            nullifier = NFTClient.decode_showing(row["showing"])[1].nullifier.format()
            if await conn.fetchone(
                "SELECT nullifier FROM ps_nullifiers WHERE nullifier=:n",
                {"n": nullifier},
            ):
                await conn.execute(
                    "UPDATE portfolio_cards SET status='sent',sent=:t,credential=NULL,encrypted_credential=NULL WHERE id=:id",
                    {"id": row["id"], "t": int(time.time())},
                )

    async def profile(self, pubkey: str) -> dict:
        async with self.db.get_connection(
            locks=[LockOptions(table="portfolio_cards")]
        ) as conn:
            profile = await conn.fetchone(
                "SELECT * FROM portfolio_profiles WHERE pubkey=:p", {"p": pubkey}
            )
            if profile is None:
                raise HTTPException(404, "This portfolio has not been created yet.")
            await self.reconcile(conn, pubkey)
            rows = await conn.fetchall(
                "SELECT * FROM portfolio_cards WHERE pubkey=:p ORDER BY created DESC,rowid DESC",
                {"p": pubkey},
            )
            return {
                **dict(profile),
                "nostr": bool(profile["nostr"]),
                "cards": [self.public_card(dict(r), owner=False) for r in rows],
            }

    async def store_vault(
        self, conn: Connection, pubkey: str, vault: VaultRequest
    ) -> None:
        """Write-once: a vault can be re-stored only with the same check value."""
        row = await conn.fetchone(
            "SELECT vault_check FROM portfolio_vaults WHERE pubkey=:p", {"p": pubkey}
        )
        if row is not None and row["vault_check"] != vault.check:
            raise HTTPException(409, "This collection already has another wallet key.")
        await conn.execute(
            """INSERT INTO portfolio_vaults(pubkey,ciphertext,vault_check,updated)
            VALUES(:p,:c,:k,:t) ON CONFLICT(pubkey) DO UPDATE SET ciphertext=:c, updated=:t""",
            {
                "p": pubkey,
                "c": vault.ciphertext,
                "k": vault.check,
                "t": int(time.time()),
            },
        )

    async def pending_cards(self, pubkey: str) -> List[str]:
        rows = await self.db.fetchall(
            "SELECT id FROM portfolio_cards WHERE pubkey=:p AND status='ready'",
            {"p": pubkey},
        )
        return [r["id"] for r in rows]

    async def owned_card(self, conn: Connection, pubkey: str, card_id: str) -> dict:
        row = await conn.fetchone(
            "SELECT * FROM portfolio_cards WHERE id=:id AND pubkey=:p",
            {"id": card_id, "p": pubkey},
        )
        if row is None:
            raise HTTPException(404, "Card not found in your portfolio.")
        if row["status"] == "sent":
            raise HTTPException(409, "This card has already been transferred.")
        return dict(row)

    async def export(self, pubkey: str, card_id: str) -> bytes:
        async with self.db.get_connection(locks=CARD_LOCKS) as conn:
            await self.reconcile(conn, pubkey)
            row = await self.owned_card(conn, pubkey, card_id)
            if row["signature"] is None:
                raise HTTPException(
                    409, "Finish signing this card's ownership proof first."
                )
            image = await conn.fetchone(
                "SELECT jpg FROM portfolio_images WHERE h=:h", {"h": row["h"]}
            )
            if image is None:
                raise HTTPException(404, "The image is unavailable.")
            cred = Credential.from_bytes(bytes(row["credential"]))
            jpg = embed_token(bytes(image["jpg"]), TOKEN_PREFIX + cred.to_bytes().hex())
            await conn.execute(
                "UPDATE portfolio_cards SET status='ready' WHERE id=:id",
                {"id": card_id},
            )
            return jpg

    async def rename(self, pubkey: str, card_id: str, title: str) -> dict:
        """Rename an NFT the profile holds. Listings, activity and previews read
        the card's title; links and finished sales keep the title they had."""
        async with self.db.get_connection(locks=CARD_LOCKS) as conn:
            await self.reconcile(conn, pubkey)
            row = await self.owned_card(conn, pubkey, card_id)
            await conn.execute(
                "UPDATE portfolio_cards SET title=:t WHERE id=:id",
                {"t": title, "id": card_id},
            )
            return self.public_card({**row, "title": title})

    async def cancel(self, pubkey: str, card_id: str) -> dict:
        async with self.db.get_connection(locks=CARD_LOCKS) as conn:
            await self.reconcile(conn, pubkey)
            row = await self.owned_card(conn, pubkey, card_id)
            if row["status"] != "ready":
                raise HTTPException(409, "There is no exported transfer to cancel.")
            old = Credential.from_bytes(bytes(row["credential"]))
            secret = secrets.randbelow(curve_order - 1) + 1
            commitment, proof = prove_owner_secret(secret)
            u, v = await self.ledger.transfer(
                present(old, binding=commitment.format()), commitment, proof, conn=conn
            )
            cred = Credential(u=u, v=v, h=old.h, s=secret, keyset_id=old.keyset_id)
            showing = make_showing(pubkey, cred)
            await conn.execute(
                """UPDATE portfolio_cards SET credential=:c,showing=:s,signature=NULL,status='owned'
                WHERE id=:id""",
                {"id": card_id, "c": cred.to_bytes(), "s": showing},
            )
            return self.public_card(
                {**row, "showing": showing, "signature": None, "status": "owned"}
            )


def create_portfolio_app(
    data_dir: str,
    *,
    max_image_bytes: int = 10 * 1024 * 1024,
    max_cards: Optional[int] = None,
    max_storage_bytes: int = 1024**3,
    market_dev_mints: Optional[List[str]] = None,
    executor_interval: float = 5.0,
    run_executor: bool = True,
    nsfw_model: Optional[str] = None,
    nsfw_threshold: float = DEFAULT_THRESHOLD,
    turnstile_sitekey: Optional[str] = None,
    turnstile_secret: Optional[str] = None,
) -> FastAPI:
    portfolio = Portfolio(
        data_dir,
        max_image_bytes,
        max_cards,
        max_storage_bytes,
        NSFWClassifier(nsfw_model) if nsfw_model else None,
        nsfw_threshold,
    )
    browser_wallet = BrowserPortfolio(portfolio)
    social = Social(portfolio)
    links = Links(portfolio)
    dev_mints = frozenset(market_dev_mints or [])
    market = Market(portfolio, data_dir, MintNetPolicy(dev_mints=dev_mints))
    executor = Executor(market, interval=executor_interval)
    # Browsers talk to payment mints directly (wallet balances, funding,
    # verification), so connect-src admits HTTPS/WSS plus configured dev mints.
    connect_src = " ".join(["'self'", "https:", "wss:", *sorted(dev_mints)])
    # Image uploads carry a Cloudflare Turnstile token when keys are set. Its
    # script and (invisible) iframe come from challenges.cloudflare.com.
    turnstile = configure_turnstile(turnstile_sitekey, turnstile_secret)
    challenges = " https://challenges.cloudflare.com" if turnstile else ""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await portfolio.migrate()
        await browser_wallet.migrate()
        await social.migrate()
        await links.migrate()
        await market.migrate()
        if run_executor:
            executor.start()
        yield
        await executor.stop()
        await market.client.aclose()
        if turnstile:
            await turnstile.aclose()
        await portfolio.db.engine.dispose()

    app = FastAPI(
        title="Nonfungible.cash", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.portfolio = portfolio
    app.state.browser_wallet = browser_wallet
    app.state.market = market
    app.state.executor = executor
    requests: Dict[str, Deque[float]] = defaultdict(deque)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith(("/api/", "/v1/")):
            address = request.client.host if request.client else "unknown"
            now = time.monotonic()
            if address not in requests and len(requests) >= 5000:
                for old in list(requests):
                    if not requests[old] or requests[old][-1] < now - 60:
                        del requests[old]
            window = requests[address]
            while window and window[0] < now - 60:
                window.popleft()
            if len(window) >= 240:
                return JSONResponse(
                    {"detail": "Too many requests. Try again in a minute."},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )
            window.append(now)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; script-src 'self'{challenges}; frame-src 'self'{challenges}; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; font-src 'self'; connect-src {connect_src}; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        # Routes serving versioned URLs (avatars, previews) set their own.
        if request.url.path.startswith(("/api/", "/v1/")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request: Request, error: ValueError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(AlreadyMintedError)
    async def duplicate(request: Request, error: AlreadyMintedError):
        return JSONResponse(
            {"detail": "Already minted. Upload its transfer file to receive it."},
            status_code=409,
        )

    @app.exception_handler(AlreadySpentError)
    async def spent(request: Request, error: AlreadySpentError):
        return JSONResponse(
            {"detail": "This transfer was already redeemed or canceled."},
            status_code=409,
        )

    @app.exception_handler(NFTError)
    async def invalid_nft(request: Request, error: NFTError):
        return JSONResponse({"detail": str(error)}, status_code=400)

    async def read_body(request: Request, limit: int) -> bytes:
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, "This upload is too large.")
            chunks.append(chunk)
        return b"".join(chunks)

    async def authorize(
        request: Request, pubkey: str, body: bytes, sessions: bool = True
    ) -> None:
        """A single-use challenge signed by the profile key (raw, or an event
        from a signing extension) or, with X-Portfolio-Session, by a live
        session key the profile key authorized."""
        validate_pubkey(pubkey)
        nonce = request.headers.get("X-Portfolio-Challenge", "")
        session = request.headers.get("X-Portfolio-Session")
        signature = request.headers.get("X-Portfolio-Signature", "")
        target = request.url.path + (
            "?" + request.url.query if request.url.query else ""
        )
        async with portfolio.db.get_connection(
            locks=[LockOptions(table="portfolio_challenges")]
        ) as conn:
            row = await conn.fetchone(
                "SELECT * FROM portfolio_challenges WHERE nonce=:n", {"n": nonce}
            )
            if (
                row is None
                or row["pubkey"] != pubkey
                or row["expires"] <= int(time.time())
            ):
                raise HTTPException(401, "Unlock your portfolio and try again.")
            expected = auth_message(
                pubkey,
                request.method,
                target,
                hashlib.sha256(body).hexdigest(),
                nonce,
                row["expires"],
            )
            digest = hashlib.sha256(expected.encode()).digest()
            if session is None:
                valid = verify_signature(pubkey, "auth", digest, signature)
            else:
                if not sessions:
                    raise HTTPException(
                        403, "This action needs a signature from the profile key."
                    )
                live = re.fullmatch(r"[0-9a-f]{64}", session) and await conn.fetchone(
                    """SELECT session_pubkey FROM portfolio_sessions
                        WHERE session_pubkey=:s AND pubkey=:p AND expires>:now""",
                    {"s": session, "p": pubkey, "now": int(time.time())},
                )
                if not live:
                    raise HTTPException(401, "Your sign-in has expired. Sign in again.")
                # Session keys live in the page and sign raw.
                valid = re.fullmatch(
                    r"[0-9a-f]{128}", signature
                ) is not None and verify_signature(session, "auth", digest, signature)
            if row["message"] != expected or not valid:
                raise HTTPException(
                    403, "This action needs a valid signature from the profile key."
                )
            await conn.execute(
                "DELETE FROM portfolio_challenges WHERE nonce=:n", {"n": nonce}
            )

    @app.get("/api/config")
    async def config():
        return {
            "keyset_id": portfolio.ledger.keyset.keyset_id,
            "public_key": portfolio.ledger.keyset.to_bytes().hex(),
            "max_image_bytes": max_image_bytes,
            # The previous web app reads this name.
            "max_jpg_bytes": max_image_bytes,
            "max_cards": max_cards,
            "wallet_mode": "browser",
            "wallet_version": 2,
            "turnstile_sitekey": turnstile.sitekey if turnstile else None,
        }

    @app.post("/api/auth/challenge")
    async def challenge(body: ChallengeRequest):
        validate_pubkey(body.pubkey)
        if not body.path.startswith(f"/api/profiles/{body.pubkey}"):
            raise HTTPException(400, "Challenge must address your profile.")
        nonce, expires = secrets.token_hex(32), int(time.time()) + 120
        message = auth_message(
            body.pubkey, body.method, body.path, body.body_hash, nonce, expires
        )
        async with portfolio.db.get_connection(
            locks=[LockOptions(table="portfolio_challenges")]
        ) as conn:
            await conn.execute(
                "DELETE FROM portfolio_challenges WHERE expires<=:now",
                {"now": int(time.time())},
            )
            count = await conn.fetchone(
                "SELECT COUNT(*) AS n FROM portfolio_challenges"
            )
            if count is not None and count["n"] >= 10000:
                raise HTTPException(
                    429, "Too many pending requests. Try again shortly."
                )
            await conn.execute(
                "INSERT INTO portfolio_challenges VALUES(:n,:p,:m,:e)",
                {"n": nonce, "p": body.pubkey, "m": message, "e": expires},
            )
        return {"nonce": nonce, "expires": expires, "message": message}

    @app.post("/api/profiles/{pubkey}")
    async def create_profile(pubkey: str, request: Request):
        raw = await read_body(request, 1024)
        await authorize(request, pubkey, raw)
        try:
            body = ProfileRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(
                400, "Use a collection name between 1 and 40 characters."
            )
        async with portfolio.db.get_connection(
            locks=[LockOptions(table="portfolio_profiles")]
        ) as conn:
            exists = await conn.fetchone(
                "SELECT pubkey FROM portfolio_profiles WHERE pubkey=:p", {"p": pubkey}
            )
            if exists is None:
                count = await conn.fetchone(
                    "SELECT COUNT(*) AS n FROM portfolio_profiles"
                )
                if count is not None and count["n"] >= 1000:
                    raise HTTPException(409, "The mint has reached its profile limit.")
                await conn.execute(
                    """INSERT INTO portfolio_profiles(pubkey,name,created,nostr)
                    VALUES(:p,:name,:t,:nostr)""",
                    {
                        "p": pubkey,
                        "name": body.name.strip() or "Collector",
                        "t": int(time.time()),
                        "nostr": int(body.vault is not None),
                    },
                )
                if body.vault is not None:
                    await portfolio.store_vault(conn, pubkey, body.vault)
        return await portfolio.profile(pubkey)

    @app.post("/api/profiles/{pubkey}/session")
    async def create_session(pubkey: str, request: Request):
        raw = await read_body(request, 512)
        # Only the profile key itself, never another session, adds a session.
        await authorize(request, pubkey, raw, sessions=False)
        try:
            body = SessionRequest.model_validate_json(raw)
            PublicKeyXOnly(bytes.fromhex(body.session_pubkey))
        except (ValidationError, ValueError):
            raise HTTPException(400, "Invalid session key.")
        now = int(time.time())
        if not now < body.expires <= now + SESSION_TTL:
            raise HTTPException(400, "A sign-in lasts at most 30 days.")
        async with portfolio.db.get_connection(
            locks=[LockOptions(table="portfolio_sessions")]
        ) as conn:
            await conn.execute(
                "DELETE FROM portfolio_sessions WHERE expires<=:now", {"now": now}
            )
            taken = await conn.fetchone(
                "SELECT pubkey FROM portfolio_sessions WHERE session_pubkey=:s",
                {"s": body.session_pubkey},
            )
            if taken is not None and taken["pubkey"] != pubkey:
                raise HTTPException(409, "Invalid session key.")
            await conn.execute(
                """INSERT INTO portfolio_sessions(session_pubkey,pubkey,expires,created)
                VALUES(:s,:p,:e,:t) ON CONFLICT(session_pubkey) DO UPDATE SET expires=:e""",
                {"s": body.session_pubkey, "p": pubkey, "e": body.expires, "t": now},
            )
            # Beyond the cap, the sessions closest to expiry end first.
            await conn.execute(
                """DELETE FROM portfolio_sessions WHERE pubkey=:p AND session_pubkey NOT IN (
                SELECT session_pubkey FROM portfolio_sessions WHERE pubkey=:p
                ORDER BY expires DESC LIMIT :n)""",
                {"p": pubkey, "n": MAX_SESSIONS},
            )
        return {"session_pubkey": body.session_pubkey, "expires": body.expires}

    @app.post("/api/profiles/{pubkey}/session/revoke")
    async def revoke_session(pubkey: str, request: Request):
        raw = await read_body(request, 256)
        await authorize(request, pubkey, raw)
        try:
            body = RevokeRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(400, "Invalid session key.")
        await portfolio.db.execute(
            "DELETE FROM portfolio_sessions WHERE session_pubkey=:s AND pubkey=:p",
            {"s": body.session_pubkey, "p": pubkey},
        )
        return {"ok": True}

    @app.post("/api/profiles/{pubkey}/vault/get")
    async def get_vault(pubkey: str, request: Request):
        await authorize(request, pubkey, await read_body(request, 0))
        row = await portfolio.db.fetchone(
            "SELECT ciphertext, vault_check FROM portfolio_vaults WHERE pubkey=:p",
            {"p": pubkey},
        )
        if row is None:
            raise HTTPException(404, "No wallet key is stored for this collection.")
        return {"ciphertext": row["ciphertext"], "check": row["vault_check"]}

    @app.post("/api/profiles/{pubkey}/vault")
    async def put_vault(pubkey: str, request: Request):
        raw = await read_body(request, 2048)
        await authorize(request, pubkey, raw)
        try:
            body = VaultRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(400, "Invalid wallet key backup.")
        async with portfolio.db.get_connection(
            locks=[
                LockOptions(table="portfolio_profiles"),
                LockOptions(table="portfolio_vaults"),
            ]
        ) as conn:
            profile = await conn.fetchone(
                "SELECT nostr FROM portfolio_profiles WHERE pubkey=:p", {"p": pubkey}
            )
            if profile is None:
                raise HTTPException(404, "Create this collection first.")
            if not profile["nostr"]:
                raise HTTPException(409, "This collection uses its own key.")
            await portfolio.store_vault(conn, pubkey, body)
        return {"ok": True}

    @app.get("/api/profiles/{pubkey}")
    async def get_profile(pubkey: str):
        validate_pubkey(pubkey)
        profile = await portfolio.profile(pubkey)
        avatar = await portfolio.db.fetchone(
            "SELECT updated FROM portfolio_avatars WHERE pubkey=:p", {"p": pubkey}
        )
        return {
            **profile,
            **await social.summary(pubkey),
            "avatar": avatar["updated"] if avatar else None,
        }

    @app.post("/api/profiles/{pubkey}/settings")
    async def update_settings(pubkey: str, request: Request):
        raw = await read_body(request, 1024)
        await authorize(request, pubkey, raw)
        try:
            body = SettingsRequest.model_validate_json(raw)
        except ValidationError as error:
            fields = {str(e["loc"][0]) for e in error.errors() if e["loc"]}
            raise HTTPException(
                400,
                "Pick a cover from the NFTs in your collection."
                if fields == {"cover"}
                else "Use a name between 1 and 40 characters.",
            )
        await social.update(pubkey, body)
        return await get_profile(pubkey)

    async def toggle(request: Request, pubkey: str, target: str) -> bool:
        validate_pubkey(target)
        raw = await read_body(request, 256)
        await authorize(request, pubkey, raw)
        try:
            return ToggleRequest.model_validate_json(raw).on
        except ValidationError:
            raise HTTPException(400, 'Send {"on": true} or {"on": false}.')

    @app.post("/api/profiles/{pubkey}/likes/{target}")
    async def like(pubkey: str, target: str, request: Request):
        return await social.set_like(
            pubkey, target, await toggle(request, pubkey, target)
        )

    @app.post("/api/profiles/{pubkey}/follows/{target}")
    async def follow(pubkey: str, target: str, request: Request):
        return await social.set_follow(
            pubkey, target, await toggle(request, pubkey, target)
        )

    @app.get("/api/profiles/{pubkey}/relations")
    async def relations(pubkey: str):
        validate_pubkey(pubkey)
        return await social.relations(pubkey)

    @app.get("/api/profiles/{pubkey}/network")
    async def network(pubkey: str):
        validate_pubkey(pubkey)
        return await social.network(pubkey)

    @app.get("/api/profiles/{pubkey}/feed")
    async def feed(
        pubkey: str,
        limit: int = Query(default=30, ge=1, le=100),
        before: Optional[int] = Query(default=None, ge=0),
    ):
        validate_pubkey(pubkey)
        followees = await social.following(pubkey)
        return await social.activity(
            limit, before, followees, ["mint", "receive", "collection"]
        )

    async def refuse_if_listed(card_id: str) -> None:
        """Listed NFTs can't be exported, linked or rotated: unlist first."""
        async with portfolio.db.get_connection() as conn:
            if await market.card_listed(conn, card_id):
                raise HTTPException(409, "Unlist this NFT before sending it.")

    app.include_router(market_router(market, executor, authorize, read_body))

    @app.post("/api/profiles/{pubkey}/links")
    async def create_link(pubkey: str, request: Request):
        raw = await read_body(request, 8192)
        await authorize(request, pubkey, raw)
        try:
            body = LinkRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(400, "Invalid transfer link.")
        await refuse_if_listed(body.card_id)
        return await links.create(pubkey, body)

    @app.get("/api/links/{link_id}")
    async def get_link(link_id: str):
        if not re.fullmatch(r"[0-9a-f]{32}", link_id):
            raise HTTPException(404, "This link doesn't exist.")
        return await links.get(link_id)

    @app.get("/api/explore/collections")
    async def explore_collections(
        sort: CollectionSort = "popular",
        q: str = Query(default="", max_length=40),
        limit: int = Query(default=24, ge=1, le=60),
        offset: int = Query(default=0, ge=0, le=10000),
    ):
        return await social.collections(sort, q.strip(), limit, offset)

    @app.get("/api/explore/nfts")
    async def explore_nfts(
        sort: NFTSort = "new",
        q: str = Query(default="", max_length=80),
        limit: int = Query(default=30, ge=1, le=60),
        offset: int = Query(default=0, ge=0, le=10000),
    ):
        return await social.nfts(sort, q.strip(), limit, offset)

    @app.get("/api/activity")
    async def activity(
        limit: int = Query(default=30, ge=1, le=100),
        before: Optional[int] = Query(default=None, ge=0),
        actor: Optional[str] = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    ):
        return await social.activity(limit, before, [actor] if actor else None)

    @app.post("/api/profiles/{pubkey}/mint")
    async def mint(
        pubkey: str,
        request: Request,
        title: str = Query(default="Untitled", min_length=1, max_length=80),
    ):
        raw = await read_body(request, max_image_bytes)
        await authorize(request, pubkey, raw)
        raise HTTPException(410, "Mint with the browser wallet.")

    @app.post("/api/profiles/{pubkey}/receive")
    async def receive(
        pubkey: str,
        request: Request,
        title: str = Query(default="Received", min_length=1, max_length=80),
    ):
        raw = await read_body(request, max_image_bytes + 65536)
        await authorize(request, pubkey, raw)
        raise HTTPException(
            410, "Receive transfer files locally with the browser wallet."
        )

    @app.post("/api/profiles/{pubkey}/cards/{card_id}/claim")
    async def sign_claim(pubkey: str, card_id: str, request: Request):
        raw = await read_body(request, 8192)
        await authorize(request, pubkey, raw)
        try:
            body = ClaimRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(400, "Invalid ownership signature.")
        async with portfolio.db.get_connection(locks=CARD_LOCKS) as conn:
            await portfolio.reconcile(conn, pubkey)
            row = await portfolio.owned_card(conn, pubkey, card_id)
            if body.showing != row["showing"] or not verify_signature(
                pubkey, "claim", claim_digest(body.showing), body.signature
            ):
                raise HTTPException(
                    403, "The profile signature does not match this ownership proof."
                )
            await conn.execute(
                "UPDATE portfolio_cards SET signature=:s WHERE id=:id",
                {"s": body.signature, "id": card_id},
            )
            return portfolio.public_card({**row, "signature": body.signature})

    @app.post("/api/profiles/{pubkey}/cards/{card_id}/title")
    async def rename(pubkey: str, card_id: str, request: Request):
        raw = await read_body(request, 512)
        await authorize(request, pubkey, raw)
        try:
            title = TitleRequest.model_validate_json(raw).title.strip()
        except ValidationError:
            title = ""
        if not title:
            raise HTTPException(400, "Use a name between 1 and 80 characters.")
        return await portfolio.rename(pubkey, card_id, title)

    @app.post("/api/profiles/{pubkey}/cards/{card_id}/export")
    async def export(pubkey: str, card_id: str, request: Request):
        raw = await read_body(request, 0)
        await authorize(request, pubkey, raw)
        raise HTTPException(
            410, "Generate transfer files locally with the browser wallet."
        )

    @app.post("/api/profiles/{pubkey}/cards/{card_id}/cancel")
    async def cancel(pubkey: str, card_id: str, request: Request):
        raw = await read_body(request, 0)
        await authorize(request, pubkey, raw)
        raise HTTPException(
            410, "Rotate the credential locally with the browser wallet."
        )

    @app.post("/api/profiles/{pubkey}/wallet/prepare")
    async def wallet_prepare(
        pubkey: str,
        request: Request,
        kind: Literal["mint", "receive", "rotate", "refresh", "migrate"] = Query(),
        title: str = Query(default="Untitled", min_length=1, max_length=80),
        card_id: Optional[str] = Query(default=None),
        issuance_version: Literal["1", "2", "3"] = Query(default="1"),
    ):
        raw = await read_body(request, max_image_bytes)
        await authorize(request, pubkey, raw)
        if turnstile and kind in ("mint", "receive"):
            await turnstile.verify(request)
        if card_id and kind in ("rotate", "refresh", "migrate"):
            await refuse_if_listed(card_id)
        return await browser_wallet.prepare(
            pubkey,
            kind,
            raw,
            title,
            card_id,
            3 if issuance_version == "3" else 2 if issuance_version == "2" else 1,
        )

    @app.post("/api/profiles/{pubkey}/wallet/operations/{operation_id}/backup")
    async def wallet_backup(pubkey: str, operation_id: str, request: Request):
        raw = await read_body(request, 40000)
        await authorize(request, pubkey, raw)
        try:
            body = Envelope.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(400, "Invalid encrypted recovery envelope.")
        await browser_wallet.backup(pubkey, operation_id, body)
        return {"saved": True}

    @app.post("/api/profiles/{pubkey}/wallet/operations/{operation_id}/finish")
    async def wallet_finish(pubkey: str, operation_id: str, request: Request):
        raw = await read_body(request, 8192)
        await authorize(request, pubkey, raw)
        try:
            body = WalletProofRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(400, "Invalid browser wallet proof.")
        return await browser_wallet.finish(pubkey, operation_id, body)

    @app.post("/api/profiles/{pubkey}/wallet/operations/{operation_id}/discard")
    async def wallet_discard(pubkey: str, operation_id: str, request: Request):
        raw = await read_body(request, 0)
        await authorize(request, pubkey, raw)
        await browser_wallet.discard(pubkey, operation_id)
        return {"discarded": True}

    @app.post("/api/profiles/{pubkey}/wallet/operations/{operation_id}/publish")
    async def wallet_publish(pubkey: str, operation_id: str, request: Request):
        raw = await read_body(request, 40000)
        await authorize(request, pubkey, raw)
        try:
            body = PublishRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(400, "Invalid encrypted NFT publication.")
        return await browser_wallet.publish(pubkey, operation_id, body)

    @app.post("/api/profiles/{pubkey}/wallet/recover")
    async def wallet_recover(pubkey: str, request: Request):
        raw = await read_body(request, 0)
        await authorize(request, pubkey, raw)
        return await browser_wallet.recover(pubkey)

    @app.post("/api/profiles/{pubkey}/wallet/cards/{card_id}/ready")
    async def wallet_ready(pubkey: str, card_id: str, request: Request):
        raw = await read_body(request, 0)
        await authorize(request, pubkey, raw)
        await refuse_if_listed(card_id)
        async with portfolio.db.get_connection(locks=CARD_LOCKS) as conn:
            await portfolio.reconcile(conn, pubkey)
            row = await portfolio.owned_card(conn, pubkey, card_id)
            if not row["encrypted_credential"]:
                raise HTTPException(
                    409, "Move this NFT into your browser wallet first."
                )
            await conn.execute(
                "UPDATE portfolio_cards SET status='ready' WHERE id=:id",
                {"id": card_id},
            )
            return portfolio.public_card({**row, "status": "ready"})

    @app.post("/api/profiles/{pubkey}/wallet/cards/{card_id}/delete")
    async def wallet_delete(pubkey: str, card_id: str, request: Request):
        raw = await read_body(request, 2048)
        await authorize(request, pubkey, raw)
        try:
            body = DeleteRequest.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(400, "Invalid burn presentation.")
        await browser_wallet.delete(pubkey, card_id, body, market.card_listed)
        return {"deleted": card_id}

    @app.post("/api/profiles/{pubkey}/cards/pending")
    async def pending_cards(pubkey: str, request: Request):
        # Owner-only: which cards have an outstanding transfer file or link.
        await authorize(request, pubkey, await read_body(request, 0))
        return {"ids": await portfolio.pending_cards(pubkey)}

    @app.post("/api/profiles/{pubkey}/avatar")
    async def upload_avatar(pubkey: str, request: Request):
        raw = await read_body(request, MAX_AVATAR_BYTES)
        await authorize(request, pubkey, raw)
        if turnstile:
            await turnstile.verify(request)
        try:
            jpg = await run_in_threadpool(avatar_jpg, raw)
        except ValueError as error:
            raise HTTPException(400, str(error))
        await portfolio.moderation.check(pubkey, jpg)
        await portfolio.db.execute(
            """INSERT INTO portfolio_avatars(pubkey,jpg,updated) VALUES(:p,:j,:t)
            ON CONFLICT(pubkey) DO UPDATE SET jpg=:j, updated=:t""",
            {"p": pubkey, "j": jpg, "t": int(time.time())},
        )
        return await get_profile(pubkey)

    @app.post("/api/profiles/{pubkey}/avatar/remove")
    async def remove_avatar(pubkey: str, request: Request):
        await authorize(request, pubkey, await read_body(request, 0))
        await portfolio.db.execute(
            "DELETE FROM portfolio_avatars WHERE pubkey=:p", {"p": pubkey}
        )
        return await get_profile(pubkey)

    @app.get("/api/avatars")
    async def avatar_versions(pubkeys: str = Query(default="", max_length=6600)):
        """Batched lookup: which profiles have a picture, and its version."""
        keys = [k for k in pubkeys.split(",") if k][:100]
        for key in keys:
            validate_pubkey(key)
        if not keys:
            return {}
        clause = ",".join(f":k{i}" for i in range(len(keys)))
        rows = await portfolio.db.fetchall(
            f"SELECT pubkey, updated FROM portfolio_avatars WHERE pubkey IN ({clause})",
            {f"k{i}": k for i, k in enumerate(keys)},
        )
        return {r["pubkey"]: r["updated"] for r in rows}

    @app.get("/api/avatars/{pubkey}.jpg")
    async def avatar(pubkey: str):
        validate_pubkey(pubkey)
        row = await portfolio.db.fetchone(
            "SELECT jpg FROM portfolio_avatars WHERE pubkey=:p", {"p": pubkey}
        )
        if row is None:
            raise HTTPException(404, "No profile picture.")
        # URLs carry ?v=<updated>, so a new picture is a new URL.
        return Response(
            bytes(row["jpg"]),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # /api/images/<h>, or with any extension: links from when every image was
    # a JPG end in .jpg. The stored bytes decide the Content-Type.
    @app.get("/api/images/{name}")
    async def image(name: str):
        h = name.split(".", 1)[0]
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise HTTPException(404, "Image not found.")
        row = await portfolio.db.fetchone(
            "SELECT jpg FROM portfolio_images WHERE h=:h", {"h": h}
        )
        if row is None:
            raise HTTPException(404, "Image not found.")
        data = bytes(row["jpg"])
        return Response(data, media_type=image_format(data).mime)

    # Social preview images. Renders are cached by content version, so a
    # crawler can't force fresh renders by varying ?v=.
    og_cache: "OrderedDict[str, bytes]" = OrderedDict()

    async def stored_jpg(h: str) -> Optional[bytes]:
        row = await portfolio.db.fetchone(
            "SELECT jpg FROM portfolio_images WHERE h=:h", {"h": h}
        )
        return bytes(row["jpg"]) if row else None

    async def avatar_row(pubkey: str) -> Optional[dict]:
        row = await portfolio.db.fetchone(
            "SELECT jpg, updated FROM portfolio_avatars WHERE pubkey=:p",
            {"p": pubkey},
        )
        return {"jpg": bytes(row["jpg"]), "updated": row["updated"]} if row else None

    async def og_jpg(
        key: str, version: str, asked: str, render: Callable[[], bytes]
    ) -> Response:
        jpg = og_cache.get(key + version)
        if jpg is None:
            jpg = await run_in_threadpool(render)
            og_cache[key + version] = jpg
            while len(og_cache) > 64:
                og_cache.popitem(last=False)
        else:
            og_cache.move_to_end(key + version)
        return Response(
            jpg,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable"
                if asked == version
                else "public, max-age=300"
            },
        )

    @app.api_route("/api/og/p/{pubkey}.jpg", methods=["GET", "HEAD"])
    async def og_collection(pubkey: str, v: str = ""):
        profile = await get_profile(pubkey)
        avatar = await avatar_row(pubkey) if profile["avatar"] else None
        cards = [Card(c["title"] or "", await stored_jpg(c["h"])) for c in fan(profile)]
        preview = CollectionPreview(
            pubkey=pubkey,
            name=profile["name"] or "",
            avatar=avatar["jpg"] if avatar else None,
            nfts=owned_count(profile),
            followers=profile["followers"],
            likes=profile["likes"],
            cards=cards,
        )
        return await og_jpg(
            f"p/{pubkey}/",
            profile_version(profile),
            v,
            lambda: collection_image(preview),
        )

    @app.api_route("/api/og/claim/{link_id}.jpg", methods=["GET", "HEAD"])
    async def og_link(link_id: str, v: str = ""):
        link = await get_link(link_id)
        avatar = await avatar_row(link["sender"])
        preview = LinkPreview(
            sender=link["sender"],
            sender_name=link["sender_name"] or "",
            avatar=avatar["jpg"] if avatar else None,
            card=Card(link["title"] or "", await stored_jpg(link["h"])),
            status=link["status"],
        )
        return await og_jpg(
            f"claim/{link_id}/",
            link_version(link, avatar["updated"] if avatar else None),
            v,
            lambda: link_image(preview),
        )

    @app.api_route("/api/og/p/{pubkey}/{card_id}.jpg", methods=["GET", "HEAD"])
    async def og_nft(pubkey: str, card_id: str, v: str = ""):
        profile = await get_profile(pubkey)
        card = next((c for c in profile["cards"] if c["id"] == card_id), None)
        if card is None:
            raise HTTPException(404, "NFT not found.")
        avatar = await avatar_row(pubkey) if profile["avatar"] else None
        preview = NFTPreview(
            pubkey=pubkey,
            name=profile["name"] or "",
            avatar=avatar["jpg"] if avatar else None,
            card=Card(card["title"] or "", await stored_jpg(card["h"])),
            nfts=owned_count(profile),
            sent=card["status"] == "sent",
        )
        return await og_jpg(
            f"nft/{card_id}/",
            nft_version(profile, card),
            v,
            lambda: nft_image(preview),
        )

    @app.api_route("/api/og/market/{listing_id}.jpg", methods=["GET", "HEAD"])
    async def og_listing(listing_id: str, v: str = ""):
        if not re.fullmatch(r"[0-9a-f]{32}", listing_id):
            raise HTTPException(404, "Listing not found.")
        listing = await market.listing(listing_id)
        avatar = await avatar_row(listing["seller"])
        preview = ListingPreview(
            seller=listing["seller"],
            seller_name=listing["seller_name"] or "",
            avatar=avatar["jpg"] if avatar else None,
            card=Card(listing["title"] or "", await stored_jpg(listing["h"])),
            price=listing["price"],
            state=listing["state"],
            bids=listing["bids"]["count"],
            top_bid=listing["bids"]["top"],
        )
        return await og_jpg(
            f"market/{listing_id}/",
            listing_version(listing, avatar["updated"] if avatar else None),
            v,
            lambda: listing_image(preview),
        )

    # Retain bearer-redemption compatibility, but do not expose unauthenticated
    # minting: it would bypass the portfolio's upload and storage quotas.
    router = create_router(portfolio.ledger)
    allowed = {
        "/info",
        "/checkstate",
        "/asset/{asset_hash}",
        "/transfer",
        "/transfer/private/begin",
        "/transfer/private",
        "/verify",
        "/lockstate",
        "/lock",
        "/lock/claim",
        "/lock/refund",
    }
    # create_router only registers APIRoute endpoints via decorators.
    api_routes = cast(List[APIRoute], router.routes)
    router.routes = [route for route in api_routes if route.path in allowed]
    app.include_router(router, prefix="/v1/nft")
    if (WEB_DIR / "assets").exists():
        app.mount(
            "/assets", StaticFiles(directory=str(WEB_DIR / "assets")), name="assets"
        )

    @app.api_route("/", methods=["GET", "HEAD"])
    @app.api_route("/how-it-works", methods=["GET", "HEAD"])
    @app.api_route("/market", methods=["GET", "HEAD"])
    @app.api_route("/wallet", methods=["GET", "HEAD"])
    @app.api_route("/offers", methods=["GET", "HEAD"])
    @app.api_route("/explore", methods=["GET", "HEAD"])
    @app.api_route("/explore/nfts", methods=["GET", "HEAD"])
    @app.api_route("/activity", methods=["GET", "HEAD"])
    async def frontend():
        if not (WEB_DIR / "index.html").exists():
            return JSONResponse(
                {
                    "detail": "Build the portfolio frontend with npm ci and npm run build in cashu/nft/portfolio_web."
                },
                status_code=503,
            )
        return FileResponse(
            WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    def page_with_meta(meta_for) -> Response:
        """index.html with this page's social preview tags."""
        html = (WEB_DIR / "index.html").read_text()
        html = with_meta(html, meta_for(site_base(html)))
        return Response(
            html, media_type="text/html", headers={"Cache-Control": "no-cache"}
        )

    @app.api_route("/p/{pubkey}", methods=["GET", "HEAD"])
    async def profile_page(pubkey: str, nft: Optional[str] = None):
        validate_pubkey(pubkey)
        if not (WEB_DIR / "index.html").exists():
            return await frontend()
        try:
            profile = await get_profile(pubkey)
        except HTTPException:
            return await frontend()
        # /p/<pubkey>?nft=<card> opens that NFT, so its preview shows the NFT.
        card = next((c for c in profile["cards"] if c["id"] == nft), None)
        if card is not None:
            version = nft_version(profile, card)
            return page_with_meta(lambda base: nft_meta(base, profile, card, version))
        return page_with_meta(lambda base: collection_meta(base, profile))

    @app.api_route("/market/{listing_id}", methods=["GET", "HEAD"])
    async def market_page(listing_id: str):
        if not re.fullmatch(r"[0-9a-f]{32}", listing_id):
            raise HTTPException(404, "Listing not found.")
        if not (WEB_DIR / "index.html").exists():
            return await frontend()
        try:
            listing = await market.listing(listing_id)
        except HTTPException:
            return await frontend()
        avatar = await avatar_row(listing["seller"])
        version = listing_version(listing, avatar["updated"] if avatar else None)
        return page_with_meta(lambda base: listing_meta(base, listing, version))

    @app.api_route("/claim/{link_id}", methods=["GET", "HEAD"])
    async def claim_page(link_id: str):
        if not re.fullmatch(r"[0-9a-f]{32}", link_id):
            raise HTTPException(404, "This link doesn't exist.")
        if not (WEB_DIR / "index.html").exists():
            return await frontend()
        try:
            link = await links.get(link_id)
        except HTTPException:
            return await frontend()
        avatar = await avatar_row(link["sender"])
        version = link_version(link, avatar["updated"] if avatar else None)
        return page_with_meta(lambda base: link_meta(base, link, version))

    return app


def main() -> None:
    app = create_portfolio_app(
        os.environ.get("NFT_PORTFOLIO_DIR", "data/nft-portfolio"),
        max_image_bytes=int(
            os.environ.get("NFT_PORTFOLIO_MAX_IMAGE_BYTES")
            # The setting's earlier name, from when only JPGs were supported.
            or os.environ.get("NFT_PORTFOLIO_MAX_JPG_BYTES")
            or str(10 * 1024 * 1024)
        ),
        # Unset means no per-collection limit (storage is still capped).
        max_cards=(
            int(os.environ["NFT_PORTFOLIO_MAX_CARDS"])
            if os.environ.get("NFT_PORTFOLIO_MAX_CARDS")
            else None
        ),
        max_storage_bytes=int(
            os.environ.get("NFT_PORTFOLIO_MAX_STORAGE_BYTES", str(1024**3))
        ),
        # Development only: exact mint URLs the settlement worker may reach
        # over plain HTTP on local addresses (e.g. the dev ecash mint).
        market_dev_mints=[
            u.strip()
            for u in os.environ.get("NFT_MARKET_DEV_MINTS", "").split(",")
            if u.strip()
        ],
        # The ONNX model from scripts/export_nsfw_model.py. Unset disables
        # the classifier; previously rejected images stay refused.
        nsfw_model=os.environ.get("NFT_PORTFOLIO_NSFW_MODEL") or None,
        nsfw_threshold=float(
            os.environ.get("NFT_PORTFOLIO_NSFW_THRESHOLD", str(DEFAULT_THRESHOLD))
        ),
        # Cloudflare Turnstile keys; image uploads need a token when set.
        turnstile_sitekey=os.environ.get("NFT_PORTFOLIO_TURNSTILE_SITEKEY") or None,
        turnstile_secret=os.environ.get("NFT_PORTFOLIO_TURNSTILE_SECRET") or None,
    )
    # Behind a reverse proxy, trust X-Forwarded-For only from the proxy's
    # address so per-client rate limits see real client IPs.
    trusted_proxy = os.environ.get("NFT_PORTFOLIO_TRUSTED_PROXY", "")
    uvicorn.run(
        app,
        host=os.environ.get("NFT_PORTFOLIO_HOST", "127.0.0.1"),
        port=int(os.environ.get("NFT_PORTFOLIO_PORT", "8401")),
        proxy_headers=bool(trusted_proxy),
        forwarded_allow_ips=trusted_proxy or None,
    )


if __name__ == "__main__":
    main()
