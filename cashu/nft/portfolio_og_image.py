"""Social preview images (1200×630 JPG) for collection, NFT, listing and
transfer-link pages.

Drawn with Pillow in the app's look on its dark theme: a framed board, ink
borders, hard offset shadows, tilted cards and stickers, Bricolage headlines
and Inter text. Everything is drawn at twice the size and scaled down, which
smooths the edges of rotated shapes.
"""

import io
import warnings
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

from .portfolio_og import sats

FONTS = Path(__file__).parent / "fonts"
SUBSETS = ("latin", "latin-ext")
DISPLAY = "bricolage-grotesque"
SANS = "inter"

W, H = 1200, 630
S = 2  # supersampling factor
FX, FY, FW, FH = 26, 26, 1140, 570  # the frame, as in og/og.html

Color = Tuple[int, int, int]
BG: Color = (18, 18, 18)
PAPER: Color = (27, 27, 27)
SURFACE: Color = (38, 38, 38)
INK: Color = (244, 240, 230)
DARK: Color = (17, 17, 17)
LIME: Color = (200, 245, 60)
ORANGE: Color = (255, 106, 31)
PINK: Color = (255, 90, 168)
YELLOW: Color = (255, 210, 61)
STONE: Color = (168, 163, 151)
# Light paper behind avatars: identicon colours (one is near-black) vanish on dark.
CREAM: Color = (255, 253, 247)
# Same palette and pick as the web app's Identicon.
IDENTICON: Sequence[Color] = (
    (31, 111, 235),
    (15, 138, 95),
    (217, 72, 15),
    (201, 42, 42),
    (11, 114, 133),
    (176, 136, 0),
    (23, 23, 23),
)


def _s(value: float) -> int:
    return round(value * S)


def _mix(a: Color, b: Color, amount: float) -> Color:
    """``amount`` of ``a`` over ``b``."""
    return (
        round(a[0] * amount + b[0] * (1 - amount)),
        round(a[1] * amount + b[1] * (1 - amount)),
        round(a[2] * amount + b[2] * (1 - amount)),
    )


# --- Text ------------------------------------------------------------------


@lru_cache(maxsize=None)
def _font(family: str, subset: str, px: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONTS / f"{family}-{subset}-wght-normal.woff2"), px)
    font.set_variation_by_axes([weight])
    return font


def _glyph(family: str, subset: str, ch: str) -> bytes:
    image = Image.new("L", (48, 48))
    ImageDraw.Draw(image).text(
        (8, 8), ch, font=_font(family, subset, 28, 400), fill=255
    )
    return image.tobytes()


@lru_cache(maxsize=8192)
def _has_glyph(family: str, subset: str, ch: str) -> bool:
    """The font subsets carry no usable character map here, so compare the
    drawn glyph with the font's "missing glyph" box."""
    return _glyph(family, subset, ch) != _glyph(family, subset, "\U0010fffd")


@dataclass(frozen=True)
class Style:
    family: str
    size: float
    weight: int


def _runs(text: str, style: Style) -> List[Tuple[ImageFont.FreeTypeFont, str]]:
    """Split text by the font subset that draws it; drop what none can."""
    runs: List[Tuple[ImageFont.FreeTypeFont, str]] = []
    for ch in text:
        subset = next(
            (sub for sub in SUBSETS if ch == " " or _has_glyph(style.family, sub, ch)),
            None,
        )
        if subset is None:
            continue
        font = _font(style.family, subset, _s(style.size), style.weight)
        if runs and runs[-1][0] is font:
            runs[-1] = (font, runs[-1][1] + ch)
        else:
            runs.append((font, ch))
    return runs


def _clean(text: str, style: Style) -> str:
    drawn = "".join(seg for _, seg in _runs(" ".join(text.split()), style))
    return " ".join(drawn.split())


def _width(text: str, style: Style) -> float:
    return sum(font.getlength(seg) for font, seg in _runs(text, style)) / S


def _text(
    draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str, style: Style, fill
) -> None:
    pos = _s(x)
    for font, seg in _runs(text, style):
        draw.text((pos, _s(baseline)), seg, font=font, fill=fill, anchor="ls")
        pos += round(font.getlength(seg))


def _ellipsize(text: str, style: Style, width: float) -> str:
    if _width(text, style) <= width:
        return text
    while text and _width(text + "…", style) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(text: str, style: Style, width: float) -> List[str]:
    lines: List[str] = []
    line = ""
    for word in text.split(" "):
        candidate = f"{line} {word}".strip()
        if _width(candidate, style) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        while _width(word, style) > width:  # a word wider than the line
            cut = len(word)
            while cut > 1 and _width(word[:cut], style) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


