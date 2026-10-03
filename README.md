# Cashu NFT

**The NFT is the JPG.** Cashu NFT turns any picture into a private, bearer
collectible built on [Cashu](https://cashu.space) ecash. The ownership token
travels inside the image file's EXIF header, so sending the JPG (by email, USB
stick or a link) sends the NFT. There's no blockchain, no gas and no wallet
extension; everything runs in an ordinary browser.

- **Private by design.** Each NFT is a blind-signed credential. A
  zero-knowledge proof shows on every transfer that the hidden picture hash
  stays the same, so the mint stops double spends without learning who holds
  what.
- **A real marketplace.** Collectors list NFTs and buyers bid with ordinary
  Cashu ecash from any mint. Offers are locked with NUT-14 hash time locks: the
  seller is paid only by delivering the NFT, and unaccepted bids refund after
  their deadline.
- **Collections, profiles and activity.** Public collections, likes, follows,
  public bids and a live activity feed.

> Experimental, unaudited cryptography. Don't use it for anything of real
> value yet.

This repository is a fork of [Nutshell](https://github.com/cashubtc/nutshell),
the reference Cashu implementation. The NFT code lives in `cashu/nft/` and the
Pointcheval–Sanders credential scheme in `cashu/core/crypto/ps.py`. Nutshell's
own README is in [NUTSHELL.md](NUTSHELL.md).

## Requirements

- Python 3.10 or newer and [Poetry](https://python-poetry.org/)
- Node.js 20 or newer and npm (to build the web app)

## Run it locally

```bash
git clone https://github.com/callebtc/cashu-nft.git
cd cashu-nft

# Backend dependencies
poetry install

# Build the web app
cd cashu/nft/portfolio_web
npm ci
npm run build
cd ../../..

# Start the NFT mint and web app on http://127.0.0.1:8401
poetry run python -m cashu.nft.portfolio
```

On first start the server creates `data/nft-portfolio/` with a random
32-byte mint seed (`mint.seed`) and an SQLite database. Open
<http://127.0.0.1:8401>, choose **Get started** to create a collection, save
the collection key it shows you, and add a JPG.

### Marketplace with a local test mint

Bids are paid in ordinary Cashu ecash. Public mints work out of the box. To
develop without real funds, run a local Nutshell mint whose Lightning
invoices settle by themselves, and allow it over plain HTTP:

```bash
# Terminal 1: fake-value ecash mint on http://127.0.0.1:3339
poetry run python -m cashu.nft.dev_ecash_mint --port 3339 --fee-ppk 0

# Terminal 2: the app, allowed to reach that mint
NFT_MARKET_DEV_MINTS=http://127.0.0.1:3339 poetry run python -m cashu.nft.portfolio
```

In the app, open **Wallet**, add `http://127.0.0.1:3339` as a mint and
receive some sats. The invoice pays itself. Never set `NFT_MARKET_DEV_MINTS`
in production.

### Frontend development

```bash
cd cashu/nft/portfolio_web
npm run dev
```

Vite serves the app on <http://127.0.0.1:5173> and proxies `/api` and `/v1`
to the backend on port 8401. Set `NFT_PORTFOLIO_API` to point it at another
backend.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NFT_PORTFOLIO_DIR` | `data/nft-portfolio` | Database and mint seed |
| `NFT_PORTFOLIO_HOST` / `NFT_PORTFOLIO_PORT` | `127.0.0.1` / `8401` | Listen address |
| `NFT_PORTFOLIO_MAX_JPG_BYTES` | 10 MB | Upload limit per JPG |
| `NFT_PORTFOLIO_MAX_CARDS` | unset (no limit) | Optional cap on NFTs per collection |
| `NFT_PORTFOLIO_MAX_STORAGE_BYTES` | 1 GB | Total image storage |
| `NFT_PORTFOLIO_TRUSTED_PROXY` | unset | Reverse proxy address (e.g. `127.0.0.1`) whose `X-Forwarded-For` is trusted for rate limiting |
| `NFT_PORTFOLIO_NSFW_MODEL` | unset (classifier off) | Path to the NSFW image classifier (see [Content filtering](#content-filtering)) |
| `NFT_PORTFOLIO_NSFW_THRESHOLD` | `0.8` | NSFW score at which an upload is refused |
| `NFT_PORTFOLIO_TURNSTILE_SITEKEY` / `NFT_PORTFOLIO_TURNSTILE_SECRET` | unset (off) | Cloudflare Turnstile keys; image uploads then need a token (see [Content filtering](#content-filtering)) |
| `NFT_MARKET_DEV_MINTS` | unset | Development only: comma-separated mint URLs the marketplace may reach over plain HTTP on local addresses |
| `PUBLIC_URL` (build time) | unset | Absolute site URL for social preview tags, e.g. `https://nft.example.com` |
| `VITE_MINT_KEYSET_ID` (build time) | unset | Pin the expected mint keyset in the bundle |

## Content filtering

The server screens every image it would publish (new NFTs, received
transfers and profile pictures) with
[Marqo's NSFW classifier](https://huggingface.co/Marqo/nsfw-image-detection-384),
a small Apache-2.0 model that runs on the CPU in about 30 ms per image.
Refused images are remembered by their hash and a perceptual hash, so
re-saved, recompressed or resized copies are refused too, for every
collector.

Export the model to ONNX once, on any machine (this needs PyTorch and timm,
which the server doesn't):

```bash
pip install torch timm onnx onnxscript onnxruntime
python scripts/export_nsfw_model.py nsfw-image-detection-384.onnx
```

Copy the file to the server and set `NFT_PORTFOLIO_NSFW_MODEL` to its path.
Without it, only previously refused images are blocked.

The default threshold of 0.8 blocks clearly explicit images. The model
scores some abstract digital art (smooth skin-toned shapes) as NSFW: in our
tests it refused about 7% of abstract wallpapers at 0.8 and about 22% at 0.5,
and no photos of animals, flowers or objects. Lower the threshold to block
more, at the cost of more refused art.

Image uploads can also require a
[Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/) token.
Create a widget in **invisible** mode for your hostname and set both
`NFT_PORTFOLIO_TURNSTILE_*` keys. The browser loads Turnstile only when it
uploads an image, and nothing is shown. The app's Content-Security-Policy
then allows `https://challenges.cloudflare.com` in `script-src` and
`frame-src`; a reverse proxy that sets its own policy needs the same. For
local testing, Cloudflare's dummy keys always pass
(`1x00000000000000000000BB` with `1x0000000000000000000000000000000AA`).

## Deploying

The app is a single process: the NFT mint, the marketplace settlement worker
and the API all run in it, and it can serve the built web app itself. A typical
setup puts it behind a TLS reverse proxy.

1. Build the web app with your public URL:

   ```bash
   cd cashu/nft/portfolio_web
   npm ci
   PUBLIC_URL=https://nft.example.com npm run build
   ```

2. Install the backend into a virtualenv inside the checkout, then run it as
   a service:

   ```bash
   poetry config virtualenvs.in-project true
   poetry install --only main
   ```

   For example, with systemd:

   ```ini
   # /etc/systemd/system/cashu-nft.service
   [Unit]
   Description=Cashu NFT
   After=network-online.target

   [Service]
   User=cashu-nft
   WorkingDirectory=/opt/cashu-nft/app
   Environment=NFT_PORTFOLIO_DIR=/opt/cashu-nft/data
   Environment=NFT_PORTFOLIO_TRUSTED_PROXY=127.0.0.1
   ExecStart=/opt/cashu-nft/app/.venv/bin/python -m cashu.nft.portfolio
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```

3. Put a reverse proxy in front. With [Caddy](https://caddyserver.com):

   ```caddy
   nft.example.com {
   	encode zstd gzip
   	request_body {
   		max_size 12MB
   	}
   	reverse_proxy 127.0.0.1:8401
   }
   ```

   If you serve the built files from the proxy instead, route `/api/*` and
   `/v1/*` to the backend. Any Content-Security-Policy you add must allow
   `connect-src 'self' https: wss:`, so the browser wallet can reach ecash
   mints.

**Back up the data directory, and keep a copy off the server.** `mint.seed`
is the mint's identity: losing it invalidates every NFT ever issued, and
browsers that already know the old mint refuse a new one. The database holds
the ownership records and encrypted wallet backups. Stop the service, or copy
the SQLite file with `sqlite3 .backup`, before you take a copy.

## Tests

```bash
# Backend
poetry run pytest tests/test_nft_portfolio.py tests/test_nft_market.py tests/test_nft_market_browser.py

# Web app: unit tests and type check
cd cashu/nft/portfolio_web
npm test
npm run typecheck
```

The test suite starts its own FakeWallet ecash mint, so no Lightning node is
needed. The browser end-to-end tests also need Node and the web app's
dependencies (`npm ci` in `cashu/nft/portfolio_web`).

## How it works

- [cashu/nft/portfolio_web/README.md](cashu/nft/portfolio_web/README.md) covers
  the web app, its trust model, transfer JPGs and transfer links.
- [cashu/nft/BLIND_ISSUANCE.md](cashu/nft/BLIND_ISSUANCE.md) covers blind hash
  issuance.
- [cashu/nft/MARKETPLACE_PLAN.md](cashu/nft/MARKETPLACE_PLAN.md) covers the
  marketplace protocol (funded HTLC offers, preimage escrow, delivery receipts)
  and its implementation log.
- The app's own **How it works** page explains it for collectors, with a
  separate cryptography section.

## License

MIT, like Nutshell. See [LICENSE.md](LICENSE.md).
