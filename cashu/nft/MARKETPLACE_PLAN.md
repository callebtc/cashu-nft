# Cashu NFT marketplace implementation plan

Implement a marketplace where collectors list JPG NFTs and receive funded offers
in ordinary Cashu ecash. Buyers may go offline after funding an offer. A seller
can later accept it, deliver the NFT to the buyer's preauthorized wallet, and
receive the ecash. Background workers execute narrowly authorized claims and
refunds; wallet spending keys remain with their owners.

This is the agreed implementation handoff following the planning interview.
The user approved all product decisions below. The marketplace itself has not
been implemented. Baseline: `feature/ps-nft-credentials`, commit `b3ebde0b`.
Reinspect the current branch before editing and preserve subsequent work.

## Execute in this order

1. Prove the offline settlement and refund constructions with protocol tests.
2. Implement NFT spend conditions and durable mint settlement receipts.
3. Implement the encrypted ordinary ecash wallet and recovery journal.
4. Implement listings, funded offers, settlement workers and notifications.
5. Build marketplace and wallet UI using the existing design system.
6. Complete failure, recovery, interoperability and browser acceptance tests.

The first step is a gate: write the precise transcripts and adversarial tests
before building marketplace screens. If offline delivery requires additional
trust, a weaker ownership guarantee, or exposing wallet secrets beyond this
plan, stop that protocol path and report the concrete incompatibility. Resolve
routine implementation choices autonomously within the approved design.

## Approved product behavior

| Area | Decision |
| --- | --- |
| Listings | Owners choose individual JPGs and set a positive integer asking price in sats. |
| Offers | Fund the offer when submitted. Amount must meet or exceed the ask applicable when the offer is created. Offers above ask are allowed. |
| Payment mints | Accept offers from any technically compatible mint. There is no seller-configured mint allowlist. The seller decides whether to trust each offer's mint when accepting. |
| Amount semantics | Ask and offer mean net seller proceeds. Buyer pays required funding and redemption fees, displayed before funding. No marketplace fee in v1. |
| Competing offers | Multiple funded offers per NFT; at most one accepted settlement can consume that NFT. Losing offers enter the refund flow. Early cooperative refunds are deferred. |
| Expiry | Default offer lifetime is 24 hours. Acceptance closes one hour before the cash refund deadline. Show both timestamps. |
| Offline buyer | Buyer may close the browser after the offer is fully funded, its recovery data is saved, and its refund job is registered. Acceptance does not require the buyer to reconnect. |
| Automatic refunds | A durable background executor submits the buyer's pre-signed refund after expiry and retries failures. The browser also recovers/submits the same operation when reopened. |
| Notifications | Private in-app inbox, unread badge and live updates while open; recover missed events on return. Email, web push and external messaging are deferred. |
| Privacy | Asking prices are public. Completed-sale activity shows NFT, buyer, seller and time. Actual offer amount, payment mint and settlement details are participant-only API data. |
| Wallet | Per-mint balances, token receive, Lightning top-up, token send/export and Lightning withdrawal. No automatic exchange between mints or mixed-mint payment within one offer. |
| Recovery | Same profile private key unlocks encrypted wallet and swap backups. Recovery requires the encrypted records; a seed alone is insufficient for every pending swap. |

An offer's signed terms remain immutable. Editing an ask affects new offers
only; existing offers retain their original terms. Unlisting stops new offers
and declines pending offers, whose locked funds still follow their refund
schedule. Disable listing edits once an acceptance is reserved until it either
settles or safely recovers.

Listing first rotates the NFT credential, invalidating previously exported
JPGs and transfer links. Preserve the card ID, image and collection history.
Disable ordinary export/send while listed; owners unlist before sending.
Listing is not itself the buyer-specific NFT HTLC. That contract is created
for the selected offer during acceptance.

## Trust and atomicity contract

Use **NFT-mint-assisted settlement**, not a claim of a generic trustless
two-party cross-mint swap. The NFT issuer already controls NFT issuance; this
design additionally trusts it to withhold an escrowed preimage until the
specified NFT is irrevocably delivered to the buyer's fixed destination.
An issuer that leaks the secret early to a seller can violate that ordering.

The marketplace relay receives no unrestricted ecash signing keys, NFT owner
secrets, output secrets or output blinding factors. Public-key encryption of
the preimage is specifically to the NFT issuer's settlement service, not to
an arbitrary marketplace relay. Use separate, versioned encryption keys and
authenticated envelopes from a reviewed construction. Bind the offer manifest,
recipient and key version, and authenticate/pin service and receipt keys with
the NFT mint identity. Define key retention/rotation for outstanding jobs.

Both issuers must enforce their advertised protocols. Settlement still relies
on the ecash mint being reachable before its refund deadline. Background
execution and long margins improve availability; they cannot guarantee payment
through arbitrary mint outages or misconduct. A mint-confirmed NFT delivery
and a mint-confirmed cash claim are separate facts and must be tracked as such.

The refund deadline enables a competing refund spend; it does not invalidate
the receiver's claim path. Only a confirmed spend establishes which path won.
Never equate a backend timer or an offer status with refunded money.

## Protocol gate and funded offer construction

Specify canonical, versioned terms including offer/listing IDs and revision,
asset hash and current NFT nullifier, both profile identities, NFT mint/keyset,
payment mint and sat unit, net price and exact proof/fee budget, hashlock,
claim/refund keys, deadlines, and the fixed buyer NFT destination. Parties sign
the relevant complete manifests with explicit purpose/domain separation.
IDs, amounts, URLs, keys and byte encodings must be unambiguous.

1. Buyer generates fresh 32-byte `r` and `H = SHA256(r)` for this offer only.
   Each competing offer has an independent secret. The cash HTLC has seller
   claim pubkey, buyer refund pubkey, `H`, cash deadline and `SIG_ALL`.
2. Buyer prepares a fresh NFT owner commitment and proof of knowledge. Bind
   the receive authorization to this NFT and exact destination; it is not a
   reusable authorization to spend or acquire arbitrary assets.
3. Persist encrypted funding intent, counters and complete output recovery
   material before spending the buyer's ordinary ecash. Fund the HTLC using
   cashu-ts and preserve change through the normal wallet accounting path.
4. From the actual issued HTLC proofs, construct and pre-sign a refund into
   fixed buyer-controlled blinded outputs, net of applicable refund fees.
   Save full recovery data encrypted. Register the restricted executor job
   durably before displaying the offer as fully submitted and safe to leave.
5. Encrypt `r` to the NFT issuer, authenticated against the complete offer
   manifest. Issuer validates it matches `H`. Offer inspection, failed
   registration, logging, lock preparation and losing-offer paths never expose
   the plaintext. An escrow-valid receipt does not reveal the secret.
6. Persist the funded offer, delivery authorization, encrypted recovery and
   executor receipt idempotently. A crash between funding and registration is
   a recoverable incomplete offer, not a reason to create new outputs blindly.

The gate must prove that the buyer can later recover the exact usable NFT
credential. Current PS private reissuance couples the old holder's proof with
the output blinding. Design the required blinding cooperation explicitly:
the seller must be able to prove the transfer, and the buyer must retain the
correct unblinding material, without disclosing the buyer's new owner secret.
A signature nominally addressed to the buyer but impossible to unblind is not
delivery. Test this using the actual Python and TypeScript implementations.

## Seller acceptance and offline delivery

1. Seller reviews the mint URL, test-value status, net amount, fees and times.
   Adding a mint to the buyer's wallet does not make it trusted by the seller.
   Capability-check and verify authentic HTLC proofs/DLEQ where applicable,
   units, keysets, amounts, conditions and live `UNSPENT` state. State lookup
   alone is not proof that a supplied signature is genuine.
2. Atomically reserve this listing/current NFT for one offer. Recheck its
   identity, signed revision and availability; serialize competing accepts.
   The seller prepares the NFT transfer to the buyer's fixed destination and
   a pre-signed, fixed-output ecash claim. Persist seller recovery material and
   register the cash-claim job before NFT delivery can commit.
3. Install a buyer-specific NFT HTLC: same `H`, buyer claim authorization,
   seller refund authorization and a shorter NFT deadline. The exact timeout
   and clock-skew margins are protocol-gate outputs. Enforce a strict safety
   gap before the cash deadline, within the approved one-hour acceptance
   cutoff; do not silently reduce that cutoff.
4. NFT issuer validates ownership, conditions and both parties' narrowly bound
   authorizations. Atomically consume the locked NFT nullifier, issue to the
   fixed buyer destination, and save the complete recoverable issuance result,
   delivery receipt and pending collection projection.