def _fit(
    text: str, family: str, weight: int, sizes: Sequence[int], width: float, lines: int
) -> Tuple[Style, List[str]]:
    """The largest size at which ``text`` fits in ``lines`` lines."""
    for size in sizes:
        style = Style(family, size, weight)
        wrapped = _wrap(text, style, width)
        if len(wrapped) <= lines:
            return style, wrapped
    style = Style(family, sizes[-1], weight)
    wrapped = _wrap(text, style, width)
    rest = " ".join(wrapped[lines - 1 :])
    return style, wrapped[: lines - 1] + [_ellipsize(rest, style, width)]


# --- Shapes ----------------------------------------------------------------


def _layer(w: float, h: float) -> Image.Image:
    return Image.new("RGBA", (_s(w), _s(h)), (0, 0, 0, 0))


def _box(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    w: float,
    h: float,
    radius: float,
    fill: Color,
    border: Color,
    border_width: float,
    shadow: float = 0,
    shadow_color: Color = INK,
) -> None:
    """A bordered rounded box with a hard offset shadow, like .nft-card."""
    if shadow:
        draw.rounded_rectangle(
            (_s(x + shadow), _s(y + shadow), _s(x + w + shadow), _s(y + h + shadow)),
            _s(radius),
            fill=shadow_color,
        )
    draw.rounded_rectangle(
        (_s(x), _s(y), _s(x + w), _s(y + h)),
        _s(radius),
        fill=fill,
        outline=border,
        width=_s(border_width),
    )


def _paste_rounded(
    target: Image.Image, image: Image.Image, x: float, y: float, radius: float
) -> None:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, image.width - 1, image.height - 1), _s(radius), fill=255
    )
    target.paste(image, (_s(x), _s(y)), mask)


def _place(canvas: Image.Image, layer: Image.Image, cx: float, cy: float, deg: float):
    """Composite ``layer`` centred at (cx, cy), rotated clockwise like CSS."""
    if deg:
        layer = (
            layer.convert("RGBa")
            .rotate(-deg, resample=Image.Resampling.BICUBIC, expand=True)
            .convert("RGBA")
        )
    canvas.alpha_composite(
        layer, (_s(cx) - layer.width // 2, _s(cy) - layer.height // 2)
    )


# --- Pictures --------------------------------------------------------------


def _open(jpg: Optional[bytes], size: int) -> Optional[Image.Image]:
    """Decode a stored JPG to a centre-cropped square, or None if unusable."""
    if jpg is None:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(jpg)) as image:
                image.draft("RGB", (size, size))
                oriented = ImageOps.exif_transpose(image).convert("RGB")
                return ImageOps.fit(oriented, (size, size), Image.Resampling.LANCZOS)
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        return None


def _tint(image: Optional[Image.Image]) -> Color:
    """The card frame colour: the picture's average mixed into paper."""
    if image is None:
        return SURFACE
    r, g, b = image.resize((1, 1), Image.Resampling.BOX).tobytes()
    return _mix((r, g, b), PAPER, 0.28)


def _identicon(pubkey: str, size: int) -> Image.Image:
    data = bytes.fromhex(pubkey)
    color = IDENTICON[data[0] % len(IDENTICON)]
    image = Image.new("RGB", (size, size), CREAM)
    draw = ImageDraw.Draw(image)
    unit = size / 7
    for y in range(5):
        for x in range(3):
            if data[1 + y * 3 + x] % 2:
                for cx in {x, 4 - x}:
                    x0, y0 = (cx + 1) * unit, (y + 1) * unit
                    draw.rectangle(
                        (round(x0), round(y0), round(x0 + unit), round(y0 + unit)),
                        fill=color,
                    )
    return image


def _avatar(
    canvas: Image.Image,
    x: float,
    y: float,
    size: float,
    radius: float,
    pubkey: str,
    jpg: Optional[bytes],
    shadow: float,
) -> None:
    draw = ImageDraw.Draw(canvas)
    _box(draw, x, y, size, size, radius, CREAM, INK, 3, shadow)
    inner = size - 6
    picture = _open(jpg, _s(inner)) or _identicon(pubkey, _s(inner))
    _paste_rounded(canvas, picture, x + 3, y + 3, radius - 3)


# --- Pieces ----------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    title: str
    jpg: Optional[bytes]


