"""Social layer for portfolios: likes, follows, covers, discovery and activity.

Everything here is public metadata about profiles and public cards. Owner
actions are authorized by the caller with the same signed challenges as the
wallet. Activity is derived from existing rows rather than a separate event
log: a card whose asset hash was held by an earlier card is a receipt,
otherwise it is a mint.
"""

import time
from typing import TYPE_CHECKING, Dict, List, Literal, Optional, Tuple

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ..core.db import Connection, LockOptions

if TYPE_CHECKING:
    from .portfolio import Portfolio

CollectionSort = Literal["popular", "new", "largest"]
NFTSort = Literal["new", "old", "title"]
EventKind = Literal["collection", "mint", "receive", "like", "follow", "sale", "bid"]

COLLECTION_ORDER: Dict[str, str] = {
    "popular": "likes DESC, followers DESC, nfts DESC, p.created DESC",
    "new": "p.created DESC",
    "largest": "nfts DESC, likes DESC, p.created DESC",
}
NFT_ORDER: Dict[str, str] = {
    "new": "c.created DESC, c.rowid DESC",
    "old": "c.created ASC, c.rowid ASC",
    "title": "lower(c.title) ASC, c.created DESC",
}


class ToggleRequest(BaseModel):
    on: bool


class SettingsRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=40)
    # A 64-hex asset hash of one of the owner's NFTs, or "" for the default cover.
    cover: Optional[str] = Field(default=None, pattern=r"^([0-9a-f]{64})?$")


def _in(prefix: str, values: List[str]) -> Tuple[str, Dict[str, str]]:
    """Named placeholders for an IN clause."""
    params = {f"{prefix}{i}": value for i, value in enumerate(values)}
    return ",".join(f":{key}" for key in params), params