5. Expose `r` only after that durable commit. Use the NFT mint's authoritative
   witness/receipt endpoint, backed by a transactional outbox if work crosses
   process boundaries. HTTP disconnects and publishing failures cannot erase
   delivery or cause premature release.
6. Seller or executor appends the committed preimage to the pre-signed ecash
   claim and submits it. On confirmed payment, update balances, mark the sale
   complete and emit its public event. Keep retryable payment failures visible
   as payment pending, with escalation as the refund deadline approaches.
7. Buyer sees the NFT immediately as **Purchased · awaiting wallet sync**.
   On reopening, recover/unblind and verify the credential, save it encrypted,
   then publish the ordinary buyer-signed ownership showing. A mint delivery
   receipt does not receive the existing **Verified owner** badge.

Installing and satisfying the NFT contract may occur in one mint transaction,
or through a durable intermediate lock. Choose during the protocol gate and
test every resulting interruption boundary. Losing offers never release their
preimages. Before delivery, failed acceptance may be released only after the
mint confirms there is no unresolved NFT lock or possible outstanding claim.

## NFT spending conditions

Use a mint-enforced contract registry keyed by the credential's current
nullifier. Preserve existing PS credential/signature wire formats and the
invariant that the JPG hash remains constant through transfers. Return a signed
contract receipt binding all conditions and verify contract state online.

Enforcement belongs inside the transaction that consumes the nullifier.
Unify checks for public transfer, private transfer, burn, portfolio rotation,
migration and all other spend routes. Marketplace API checks alone are
insufficient. Ordinary JPG/link redemption must also obey the lock.

Claim requires the correct preimage and buyer authorization bound to contract,
mint/keyset, branch and complete allowed destination/request. Refund requires
seller authorization after the NFT refund time. Both terminal paths spend
the old nullifier and issue a fresh credential. Never remove an expired lock
and make its original bearer credential spendable again.

Store durable terminal witnesses and exact-request issuance receipts. Preserve
existing checkstate clients while providing explicit lock/outcome information
to new wallets. A locked but unspent NFT must not appear freely transferable.

## Background claims and refunds

Executor jobs contain only locked inputs, fixed blinded output amounts/points,
required signatures, allowed execution times, mint/keyset policy and operation
identity. Full serialized swap previews contain private output material and
belong only in encrypted owner recovery records.

Use output-bound `SIG_ALL`, not input-only signatures. Current NUT-11 binds
input secrets/signatures and output amounts/blinded points; witness preimage
is outside that digest. Consequently a seller can sign the exact cash claim
before preimage release, and a buyer can sign a refund before it is executable.
The mint still enforces the deadline.

Pinned cashu-ts helpers reject signing with a refund-only key before timeout.
A small reviewed adapter may use `SigAll.computeDigests`, `signDigest` and
the correct witness placement for advance authorization. Test it against
real verifier behavior; do not falsify the clock or weaken normal validation.

Jobs need durable leases, bounded retries/backoff, idempotency, receipt storage,
startup recovery, and an independent browser recovery path. Restore lost
responses using saved blinded outputs and tested NUT-09 support. NUT-19 HTTP
caching is optional and can be short-lived; it is not long-term recovery.

Keyset ID is outside the current `SIG_ALL` output digest. A relay may adapt
only to a supported active output keyset with the same curve/unit and identical
amounts/blinded points. Verify actual returned keyset and signatures during
recovery. Any required change to input set, output amounts or blinded points
needs new owner authorization; report **Refund needs wallet attention** when
safe automatic execution is no longer possible.

Expired, declined and losing offers all use the same refund mechanism. Jobs
must determine actual mint outcomes when refund and claim race. Show refund
pending during outages and refunded only after confirmed execution. Refunds
can incur mint fees, so returning less than the originally debited total is
possible and must be explained before funding.

## Ordinary ecash wallet

Build on cashu-ts `5.0.0-rc.11` and Coco `2.0.0`; preserve the project's override
and verify ordinary-wallet compatibility, not just existing NFT-plugin tests.
Coco's shipped send handlers cover default/P2PK and reject HTLC payment
requests. Implement a dedicated HTLC coordinator with supported proof/counter
reservation seams; do not cast an HTLC into the P2PK handler.

Keep money-wallet seed derivation and storage versioned and profile-scoped,
independent of the NFT mint keyset. Preserve existing NFT derivation and
backups. Derive separate keys/domains for encryption, ordinary wallet state
and operation authorizations.

Stock Coco IndexedDB stores proof secrets and keypair secrets in plaintext,
including secret-derived primary keys. Implement an encrypted repository
boundary, with suitable opaque lookup indexes, before claiming encrypted
local persistence. Decrypted spend material may exist in the unlocked wallet's
memory. Profile keys retain the existing local-browser storage model; do not
claim this protects against malicious same-origin JavaScript.

Back up proofs/change, quotes and required quote keys, mint discovery data,
counter reservations, HTLC keys/preimages, output blindings, complete operation
previews, signed terms and receipts. Commit encrypted intent before each remote
spend and reconcile exact outputs before retrying. Deterministic seed restore
does not reconstruct every custom HTLC or random swap secret.

Web Locks cover tabs only. Add a defined multi-device synchronization and
reservation strategy for proofs/counters and backup revisions, so two devices
cannot unknowingly allocate the same outputs or treat reserved proofs as
available. Enable the necessary Coco quote/proof watchers and processors with
explicit start, unlock, visibility, reconnect and disposal behavior.

Balances are per mint and distinguish available, offer-locked, pending claim
and pending refund. Offer prices are net seller amounts; calculate funding,
redemption and refund fees from actual input keysets/proof decompositions.
Use checked integer arithmetic and respect mint limits.

## Mint selection and networking

| Choice | URL |
| --- | --- |
| Default test mint | `https://testnut.cashu.space` |
| Minibits | `https://mint.minibits.cash/Bitcoin` |
| Coinos | `https://mint.coinos.io` |
| Macadamia | `https://mint.macadamia.cash` |

Also accept a custom mint URL. These are discovery shortcuts, not an endorsement
or a permanent capability guarantee. Testnut uses fake value and automatically
paid test invoices: label **test sats** distinctly in balances, offers and
seller confirmation, even though its protocol unit is `sat`.

Check capabilities and browser connectivity when adding a mint and before
funding. Marketplace eligibility requires the HTLC/pubkey and exact SIG_ALL
semantics, authenticity verification, proof-state and recovery support used by
this protocol. An ordinary wallet may support mints ineligible for trading.
Advertised NUT support alone does not prove the deployed SIG_ALL revision;
use a documented compatibility/conformance policy and fail closed on unknown
or incompatible enforcement. The initial observed real/test mint keysets use
ordinary secp256k1 ecash; NFT BLS support is a separate requirement.

Update the existing `connect-src 'self'` CSP deliberately for browser HTTPS/WSS
mint access and retain the other protections. Handle CORS, unavailable sockets
and polling fallback. Preserve valid URL path prefixes such as `/Bitcoin`.
Normalize mint identity consistently and reject credentials in URLs.

Custom URLs also reach the server-side worker. Apply SSRF defenses to every
fetch/redirect and DNS resolution: production HTTPS, no private/loopback/link-
local/metadata destinations or DNS-rebinding bypass, bounded bodies/timeouts
and mint count. Local test mints use an explicit development-only configuration.
This is not a seller mint allowlist; it protects the worker's network boundary.

## Application integration

Preserve the latest frontend, social discovery, activity feeds, encrypted links
and dropdown stacking fixes. Add a marketplace browse/filter/detail flow and
owner list/edit/unlist actions. Offers show amount, full mint identity,
test-value label, relevant deadlines and precise state before acceptance.

Add **Profile** in the top bar with Wallet, My collection and Offers/unread
badge. On collection pages place Explore, Activity and How it works in that
dropdown. Preserve the front page's existing navigation and appearance.
Provide a discoverable Market entry without removing its current links.
Keep product labels in sentence case.

Use authenticated event delivery with durable cursors for offers, notifications
and wallet operations, with batched polling fallback. Current 15-second visible
collection polling and the global API rate limit are not a settlement engine.
Reconnect from stored state and mint outcomes rather than assuming events were
received. Public feeds must whitelist sale fields; private payloads, proofs,
mint/amount details and recovery material must not leak through existing
explore/activity/profile/OG endpoints.

## State and module boundaries