def _card(card: Card, width: float, badge: Optional[str], dim: bool) -> Image.Image:
    """An NFT card as drawn in the app: tinted frame, square picture, title."""
    k = width / 230
    pad, border, shadow = 10 * k, 3, 6 * k
    media = width - 2 * pad
    title = Style(DISPLAY, 19 * k, 800)
    height = pad + media + 12 * k + title.size * 1.15 + pad
    if badge is not None:
        height += 36 * k
    layer = _layer(width + shadow + 2, height + shadow + 2)
    draw = ImageDraw.Draw(layer)

    picture = _open(card.jpg, _s(media))
    if picture is not None and dim:
        gray = ImageOps.grayscale(picture).convert("RGB")
        picture = Image.blend(gray, Image.new("RGB", picture.size, PAPER), 0.35)
    _box(draw, 0, 0, width, height, 20 * k, _tint(picture), INK, border, shadow)
    if picture is None:
        picture = Image.new("RGB", (_s(media), _s(media)), SURFACE)
    _paste_rounded(layer, picture, pad, pad, 12 * k)
    draw.rounded_rectangle(
        (_s(pad), _s(pad), _s(pad + media), _s(pad + media)),
        _s(12 * k),
        outline=INK,
        width=_s(2.5),
    )
    name = _ellipsize(_clean(card.title, title) or "Untitled", title, media)
    baseline = pad + media + 12 * k + title.size * 0.92
    _text(draw, pad + 2, baseline, name, title, INK)
    if badge is not None:
        _pill(draw, pad, baseline + 12 * k, badge, LIME, 13 * k, 26 * k, dot=True)
    return layer


def _pill(
    draw: ImageDraw.ImageDraw,
    x: float,
    y: float,
    text: str,
    fill: Color,
    size: float,
    height: float,
    dot: bool = False,
) -> float:
    """A flat pill label (.pill / .badge). Returns its width."""
    style = Style(SANS, size, 750)
    pad = height * 0.42
    dot_w = height * 0.27 + height * 0.23 if dot else 0
    width = pad * 2 + dot_w + _width(text, style)
    draw.rounded_rectangle(
        (_s(x), _s(y), _s(x + width), _s(y + height)),
        _s(height / 2),
        fill=fill,
        outline=DARK,
        width=_s(2.5 if height > 30 else 2),
    )
    if dot:
        r = height * 0.135
        cx, cy = x + pad + r, y + height / 2
        draw.ellipse((_s(cx - r), _s(cy - r), _s(cx + r), _s(cy + r)), fill=DARK)
    _text(draw, x + pad + dot_w, y + height / 2 + size * 0.36, text, style, DARK)
    return width


def _sticker(text: str, fill: Color, size: float = 24) -> Image.Image:
    style = Style(DISPLAY, size, 800)
    height = size * 1.9
    width = _width(text, style) + size * 1.66
    layer = _layer(width + 6, height + 6)
    draw = ImageDraw.Draw(layer)
    _box(draw, 0, 0, width, height, height / 2, fill, DARK, 2.5, 4)
    _text(draw, size * 0.83, height / 2 + size * 0.34, text, style, DARK)
    return layer


def _frame() -> Image.Image:
    canvas = Image.new("RGBA", (_s(W), _s(H)), BG + (255,))
    _box(ImageDraw.Draw(canvas), FX, FY, FW, FH, 36, PAPER, INK, 3, 9)
    return canvas


def _brand(canvas: Image.Image, host: str) -> None:
    draw = ImageDraw.Draw(canvas)
    x, y = FX + 60, FY + FH - 44 - 38
    _box(draw, x, y, 38, 38, 10, LIME, INK, 2.5)
    draw.ellipse(
        (_s(x + 7), _s(y + 7), _s(x + 31), _s(y + 31)),
        fill=ORANGE,
        outline=DARK,
        width=_s(2.5),
    )
    name = Style(DISPLAY, 28, 800)
    _text(draw, x + 52, y + 29, "Cashu NFT", name, INK)
    if host:
        url = Style(SANS, 20, 600)
        _text(
            draw,
            x + 52 + _width("Cashu NFT", name) + 14,
            y + 28,
            host,
            url,
            _mix(INK, PAPER, 0.55),
        )


def _headline(
    canvas: Image.Image, text: str, top: float, width: float, sizes: Sequence[int]
) -> float:
    """Draw a big Bricolage headline; returns the y below its last line."""
    style, lines = _fit(text, DISPLAY, 800, sizes, width, 2)
    draw = ImageDraw.Draw(canvas)
    line_height = style.size * 0.92
    for i, line in enumerate(lines):
        _text(draw, FX + 60, top + style.size * 0.8 + i * line_height, line, style, INK)
    return top + style.size * 0.8 + (len(lines) - 1) * line_height + style.size * 0.2