class Social:
    def __init__(self, portfolio: "Portfolio"):
        self.db = portfolio.db

    async def migrate(self) -> None:
        async with self.db.get_connection() as conn:
            for statement in (
                """CREATE TABLE IF NOT EXISTS portfolio_likes (
                    liker TEXT NOT NULL, target TEXT NOT NULL, created INTEGER NOT NULL,
                    PRIMARY KEY (liker, target))""",
                """CREATE TABLE IF NOT EXISTS portfolio_follows (
                    follower TEXT NOT NULL, followee TEXT NOT NULL, created INTEGER NOT NULL,
                    PRIMARY KEY (follower, followee))""",
                """CREATE TABLE IF NOT EXISTS portfolio_covers (
                    pubkey TEXT PRIMARY KEY, h TEXT NOT NULL)""",
                "CREATE INDEX IF NOT EXISTS portfolio_likes_target ON portfolio_likes(target)",
                "CREATE INDEX IF NOT EXISTS portfolio_follows_followee ON portfolio_follows(followee)",
                "CREATE INDEX IF NOT EXISTS portfolio_cards_created ON portfolio_cards(created)",
                "CREATE INDEX IF NOT EXISTS portfolio_cards_h ON portfolio_cards(h)",
            ):
                await conn.execute(statement)

    @staticmethod
    async def _require_profile(conn: Connection, pubkey: str, message: str) -> dict:
        row = await conn.fetchone(
            "SELECT * FROM portfolio_profiles WHERE pubkey=:p", {"p": pubkey}
        )
        if row is None:
            raise HTTPException(404, message)
        return dict(row)

    async def _covers(self, conn: Connection, pubkeys: List[str]) -> Dict[str, dict]:
        """Cover hash, banner and up to four recent previews for each profile."""
        result: Dict[str, dict] = {
            pk: {"cover": None, "previews": []} for pk in pubkeys
        }
        if not pubkeys:
            return result
        clause, params = _in("p", pubkeys)
        rows = await conn.fetchall(
            f"""SELECT pubkey, h FROM portfolio_cards WHERE status!='sent' AND pubkey IN ({clause})
            ORDER BY created DESC, rowid DESC""",
            params,
        )
        held: Dict[str, List[str]] = {pk: [] for pk in pubkeys}
        for row in rows:
            held[row["pubkey"]].append(row["h"])
        chosen = await conn.fetchall(
            f"SELECT pubkey, h FROM portfolio_covers WHERE pubkey IN ({clause})", params
        )
        custom = {row["pubkey"]: row["h"] for row in chosen}
        banners = {
            row["pubkey"]: row
            for row in await conn.fetchall(
                f"SELECT pubkey, h, updated FROM portfolio_banners WHERE pubkey IN ({clause})",
                params,
            )
        }
        for pk in pubkeys:
            hs = held[pk]
            # A custom cover only applies while the collection still holds it,
            # and so does a banner cut from an NFT. An uploaded banner (no
            # NFT) is the collection's cover on Explore.
            cover = custom.get(pk) if custom.get(pk) in hs else None
            banner = banners.get(pk)
            if banner and banner["h"] is not None and banner["h"] not in hs:
                banner = None
            result[pk] = {
                "cover": cover or (hs[0] if hs else None),
                "custom_cover": cover is not None,
                "banner": banner["updated"] if banner else None,
                "banner_nft": banner["h"] if banner else None,
                "previews": hs[:4],
            }
        return result

    async def summary(self, pubkey: str) -> dict:
        async with self.db.get_connection() as conn:
            counts = await conn.fetchone(
                """SELECT
                (SELECT COUNT(*) FROM portfolio_likes WHERE target=:p) AS likes,
                (SELECT COUNT(*) FROM portfolio_follows WHERE followee=:p) AS followers,
                (SELECT COUNT(*) FROM portfolio_follows WHERE follower=:p) AS following""",
                {"p": pubkey},
            )
            covers = await self._covers(conn, [pubkey])
        return {**(dict(counts) if counts else {}), **covers[pubkey]}

    async def collections(
        self, sort: CollectionSort, q: str, limit: int, offset: int
    ) -> dict:
        async with self.db.get_connection() as conn:
            rows = await conn.fetchall(
                f"""SELECT p.pubkey, p.name, p.created,
                (SELECT COUNT(*) FROM portfolio_likes l WHERE l.target=p.pubkey) AS likes,
                (SELECT COUNT(*) FROM portfolio_follows f WHERE f.followee=p.pubkey) AS followers,
                (SELECT COUNT(*) FROM portfolio_cards c WHERE c.pubkey=p.pubkey AND c.status!='sent') AS nfts
                FROM portfolio_profiles p
                WHERE (:q='' OR lower(p.name) LIKE :like)
                ORDER BY {COLLECTION_ORDER[sort]} LIMIT :limit OFFSET :offset""",
                {
                    "q": q,
                    "like": f"%{q.lower()}%",
                    "limit": limit + 1,
                    "offset": offset,
                },
            )
            items = [dict(r) for r in rows[:limit]]
            covers = await self._covers(conn, [i["pubkey"] for i in items])
        return {
            "items": [{**i, **covers[i["pubkey"]]} for i in items],
            "more": len(rows) > limit,
        }

    async def nfts(self, sort: NFTSort, q: str, limit: int, offset: int) -> dict:
        async with self.db.get_connection() as conn:
            rows = await conn.fetchall(
                f"""SELECT c.id, c.pubkey, c.h, c.title, c.created, c.status, p.name AS owner_name
                FROM portfolio_cards c JOIN portfolio_profiles p ON p.pubkey=c.pubkey
                WHERE c.status!='sent' AND (:q='' OR lower(c.title) LIKE :like)
                ORDER BY {NFT_ORDER[sort]} LIMIT :limit OFFSET :offset""",
                {
                    "q": q,
                    "like": f"%{q.lower()}%",
                    "limit": limit + 1,
                    "offset": offset,
                },
            )
        # Pending transfers stay private: public listings show the card as owned.
        items = [
            {**dict(r), "status": "owned" if r["status"] == "ready" else r["status"]}
            for r in rows[:limit]
        ]
        return {"items": items, "more": len(rows) > limit}

    async def activity(
        self,
        limit: int,
        before: Optional[int],
        actors: Optional[List[str]] = None,
        kinds: Optional[List[EventKind]] = None,
    ) -> List[dict]:
        """Recent public events, newest first, optionally restricted to actors."""
        if actors is not None and not actors:
            return []
        wanted = set(
            kinds or ["collection", "mint", "receive", "like", "follow", "sale", "bid"]
        )
        cutoff = before if before is not None else int(time.time()) + 1
        params: Dict[str, object] = {"before": cutoff, "limit": limit}
        actor_filter = {
            "cards": "",
            "profiles": "",
            "likes": "",
            "follows": "",
            "sales": "",
            "bids": "",
        }
        if actors is not None:
            clause, actor_params = _in("a", actors)
            params.update(actor_params)
            actor_filter = {
                "cards": f" AND c.pubkey IN ({clause})",
                "profiles": f" AND p.pubkey IN ({clause})",
                "likes": f" AND l.liker IN ({clause})",
                "follows": f" AND f.follower IN ({clause})",
                "sales": f" AND s.buyer IN ({clause})",
                "bids": f" AND o.buyer IN ({clause})",
            }
        events: List[dict] = []
        async with self.db.get_connection() as conn:
            if wanted & {"mint", "receive"}:
                rows = await conn.fetchall(
                    f"""SELECT c.id, c.pubkey, c.h, c.title, c.created,
                    (SELECT prev.pubkey FROM portfolio_cards prev WHERE prev.h=c.h
                     AND (prev.created<c.created OR (prev.created=c.created AND prev.rowid<c.rowid))
                     ORDER BY prev.created DESC, prev.rowid DESC LIMIT 1) AS previous
                    FROM portfolio_cards c WHERE c.created<:before{actor_filter["cards"]}
                    AND c.id NOT IN (SELECT offer_id FROM market_sales)
                    ORDER BY c.created DESC LIMIT :limit""",
                    params,
                )
                for r in rows:
                    kind = "receive" if r["previous"] else "mint"
                    if kind in wanted:
                        events.append(
                            {
                                "id": f"{kind}:{r['id']}",
                                "kind": kind,
                                "actor": r["pubkey"],
                                "target": r["previous"],
                                "card_id": r["id"],
                                "h": r["h"],
                                "title": r["title"],
                                "created": r["created"],
                            }
                        )
            if "collection" in wanted:
                rows = await conn.fetchall(
                    f"""SELECT p.pubkey, p.created FROM portfolio_profiles p
                    WHERE p.created<:before{actor_filter["profiles"]} ORDER BY p.created DESC LIMIT :limit""",
                    params,
                )
                events += [
                    {
                        "id": f"collection:{r['pubkey']}",
                        "kind": "collection",
                        "actor": r["pubkey"],
                        "created": r["created"],
                    }
                    for r in rows
                ]
            if "like" in wanted:
                rows = await conn.fetchall(
                    f"""SELECT l.liker, l.target, l.created FROM portfolio_likes l
                    WHERE l.created<:before{actor_filter["likes"]} ORDER BY l.created DESC LIMIT :limit""",
                    params,
                )
                events += [
                    {
                        "id": f"like:{r['liker']}:{r['target']}",
                        "kind": "like",
                        "actor": r["liker"],
                        "target": r["target"],
                        "created": r["created"],
                    }
                    for r in rows
                ]
            if "follow" in wanted:
                rows = await conn.fetchall(
                    f"""SELECT f.follower, f.followee, f.created FROM portfolio_follows f
                    WHERE f.created<:before{actor_filter["follows"]} ORDER BY f.created DESC LIMIT :limit""",
                    params,
                )
                events += [
                    {
                        "id": f"follow:{r['follower']}:{r['followee']}",
                        "kind": "follow",
                        "actor": r["follower"],
                        "target": r["followee"],
                        "created": r["created"],
                    }
                    for r in rows
                ]
            if "sale" in wanted:
                # Public sale projection: NFT, buyer, seller, sale price and time.
                # The payment mint and settlement details stay participant-only.
                rows = await conn.fetchall(
                    f"""SELECT s.offer_id, s.card_id, s.h, s.title, s.seller, s.buyer, s.created, o.price
                    FROM market_sales s LEFT JOIN market_offers o ON o.id=s.offer_id
                    WHERE s.created<:before{actor_filter["sales"]}
                    ORDER BY s.created DESC LIMIT :limit""",
                    params,
                )
                events += [
                    {
                        "id": f"sale:{r['offer_id']}",
                        "kind": "sale",
                        "actor": r["buyer"],
                        "target": r["seller"],
                        # The buyer's card (published under the offer id) is the live one.
                        "card_id": r["offer_id"],
                        "h": r["h"],
                        "title": r["title"],
                        "price": r["price"],
                        "created": r["created"],
                    }
                    for r in rows
                ]
            if "bid" in wanted:
                # Bids are public like on any NFT marketplace: bidder, NFT,
                # amount and time. NFTs deleted since then drop out.
                rows = await conn.fetchall(
                    f"""SELECT o.id, o.listing_id, o.buyer, o.seller, o.price, o.test_value,
                    o.created, l.card_id, c.h, c.title FROM market_offers o
                    JOIN market_listings l ON l.id=o.listing_id
                    JOIN portfolio_cards c ON c.id=l.card_id
                    WHERE o.created<:before{actor_filter["bids"]}
                    ORDER BY o.created DESC LIMIT :limit""",
                    params,
                )
                events += [
                    {
                        "id": f"bid:{r['id']}",
                        "kind": "bid",
                        "actor": r["buyer"],
                        "target": r["seller"],
                        "listing_id": r["listing_id"],
                        "card_id": r["card_id"],
                        "h": r["h"],
                        "title": r["title"],
                        "price": r["price"],
                        "test_value": bool(r["test_value"]),
                        "created": r["created"],
                    }
                    for r in rows
                ]
            events.sort(key=lambda e: e["created"], reverse=True)
            events = events[:limit]
            names = {e["actor"] for e in events} | {
                e["target"] for e in events if e.get("target")
            }
            if names:
                clause, name_params = _in("n", sorted(names))
                rows = await conn.fetchall(
                    f"SELECT pubkey, name FROM portfolio_profiles WHERE pubkey IN ({clause})",
                    name_params,
                )
                lookup = {r["pubkey"]: r["name"] for r in rows}
                for e in events:
                    e["actor_name"] = lookup.get(e["actor"])
                    if e.get("target"):
                        e["target_name"] = lookup.get(e["target"])
        return events

    async def following(self, pubkey: str) -> List[str]:
        async with self.db.get_connection() as conn:
            rows = await conn.fetchall(
                "SELECT followee FROM portfolio_follows WHERE follower=:p",
                {"p": pubkey},
            )
        return [r["followee"] for r in rows]

    async def relations(self, pubkey: str) -> dict:
        async with self.db.get_connection() as conn:
            likes = await conn.fetchall(
                "SELECT target FROM portfolio_likes WHERE liker=:p", {"p": pubkey}
            )
            follows = await conn.fetchall(
                "SELECT followee FROM portfolio_follows WHERE follower=:p",
                {"p": pubkey},
            )
        return {
            "likes": [r["target"] for r in likes],
            "following": [r["followee"] for r in follows],
        }

    async def network(self, pubkey: str) -> dict:
        async with self.db.get_connection() as conn:
            followers = await conn.fetchall(
                """SELECT p.pubkey, p.name FROM portfolio_follows f JOIN portfolio_profiles p ON p.pubkey=f.follower
                WHERE f.followee=:p ORDER BY f.created DESC LIMIT 200""",
                {"p": pubkey},
            )
            following = await conn.fetchall(
                """SELECT p.pubkey, p.name FROM portfolio_follows f JOIN portfolio_profiles p ON p.pubkey=f.followee
                WHERE f.follower=:p ORDER BY f.created DESC LIMIT 200""",
                {"p": pubkey},
            )
        return {
            "followers": [dict(r) for r in followers],
            "following": [dict(r) for r in following],
        }

    async def _toggle(
        self,
        table: str,
        actor_col: str,
        target_col: str,
        actor: str,
        target: str,
        on: bool,
    ) -> None:
        if actor == target:
            raise HTTPException(400, "You can't do that to your own collection.")
        async with self.db.get_connection(locks=[LockOptions(table=table)]) as conn:
            await self._require_profile(conn, actor, "Create your collection first.")
            await self._require_profile(conn, target, "That collection doesn't exist.")
            if on:
                await conn.execute(
                    f"INSERT INTO {table}({actor_col},{target_col},created) VALUES(:a,:t,:now) ON CONFLICT DO NOTHING",
                    {"a": actor, "t": target, "now": int(time.time())},
                )
            else:
                await conn.execute(
                    f"DELETE FROM {table} WHERE {actor_col}=:a AND {target_col}=:t",
                    {"a": actor, "t": target},
                )

    async def set_like(self, liker: str, target: str, on: bool) -> dict:
        await self._toggle("portfolio_likes", "liker", "target", liker, target, on)
        return await self.summary(target)

    async def set_follow(self, follower: str, followee: str, on: bool) -> dict:
        await self._toggle(
            "portfolio_follows", "follower", "followee", follower, followee, on
        )
        return await self.summary(followee)

    async def update(self, pubkey: str, body: SettingsRequest) -> None:
        async with self.db.get_connection(
            locks=[
                LockOptions(table="portfolio_profiles"),
                LockOptions(table="portfolio_covers"),
            ]
        ) as conn:
            await self._require_profile(conn, pubkey, "This collection doesn't exist.")
            if body.name is not None:
                name = body.name.strip()
                if not name:
                    raise HTTPException(
                        400, "Use a collection name between 1 and 40 characters."
                    )
                await conn.execute(
                    "UPDATE portfolio_profiles SET name=:n WHERE pubkey=:p",
                    {"n": name, "p": pubkey},
                )
            if body.cover == "":
                await conn.execute(
                    "DELETE FROM portfolio_covers WHERE pubkey=:p", {"p": pubkey}
                )
            elif body.cover is not None:
                held = await conn.fetchone(
                    "SELECT id FROM portfolio_cards WHERE pubkey=:p AND h=:h AND status!='sent'",
                    {"p": pubkey, "h": body.cover},
                )
                if held is None:
                    raise HTTPException(
                        400, "Pick a cover from the NFTs in your collection."
                    )
                await conn.execute(
                    """INSERT INTO portfolio_covers(pubkey,h) VALUES(:p,:h)
                    ON CONFLICT(pubkey) DO UPDATE SET h=:h""",
                    {"p": pubkey, "h": body.cover},
                )