Keep NFT ownership, listing visibility, funded-offer disposition, NFT delivery
and cash settlement as separate durable facts. Avoid one overloaded `status`.
Useful state dimensions are:

- Listing: draft, active, reserved, sold, unlisted, stale.
- Offer: preparing, funded, acceptance reserved, declined, acceptance closed.
- NFT leg: unlocked, locked, delivered, refund pending, refunded.
- Cash leg: locked, claim pending, claimed, refund pending, refunded, needs attention.
- Buyer publication: awaiting wallet sync, verified showing published.

Every transition names its actor, authorization, transaction boundary, retry
key and authoritative evidence. Funded never means paid; deadline reached never
means refunded; delivery never by itself means seller cash received. Reserve
once per current NFT and use compare-and-swap/revision checks across workers.

Add dedicated listing/offer/contract/job/event/recovery records with migration
and restart tests. Existing card states are only owned/ready/sent; `ready` means
exported bearer, not escrow. Adapt reconciliation so locks and refunds are not
misclassified as completed sales. Public activity currently synthesizes rows;
add durable sale events with deduplication and explicit privacy projections.

| Existing area | Implementation responsibility |
| --- | --- |
| `cashu/core/crypto/ps.py`, `cashu/nft/ledger.py`, `api.py` | Purpose-bound authorizations, all-route condition enforcement, issuance and witness receipts. |
| `portfolio.py`, `portfolio_wallet.py` | App wiring, auth/CSP, lock-aware reconciliation, encrypted recovery and offline delivery projection. |
| `portfolio_social.py`, `portfolio_links.py` | Sale activity/privacy; invalidate stale exports/links through credential rotation. |
| New marketplace/settlement modules | Listings, offers, admission checks, atomic acceptance, escrow service boundary, worker/outbox and inbox. |
| `portfolio_web/src/wallet/` | Stable ordinary-wallet service, encrypted Coco storage, HTLC adapter, preauthorization and recovery. |
| `src/main.jsx`, `social.jsx`, `ui.jsx`, `style.css` | Profile navigation, marketplace, wallet, offers, notifications and pending-proof states. |

Keep HTTP/UI handling separate from the swap state machine and crypto adapters.
Keep the issuer's preimage escrow separate from the unprivileged relay job
executor, even when initially deployed in one application.

## Acceptance tests and delivery criteria

The implementation is complete when the following are automated or, for live
third-party compatibility, accompanied by explicit reproducible evidence:

- Buyer funds an offer, closes every browser, seller accepts later, worker
  completes payment, and buyer reopens to recover the usable NFT and publish
  its verified showing. Neither backend nor seller learns buyer wallet keys.
- Buyer closes every browser; an unaccepted, declined or losing offer expires;
  worker refunds to fixed buyer outputs; reopening recovers the actual net
  balance without manual intervention.
- Two simultaneous accepts can deliver one NFT exactly once. Losing offer
  secrets remain unreleased. Price edits and unlisting honor signed revisions.
- Preimage escrow validates hash/manifest and never leaks before delivery,
  including rejected requests, transaction rollback, logs and status APIs.
- Wrong preimage, keys, mint, unit, amount, deadline, destination, signature
  transcript or malformed/forged proofs fail before irreversible delivery.
- Every legacy spend route, burn, direct API call, exported JPG and encrypted
  link obeys NFT contracts. Listing rotation invalidates old exports; both
  terminal NFT branches invalidate the old credential permanently.
- Fault injection at every spend/commit/response boundary demonstrates exact
  retry and recovery across browser/server restart, lost replies, duplicate
  events, lease loss and multiple tabs/devices.
- Claim/refund races, exact deadlines, clock skew, unsupported SIG_ALL, fee and
  keyset changes, CORS failures and mint outages produce honest recoverable
  states, never fabricated success or redirected outputs.
- Local and remote stored spending material is encrypted; public serializers
  expose only intended fields. Executor payloads cannot alter destinations.
  Test custom-URL SSRF, redirect and DNS-rebinding defenses.
- Existing NFT mint/receive/send/cancel, public verification, encrypted links,
  social features and recovery remain functional.
- Browser coverage exercises wallet token/Lightning receive and withdrawal,
  per-mint/test-value balances, list/offer/accept/refund, notification reconnect,
  offline purchase projection and profile navigation on desktop/mobile.

Use fake/local mints for deterministic failure and clock tests. Validate the
external test mint flow separately. Do not spend real funds as an unattended
test. Record capability observations as dated evidence, not permanent facts.

Run relevant NFT/marketplace Python tests, frontend tests/typecheck/build, and
repository lint/type/format hooks. Supply migrations, setup/worker instructions,
protocol transcripts, tested recovery procedures and a concise review of the
new security-sensitive logic. Follow repository human-review requirements for
cryptography and schema changes before production rollout.

## Protocol references

