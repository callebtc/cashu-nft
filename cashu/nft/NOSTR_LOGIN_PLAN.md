# Nonfungible.cash Nostr login implementation plan

Let collectors log in with a Nostr signing extension (NIP-07) or by pasting a
Nostr private key (`nsec`, hex, or password-protected `ncryptsec`). The Nostr
public key becomes the collection's public key. On first login the app
prefills the collection name from the user's Nostr profile and imports their
profile picture when it can be fetched.

This is the agreed implementation handoff following the planning interview.
The user approved every product decision below. It is implemented on branch
`claude/nostr-login-integration-c30b89` (baseline commit `2645958`); where the
build settled a detail differently, the text below says what was built and
"Implementation notes" lists the changes.

## Execute in this order

1. **Identity refactor, no behaviour change.** Replace the raw `secret` that is
   passed around with `{ pubkey, signer, root }`. Pin today's derivations with
   golden-vector tests first. Collections that use their own key must derive
   bit-identical wallets, encryption keys and market keys afterwards.
2. **Server.**
   - Add a shared verifier that accepts both signature forms.
   - Add session keys to `authorize()`.
   - Store the vault (table `portfolio_vaults`) and add the `nostr` profile flag.
   - Add backend tests.
3. **Nostr client module**, lazy-loaded, built on `nostr-tools`:
   - parsing for `nsec`, `ncryptsec`, `npub` and hex
   - a local signer and an extension signer, with deferral
   - relay queries for kinds 0, 10002 and 30078
   - the NIP-44 vault
4. **UI.**
   - the login paths in the Create and Unlock dialogs
   - the prefilled first-login dialog
   - the restore dialog
   - picture import and the "Import from Nostr" button
   - npub routes and the badge
   - "Back up wallet key"
   - "Log out"
   - the "signatures waiting" notice
5. **Docs and browser acceptance** with real extensions (see Acceptance).

Step 1 is a gate. If any derivation for a collection that uses its own key
changes, stop and fix it before continuing.

## Approved product behavior

| Area | Decision |
| --- | --- |
| Signers | NIP-07 extension. Paste `nsec`, 64-hex or NIP-49 `ncryptsec` (password prompt). NIP-46/NIP-55 deferred; the signer interface leaves room for them. |
| Identity | The Nostr pubkey **is** the collection pubkey (`/p/<hex>`). |
| Wallet root | Nostr collections use a random 32-byte **vault secret** as the HKDF root for every wallet, encryption and market key. It is NIP-44-encrypted to the user's own pubkey. A pasted `nsec` and an extension therefore open the same wallets. |
| Vault storage | Primary copy on the server, backup copy as a NIP-78 kind 30078 event on the user's NIP-65 write relays (default relays if none). The user can export the vault secret and restore from that export. |
| HTTP auth | Extension users authorize a 30-day **session key** with one signature at login. Owner requests then sign exactly as today with the session key. Pasted keys sign locally and need no session. |
| Public signatures | NFT showings, listings, offers and acceptances are signed by the Nostr key. Extension users approve them per click. Signatures the app would make on its own are queued behind a "Sign" notice. Pasted keys keep today's raw BIP-340 signatures. |
| Key storage | Pasted `nsec`/hex: kept unencrypted in the localStorage keyring, like today's keys. `ncryptsec`: stored encrypted, password asked once per tab session. The nsec field suggests an extension instead. |
| First login | Prefilled Create dialog: name and picture preview, editable, one click to confirm. No "I saved my key" step. Existing collection: log straight in. |
| Profile import | Once, on first login. "Import from Nostr" in Edit profile pulls it again. Nothing is ever published back to Nostr. |
| Name | `display_name`, then `name`, then the NIP-05 local part, then "Untitled collection". Trimmed, cut to 40 characters. |
| Picture | Fetched in the browser, re-encoded by `shrinkPicture`, uploaded through the existing avatar endpoint (Turnstile, moderation, 256×256 crop). Any failure is ignored silently. |
| Relays | One constant list of profile indexers: `purplepag.es`, `relay.damus.io`, `nos.lol`, `relay.primal.net`. Add `getRelays()` and the user's NIP-65 relays when known. 3 s timeout, newest event wins. |
| Entry points | Inside the existing dialogs. Create gets "Continue with Nostr extension". Unlock auto-detects hex, `nsec` or `ncryptsec` and gets the same button. "No Nostr extension found" appears if `window.nostr` is still missing after about 1 s. |
| npub | Accept `/p/npub1…` and npub in "Open by public key", redirecting to hex. Show a shortened, copyable npub and a Nostr badge on Nostr collections only. Hex stays canonical in links and link previews. |
| Log out | Nostr collections only. Revoke the session and forget its key, the cached vault secret and any stored nsec or ncryptsec. Keep the local wallet databases so logging in again on this device resumes. |
| Library | `nostr-tools` 2.x (already on noble 2.x), imported by subpath and loaded with dynamic `import()` only when a login dialog or Nostr feature needs it. |

