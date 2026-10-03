from cashu.nft.portfolio_og import collection_meta, link_meta, site_base, with_meta

INDEX = """<title>Cashu NFT</title>
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
    assert "<title>Tom &amp; &quot;Jerry&quot; &lt;script&gt; · Cashu NFT</title>" in html
    assert 'content="1 NFT on Cashu NFT: Dawn"' in html
    assert f'og:image" content="https://jpg.example/api/og/p/{pk}.jpg?v=' in html
    assert "Hidden" not in html


def test_link_meta_depends_on_status():
    link = {"id": "f" * 32, "sender_name": "Ana", "title": "Dawn", "status": "open"}
    assert link_meta("", link, "1")["og:title"] == "Ana sent you Dawn"
    assert "already been used" in link_meta("", {**link, "status": "claimed"}, "1")["description"]
