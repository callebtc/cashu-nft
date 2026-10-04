"""Browser-wallet orchestration: public proofs and encrypted backups only.

The Portfolio type-only import avoids a circular dependency with app wiring.
Legacy credentials are released only to an authenticated owner for rotation.
"""

import base64
import hashlib
import json
import time
import uuid
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Optional

from coincurve import PublicKeyXOnly
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ..core.crypto.ps import Presentation, asset_tag, hash_asset, verify_showing
from ..core.db import Connection, LockOptions
from .api import (
    PrivatePresentation,
    _parse_g1,
    _parse_linear_proof,
    _parse_proof,
)
from .ledger import AlreadyMintedError, AlreadySpentError
from .portfolio_image import normalize_image, split_transfer, validate_image
from .wallet import NFTClient

if TYPE_CHECKING:
    from .portfolio import Portfolio

LOCKS = [
    LockOptions(table=name)
    for name in ("ps_assets", "ps_nullifiers", "portfolio_cards")
]
CLAIM_DOMAIN = "Cashu_NFT_Portfolio_Claim_v1\n"
# Unfinished wallet actions stage images server-side; bound them per profile
# even when collections themselves are unlimited.
MAX_PENDING_OPS = 100


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    nonce: str = Field(pattern=r"^[0-9a-f]{24}$")
    ciphertext: str = Field(
        min_length=32, max_length=32768, pattern=r"^(?:[0-9a-f]{2})+$"
    )


class WalletProofRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1, 2, 3] = 1
    b: str
    proof: str
    asset_tag: Optional[str] = None
    session: Optional[str] = None
    owner_commitment: Optional[str] = None
    presentation: Optional[str] = None
    new_owner_commitment: Optional[str] = None
    new_proof: Optional[str] = None


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The card's current credential, presented for the mint's burn binding.
    presentation: str = Field(pattern=r"^[0-9a-f]{642}$")


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    encrypted_credential: Envelope
    showing: str = Field(max_length=4096)
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")