- [NUT-14 HTLCs](https://github.com/cashubtc/nuts/blob/main/14.md)
- [NUT-11 pubkey conditions and SIG_ALL](https://github.com/cashubtc/nuts/blob/main/11.md)
- [NUT-07 proof state and witnesses](https://github.com/cashubtc/nuts/blob/main/07.md)
- [NUT-09 restore](https://github.com/cashubtc/nuts/blob/main/09.md)
- [NUT-02 keysets and input fees](https://github.com/cashubtc/nuts/blob/main/02.md)
- [NUT-12 signature proofs](https://github.com/cashubtc/nuts/blob/main/12.md)
- [NUT-13 deterministic secrets](https://github.com/cashubtc/nuts/blob/main/13.md)
- [NUT-19 request caching](https://github.com/cashubtc/nuts/blob/main/19.md)
- [cashu-ts rc.11](https://github.com/cashubtc/cashu-ts/releases/tag/v5.0.0-rc.11)
- [Coco](https://github.com/cashubtc/coco)

Consult the installed package source and current primary specifications during
the protocol gate. Capability/type names and compatibility observations above
were inspected during planning; they are not substitutes for interoperability
tests of the final implementation.

---

# Implementation log

Progress is recorded here as work lands. Each phase lists what exists, how it
was verified, and what remains. Dates are absolute.

## Phase 1 · Protocol gate (2026-10-01)

### Findings from the installed code

| Question | Finding | Consequence |
| --- | --- | --- |
| Can the buyer recover a usable NFT without the seller learning the new owner secret? | The private (hidden-`h`) reissue proves `h, o, t` in one linear proof: `o` is the seller's presentation blinding, `t` the receiver's output blinding. One party must know both, so offline delivery through it forces the buyer to hand `t` to the seller. | Marketplace delivery uses the **public reissue** (`PSLedger.transfer` path): the seller's presentation reveals `h` (already public on a listing) and the mint issues `(u, v)` to the buyer's commitment `S' = s'·G1` with no output blinding. The buyer later combines the mint's receipt with `s'`. The seller only sees `S'`. |
| Is the existing receive proof purpose-bound? | `prove_owner_secret` uses `Cashu_PS_Issue_v1` with an empty binding, so it is reusable. | Market offers carry a second proof of knowledge of `s'` under `Cashu_NFT_Market_Receive_v1`, bound to the offer manifest hash. The NFT mint rejects destinations that are not fresh. |
| What does SIG_ALL sign? | Nutshell: `sha256(Σ(secret‖C) ‖ Σ(amount‖B_))`. cashu-ts rc.11 `SigAll.computeDigests().v0` computes the same digest. The HTLC preimage and the output keyset id are outside it. | The seller pre-signs the exact cash claim before the preimage exists, and the buyer pre-signs the exact refund before the locktime. The mint still enforces the deadline and the hashlock at execution. |
| Can cashu-ts sign a refund before the locktime? | `SigAll.signPackage` refuses a key the lock does not currently name; `computeDigests` + `signDigest` do not. | The browser adapter signs the recomputed digest directly, then tests confirm the mint rejects it before the locktime and accepts it after. |
| Which keysets support HTLCs? | This Nutshell version issues v3 (BLS) keysets by default; NUT-10 secrets are only valid on pre-v3 keysets. | Payment mints must expose an active secp256k1 (pre-v3) keyset in the offer unit; this is part of the eligibility check. The local test mint provides one. |

### Protocol v1 (`cashu-nft-offer-1`)

Canonical encoding: JSON with sorted keys, no insignificant whitespace,
lowercase hex, integers as JSON numbers, mint URLs normalized (scheme and host
lowercased, no trailing slash, path prefix kept, no credentials or fragment).
`manifest_hash = SHA256("Cashu_NFT_Market_Offer_v1\n" ‖ canonical_json)`.

Offer manifest fields: `v`, `offer_id` (random 16 bytes), `listing_id`,
`listing_revision`, `nft` (`keyset_id`, `h`, `nullifier` of the listed
credential), `seller`, `buyer` (profile x-only pubkeys), `price` (net sats to
the seller), `payment` (`mint`, `unit`, `keyset_id`, `amount` locked,
`claim_fee`, `refund_fee`), `hashlock` `H`, `claim_pubkey` (seller cash key
from the listing), `refund_pubkey` (fresh buyer key), `cash_deadline`,
`accept_deadline` (= `cash_deadline − 3600`), `nft_destination` `S'`,
`escrow_key` (version id), `created`.

1. **Offer.** Buyer picks fresh `r`, `H = SHA256(r)`, fresh `s'`, `S' = s'·G1`,
   a fresh refund key. Funds HTLC proofs at the payment mint:
   `["HTLC", {data: H, tags: [pubkeys=claim_pubkey, refund=refund_pubkey,
   locktime=cash_deadline, sigflag=SIG_ALL]}]` totalling `price + claim_fee`.
   Pre-signs the refund swap (all HTLC proofs → fixed buyer blinded outputs
   worth `amount − refund_fee`). Signs `manifest_hash` with the profile key
   (`Cashu_NFT_Market_Offer_Sig_v1`). Proves knowledge of `s'` bound to
   `manifest_hash`. Encrypts `r` to the NFT mint's escrow key.
2. **Registration** (NFT mint, one transaction): validates the manifest,
   signatures, receive proof, freshness of `S'`, listing revision and ask,
   decrypts the escrow envelope in memory only to check `SHA256(r) = H`, and
   stores the ciphertext, the public refund job (inputs, fixed outputs,
   signature, `not_before = cash_deadline`) and the offer. Nothing about `r`
   is logged or returned.
3. **Acceptance** (seller, any time before `accept_deadline`): verifies the HTLC
   proofs' DLEQ, keyset, unit, amount, tags and live `UNSPENT` state at the
   payment mint; pre-signs the claim swap to fixed seller outputs; creates a
   public presentation of the listed credential bound to
   `Cashu_NFT_Market_Deliver_v1 ‖ manifest_hash ‖ S'`; signs the acceptance.
4. **Delivery** (NFT mint, one transaction): compare-and-swap reservation of
   the listing and offer, register the claim job, install and satisfy the NFT
   contract (lock keyed by `N` with `H`, `S'`, deadlines), spend `N`, issue
   `(u, v)` to `S'`, store the signed delivery receipt and an outbox row. Only
   after commit is `r` decrypted and attached to the claim job.
5. **Settlement** (executor): submits the claim (pre-signed signature +
   released `r`) to the payment mint. Refund jobs run from `cash_deadline`.
   Outcomes come from the mint (proof state and NUT-07 witness), never from
   timers. Lost responses are recovered with NUT-09 restore of the fixed
   outputs.
6. **Buyer recovery:** fetch the delivery receipt, check it is signed by the
   pinned receipt key, verify `(u, v, h, s')` against the NFT keyset, store it
   encrypted, publish the showing.

Timing: NFT delivery must commit before `accept_deadline`, leaving one hour for
the claim before the cash refund opens. Clock skew allowance between the NFT
mint, the worker and payment mints: 5 minutes, checked at registration
(`cash_deadline ≥ now + 2h`) and at acceptance (`now ≤ accept_deadline − 5min`).

### Gate result: passed (2026-10-01)

`tests/test_nft_market_protocol.py`, 17 tests against the real test Nutshell
mint (FakeWallet backend, v2 keyset) and the real PS ledger:

| Claim | Evidence |
| --- | --- |
| Seller claim can be signed before the preimage is known | `test_claim_presigned_before_preimage_wins_and_refund_waits` |
| Buyer refund signed at funding is rejected before the locktime, accepted after | same test, `test_presigned_refund_executes_after_locktime_and_beats_late_claim` |
| Hashlock path remains valid after the locktime until a refund spends first; only one spend wins | `test_hashlock_path_still_valid_after_locktime_until_refunded` |
| Pre-signed outputs cannot be redirected or re-amounted | `test_presigned_outputs_cannot_be_redirected` |
| Wrong preimage or a refund key on the claim branch fail | `test_wrong_preimage_and_wrong_key_rejected` |
| A lost swap reply is recovered with NUT-09 restore; blind retry is refused | `test_lost_swap_reply_is_recovered_with_restore` |
| Which branch won is read from the mint's NUT-07 witness | `spent_by` assertions in the claim/refund tests |
| Payment proofs must carry exactly the agreed HTLC terms | `test_payment_proof_terms_are_checked_exactly` |
| Atomic lock + claim gives the buyer a usable credential; the seller cannot use the issuance; retries return the same issuance | `test_atomic_delivery_gives_the_buyer_a_usable_credential` |
| Wrong binding/destination/preimage fail and roll back completely | `test_delivery_rejects_wrong_binding_destination_and_preimage` |
| A lock blocks public transfer, private transfer, burn and a second lock | `test_locked_credential_blocks_every_spend_route` |
| Refund branch only after the NFT deadline; the old bearer credential stays dead | `test_refund_branch_only_after_deadline_and_never_restores_old_credential` |
| Receive proof, escrow envelope and receipts are purpose-bound | `test_receive_proof_is_bound_to_one_manifest`, `test_escrow_envelope_binds_manifest_and_version`, `test_receipts_are_signed_by_the_pinned_key` |
| cashu-ts and Python agree on the SIG_ALL digest; Python opens a WebCrypto escrow envelope | `test_typescript_sigall_digest_and_escrow_match_python` |

No path required more trust than the plan allows. Decisions taken in the gate:

- **One-transaction NFT contract.** Marketplace delivery installs and satisfies
  the NFT contract in the same database transaction as the nullifier spend,
  so there is no window where a buyer-specific lock exists without delivery.
  The generic lock (install, claim, refund) remains available and enforced on
  every route for future flows.
- **Settlement keys** (escrow `esc1`, receipts `rcpt1`) are derived from the NFT
  mint seed with domain separation and pinned by browsers together with the
  keyset (trust on first use). A PS-key attestation was considered and
  rejected: the private transfer route does not check asset registration, so
  a public attestation credential could itself be transferred.
- Existing NFT suites (`tests/test_ps_*.py`, `tests/test_nft_portfolio*.py`)
  still pass with lock enforcement in `_spend` and `transfer_private`:
  229 tests.

## Phase 2 · NFT spending conditions (2026-10-01)

- `ps_nft_locks` registry in `cashu/nft/ledger.py`, keyed by nullifier, with
  contract digest, hashlock, fixed destination, deadline, state, terminal
  witness and the exact issuance (for lost-reply recovery).
- `_require_unlocked` runs inside the transaction of both nullifier-consuming
  paths (`_spend`: public transfer, burn, portfolio rotation and migration via
  transfer; `transfer_private`: private transfer, JPG/link redemption). There
  is no other insert into `ps_nullifiers` outside the contract branches.
- Mint API (`cashu/nft/api.py`, exposed by the portfolio): `POST /v1/nft/lock`,
  `/lock/claim`, `/lock/refund`, `/lockstate`. `/checkstate` is unchanged for
  existing clients. Locked spends return `423`.
- Verified by `test_lock_api_enforces_contract_on_public_routes` plus the gate
  tests above (18 total).

## Phase 3 groundwork · ecash interop (2026-10-01)

- `cashu/nft/dev_ecash_mint.py`: local Nutshell mint (FakeWallet, auto-paid
  invoices, pre-v3 keyset, optional input fees) for deterministic frontend
  tests. Fake value only.
- Observed 2026-10-01 against that mint: cashu-ts rc.11 `prepareSwapToSend`
  with `{type: 'lock', options: {mainKeys, hashlock, locktime, refundKeys,
  sigAll: true}}` on the v2 keyset produces exactly the tags
  `check_payment_proofs` requires, with DLEQ proofs attached. rc.11 locks
  every new mint quote to a key (NUT-20); Coco manages that key.
- Coco stores `Amount` class instances and `Uint8Array` key material inside
  its in-memory repositories, so the encrypted snapshot uses a typed codec
  rather than plain JSON.

## Phase 4 · Listings, funded offers, settlement and inbox (2026-10-01)

Modules (HTTP handling separate from state machines and crypto):

| Module | Role |
| --- | --- |
| `cashu/nft/market_protocol.py` | Canonical manifests, purpose-bound signatures, receive proof, HTLC term checks, SIG_ALL digest, escrow envelope, receipts |
| `cashu/nft/market_cash.py` | Fixed-output swaps, NUT-07 outcome (`spent_by`), NUT-09 restore, owner-side blinding/unblinding |
| `cashu/nft/market_net.py` | SSRF-guarded mint client (public IPs only, pinned connect, SNI, no redirects, bounded bodies) |
| `cashu/nft/market.py` | `Market` (listings, offers, atomic delivery, inbox, recovery records, wallet backups/leases), `Executor` (restricted job runner), `market_router` (HTTP) |
| `cashu/nft/dev_ecash_mint.py` | Local FakeWallet mint with a pre-v3 keyset for development |

Schema (created on startup; requires human review per AGENTS.md):
`market_listings`, `market_listing_revisions`, `market_offers`, `market_jobs`,
`market_deliveries`, `market_events`, `market_sales`, `market_recovery`,
`money_backups`, `money_leases`, and `ps_nft_locks` in the ledger.

State dimensions are separate columns: listing `state`
(active/reserved/sold/unlisted/stale), offer `disposition`
(funded/accepted/declined/superseded/closed), `nft_leg` (unlocked/delivered),
`cash_leg` (locked/claim_pending/claimed/refund_pending/refunded/needs_attention),
`publication` (none/awaiting_sync/published). The cash leg only changes from
mint evidence read by the executor.

Transitions:

| Transition | Actor · authorization | Transaction | Retry key | Evidence |
| --- | --- | --- | --- | --- |
| list / revise / unlist | seller · profile-signed request + signed listing manifest | listing row + revision row | listing id + revision (CAS) | card's current nullifier unspent; card status `owned` (never exported) |
| register offer | buyer · signed request + signed manifest + bound receive proof | offer row + refund job + inbox | offer id + manifest hash (idempotent) | DLEQ, live `UNSPENT`, escrow opens to `H`, refund SIG_ALL verifies |
| accept + deliver | seller · signed request + signed acceptance + delivery-bound presentation | listing CAS, offer CAS, lock install+claim, nullifier spend, claim job, receipt, card → sent, losers superseded, sale + inbox | offer id (CAS on disposition) | live `UNSPENT`, claim SIG_ALL verifies, ledger contract |
| claim / refund | executor · pre-signed SIG_ALL | job row + offer cash leg | job id + lease (CAS) | NUT-07 state/witness, swap reply or NUT-09 restore |
| publish purchase | buyer · signed request + signed showing | card row + publication | offer id | showing verifies for `(buyer, h, keyset)`, nullifier unspent |

Public projections: `/api/market/listings` exposes asking prices only;
`/api/market/sales` and `sale` activity carry NFT, buyer, seller and time.
Offer amounts, mints, proofs, deadlines and jobs are participant-only via
signed requests.

Tests: `tests/test_nft_market.py`, 16 tests through the HTTP API, the real PS
ledger and the real test mint. All pass, together with the 18 gate tests.

| Plan acceptance criterion | Test |
| --- | --- |
| Buyer funds, goes offline; seller accepts; worker pays; buyer recovers the usable NFT and publishes | `test_offline_buyer_purchase_settles_and_recovers` |
| Losing offer refunds to fixed buyer outputs after its deadline without the buyer | `test_competing_offers_one_wins_losers_refund_without_preimage` |
| Two simultaneous accepts deliver once; losing preimages stay unreleased | `test_simultaneous_accepts_deliver_exactly_once`, previous test |
| Escrow never leaks before delivery (responses, DB file, decline) | `test_preimage_never_stored_or_returned_before_delivery` |
| Wrong keys, amounts, revision, escrow, DLEQ, destination, signatures fail before delivery | `test_offer_registration_rejects_bad_terms`, `test_acceptance_rejects_wrong_destination_signature_and_late_accept` |
| Price edits and unlisting honour signed revisions; listed NFTs can't be exported | `test_listing_guards_revisions_and_unlisting`, `test_listing_requires_current_credential_and_owner` |
| Lost replies and mint outages produce honest, recoverable states | `test_executor_recovers_a_claim_whose_reply_was_lost`, `test_executor_keeps_jobs_pending_while_the_mint_is_down` |
| Multi-device writes are serialized | `test_wallet_backup_lease_and_revisions` |
| SSRF, redirects and DNS rebinding | `test_public_address_classification`, `test_guarded_client_blocks_internal_targets_and_rebinding`, `test_guarded_client_does_not_follow_redirects` |
| Eligibility and net-price fee math | `test_market_config_and_mint_eligibility`, `test_fee_math_pays_exact_net_price` |

Configuration: `NFT_MARKET_DEV_MINTS` (comma-separated exact URLs) lets the
worker reach local development mints over HTTP. Leave it unset in production.
The executor starts with the app and polls every 5 seconds; it is restart-safe
because jobs, leases and outcomes are durable and every pass re-reads mint
state before acting.

## Phase 3 · Encrypted ordinary wallet, HTLC coordinator and browser client (2026-10-01)

| Module | Role |
| --- | --- |
| `src/money/compat.ts` | Coco 2.0.0 ↔ cashu-ts 5.0.0-rc.11 adapter (below) |
| `src/money/store.ts` | `EncryptedRepositories`: Coco `MemoryRepositories` persisted as one AES-GCM snapshot (local IndexedDB + server backup), typed codec, single-writer lease, client-numbered revisions |
| `src/money/wallet.ts` | `MoneyWallet`: add mint with capability/CORS check, per-mint balances (available, reserved, offer-locked, pending refund, pending claim, test-value flag), Lightning top-up (NUT-20 locked quote, auto-claim), token receive/send, Lightning withdrawal, lease takeover, visibility pause/resume, dispose |
| `src/market/protocol.ts` | Byte-compatible port of `market_protocol.py`: canonical JSON, manifest/listing/acceptance/receipt hashes, purpose-bound signatures, bound receive proof, delivery binding, SIG_ALL digest signing, fee math |
| `src/market/coordinator.ts` | Coco plugin `market`: listing (rotate first), funded offers, seller review/accept, reconciliation (refund/claim import, purchase recovery, independent refund path) on an encrypted journal |
| `src/market/api.ts` | Typed client for the market and money routes |
| `src/wallet/index.ts` | NFT wallet hooks: `rotate`, `presentForDelivery` (public presentation bound to `Deliver_v1 ‖ mhash ‖ S'`), `importPurchased`, `verify` |

### Compatibility observations (2026-10-01, Coco 2.0.0 with the cashu-ts rc.11 override)

1. Coco calls `wallet.createLockedMintQuote(amount, pubkey)`; rc.11 folds it
   into `createMintQuoteBolt11(amount, pubkey)`. It is the only one of the 12
   cashu-ts `Wallet` methods Coco calls that rc.11 lacks. Coco's unlocked path
   (`createMintQuoteBolt11(amount)`) is rejected by rc.11, which requires a lock key.
2. rc.11 passes custom request functions a pre-serialized `requestBody`
   string (so auth headers bind exact bytes); Coco's request function
   serializes again and sent a JSON string literal (`422` from the mint).
3. Nutshell reports quote `updated_at` in whole seconds. A FakeWallet mint
   (the dev mint, testnut) can pay a quote within the second it was created;
   Coco then sees new amounts at an unchanged update time, ignores them as
   "conflicting accounting", and the top-up never completes.

Decision: `installCocoCompat()` adds `createLockedMintQuote` only when
missing, parses the pre-serialized body with cashu-ts' big-int-safe `JSONInt`
before Coco's request function sees it, and drops `updated_at` from polled
bolt11 mint quotes so Coco uses its monotonic paid/issued rule. Each shim is
a no-op once the upstream signatures match. NUT-20 is part of the ordinary
wallet's mint requirements (NUT-4, 5, 7, 20); trading eligibility stays the
server's stricter check.

Store bug found and fixed while integrating: the repository proxy returned
read methods unbound, so Coco's internal synchronous helpers (`makeKey`)
went through the proxy, became async and produced `"[object Promise]"` map
keys. Methods now always run against the real repository; only Coco's
native async mutators persist (reads and sync helpers are bound directly).

### Journal discipline (browser side of the recovery procedures)

Every spend writes the complete operation (inputs, every output secret and
blinding factor, refund/claim keys, preimage, signed terms) to the encrypted
journal — locally and as a server recovery record (`market/recovery/put`,
AES-GCM under `moneyKey(secret, 'market-journal')`, AAD bound to profile and
record id) — before the remote call. Reconciliation runs from mint evidence:

| Record · stage | Evidence | Action |
| --- | --- | --- |
| offer · intent | funding inputs `UNSPENT` | release reservation → abandoned |
| offer · intent | funding inputs spent | NUT-09 restore of saved outputs → funded |
| offer · funded | — | sign refund (once), register (idempotent) → registered / rejected |
| offer · registered | `cash_leg = refunded` | import refund signatures into Coco → refunded |
| offer · registered | `nft_leg = delivered` | verify pinned receipt key and receipt fields, rebuild `(u, v_, h, s')`, verify credential, store encrypted, publish showing → purchased |
| offer · rejected | after `cash_deadline` | submit pre-signed refund directly (or restore if it ran) → refunded |
| sale · prepared | accept failed | → failed (claim outputs kept) |
| sale · accepted | claim job outcome `claim` | import claim signatures into Coco → paid |

Escrow and receipt keys are pinned on first use with the NFT keyset; a
change stops trading until reviewed.

### Server changes in this phase

- `money/lease` takes `release: true` from the holder (wallet closed on that
  device); a crashed tab still holds the lease for its 120 s TTL.
- New wallet operation kind `refresh` (`portfolio_wallet.prepare`): the same
  private credential rotation as `rotate`, but for an `owned` card; listing
  runs it first. `rotate` keeps its contract (cancel an exported `ready`
  card; `409` otherwise, as `test_cancel_invalidates_exported_jpg` asserts).
  Listed cards refuse `rotate`, `refresh` and `migrate`.

### Tests

`tests/test_nft_market_browser.py` runs the real browser modules
(`tests/market.e2e.mjs`) in separate Node processes, each with an empty
IndexedDB (a fresh browser), against the portfolio server (NFT mint, market,
executor) and the test Nutshell mint:

| Scenario | Evidence |
| --- | --- |
| Listing rotates the credential | `rotated` (showing changed) |
| Competing offer that expires; buyer offline | Bob's offer registered, `offerLocked` = 120 |
| Funding reply lost after the mint processed the swap | `reply_dropped`, NUT-09 restore, balances exact |
| Seller on a new device recovers the NFT vault, reviews (DLEQ, `UNSPENT`, amounts, window) and accepts | `review.ok`, `nft_leg = delivered`; second accept rejected |
| Executor claims; seller's new browser imports the payment | journal `sale · paid`, available = 100 |
| Losing offer refunds after its deadline; buyer's new browser imports it | journal `offer · refunded`, available = 500 |
| Buyer's new browser recovers the purchase and publishes; credential usable | card `owned`, `sendToken` → `psnft1…` |
| Public sale activity fields | exactly NFT, buyer, seller, names, time |
| Ordinary wallet: Lightning top-up, token send/receive between profiles | 100 → 79 / 21 |
| Single-writer lease: second device read-only, blocked writes, explicit takeover | `readOnly`, error, `tookOver` |

Later in this phase (2026-10-01):

- Relayed signatures are verified before import: executor claim/refund
  results and NUT-09 restores go through cashu-ts `verifyProofsForReceive`
  with `requireDleq: true`. A compromised relay can withhold, but can no longer
  make the wallet show ecash the mint never signed.
- The buyer's refund key and new owner secret `s'` are derived from the
  profile key and the offer id (`authorizationKey(secret, "refund:"|"receive:"
  + offer_id)`), not random. A purchase is recoverable even if every journal
  copy is lost (`test_browser_recovers_purchase_without_journal`). The refund
  outputs' blinding factors are still journal-only, so a lost journal still
  loses an *unclaimed refund* (see open items).

## Phase 5 · Marketplace and wallet UI (2026-10-01)

`src/market.jsx` (UI only; logic stays in `src/market/` and `src/money/`):

| Surface | What it does |
| --- | --- |
| Top bar | **Market** link everywhere (also on phones). **Profile** dropdown: Wallet, My collection, Offers with unread badge; on collection pages Explore, Activity and How it works move into it (on phones they always live there). The front page keeps its existing links. |
| `/market` | Listings with search and sort (newest, price low/high), sale-in-progress tag |
| `/market/{id}` | Listing detail, offer form: pay-from mint with balance and test-sats label, net offer ≥ ask, lifetime (6 h – 7 d), fee table (seller receives, claim fee, locked, refund amount, accept-by, refund-from), hold-to-fund, "Offer funded · safe to close this tab" |
| Card detail (owner) | List for sale (explains credential refresh), edit price (new offers only), unlist (declines pending offers), Send hidden while listed |
| `/offers` | Received / Made; separate badges for offer, NFT leg, cash leg and publication; full mint URL, test-sats label, deadlines while money is locked; Review dialog with live checks (DLEQ + `UNSPENT` + amounts + window, NFT present) → hold to accept and deliver, or decline |
| `/wallet` | Per-mint cards (available, in offers, refund pending, payment pending, reserved), add mint (shortcuts + custom URL with capability/CORS check), Lightning top-up, send token, receive token, withdraw (quote → hold to pay), "open on another device · Use it here" |
| Collection (owner) | "Purchased · awaiting wallet sync" tiles until the purchase is recovered and published, then the normal card |
| Inbox | Long-poll from a stored cursor (25 s while visible, 15 s polling while hidden); toasts per event kind; replays missed events on return; every event triggers reconciliation |
| Activity | `sale` events ("X bought Y from Z") link to the buyer's live card |

Found and fixed during browser acceptance:

- **Lease storm.** Every `visibilitychange` re-acquired the lease and
  re-read the backup (3 signed requests ≈ 6 HTTP requests). Rapid
  visibility changes exhausted the server's per-IP limit (240/min). Now a lease we
  hold is renewed only within 60 s of expiry without a backup re-read, and
  resume syncs at most every 30 s.
- **Unread badge** stayed set after opening Offers: "mark read" could run
  before the first poll stored a cursor. The cursor is now stored before the
  count is published, and Offers marks read whenever the count is non-zero.

## Phase 6 · Acceptance results (2026-10-01)

Automated (all passing on 2026-10-01):

| Suite | Result |
| --- | --- |
| `pytest tests/test_ps_*.py tests/test_nft_*.py` (NFT, portfolio, social, links, market protocol, market API, browser e2e) | 249 passed |
| `tests/test_nft_market_browser.py` (real browser modules in fresh Node processes) | 3 passed |
| `npm test` (`portfolio_web`: link, verify, wallet, money/protocol) | 26 passed |
| `npm run typecheck`, `vite build` | clean |
| `make check` (ruff + mypy `--check-untyped-defs` over `cashu/`) | clean |

Manual browser acceptance on a local stack (portfolio on `localhost:8411`,
dev mint on `127.0.0.1:3339`, fresh data dir, test keys only), phone width
and 1280 px desktop:

1. Market page, sentence-case labels, Market entry visible on phones.
2. Create a collection → Wallet → add the dev mint by URL (browser CORS and CSP
   OK) → top up 400 test sats (auto-paid) → balance 400.
3. Listing → Make an offer → fee table and both deadlines → hold to fund →
   "Offer funded"; Offers › Made shows waiting / NFT with seller / payment locked.
4. Switch to the seller (key import) → Offers › Received → Review: both checks
   green → hold to accept → Accepted · NFT delivered · Seller paid · Purchased ·
   awaiting wallet sync. The seller's wallet shows 150.
5. Switch back to the buyer → collection auto-syncs the purchase (toast "You
   bought Sunset…", card owned, browser custody, signed showing, "Verified
   owner"); public activity shows the sale; the listing disappeared.
6. Buyer relists it from the card (credential refreshed), edits the price (revision
   2), unlists; Send returns.
7. Profile dropdown on phones (Wallet, My collection, Offers + badge,
   Explore, Activity, How it works) and the desktop collection page (links
   moved into the dropdown).

Fault coverage across phases: lost swap reply (funding, claim), mint outage,
claim/refund race and hashlock-after-locktime, exact deadlines and clock skew
checks, wrong preimage/key/destination/binding/amount/keyset/DLEQ, two
simultaneous accepts, lease loss and takeover, browser restart (every
browser e2e step is a fresh process), journal loss, SSRF/redirect/rebinding.

Not covered automatically: a real third-party mint (no real funds were spent;
testnut requires network and is fake-value), web push/email (deferred by the
plan), and multiple tabs of one device racing a spend (Web Locks serialize
the NFT wallet; the money wallet relies on Coco's operation locks plus the
lease, untested across tabs).

## Setup and worker instructions

Local development:

```bash
poetry run python -m cashu.nft.dev_ecash_mint --port 3339
cd cashu/nft/portfolio_web && npm install && npm run build
NFT_MARKET_DEV_MINTS=http://127.0.0.1:3339 NFT_PORTFOLIO_PORT=8401 poetry run python -m cashu.nft.portfolio
```

- `NFT_MARKET_DEV_MINTS` (comma-separated exact URLs) is the only way the
  worker may reach HTTP/local mints. **Never set it in production.** The CSP
  `connect-src` adds the same URLs.
- The executor runs inside the portfolio process (`run_executor=True`,
  5 s interval). It is safe to restart at any time: jobs, leases (CAS on
  `lease_owner/lease_until`) and outcomes are durable, and every pass re-reads
  mint state (NUT-07) before acting. Running two processes against one data
  dir is safe for jobs (leases) but not supported for the portfolio itself.
- Schema migrates on startup (`Market.migrate`, `CREATE TABLE IF NOT
  EXISTS`); back up the data dir before upgrading. These tables and the
  `ps_nft_locks` ledger table need human review before production (AGENTS.md).
- Settlement keys (`esc1`, `rcpt1`) derive from the NFT mint seed; browsers pin
  them on first use. Rotating them means a new version label and keeping the
  old escrow key until every offer sealed to it has expired.

## Recovery procedures

| Situation | What recovers it |
| --- | --- |
| Browser closed after funding | Nothing to do: the offer and its refund job are on the server; the executor refunds after the deadline; the next open imports the refund. |
| New device / cleared storage | Unlock with the profile key. The wallet snapshot comes from the encrypted server backup (takeover after a fresh read), the journal from encrypted recovery records, the NFT vault from `wallet/recover`. Reconciliation imports refunds/claims and recovers purchases. |
| Lost reply while funding | `intent` record: inputs `UNSPENT` → released; spent → NUT-09 restore of the saved outputs. |
| Lost reply on claim/refund (executor) | Executor restores the fixed outputs (NUT-09) after seeing its branch won (NUT-07 witness). |
| Server never registered a funded offer | Journal `rejected`; after the deadline the browser submits the pre-signed refund itself (or restores it). |
| Journal lost everywhere | Purchases: recovered from the receipt with the re-derived `s'`. Refunds not yet imported: **not recoverable** (blinding factors are journal-only). |
| Mint down | Jobs retry with backoff up to 40 attempts, then `needs_attention` (inbox `payment_attention`). Offers stay "Payment pending", never "paid". |
| Wallet open on another device | Read-only here; "Use it here" takes over the lease after re-reading the latest backup. |

## Security review (concise, for the human reviewer)

Trust and guarantees:

- NFT-mint-assisted settlement: the NFT issuer holds escrowed preimages and
  could release one early. It already controls NFT issuance. The relay never
  holds wallet keys, NFT owner secrets or output blinding factors.
- Funds can't be redirected: claim and refund are SIG_ALL over exact
  inputs and blinded outputs; the executor only appends a witness
  (preimage). Keyset id is outside the digest; imported proofs are
  DLEQ-verified against the mint's keys.
- Delivery is bound: the seller's presentation binds
  `Deliver_v1 ‖ manifest hash ‖ S'`, the buyer's receive proof binds the
  manifest, and lock install, claim and nullifier spend happen in one
  transaction.

Points that need attention before production:

1. **Cryptography and schema need human review** (AGENTS.md): purpose
   domains, escrow envelope, receipt keys, ledger lock table, market tables.
2. **TOFU pins** (escrow, receipt, keyset) live in the profile's IndexedDB; a
   first contact with a malicious server is not protected.
3. **Browser storage model** is unchanged: the profile key sits in
   `localStorage`; same-origin script injection compromises everything. The
   encryption protects data at rest and server backups only.
4. **Rate limit**: signed requests cost two HTTP requests (challenge +
   call). Steady state is about 10 req/min per wallet, but bursts (open plus
   reconcile with many offers) can approach the 240/min per-IP limit behind
   shared NATs. Consider a batched reconcile endpoint or a session token.
5. **Refund outputs are random**. Deterministic (NUT-13-style) refund
   outputs would make refunds recoverable without the journal.
6. **SIG_ALL compatibility policy** (Nutshell ≥ 0.20.3, cdk ≥ 0.14.0) is a
   version allowlist, not a conformance test of the deployed mint.
7. **Compat shims** (`src/money/compat.ts`) patch library prototypes; pin
   versions and drop them once Coco and cashu-ts agree.
8. **Dev mint flags**: `NFT_MARKET_DEV_MINTS` disables the SSRF address checks
   for the listed URLs.

## Open items

- External test-mint run against testnut with the UI. Observing
  capabilities is safe; funding uses fake value. Not done here, to stay off
  third-party services unattended.
- Multi-tab money-wallet races (see above) and an automated browser (DOM)
  suite; the acceptance above was driven manually through the in-app browser.
- Early cooperative refunds, web push and email (deferred by the plan).

## Follow-up fixes and testnut run (2026-10-02)

Reported: inputs unreadable in dark mode; listing failed on the local
server with `422` from `wallet/prepare?kind=refresh`.

- **`422` on listing**: that server process predated the `refresh`
  operation kind (FastAPI rejected the unknown `kind`). Restarting it with
  the current code fixed it. Verified in the UI: list → `prepare?kind=refresh` 200
  → listed.
- **Dark-mode controls**: inputs inside light panels (`.pending-box`,
  `.notice`, `.keybox`, `.link-box`) inherited their forced `#111` text onto a
  dark `var(--bg)` field (contrast 1.01). Controls now set their own theme
  colours (`.field input/select/textarea`, `.input`, `.search`); `select`
  inherits like other controls. In the same panels the hold button (dark
  `--paper` with `#111` text, so "Hold to list" was invisible) and `.hint/.muted`
  text are fixed. Separately, How it works equations (`pre.eq.mono`) lost
  their dark background to `.how-article .mono` and showed lime on beige in
  light mode. Verified with an in-page contrast audit: every control in
  11 container types plus every route and the card/listing dialogs, dark and
  light, ≥ 4.5:1 for controls, ≥ 3:1 for text. Remaining flags were
  measurement artefacts (`color(srgb…)` card tints, transitions frozen in a
  hidden pane).

### Testnut, end to end (2026-10-02)

Observed: `cdk-mintd/0.17.0-rc.3`; NUT-7/9/10/11/12/14/20 supported; active
pre-v3 sat keyset `0184237e…` with `input_fee_ppk = 100`; CORS `*`. Eligible
under the compatibility policy. Fake value only.

- `test_testnut_end_to_end` (opt-in: `NFT_MARKET_TESTNUT=1`) runs the real
  browser modules against testnut. It covers eligibility, top-ups,
  funding with a lost reply (NUT-09 restore on cdk), review (cdk DLEQ),
  acceptance, the executor's claim (seller nets exactly 21 after fees),
  purchase recovery and use, and a competing offer refunded after its
  deadline (exact to the sat). Passing, ~45 s.
- Through the UI on `localhost:8411`: add Testnut from the shortcut, top up
  100 test sats, offer 64 (fee table: claim fee 1, locked 65, test-sats
  label), funded. The seller's review shows the test-sats warning and passing
  checks; accept → delivered → executor paid. The seller sees 64 test sats; the
  buyer's collection syncs the NFT ("Verified owner"); the buyer's testnut
  balance is 34 (100 − 65 − 1 funding fee).

Bugs found by the testnut run and fixed:

1. **Claimed ecash hidden when the seller never added the payment mint.**
   Coco saved the proofs under an untrusted mint, and its balances ignore those.
   Accepting after review now trusts the mint (`addMintByUrl(…, {trusted:
   true})`), as does every import. Reconciliation re-trusts mints of settled
   records, which repairs wallets already affected. The seller e2e phase no
   longer pre-adds the payment mint, so the local tests cover this path.
2. **Test harness**: Bob's expiring offer shifted `Date.now` process-wide,
   which stalls Coco's request rate limiter (it reads `Date.now`) against a
   remote mint. `makeOffer` takes an explicit `created` instead.

Totals on 2026-10-02: `pytest tests/test_ps_*.py tests/test_nft_*.py` with
`NFT_MARKET_TESTNUT=1`: 250 passed; `npm test`: 26 passed; typecheck, build
and `make check` clean.

## Onboarding to an offer, and Offers tabs (2026-10-02)

- **New user from an NFT**: "Start a collection to make an offer" (or Get
  started / Unlock on a listing page) → collection created → `/wallet?then=
  /market/<id>`. A guide banner shows the NFT, its asking price ("plus a
  small mint fee"), "add a mint, then top up or receive a token", and a
  Testnut hint. A finished top-up or a received token returns to
  `/market/<id>?offer=1`, which opens the offer form with the funded mint
  preselected. "Back to the NFT" / "Continue to offer" works at any time.
  The "Add ecash" button in the offer form uses the same return path. Only
  internal listing paths are accepted as return targets (no open redirect).
  Onboarding started from Wallet or Offers stays there.
- **Offers tab**: `/offers?tab=made|received`. After funding, "See my
  offers" opens Made; a seller's "See offers" opens Received; switching tabs
  updates the URL. Without a tab, the side with the most recent offer opens.
  Inbox toasts get a "View" action to the matching tab (buyer events → Made,
  seller events → Received, payment attention by job kind).
- Verified in the browser at phone width with testnut: listing "Orbit" →
  create collection → wallet guide → add Testnut → top up 40 → back on
  Orbit with the offer form open (fee table: 30 + 1 claim fee) → funded →
  "See my offers" → Made with the Orbit offer. `/offers` without a tab opened
  Made for that buyer and Received for a profile whose latest offer was a
  received one. The toast "View" action is covered by code review only.

## Product changes (2026-10-02)

- **Turnstile on uploads (2026-10-03).** When
  `NFT_PORTFOLIO_TURNSTILE_SITEKEY`/`_SECRET` are set, mint and receive
  preparations and profile-picture uploads need a fresh Cloudflare Turnstile
  token in `X-Turnstile-Token`, checked with siteverify after the request
  signature (403 if invalid, 503 if Cloudflare can't be reached). The browser
  renders an invisible widget off-screen only at upload time. Cloudflare's
  scripts are otherwise never loaded.
- **Content filtering (2026-10-03).** New NFTs, received transfer JPGs and
  profile pictures are scored by Marqo's nsfw-image-detection-384 (ONNX,
  CPU) before the server stores them. A score of 0.8 or more refuses the
  upload with 422 and records the image's SHA-256 and colour difference hash
  in `portfolio_rejected_images`; later uploads that match exactly or nearly
  are refused without the model. Cloudflare was considered and dropped: it
  has no NSFW filter for uploads, and its CSAM scanner needs the domain's DNS
  on Cloudflare.
- **Link previews (2026-10-03).** `/p/{pubkey}` and `/claim/{id}` return
  `index.html` with that page's title, description and OpenGraph/Twitter
  image, using only data the public API already returns. The images
  (`/api/og/p/{pk}.jpg`, `/api/og/claim/{id}.jpg`, 1200×630) are drawn with
  Pillow in the app's style from fonts vendored in `cashu/nft/fonts/`, and
  cached in memory by a version of what they show. In production, Caddy must
  route those two page paths to the app for crawlers to see the tags.
- **Sale price is public.** At the user's request, completed sales now show
  the sale price (the accepted offer's net price) in `/api/market/sales` and
  in `sale` activity events. This replaces the original rule that offer
  amounts are participant-only. The payment mint, proofs, deadlines and
  settlement details remain participant-only.
- **Pending transfers are private.** A card with an outstanding transfer
  JPG or link (`status = 'ready'`) is shown as `owned` on the public profile
  and in Explore. The owner's app reads pending card ids from the signed
  `POST /api/profiles/{pk}/cards/pending` and shows "Transfer pending" only
  to the owner.
- **Browsers without IndexedDB.** Mobile Safari with Lockdown Mode (and some
  in-app browsers) has no IndexedDB, and Coco's Dexie storage failed with
  "IndexedDB API missing". `src/storage.ts` probes IndexedDB once. Without it,
  the credential vault, the ecash wallet's local records and Coco's
  repositories run in memory. Every open restores from the encrypted server
  backups. In memory mode the ecash snapshot is pushed to the server about
  250 ms after each change, not every 30 s. The device id is kept in
  localStorage so a reload keeps its own lease. The owner's profile and the
  wallet page show a notice. localStorage access is wrapped so blocked
  storage cannot throw. Covered by `tests/storage.test.mjs`, which runs with
  no IndexedDB at all, and by a headless Chromium run with `indexedDB`
  removed: the collection was created, the wallet opened, and there were no
  console errors.
- **Deleting an NFT.** Owners can delete an NFT from its detail view (Delete,
  then hold to confirm). The browser wallet presents the card's current
  credential for the mint's burn binding (`Cashu_PS_Burn_v1`). The signed
  `POST /api/profiles/{pk}/wallet/cards/{id}/delete` burns the asset at the mint
  and, in the same transaction, removes:
  - every card for that JPG, including earlier owners' sent history;
  - its links, cover and sales records;
  - the JPG itself.

  Pending links and transfer JPGs die with the burn. Re-minting the same JPG
  is refused with "This JPG was deleted and can't be minted again." Deleting is
  refused while the NFT is listed or a sale is settling. The delete panel offers
  Unlist inline (unlisting already existed in the listing controls). Pictures
  that history still points at show a neutral placeholder. Covered by:
  - `test_delete_burns_nft_and_erases_jpg` and
    `test_delete_needs_the_current_credential`;
  - `test_listed_nft_cannot_be_deleted_until_unlisted`;
  - the browser end-to-end tests `test_browser_deletes_nft_and_voids_its_link`
    and `test_browser_refuses_to_delete_a_listed_nft`.
- **Bids are public.** At the user's request, offers are now shown publicly
  as bids, like on other NFT marketplaces. This replaces the earlier rule that
  offer amounts were participant-only.
  - `GET /api/market/listings/{id}/bids` lists every offer on a listing, highest
    first. Each bid shows the bidder, amount, time, expiry, the test-sats flag
    and a status: `open`, `expired`, `accepted`, `declined` or `closed`.
  - Listings carry a `bids` summary: open bids, distinct bidders and the top bid.
  - The activity feed has `bid` events ("Cy bid 240 sats on Harbour, 6am"),
    leaving out NFTs deleted since.
  - The payment mint, proofs, escrow, manifest and settlement legs stay
    participant-only.
  - UI: a Bids panel on listings for visitors (the seller keeps the actionable
    Offers panel) and a bid count on market cards.
  - Covered by `test_bids_are_public_highest_first_and_in_activity`. Checked on
    a local server: three bids placed through the browser wallet showed on the
    listing (highest first, top bid marked), in activity and on the market card.
- **Unlimited collections.** `NFT_PORTFOLIO_MAX_CARDS` is now opt-in. Unset, a
  collection has no card limit; image storage and JPG size are still capped,
  and unfinished wallet actions per profile stay bounded (100).

## Offer funding and coin reservations (2026-10-03)

A user's wallet kept reporting "The mint did not restore every funding
output yet" for one offer. Cause and fixes, all in
`src/market/coordinator.ts`:

- **Funding recovery trusted spent inputs.** When a funding swap failed,
  any spent input was taken as proof that the swap ran, and the saved outputs
  were restored. If another copy of the wallet had already spent an input,
  the mint rejected the swap, nothing could be restored, and the record stayed
  in `intent` for good (its amount still shown as locked). A swap is atomic,
  so the saved outputs now decide: none signed means the offer was never
  funded. Inputs spent elsewhere are marked spent, the rest are released,
  and the offer is abandoned with a plain reason. Inputs still `PENDING`
  wait for the next reconcile; a partial restore is reported as such.
- **Concurrent operations.** Offers, acceptances and reconciliation now run
  one at a time per wallet. Before, two offers started together could pick
  the same proofs (the second failed), and a reconcile could judge an offer
  whose swap was still in flight.
- **Reservations.** Coco's startup recovery releases every reservation it
  doesn't own, including `market:<offer id>`. Reconcile reserves an
  `intent` offer's inputs again before resolving it. Proofs selected but not
  used as swap inputs are released right after the swap is prepared, and are
  not re-saved with the change.
- **Outputs.** Offer, refund and claim outputs are random (never Coco's
  deterministic counter outputs), so they can't collide with the wallet's own
  outputs or with each other.

Covered by `test_browser_offer_with_ecash_spent_elsewhere` and
`test_browser_concurrent_offers_use_separate_ecash`; both fail on the old
coordinator.
