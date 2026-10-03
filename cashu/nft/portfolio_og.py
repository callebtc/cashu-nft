"""Per-page social previews (OpenGraph / Twitter cards).

Crawlers don't run JavaScript, so collection, NFT, listing and transfer-link
pages get their meta tags filled in on the server. Only data that the public API
already returns for that page is used.
"""

import hashlib
import re
from html import escape
from typing import Dict, List, Optional

# Built into index.html (PUBLIC_URL at build time); absolute in production.
_OG_URL = re.compile(r'<meta property="og:url" content="([^"]*)"')


def site_base(html: str) -> str:
    match = _OG_URL.search(html)
    return match.group(1).rstrip("/") if match else ""


def with_meta(html: str, meta: Dict[str, str]) -> str:
    """Replace the content of existing meta tags (and <title>) in index.html."""
    for key, value in meta.items():
        safe = escape(value, quote=True)
        if key == "title":
            html = re.sub(
                r"<title>[^<]*</title>", lambda _: f"<title>{safe}</title>", html, 1
            )
            continue
        attr = (
            "name" if key == "description" or key.startswith("twitter:") else "property"
        )
        html = re.sub(
            rf'(<meta {attr}="{re.escape(key)}" content=")[^"]*(")',
            lambda m: m.group(1) + safe + m.group(2),
            html,
            1,
        )
    return html


def page_meta(
    base: str, path: str, title: str, description: str, image: str, alt: str
) -> Dict[str, str]:
    return {
        "title": title,
        "description": description,
        "og:title": title,
        "og:description": description,
        "og:url": base + path,
        "og:image": base + image,
        "og:image:alt": alt,
        "twitter:title": title,
        "twitter:description": description,
        "twitter:image": base + image,
    }


def collection_meta(base: str, profile: dict) -> Dict[str, str]:
    name = profile.get("name") or "A collection"
    cards = [c for c in profile["cards"] if c["status"] == "owned"]
    count = f"{len(cards)} NFT" + ("" if len(cards) == 1 else "s")
    titles = ", ".join(c["title"] for c in cards[:3] if c.get("title"))
    description = f"{count} on Cashu NFT" + (f": {titles}" if titles else ".")
    return page_meta(
        base,
        f"/p/{profile['pubkey']}",
        f"{name} · Cashu NFT",
        description,
        f"/api/og/p/{profile['pubkey']}.jpg?v={profile_version(profile)}",
        f"{name}, a collection of {count} on Cashu NFT.",
    )


def link_meta(base: str, link: dict, version: str) -> Dict[str, str]:
    sender = link.get("sender_name") or "Someone"
    title = link.get("title") or "an NFT"
    if link["status"] == "open":
        headline = f"{sender} sent you {title}"
        description = "Open the link to claim the NFT. Once claimed, it's yours."
    else:
        headline = f"{title} from {sender}"
        description = "This transfer link has already been used."
    return page_meta(
        base,
        f"/claim/{link['id']}",
        headline,
        description,
        f"/api/og/claim/{link['id']}.jpg?v={version}",
        f"{title}, sent by {sender} on Cashu NFT.",
    )


def sats(n: int) -> str:
    """Like the web app's sats(): '1 sat', '1,234 sats'."""
    return f"{n:,} sat" + ("" if n == 1 else "s")


def owned_count(profile: dict) -> int:
    return sum(c["status"] == "owned" for c in profile["cards"])


def nft_meta(base: str, profile: dict, card: dict, version: str) -> Dict[str, str]:
    name = profile.get("name") or "A collection"
    title = card.get("title") or "Untitled"
    if card["status"] == "owned":
        n = owned_count(profile)
        description = f"One of {n} NFT{'' if n == 1 else 's'} in {name} on Cashu NFT."
    else:
        description = f"Sent on from {name} on Cashu NFT."
    return page_meta(
        base,
        f"/p/{profile['pubkey']}?nft={card['id']}",
        f"{title} · {name}",
        description,
        f"/api/og/p/{profile['pubkey']}/{card['id']}.jpg?v={version}",
        f"{title}, an NFT in {name} on Cashu NFT.",
    )


def listing_open(listing: dict) -> bool:
    return listing["state"] in ("active", "reserved")


def listing_meta(base: str, listing: dict, version: str) -> Dict[str, str]:
    seller = listing.get("seller_name") or "Someone"
    title = listing.get("title") or "An NFT"
    price = sats(listing["price"])
    bids = listing["bids"]["count"]
    if listing_open(listing):
        headline = f"{title} · {price}"
        description = f"For sale by {seller} on Cashu NFT. Pay with Cashu ecash." + (
            f" {bids} bid{'' if bids == 1 else 's'} so far." if bids else ""
        )
    elif listing["state"] == "sold":
        headline = f"{title} · sold"
        description = f"Sold by {seller} for {price} on Cashu NFT."
    else:
        headline = title
        description = f"Listed by {seller} on Cashu NFT. No longer for sale."
    return page_meta(
        base,
        f"/market/{listing['id']}",
        headline,
        description,
        f"/api/og/market/{listing['id']}.jpg?v={version}",
        f"{title}, listed by {seller} on Cashu NFT.",
    )


def fan(profile: dict) -> List[dict]:
    """The NFTs on the collection preview: the cover first, then the newest."""
    cards = [c for c in profile["cards"] if c["status"] == "owned"]
    cover = [c for c in cards if c["h"] == profile.get("cover")][:1]
    return (cover + [c for c in cards if c not in cover])[:3]


def _digest(*shown: object) -> str:
    return hashlib.sha256(repr(shown).encode()).hexdigest()[:10]


def profile_version(profile: dict) -> str:
    """Changes when anything drawn on the collection preview changes."""
    return _digest(
        profile.get("name"),
        profile.get("avatar"),
        owned_count(profile),
        profile.get("followers"),
        profile.get("likes"),
        [c["id"] for c in fan(profile)],
    )


def link_version(link: dict, avatar: Optional[int]) -> str:
    """Changes when anything drawn on the transfer-link preview changes."""
    return _digest(link["status"], link.get("sender_name"), avatar)


def nft_version(profile: dict, card: dict) -> str:
    """Changes when anything drawn on an NFT's preview changes."""
    return _digest(
        profile.get("name"),
        profile.get("avatar"),
        owned_count(profile),
        card["id"],
        card.get("title"),
        card["status"],
    )


def listing_version(listing: dict, avatar: Optional[int]) -> str:
    """Changes when anything drawn on a listing's preview changes."""
    return _digest(
        listing["state"],
        listing["price"],
        listing["bids"]["count"],
        listing["bids"]["top"],
        listing.get("seller_name"),
        listing.get("title"),
        avatar,
    )