def _lead(canvas: Image.Image, text: str, top: float) -> None:
    style = Style(SANS, 25, 500)
    _text(
        ImageDraw.Draw(canvas),
        FX + 60,
        top + 25,
        _ellipsize(text, style, 560),
        style,
        _mix(INK, PAPER, 0.88),
    )


def _jpg(canvas: Image.Image) -> bytes:
    out = io.BytesIO()
    canvas.convert("RGB").resize((W, H), Image.Resampling.LANCZOS).save(
        out, "JPEG", quality=88, optimize=True, progressive=True
    )
    return out.getvalue()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


# --- Previews --------------------------------------------------------------


@dataclass(frozen=True)
class CollectionPreview:
    pubkey: str
    name: str
    avatar: Optional[bytes]
    nfts: int
    followers: int
    likes: int
    cards: Sequence[Card]  # up to three, the cover first


def collection_image(preview: CollectionPreview, host: str) -> bytes:
    canvas = _frame()
    draw = ImageDraw.Draw(canvas)

    _avatar(canvas, FX + 60, FY + 56, 96, 24, preview.pubkey, preview.avatar, 5)
    _pill(draw, FX + 60 + 96 + 24, FY + 56 + 28, "Collection", LIME, 19, 40)
    name = _clean(preview.name, Style(DISPLAY, 96, 800)) or "Collector"
    bottom = _headline(canvas, name, FY + 186, 560, range(96, 47, -4))
    social = [
        _plural(n, word)
        for n, word in ((preview.followers, "follower"), (preview.likes, "like"))
        if n
    ]
    _lead(canvas, " · ".join(social) or "A collection on Cashu NFT", bottom + 20)
    _brand(canvas, host)

    stage = FX + FW - 18 - 470
    cards = list(preview.cards)[:3]
    if not cards:
        empty = _layer(250, 250)
        _box(ImageDraw.Draw(empty), 0, 0, 240, 240, 20, SURFACE, STONE, 3)
        style = Style(DISPLAY, 26, 800)
        _text(
            ImageDraw.Draw(empty),
            120 - _width("No NFTs yet", style) / 2,
            129,
            "No NFTs yet",
            style,
            STONE,
        )
        _place(canvas, empty, stage + 235, FY + 290, -4)
    elif len(cards) == 1:
        _place(canvas, _card(cards[0], 320, None, False), stage + 240, FY + 300, -4)
    else:
        spots = [(stage + 250, FY + 196 + 142, 0, 244)]  # front: the cover
        spots.append((stage + 115, FY + 118 + 135, -8, 230))
        if len(cards) > 2:
            spots.append((stage + 355, FY + 46 + 135, 6, 230))
        for card, (cx, cy, deg, width) in reversed(list(zip(cards, spots))):
            _place(canvas, _card(card, width, None, False), cx, cy, deg)
    count = _sticker(_plural(preview.nfts, "NFT"), YELLOW)
    _place(canvas, count, stage + 96 + count.width / S / 2, FY + 92, -9)
    return _jpg(canvas)


@dataclass(frozen=True)
class LinkPreview:
    sender: str
    sender_name: str
    avatar: Optional[bytes]
    card: Card
    status: str  # "open", "claimed" or "void"


def link_image(preview: LinkPreview, host: str) -> bytes:
    canvas = _frame()
    draw = ImageDraw.Draw(canvas)
    used = preview.status != "open"

    _pill(
        draw,
        FX + 60,
        FY + 56,
        "Link used" if used else "Transfer link",
        STONE if used else LIME,
        19,
        40,
    )
    _avatar(canvas, FX + 60, FY + 130, 48, 14, preview.sender, preview.avatar, 3)
    who = Style(SANS, 28, 650)
    sender = _clean(preview.sender_name, who) or "Someone"
    line = _ellipsize(f"From {sender}" if used else f"{sender} sent you", who, 490)
    _text(draw, FX + 60 + 48 + 18, FY + 130 + 34, line, who, INK)
    title = _clean(preview.card.title, Style(DISPLAY, 96, 800)) or "an NFT"
    bottom = _headline(canvas, title, FY + 212, 560, range(96, 47, -4))
    _lead(
        canvas,
        "This link has already been used." if used else "Open the link to claim it.",
        bottom + 20,
    )
    _brand(canvas, host)

    stage = FX + FW - 18 - 470
    card = _card(preview.card, 350, "1 of 1", used)
    _place(canvas, card, stage + 245, FY + 290, -4)
    if used:
        stamp = _sticker(
            "Claimed" if preview.status == "claimed" else "Link used", ORANGE, 44
        )
        _place(canvas, stamp, stage + 245, FY + 250, -12)
    else:
        sticker = _sticker("For you", PINK)
        _place(canvas, sticker, stage + 70, FY + 70, -8)
    return _jpg(canvas)