class BrowserPortfolio:
    def __init__(self, portfolio: "Portfolio"):
        self.portfolio = portfolio
        self.db = portfolio.db
        self.ledger = portfolio.ledger

    async def migrate(self) -> None:
        async with self.db.get_connection() as conn:
            columns = await conn.fetchall("PRAGMA table_info(portfolio_cards)")
            if not any(row["name"] == "encrypted_credential" for row in columns):
                await conn.execute(
                    "ALTER TABLE portfolio_cards ADD COLUMN encrypted_credential TEXT"
                )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS portfolio_wallet_ops (
                id TEXT PRIMARY KEY, pubkey TEXT NOT NULL, kind TEXT NOT NULL,
                h TEXT NOT NULL, jpg BLOB, title TEXT NOT NULL, card_id TEXT,
                created INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'prepared',
                backup TEXT, request_hash TEXT, response TEXT)"""
            )

    async def operation(self, conn: Connection, pubkey: str, operation_id: str) -> dict:
        row = await conn.fetchone(
            "SELECT * FROM portfolio_wallet_ops WHERE id=:id AND pubkey=:p",
            {"id": operation_id, "p": pubkey},
        )
        if row is None:
            raise HTTPException(404, "Wallet operation not found.")
        return dict(row)

    async def prepare(
        self,
        pubkey: str,
        kind: str,
        data: bytes,
        title: str,
        card_id: Optional[str] = None,
        issuance_version: Literal[1, 2, 3] = 1,
    ) -> dict:
        if kind not in ("mint", "receive", "rotate", "refresh", "migrate"):
            raise HTTPException(400, "Unknown wallet action.")
        if kind == "mint":
            jpg = await run_in_threadpool(normalize_image, data)
            await self.portfolio.moderation.check(pubkey, jpg)
        elif kind == "receive":
            jpg, token = split_transfer(data)
            if token:
                raise HTTPException(
                    400,
                    "Extract the transfer token locally before uploading the public image.",
                )
            await run_in_threadpool(validate_image, jpg)
            await self.portfolio.moderation.check(pubkey, jpg)
        else:
            jpg = b""
        async with self.db.get_connection(locks=LOCKS) as conn:
            if (
                await conn.fetchone(
                    "SELECT pubkey FROM portfolio_profiles WHERE pubkey=:p",
                    {"p": pubkey},
                )
                is None
            ):
                raise HTTPException(404, "Create your portfolio first.")
            await conn.execute(
                "DELETE FROM portfolio_wallet_ops WHERE state='prepared' AND created<:cutoff",
                {"cutoff": int(time.time()) - 86400},
            )
            legacy = None
            if kind in ("rotate", "refresh", "migrate"):
                card = await self.portfolio.owned_card(conn, pubkey, card_id or "")
                if kind == "rotate" and card["status"] != "ready":
                    raise HTTPException(
                        409, "Download a transfer file before canceling it."
                    )
                # Refresh: the same credential rotation for an owned card, the
                # first step of listing it (earlier exports and links die).
                if kind == "refresh" and card["status"] != "owned":
                    raise HTTPException(409, "Only cards you hold can be listed.")
                if kind == "migrate" and card["encrypted_credential"] is not None:
                    raise HTTPException(
                        409, "This NFT is already in the browser wallet."
                    )
                if (
                    kind in ("rotate", "refresh")
                    and card["encrypted_credential"] is None
                ):
                    raise HTTPException(
                        409, "Move this NFT into your browser wallet first."
                    )
                image = await conn.fetchone(
                    "SELECT jpg FROM portfolio_images WHERE h=:h", {"h": card["h"]}
                )
                if image is None:
                    raise HTTPException(404, "Public image not found.")
                jpg, title = bytes(image["jpg"]), card["title"]
                if kind == "migrate":
                    # This old credential was already known by the custodial
                    # backend. The browser rotates it before publication.
                    if card["credential"] is None:
                        raise HTTPException(
                            409, "This credential was already transferred."
                        )
                    legacy = "psnft1" + bytes(card["credential"]).hex()
            if len(jpg) > self.portfolio.max_image_bytes:
                raise HTTPException(413, "The public image is too large.")
            h = hash_asset(jpg).to_bytes(32, "big").hex()
            status = (
                await self.ledger.asset_status(int(h, 16)) if kind == "mint" else ""
            )
            if status == "burned":
                raise HTTPException(
                    409, "This picture was deleted and can't be minted again."
                )
            if status not in ("", "unknown"):
                raise AlreadyMintedError("asset was already minted")
            pending = await conn.fetchone(
                "SELECT count(*) AS n FROM portfolio_wallet_ops WHERE pubkey=:p AND state!='completed'",
                {"p": pubkey},
            )
            if pending is not None and pending["n"] >= MAX_PENDING_OPS:
                raise HTTPException(
                    409, "Finish pending wallet actions before adding another picture."
                )
            limit = self.portfolio.max_cards
            if limit is not None:
                hashes = await conn.fetchall(
                    """SELECT h FROM portfolio_cards WHERE pubkey=:p AND status!='sent'
                    UNION SELECT h FROM portfolio_wallet_ops WHERE pubkey=:p AND state!='completed'""",
                    {"p": pubkey},
                )
                if len({row["h"] for row in hashes} | {h}) > limit:
                    raise HTTPException(
                        409, "Your collection has reached its card limit."
                    )
            image = await conn.fetchone(
                "SELECT h FROM portfolio_images WHERE h=:h", {"h": h}
            )
            size = await conn.fetchone(
                "SELECT COALESCE(sum(length(jpg)),0) AS n FROM portfolio_images"
            )
            staged = await conn.fetchone(
                "SELECT COALESCE(sum(length(jpg)),0) AS n FROM portfolio_wallet_ops WHERE state!='completed'"
            )
            if (size["n"] if size else 0) + (staged["n"] if staged else 0) + (
                0 if image else len(jpg)
            ) > self.portfolio.max_storage_bytes:
                raise HTTPException(
                    507, "The mint has reached its image storage limit."
                )
            begin = (
                await self.ledger.issue_nft_begin(conn=conn)
                if kind == "mint" and issuance_version == 1
                else None
            )
            operation_id = begin["session"] if begin else uuid.uuid4().hex
            await conn.execute(
                """INSERT INTO portfolio_wallet_ops(id,pubkey,kind,h,jpg,title,card_id,created)
                VALUES(:id,:p,:kind,:h,:jpg,:title,:card,:now)""",
                {
                    "id": operation_id,
                    "p": pubkey,
                    "kind": kind,
                    "h": h,
                    "jpg": None if image else jpg,
                    "title": title,
                    "card": card_id,
                    "now": int(time.time()),
                },
            )
        return {
            "id": operation_id,
            "h": h,
            "title": title,
            "jpg": base64.b64encode(jpg).decode(),
            "begin": begin,
            "legacy_token": legacy,
            "card_id": card_id,
        }

    async def backup(self, pubkey: str, operation_id: str, envelope: Envelope) -> None:
        async with self.db.get_connection(locks=LOCKS) as conn:
            op = await self.operation(conn, pubkey, operation_id)
            if op["state"] != "prepared":
                raise HTTPException(409, "This wallet action has already started.")
            await conn.execute(
                "UPDATE portfolio_wallet_ops SET backup=:backup WHERE id=:id",
                {"backup": envelope.model_dump_json(), "id": operation_id},
            )

    async def discard(self, pubkey: str, operation_id: str) -> None:
        """Only an action that never issued a credential can be abandoned."""
        async with self.db.get_connection(locks=LOCKS) as conn:
            op = await self.operation(conn, pubkey, operation_id)
            if op["state"] != "prepared":
                raise HTTPException(
                    409, "Recover the issued credential before continuing."
                )
            await conn.execute(
                "DELETE FROM portfolio_wallet_ops WHERE id=:id", {"id": operation_id}
            )

    async def finish(
        self, pubkey: str, operation_id: str, request: WalletProofRequest
    ) -> dict:
        # Preserve the v1 transcript for operations saved by old clients.
        body_hash = hashlib.sha256(
            request.model_dump_json(
                exclude_none=True,
                exclude={"version"} if request.version == 1 else set(),
            ).encode()
        ).hexdigest()
        async with self.db.get_connection(locks=LOCKS) as conn:
            op = await self.operation(conn, pubkey, operation_id)
            if op["request_hash"]:
                if op["request_hash"] != body_hash:
                    raise HTTPException(409, "Retry the original saved wallet request.")
                return json.loads(op["response"])
            if op["backup"] is None:
                raise HTTPException(
                    409, "Save the encrypted recovery backup before spending."
                )
            if op["kind"] == "mint":
                if (
                    request.session != operation_id
                    or not request.asset_tag
                    or not request.owner_commitment
                ):
                    raise HTTPException(400, "Incomplete blind issuance request.")
                tag = _parse_g1(request.asset_tag)
                if tag != asset_tag(int(op["h"], 16)):
                    raise HTTPException(
                        400, "The blind commitment does not match the public image."
                    )
                issue = {
                    1: self.ledger.issue_nft_blind,
                    2: self.ledger.issue_nft_blind_v2,
                    3: self.ledger.issue_nft_committed,
                }[request.version]
                u, v = await issue(
                    operation_id,
                    tag,
                    _parse_g1(request.b),
                    _parse_g1(request.owner_commitment),
                    _parse_linear_proof(request.proof),
                    conn=conn,
                )
            else:
                if (
                    not request.presentation
                    or not request.new_owner_commitment
                    or not request.new_proof
                ):
                    raise HTTPException(400, "Incomplete private transfer request.")
                try:
                    pres = PrivatePresentation.from_bytes(
                        bytes.fromhex(request.presentation)
                    )
                except ValueError:
                    raise HTTPException(400, "Invalid private presentation.")
                if op["kind"] in ("rotate", "refresh", "migrate"):
                    card = await conn.fetchone(
                        "SELECT showing FROM portfolio_cards WHERE id=:id AND pubkey=:p",
                        {"id": op["card_id"], "p": pubkey},
                    )
                    if (
                        card is None
                        or NFTClient.decode_showing(card["showing"])[1].nullifier
                        != pres.nullifier
                    ):
                        raise HTTPException(
                            403, "Rotate this card's current credential."
                        )
                u, v = await self.ledger.transfer_private(
                    pres,
                    _parse_g1(request.b),
                    _parse_linear_proof(request.proof),
                    _parse_g1(request.new_owner_commitment),
                    _parse_proof(request.new_proof),
                    conn=conn,
                )
            response = {
                "u": u.format().hex(),
                "v": v.format().hex(),
                "keyset_id": self.ledger.keyset.keyset_id,
            }
            await conn.execute(
                "UPDATE portfolio_wallet_ops SET state='issued',request_hash=:hash,response=:response WHERE id=:id",
                {
                    "hash": body_hash,
                    "response": json.dumps(response),
                    "id": operation_id,
                },
            )
        return response

    async def publish(
        self, pubkey: str, operation_id: str, body: PublishRequest
    ) -> dict:
        from_context, pres = NFTClient.decode_showing(body.showing)
        expected = f"Cashu_NFT_Portfolio_Show_v1\n{pubkey}\n{pres.h.to_bytes(32, 'big').hex()}\n{pres.keyset_id}".encode()
        if (
            from_context != expected
            or pres.keyset_id != self.ledger.keyset.keyset_id
            or not verify_showing(self.ledger.keyset, pres, from_context)
        ):
            raise HTTPException(403, "Invalid ownership showing.")
        # Profile signature validation is performed at this protocol boundary.
        if not self._verify_claim(pubkey, body.signature, body.showing):
            raise HTTPException(403, "Invalid profile signature.")
        async with self.db.get_connection(locks=LOCKS) as conn:
            op = await self.operation(conn, pubkey, operation_id)
            if op["h"] != pres.h.to_bytes(32, "big").hex() or op["state"] not in (
                "issued",
                "completed",
            ):
                raise HTTPException(
                    409, "Complete this NFT's wallet action before publishing."
                )
            if op["state"] == "completed":
                row = await conn.fetchone(
                    "SELECT * FROM portfolio_cards WHERE id=:id", {"id": op["card_id"]}
                )
                if row is None:
                    raise HTTPException(404, "Card not found.")
                return self.portfolio.public_card(dict(row))
            if await self.ledger.is_spent(pres.nullifier.format()):
                raise AlreadySpentError("credential already spent")
            previous = await conn.fetchall(
                "SELECT * FROM portfolio_cards WHERE h=:h AND status!='sent'",
                {"h": op["h"]},
            )
            for old in previous:
                old_pres = NFTClient.decode_showing(old["showing"])[1]
                if not await self.ledger.is_spent(old_pres.nullifier.format()):
                    raise HTTPException(
                        409, "The previous ownership credential has not been spent."
                    )
            await conn.execute(
                "UPDATE portfolio_cards SET status='sent',sent=:now,credential=NULL,encrypted_credential=NULL WHERE h=:h AND status!='sent'",
                {"h": op["h"], "now": int(time.time())},
            )
            if op["jpg"] is not None:
                await conn.execute(
                    "INSERT INTO portfolio_images(h,jpg) VALUES(:h,:jpg) ON CONFLICT(h) DO NOTHING",
                    {"h": op["h"], "jpg": op["jpg"]},
                )
            card_id = (
                op["card_id"]
                if op["kind"] in ("migrate", "rotate", "refresh")
                else operation_id
            )
            values = {
                "id": card_id,
                "p": pubkey,
                "h": op["h"],
                "title": op["title"],
                "showing": body.showing,
                "signature": body.signature,
                "encrypted": body.encrypted_credential.model_dump_json(),
                "now": int(time.time()),
            }
            await conn.execute(
                """INSERT INTO portfolio_cards(id,pubkey,h,title,showing,signature,status,created,encrypted_credential)
                VALUES(:id,:p,:h,:title,:showing,:signature,'owned',:now,:encrypted)
                ON CONFLICT(id) DO UPDATE SET showing=:showing,signature=:signature,status='owned',sent=NULL,credential=NULL,encrypted_credential=:encrypted""",
                values,
            )
            await conn.execute(
                "UPDATE portfolio_wallet_ops SET state='completed',card_id=:card,jpg=NULL,backup=NULL WHERE id=:id",
                {"card": card_id, "id": operation_id},
            )
            row = await conn.fetchone(
                "SELECT * FROM portfolio_cards WHERE id=:id", {"id": card_id}
            )
        if row is None:
            raise HTTPException(404, "Card not found.")
        return self.portfolio.public_card(dict(row))

    async def delete(
        self,
        pubkey: str,
        card_id: str,
        body: DeleteRequest,
        listed: Callable[[Connection, str], Awaitable[bool]],
    ) -> None:
        """Burn an NFT the profile holds and erase its picture.

        The mint retires the asset for good, so pending links and transfer
        files die with it. Every card that showed this picture (earlier owners'
        sent history included) goes too: the image is gone."""
        try:
            pres = Presentation.from_bytes(bytes.fromhex(body.presentation))
        except ValueError:
            raise HTTPException(400, "Invalid burn presentation.")
        locks = LOCKS + [
            LockOptions(table=name)
            for name in ("portfolio_images", "portfolio_links", "market_listings")
        ]
        async with self.db.get_connection(locks=locks) as conn:
            await self.portfolio.reconcile(conn, pubkey)
            card = await self.portfolio.owned_card(conn, pubkey, card_id)
            if await listed(conn, card_id):
                raise HTTPException(409, "Unlist this NFT before deleting it.")
            if await conn.fetchone(
                "SELECT id FROM portfolio_wallet_ops WHERE pubkey=:p AND h=:h AND state!='completed'",
                {"p": pubkey, "h": card["h"]},
            ):
                raise HTTPException(
                    409, "Finish this NFT's pending wallet action first."
                )
            current = NFTClient.decode_showing(card["showing"])[1]
            if pres.h != current.h or pres.nullifier != current.nullifier:
                raise HTTPException(403, "Burn this card's current credential.")
            await self.ledger.burn(pres, conn=conn)
            for statement in (
                "DELETE FROM portfolio_links WHERE h=:h",
                "DELETE FROM portfolio_covers WHERE h=:h",
                "DELETE FROM market_sales WHERE h=:h",
                "DELETE FROM portfolio_cards WHERE h=:h",
                "DELETE FROM portfolio_images WHERE h=:h",
            ):
                await conn.execute(statement, {"h": card["h"]})

    @staticmethod
    def _verify_claim(pubkey: str, signature: str, showing: str) -> bool:
        return PublicKeyXOnly(bytes.fromhex(pubkey)).verify(
            bytes.fromhex(signature),
            hashlib.sha256((CLAIM_DOMAIN + showing).encode()).digest(),
        )

    async def recover(self, pubkey: str) -> dict:
        async with self.db.get_connection() as conn:
            cards = await conn.fetchall(
                "SELECT id,h,encrypted_credential FROM portfolio_cards WHERE pubkey=:p AND status!='sent' AND encrypted_credential IS NOT NULL",
                {"p": pubkey},
            )
            ops = await conn.fetchall(
                "SELECT id,h,card_id,kind,state,created,backup FROM portfolio_wallet_ops WHERE pubkey=:p AND state!='completed' AND backup IS NOT NULL",
                {"p": pubkey},
            )
        return {
            "cards": [
                {
                    "id": row["id"],
                    "h": row["h"],
                    "encrypted_credential": json.loads(row["encrypted_credential"]),
                }
                for row in cards
            ],
            "operations": [
                {**dict(row), "backup": json.loads(row["backup"])} for row in ops
            ],
        }
