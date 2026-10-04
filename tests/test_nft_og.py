import io

from PIL import Image

from cashu.nft.portfolio_og import (
    collection_meta,
    link_meta,
    listing_meta,
    nft_meta,
    site_base,
    with_meta,
)
from cashu.nft.portfolio_og_image import (
    Card,
    ListingPreview,
    NFTPreview,
    listing_image,
    nft_image,
)

INDEX = """<title>Nonfungible.cash</title>
<meta name="description" content="x" />
<meta property="og:title" content="x" />
<meta property="og:url" content="https://jpg.example/" />
<meta property="og:image" content="https://jpg.example/assets/og-1.png" />
<meta name="twitter:image" content="https://jpg.example/assets/og-1.png" />"""


def test_collection_meta_is_escaped_and_absolute():
    pk = "02" + "ab" * 32
    profile = {
        "pubkey": pk,
        "name": 'Tom & "Jerry" <script>',
        "avatar": None,
        "cover": "ha",
        "cards": [
            {"id": "a", "h": "ha", "title": "Dawn", "status": "owned"},
            {"id": "b", "h": "hb", "title": "Hidden", "status": "sent"},
        ],
    }
    html = with_meta(INDEX, collection_meta(site_base(INDEX), profile))
    assert "<script>" not in html
    assert (
        "<title>Tom &amp; &quot;Jerry&quot; &lt;script&gt; · Nonfungible.cash</title>"
        in html
    )
    assert 'content="1 NFT on Nonfungible.cash: Dawn"' in html
    assert f'og:image" content="https://jpg.example/api/og/p/{pk}.jpg?v=' in html
    assert "Hidden" not in html


def test_link_meta_depends_on_status():
    link = {"id": "f" * 32, "sender_name": "Ana", "title": "Dawn", "status": "open"}
    assert link_meta("", link, "1")["og:title"] == "Ana sent you Dawn"
    assert (
        "already been used"
        in link_meta("", {**link, "status": "claimed"}, "1")["description"]
    )


def test_nft_meta_points_at_the_nft_in_its_collection():
    pk = "ab" * 32
    owned = {"id": "c1", "h": "h1", "title": "Dawn", "status": "owned"}
    profile = {"pubkey": pk, "name": "Ana", "cards": [owned]}
    meta = nft_meta("https://jpg.example", profile, owned, "v1")
    assert meta["og:title"] == "Dawn · Ana"
    assert meta["og:url"] == f"https://jpg.example/p/{pk}?nft=c1"
    assert meta["og:image"] == f"https://jpg.example/api/og/p/{pk}/c1.jpg?v=v1"
    assert meta["description"] == "One of 1 NFT in Ana on Nonfungible.cash."
    sent = {**owned, "status": "sent"}
    assert "Sent on" in nft_meta("", profile, sent, "v1")["description"]


def test_listing_meta_depends_on_state():
    listing = {
        "id": "e" * 32,
        "seller_name": "Ana",
        "title": "Dawn",
        "price": 1234,
        "state": "active",
        "bids": {"count": 2, "top": 1500},
    }
    meta = listing_meta("https://jpg.example", listing, "v1")
    assert meta["og:title"] == "Dawn · 1,234 sats"
    assert "2 bids so far" in meta["description"]
    assert meta["og:image"] == f"https://jpg.example/api/og/market/{'e' * 32}.jpg?v=v1"
    assert (
        listing_meta("", {**listing, "state": "sold"}, "v1")["og:title"]
        == "Dawn · sold"
    )
    gone = listing_meta("", {**listing, "state": "unlisted"}, "v1")
    assert "No longer for sale" in gone["description"]


def jpg(color=(200, 80, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(out, "JPEG")
    return out.getvalue()


def assert_preview(data: bytes):
    with Image.open(io.BytesIO(data)) as image:
        assert (image.format, image.size) == ("JPEG", (1200, 630))


def test_nft_and_listing_images_render_every_state():
    card = Card("Dawn", jpg())
    for sent in (False, True):
        assert_preview(nft_image(NFTPreview("ab" * 32, "Ana", None, card, 3, sent)))
    for state in ("active", "reserved", "sold", "unlisted", "stale"):
        preview = ListingPreview("ab" * 32, "Ana", None, card, 420, state, 2, 500)
        assert_preview(listing_image(preview))
    # A missing picture and no bids still render.
    empty = ListingPreview("ab" * 32, "", None, Card("", None), 1, "active", 0, None)
    assert_preview(listing_image(empty))