def _byline(
    canvas: Image.Image, pubkey: str, avatar: Optional[bytes], text: str
) -> None:
    """A small avatar and a line of text under the pill."""
    _avatar(canvas, FX + 60, FY + 130, 48, 14, pubkey, avatar, 3)
    style = Style(SANS, 28, 650)
    _text(
        ImageDraw.Draw(canvas),
        FX + 60 + 48 + 18,
        FY + 130 + 34,
        _ellipsize(text, style, 490),
        style,
        INK,
    )


@dataclass(frozen=True)
class NFTPreview:
    pubkey: str
    name: str  # the collection's
    avatar: Optional[bytes]
    card: Card
    nfts: int  # NFTs the collection holds
    sent: bool  # the NFT has left this collection


def nft_image(preview: NFTPreview, host: str) -> bytes:
    canvas = _frame()
    draw = ImageDraw.Draw(canvas)
    _pill(
        draw,
        FX + 60,
        FY + 56,
        "Sent on" if preview.sent else "NFT",
        STONE if preview.sent else LIME,
        19,
        40,
    )
    name = _clean(preview.name, Style(SANS, 28, 650)) or "A collection"
    _byline(canvas, preview.pubkey, preview.avatar, name)
    title = _clean(preview.card.title, Style(DISPLAY, 96, 800)) or "Untitled"
    bottom = _headline(canvas, title, FY + 212, 560, range(96, 47, -4))
    _lead(
        canvas,
        "It has moved on to a new collection."
        if preview.sent
        else f"One of {_plural(preview.nfts, 'NFT')} in this collection.",
        bottom + 20,
    )
    _brand(canvas, host)

    stage = FX + FW - 18 - 470
    _place(canvas, _card(preview.card, 350, "1 of 1", False), stage + 245, FY + 290, -4)
    return _jpg(canvas)


@dataclass(frozen=True)
class ListingPreview:
    seller: str
    seller_name: str
    avatar: Optional[bytes]
    card: Card
    price: int
    state: str  # "active", "reserved", "sold", "unlisted" or "stale"
    bids: int
    top_bid: Optional[int]


def listing_image(preview: ListingPreview, host: str) -> bytes:
    canvas = _frame()
    draw = ImageDraw.Draw(canvas)
    listed = preview.state in ("active", "reserved")
    sold = preview.state == "sold"
    if preview.state == "active":
        label, fill = "For sale", LIME
    elif preview.state == "reserved":
        label, fill = "Sale pending", YELLOW
    else:
        label, fill = ("Sold" if sold else "Not for sale"), STONE
    _pill(draw, FX + 60, FY + 56, label, fill, 19, 40)
    seller = _clean(preview.seller_name, Style(SANS, 28, 650)) or "Someone"
    _byline(
        canvas,
        preview.seller,
        preview.avatar,
        f"{seller} is selling"
        if listed
        else f"Sold by {seller}"
        if sold
        else f"Listed by {seller}",
    )
    title = _clean(preview.card.title, Style(DISPLAY, 96, 800)) or "an NFT"
    bottom = _headline(canvas, title, FY + 212, 560, range(96, 47, -4))
    if sold:
        lead = f"Sold for {sats(preview.price)}."
    elif not listed:
        lead = "This NFT is no longer for sale."
    elif preview.state == "reserved":
        lead = "An offer was accepted."
    elif preview.bids:
        lead = f"{_plural(preview.bids, 'bid')} so far, top bid {sats(preview.top_bid or 0)}."
    else:
        lead = "Pay with Cashu ecash from any mint."
    _lead(canvas, lead, bottom + 20)
    _brand(canvas, host)

    stage = FX + FW - 18 - 470
    _place(
        canvas,
        _card(preview.card, 350, "1 of 1", not listed),
        stage + 245,
        FY + 290,
        -4,
    )
    if listed:
        price = _sticker(sats(preview.price), YELLOW, 34)
        _place(canvas, price, stage + 40 + price.width / S / 2, FY + 78, -8)
    else:
        stamp = _sticker("Sold" if sold else "Unlisted", ORANGE, 44)
        _place(canvas, stamp, stage + 245, FY + 250, -12)
    return _jpg(canvas)