Collections created with "Create collection" (a generated hex key) keep
working exactly as today. In this document they are called **key
collections**.

## Why the design looks like this

- The collection key is already a BIP-340 x-only secp256k1 key
  (`crypto.mjs:32`, `portfolio.py:128`), so a Nostr pubkey can be used directly.
- NIP-07 never exposes the private key, and it signs only Nostr events. Two
  things today depend on the raw secret:
  - **Eight HKDF derivations:**
    - NFT wallet seed (`wallet/vault.ts:8`)
    - credential vault key (`vault.ts:15`)
    - money seed (`money/store.ts:98`)
    - snapshot key (`store.ts:109`)
    - market journal key (`market/coordinator.ts:79`)
    - claim, refund and receive keys (`coordinator.ts:160-168`)
  - **Every signature** is BIP-340 over a domain-separated digest (`crypto.mjs:38-43`, `market/protocol.ts:64-69`).
- An extension can't produce a stable secret that only the owner can get.
  Signatures are randomised, NIP-44 uses a random nonce, and its MAC blocks
  chosen ciphertexts. Any deterministic trick could also be triggered by
  another website. So the wallet root must be a random secret stored
  encrypted to the npub. This is the same pattern NIP-60 Cashu wallets use.
- Owner requests are frequent: 12–16 on a cold load, then 3–8 a minute (inbox
  long-poll, lease renewal, pending poll). Prompting for each is unusable,
  hence session keys.
- Some public signatures happen without a click:
  - purchase delivery (`coordinator.ts:437`, `:468-500`)
  - `recover()` on every load (`main.jsx:589-598`)
  - crash recovery of offer funding (`coordinator.ts:425-431`)

  Hence deferral. Legacy-card migration (`main.jsx:609-620`) never applies to
  Nostr collections.

## Client identity model

```text
identity = { pubkey, kind: 'key' | 'nsec' | 'nip07', signer, root, nostr }
signer.signAuth(message)                  -> 128-hex   (local key or session key)
signer.signDigest(purpose, digest, opts)  -> signature (raw or event form)
root                                      -> 32 bytes  (key collections: the secret; Nostr: the vault)
```

- **Thread `root` and `pubkey` through the modules.** `wallet/vault.ts`,
  `wallet/index.ts`, `money/store.ts`, `money/wallet.ts`,
  `market/coordinator.ts` and `market/api.ts` take `root` and `pubkey` instead
  of `secret`. Calls to `profileKey(secret)` inside those modules use the
  passed `pubkey`.
- **Route all signing through the signer.** `signedRequest` calls
  `signer.signAuth`. `publicCard`, `signListing`, `signOffer` and
  `signAcceptance` call `signer.signDigest`.
- **Extend the keyring in place.** `cashu-nft-keys-v1` maps a pubkey to its
  entry. A string value stays a key collection's hex secret, so existing
  storage needs no migration. An object value is a Nostr entry:
  `{ kind: 'nsec', secret, vault }`, `{ kind: 'ncryptsec', ncryptsec, vault }`
  or `{ kind: 'nip07', session: { secret, expires }, vault }`.
- **Keep decrypted ncryptsec secrets per tab.** They live in sessionStorage.
- **Handle the old-origin move.** The handover (`main.jsx:1015-1032`,
  `move.jsx`) carries only string entries. Nostr users simply log in again.
- **Check the extension's account.** Before every extension call, compare
  `getPublicKey()` with `identity.pubkey`. If they differ, block with "Your
  extension is signed in as another account".

## Signatures

### Two accepted forms

Every profile signature already signs a 32-byte digest `D`:
- auth: `sha256(auth message)`
- claim: `sha256("Cashu_NFT_Portfolio_Claim_v1\n" + showing)`
- market: `sha256(domain || hash)`

Verifiers accept either form:

1. **Raw.** 128-hex BIP-340 over `D`. This is unchanged and is what key
   collections and pasted Nostr keys produce.
2. **Event.** `n1:<created_at>:<sig128>`. The verifier rebuilds the event:
   - `pubkey`: the profile key
   - `kind`: `SIG_KIND` = 27711, a kind in 20000–29999 that the NIPs kind
     list doesn't register
   - `tags`: `[["x", hex(D)]]`
   - `content`: a fixed human-readable label for that purpose (e.g. "Publish
     an NFT to your Nonfungible.cash collection")
   - `created_at`: the value from the signature string

   It then computes the NIP-01 id and checks `sig` over that id. `D`
   already carries the domain separation. The label exists only so the
   extension prompt is readable.

Implement one helper per language and use it at every verification site:

- **Python** (new module `nostr_sig.py`, with `PROFILE_SIG` as the field pattern):
  - `portfolio.py:593` (`authorize`)
  - `portfolio.py:857-878` (claim)
  - `portfolio_wallet.py:386`, `:504` (publish)
  - `market_protocol.py:128` (`verify_purpose`, which covers `market.py:581, 661, 940, 1213, 1548`)
- **JavaScript** (`crypto.mjs`): `verifyCard`, which runs in `verify.worker.js`.

Relax the 128-hex-only Pydantic fields (`PublishRequest`, `ClaimRequest` and
the market models) to accept both forms. Extend `tests/verify.test.mjs`
fixtures with event-form showings.

### Session keys (extension users)

- **Create.** `POST /api/profiles/{pk}/session` with body
  `{session_pubkey, expires ≤ 30 days}`. It must be signed by the profile key
  itself, never by another session: for an extension, an event-form signature
  (purpose `auth`, label "Sign in to Nonfungible.cash") over the digest of the
  usual challenge message, sent in `X-Portfolio-Signature` like any other.
  - It works before the profile exists, because first login creates the
    session and then the profile.
- **Store.** Table `portfolio_sessions(session_pubkey, pubkey, expires,
  created)`, at most 20 live sessions per pubkey, expired rows pruned.
- **Use.** `authorize()` accepts a signature from the profile key, as today.
  With header `X-Portfolio-Session: <session_pubkey>`, it instead accepts a
  signature from a live session bound to that pubkey. Single-use challenges,
  body binding and expiry are unchanged.
- **Revoke.** `POST /api/profiles/{pk}/session/revoke`, used by log out. An
  expired session returns 401, and the client asks for one new extension
  signature.

### Deferred signatures (extension users)

`signer.signDigest(purpose, digest, { interactive })`:

- Background callers pass `interactive: false`:
  - `recover()`
  - `importPurchased` reached from `reconcile()`
  - the `reconcileOffer` call to `signOffer`
- A local signer ignores the flag. An extension signer throws
  `SignatureDeferred` and adds the operation to a queue.
- The operation stays in exactly the state a failed `/publish` or a crash
  leaves it in today. Those paths already retry, so deferral adds no new
  states.
- When the queue is non-empty, a notice under the top bar reads "N signatures
  waiting · Sign". "Sign" reruns the queued paths with `interactive: true`.
- **Never sign twice.** Persist a signed showing, or a signed offer, with its
  operation or journal record before submitting it. Retries reuse it. Today a
  failing `/publish` re-signs on every load (`recover()`, `importPurchased`),
  and `signOffer` re-signs while a record stays 'funded'
  (`coordinator.ts:303-315`). Verify that each path regenerates nothing
  random on retry, and change it where it does.

## Vault

- **Create**, on a new Nostr collection:
  - `vault = random 32 bytes`
  - `ciphertext = NIP-44(own pubkey → own pubkey, hex(vault))`. The extension
    uses `window.nostr.nip44.encrypt`; a pasted key uses `nostr-tools/nip44`.
  - `check = hex(HKDF-SHA256(vault, info "Cashu_NFT_Vault_Check_v1", 16 bytes))`
  - Extensions without `nip44` get: "Your extension doesn't support NIP-44.
    Update it or paste your key."
- **Server copy.** Profile creation for a Nostr collection carries
  `{name, vault: {ciphertext, check}}` in the existing signed
  `POST /api/profiles/{pk}`. The vault goes in its own table,
  `portfolio_vaults(pubkey, ciphertext, vault_check, updated)`, and the
  profile row gets `nostr = 1`; the profile JSON gains `nostr`.
  - The vault is write-once per check value. `POST /api/profiles/{pk}/vault`
    stores it only for a Nostr collection, and only when no vault exists or
    the check value is the same. A vault sent with the create call of an
    existing collection is ignored.
  - `POST /api/profiles/{pk}/vault/get` is owner-authorized.
- **Relay copy.** A kind 30078 event with
  `d = "nonfungible.cash:wallet-vault:v1"` and the same ciphertext as
  content. Publish it to the user's NIP-65 write relays, or to the default
  list if they have none.
- **Lookup order at login:**
  1. Cached copy in the keyring.
  2. Server.
  3. Relays.
  4. Restore dialog.

  A copy is accepted only if it decrypts and matches `vault_check` (when
  known). If a relay copy is found but the server lacks one, write it back to
  the server.
- **Export and restore.**
  - Nostr collections get "Back up wallet key" in place of "Back up private
    key". It shows the vault as 64 hex, with Copy and Download
    (`nonfungible-cash-wallet-key.txt`).
  - "Restore your wallet" opens during sign-in when no copy is found. It
    accepts that hex and checks it: the server only loses its check value
    together with the vault, so the key must decrypt the money wallet's
    backup or an NFT credential backup (accepted when there are none). It
    then re-stores the server and relay copies. Without a backup, "Hold to
    start a new wallet" stores a fresh vault.

## Login flows

Rule: **for an existing collection the server decides; for a new one, the
login method decides.**

| Server profile | Vault (cache → server → relays) | Login with | Result |
| --- | --- | --- | --- |
| Nostr collection | found | anything | Log in. |
| Nostr collection | not found | anything | Restore dialog. |
| Key collection | – | pasted hex / `nsec` / `ncryptsec` | Today's unlock; root = the raw key. |
| Key collection | – | extension | Refuse: "This collection uses its own key. Unlock it with that key." |
| none | found on relays | extension / `nsec` / `ncryptsec` | Prefilled dialog that re-creates the profile with that vault (the server lost data). |
| none | none | extension / `nsec` / `ncryptsec` | New Nostr collection via the prefilled dialog. |
| none | – | pasted hex | Today's "Create it" flow (key collection). |

**Extension login:**
1. Lazy-load `nostr-tools` and wait about 1 s for `window.nostr`.
2. Call `getPublicKey()`.
3. Fetch `GET /api/profiles/{pk}`.
4. Create the session.
5. Find the vault and `nip44.decrypt` it.

The kind 0 and kind 10002 queries run in parallel with step 4.

**Extension prompts:**
- Same device: none.
- New device: up to 3 (public key permission, session, decrypt).
- New collection: up to 4 (public key permission, session, encrypt, relay copy).

**Prefilled dialog** (new collection):
- The name field holds the imported name.
- The picture preview is a `blob:` URL, which the CSP already allows.
- There is no key box and no acknowledgement checkbox.
- Confirming runs, in order:
  1. Create the vault.
  2. `POST` the profile.
  3. Publish the relay copy.
  4. Upload the avatar. Any failure here is silent.
  5. Navigate to the collection.

**Pasted keys:**
- Detect the format with `/^nsec1/`, `/^ncryptsec1/` or `/^[0-9a-f]{64}$/`.
- `ncryptsec` asks for its password (scrypt, about 1 s).
- `npub` input shows "That's a public key".
- Nostr collections fetch the vault with locally signed requests and decrypt
  it locally. No session is needed.

## Profile import

- **Query.** One REQ per relay for `{kinds:[0,10002], authors:[pk]}`, using
  `nostr-tools` `SimplePool` with a 3 s limit. Keep the newest event of each
  kind.
- **Name.** The first non-empty of `display_name`, `name`, then the local part
  of `nip05`. Strip control characters, trim, and cut to 40 characters
  (`portfolio.py:115`).
- **Picture.** `https:` only. `fetch` with a 5 s timeout, then
  `shrinkPicture` (5 MB limit) and the existing avatar upload with
  `uploadHeaders()`. CORS, network, decoding or moderation failures are
  ignored.
- **"Import from Nostr".** In Edit profile, on Nostr collections only. It
  fills the form's name and picture; the user still clicks Save.

## UI changes

- **Create dialog** (`main.jsx:958-982`): add "Continue with Nostr extension"
  above "or create a collection key". The rest is unchanged.
- **Unlock dialog**: one field for hex, `nsec` or `ncryptsec` (with a password
  step), the extension button, and the hint "Prefer a signing extension: your
  nsec never touches this site."
- **Routes and lookup**: the route regex (`main.jsx:47`) and "Open by public
  key" (`main.jsx:698`) accept npub and redirect to hex.
- **Profile header**: on Nostr collections, a short copyable npub and a Nostr
  badge.
- **Profile menu**: "Back up wallet key" for Nostr collections.
- **Top-bar menu**: "Log out" for Nostr collections. It closes the money
  wallet normally (sync and lease release, `money/wallet.ts:97-105`), revokes
  the session, and clears the keyring entry and sessionStorage. Then it
  switches to another stored identity, or to guest.
- **Sign-in notice**: when the active Nostr collection's session expired, or
  its ncryptsec password hasn't been entered in this tab, a notice under the
  top bar offers "Sign in".
- **Global**: the "N signatures waiting · Sign" notice.
- **Copy**: update `SignInFirst` (`main.jsx:489`), the HowItWorks "Keys,
  custody and trust" section (`HowItWorks.jsx:92-93`) and
  `portfolio_web/README.md` "Trust model" to cover Nostr collections, the
  vault and session keys.

## Server changes

- **New module `nostr_sig.py`:** event-id computation, verification of both
  signature forms, and the purpose labels. Reuse the event serialization
  approach from `cashu/core/nostr.py:39-48`.
- **`portfolio.py`:**
  - `authorize()` gains the event and session paths.
  - Add the session endpoints and the `portfolio_sessions` table.
  - Add the `portfolio_vaults` table, the profile column `nostr`, and the
    vault read and write routes.
  - Add `nostr` to the profile JSON.
- **Relaxed signature validators** in `portfolio.py`, `portfolio_wallet.py`
  and the market models.
- **No server-side relay or picture fetching.**

## Tests

**Backend** (`tests/test_nft_portfolio*.py`, `tests/test_nft_market*.py`):
- Event-form signatures: accepted for claim, publish, listing, offer,
  acceptance and purchase publish. Rejected for a wrong pubkey, digest, kind,
  label, tags or a tampered `created_at`.
- Sessions: create via an event, authorize, expiry, revocation, a session for
  a different pubkey, the per-pubkey cap, and single-use challenges.
- Vault: write-once, owner-only `GET`, the `vault_check` match on `PUT`, and
  the `nostr` flag.

**Frontend** (`portfolio_web/tests`):
- Golden vectors from the current code: for a fixed key-collection secret,
  `walletSeed`, the vault key, `moneySeed`, `moneyKey(*)` and
  `authorizationKey(*)` are unchanged after the refactor.
- `verifyCard` accepts event-form showings and rejects mismatches.
- Input parsing (hex, `nsec`, `ncryptsec`, `npub`), name selection from kind 0,
  and a vault NIP-44 round trip with a local key.
- An extension signer called with `interactive: false` defers and never
  touches `window.nostr`.

## Acceptance (manual, real browsers)

`tests/test_nft_nostr_browser.py` runs the full path below against a real
server with a stand-in extension, and it was walked through in a browser with
a local relay. What still needs real extensions:

- **Extensions under the CSP.** Alby and nos2x in Chrome and one Firefox
  extension. Confirm each one injects `window.nostr` under
  `script-src 'self'` (`portfolio.py:550-552`).
- **The full path:** first login (name and picture imported) → mint → new
  browser via extension (3 prompts, same NFTs and money) → log out → log in →
  restore from the exported wallet key after deleting the server vault in a
  dev database.
- **Purchase delivery.** The seller accepts while the buyer's extension tab is
  open. The buyer sees the notice, no popup appears, and one click publishes.
- **Pasted `nsec` and an extension** open the same collection and wallet.
- **Key collections** (generated hex) behave exactly as before.

## Implementation notes

What the build settled differently from the first draft of this plan:

- **One signature format for sign-in.** Creating a session uses the same
  event-form signature as everything else (purpose `auth`) in
  `X-Portfolio-Signature`, instead of a kind 27235 event in a separate
  header. One verifier, one format, and a readable extension prompt.
- **The vault has its own table.** The profile row is served publicly
  (`SELECT *`), so the ciphertext lives in `portfolio_vaults`; the profile
  keeps only `nostr`. Owner routes are all `POST`, so the read is `/vault/get`.
- **Restore lives in sign-in.** A signed-in Nostr collection always has its
  vault, so restoring is offered only where it's needed: when sign-in finds
  no copy. It adds a "start a new wallet" escape for users without a backup.
- **Signatures are made once.** A signed showing is kept (encrypted) with
  its operation until it's published, and a signed offer is journaled before
  it's submitted, so retries reuse them instead of prompting again.
- **Tests point at their own relays.** `nostr.ts` `useRelays()` replaces the
  default relays; the end-to-end tests use none.
- **Log out exposed an old assumption.** The market inbox effect assumed the
  identity never goes away while the wallet is open; it's guarded now.

## Out of scope

- NIP-46 bunkers and NIP-55 Android signers.
- Publishing kind 0 or anything other than the vault backup to relays.
- Extension login for key collections, and moving a key collection's NFTs to
  a Nostr collection.
- NIP-05 verification display.
- Server-side picture fetching.

## Risks to check during implementation

- `nostr-tools` pins exact noble versions (`@noble/curves` 2.0.1), while the
  app uses `^2.4.0`, so the lockfile has two copies. Checked: they stay in the
  lazy `nostr` chunk (about 32 kB gzipped); the main bundles don't change.
- `SIG_KIND` 27711 is unregistered in the NIPs kind list (checked).
  Extensions may still label unknown kinds oddly in their prompts.
- The server-wide 1,000-profile cap (`portfolio.py`, `create_profile`)
  applies to Nostr sign-ups unchanged.
