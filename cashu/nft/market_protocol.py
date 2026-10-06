"""Marketplace protocol v1 (``cashu-nft-offer-1``): pure, stateless helpers.

See cashu/nft/MARKETPLACE_PLAN.md ("Protocol v1") for the transcript. This
module contains no I/O: canonical encodings, purpose-bound signatures, the
buyer's bound receive proof, HTLC condition checks for payment proofs, the
SIG_ALL digest, the preimage escrow envelope and signed delivery receipts.

Experimental cryptography: requires human review before production use.
"""

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from coincurve import PrivateKey as SecpPrivateKey
from coincurve import PublicKeyXOnly
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..core.crypto.bls import PublicKey
from ..core.crypto.ps import (
    G1,
    DlogEqProof,
    prove_dlog_eq,
    verify_dlog_eq,
)
from ..core.htlc import HTLCSecret
from ..core.nuts import nut10
from ..core.secret import SecretKind
from .nostr_sig import verify_signature

PROTOCOL = "cashu-nft-offer-1"
LISTING_PROTOCOL = "cashu-nft-listing-1"
ACCEPT_PROTOCOL = "cashu-nft-accept-1"
RECEIPT_PROTOCOL = "cashu-nft-delivery-1"

OFFER_DOMAIN = b"Cashu_NFT_Market_Offer_v1\n"
OFFER_SIG_DOMAIN = b"Cashu_NFT_Market_Offer_Sig_v1\n"
LISTING_DOMAIN = b"Cashu_NFT_Market_Listing_v1\n"
ACCEPT_DOMAIN = b"Cashu_NFT_Market_Accept_v1\n"
RECEIPT_DOMAIN = b"Cashu_NFT_Market_Receipt_v1\n"
RECEIVE_DST = b"Cashu_NFT_Market_Receive_v1"
DELIVER_BINDING = b"Cashu_NFT_Market_Deliver_v1"
ESCROW_INFO = b"Cashu_NFT_Escrow_v1"

ACCEPT_WINDOW = 3600  # acceptance closes this long before the cash deadline
CLOCK_SKEW = 300  # allowance between the NFT mint, workers and payment mints
MIN_LIFETIME = 2 * 3600  # an offer must leave room for acceptance and claim
MAX_LIFETIME = 7 * 24 * 3600
DEFAULT_LIFETIME = 24 * 3600
MAX_PRICE = 2_100_000_000_000_000  # 21M BTC in sats; bounds integer inputs


class ProtocolError(ValueError):
    """A message violates protocol v1; the caller maps it to a 4xx."""


# --- canonical encodings ---------------------------------------------------


def canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def _digest(domain: bytes, obj: Any) -> bytes:
    return hashlib.sha256(domain + canonical(obj)).digest()


def manifest_hash(manifest: Dict[str, Any]) -> bytes:
    return _digest(OFFER_DOMAIN, manifest)


def listing_hash(listing: Dict[str, Any]) -> bytes:
    return _digest(LISTING_DOMAIN, listing)


def acceptance_hash(acceptance: Dict[str, Any]) -> bytes:
    return _digest(ACCEPT_DOMAIN, acceptance)


def receipt_hash(receipt: Dict[str, Any]) -> bytes:
    return _digest(RECEIPT_DOMAIN, receipt)


def normalize_mint_url(url: str, *, allow_http: bool = False) -> str:
    """One identity per mint: lowercase scheme/host, keep the path prefix
    (e.g. ``/Bitcoin``), drop a trailing slash, reject credentials, queries
    and fragments. HTTP is only allowed for explicit local development."""
    if len(url) > 512:
        raise ProtocolError("invalid mint URL")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise ProtocolError("mint URL must use https")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ProtocolError("mint URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ProtocolError("mint URL must not contain a query or fragment")
    host = (parts.hostname or "").lower()
    if not host:
        raise ProtocolError("mint URL needs a host")
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    path = parts.path.rstrip("/")
    if "//" in path or any(seg in (".", "..") for seg in path.split("/")):
        raise ProtocolError("mint URL path is not canonical")
    return urlunsplit((scheme, netloc, path, "", ""))


# --- purpose-bound profile signatures --------------------------------------


def sign_purpose(domain: bytes, digest: bytes, secret: bytes) -> str:
    """BIP-340 Schnorr over SHA256(domain || digest) with a profile key."""
    return (
        SecpPrivateKey(secret)
        .sign_schnorr(hashlib.sha256(domain + digest).digest())
        .hex()
    )


# The label a signing extension shows for each purpose (nostr_sig.SIG_LABELS).
PURPOSES = {
    LISTING_DOMAIN: "listing",
    OFFER_SIG_DOMAIN: "offer",
    ACCEPT_DOMAIN: "accept",
}


def verify_purpose(domain: bytes, digest: bytes, signature: str, pubkey: str) -> bool:
    """A profile signature (raw or extension event, nostr_sig) over SHA256(domain || digest)."""
    if domain not in PURPOSES:
        return False
    return verify_signature(
        pubkey, PURPOSES[domain], hashlib.sha256(domain + digest).digest(), signature
    )


# --- buyer receive authorization ------------------------------------------


def prove_receive(s: int, mhash: bytes) -> Tuple[PublicKey, DlogEqProof]:
    """Proof of knowledge of the buyer's new owner secret, bound to one
    offer manifest so it cannot authorize any other receipt."""
    S = G1 * s
    return S, prove_dlog_eq([G1], [S], s, RECEIVE_DST, mhash)


def verify_receive(S: PublicKey, proof: DlogEqProof, mhash: bytes) -> bool:
    if S.is_infinity():
        return False
    return verify_dlog_eq([G1], [S], proof, RECEIVE_DST, mhash)


def delivery_binding(mhash: bytes, destination: PublicKey) -> bytes:
    """What the seller's public presentation must be bound to: this offer
    and its fixed destination, nothing else."""
    return DELIVER_BINDING + mhash + destination.format()


# --- payment HTLC conditions -----------------------------------------------


@dataclass(frozen=True)
class HtlcTerms:
    hashlock: str
    claim_pubkey: str
    refund_pubkey: str
    locktime: int


def htlc_terms(manifest: Dict[str, Any]) -> HtlcTerms:
    return HtlcTerms(
        hashlock=manifest["hashlock"],
        claim_pubkey=manifest["claim_pubkey"],
        refund_pubkey=manifest["refund_pubkey"],
        locktime=int(manifest["cash_deadline"]),
    )


def _tag_values(secret: HTLCSecret, name: str) -> List[str]:
    return [v for tag in secret.tags.root if tag[0] == name for v in tag[1:]]


ALLOWED_TAGS = {"pubkeys", "refund", "locktime", "sigflag", "n_sigs", "n_sigs_refund"}


def check_htlc_secret(raw_secret: str, terms: HtlcTerms) -> None:
    """Every payment proof must carry exactly the agreed HTLC: hashlock,
    single seller claim key, single buyer refund key, locktime, SIG_ALL."""
    try:
        condition = nut10.parse_spending_condition(raw_secret)
    except Exception as exc:
        raise ProtocolError("payment proof secret is not a NUT-10 secret") from exc
    if condition is None or condition.kind != SecretKind.HTLC.value:
        raise ProtocolError("payment proofs must be HTLCs")
    try:
        secret = HTLCSecret.from_secret(condition)
    except Exception as exc:
        raise ProtocolError("payment proof is not a valid HTLC") from exc
    if secret.data != terms.hashlock:
        raise ProtocolError("payment proof hashlock differs from the offer")
    names = {tag[0] for tag in secret.tags.root}
    if not names <= ALLOWED_TAGS:
        raise ProtocolError("payment proof has unexpected tags")
    if _tag_values(secret, "pubkeys") != [terms.claim_pubkey]:
        raise ProtocolError("payment proof claim key differs from the offer")
    if _tag_values(secret, "refund") != [terms.refund_pubkey]:
        raise ProtocolError("payment proof refund key differs from the offer")
    if secret.locktime != terms.locktime:
        raise ProtocolError("payment proof locktime differs from the offer")
    if _tag_values(secret, "sigflag") != ["SIG_ALL"]:
        raise ProtocolError("payment proofs must use SIG_ALL")
    for name in ("n_sigs", "n_sigs_refund"):
        if _tag_values(secret, name) not in ([], ["1"]):
            raise ProtocolError("payment proofs must need exactly one signature")


def check_payment_proofs(
    proofs: Sequence[Dict[str, Any]], manifest: Dict[str, Any]
) -> int:
    """Structural checks on offered proofs; returns their total amount.
    Authenticity (DLEQ) and live state are checked against the mint."""
    payment = manifest["payment"]
    if not proofs or len(proofs) > 64:
        raise ProtocolError("an offer needs between 1 and 64 payment proofs")
    terms = htlc_terms(manifest)
    seen = set()
    total = 0
    for proof in proofs:
        if proof.get("id") != payment["keyset_id"]:
            raise ProtocolError("payment proofs must use the offer's keyset")
        amount = proof.get("amount")
        if not isinstance(amount, int) or amount <= 0 or amount > MAX_PRICE:
            raise ProtocolError("invalid payment proof amount")
        secret = proof.get("secret")
        if not isinstance(secret, str) or secret in seen:
            raise ProtocolError("duplicate or missing payment proof secret")
        seen.add(secret)
        check_htlc_secret(secret, terms)
        total += amount
    if total != payment["amount"]:
        raise ProtocolError("payment proofs do not add up to the offer amount")
    return total


def sigall_message(
    inputs: Sequence[Tuple[str, str]], outputs: Sequence[Tuple[int, str]]
) -> str:
    """NUT-11 SIG_ALL message: Σ(secret‖C) ‖ Σ(amount‖B_)."""
    return "".join(s + c for s, c in inputs) + "".join(str(a) + b for a, b in outputs)


def sigall_digest(
    inputs: Sequence[Tuple[str, str]], outputs: Sequence[Tuple[int, str]]
) -> bytes:
    return hashlib.sha256(sigall_message(inputs, outputs).encode()).digest()


def verify_sigall(
    inputs: Sequence[Tuple[str, str]],
    outputs: Sequence[Tuple[int, str]],
    signature: str,
    pubkey: str,
) -> bool:
    """Nutshell/cashu-ts compatible SIG_ALL signature check (x-only or
    compressed secp256k1 pubkey)."""
    try:
        raw = bytes.fromhex(pubkey)
        key = PublicKeyXOnly(raw[1:] if len(raw) == 33 else raw)
        sig = bytes.fromhex(signature)
    except ValueError:
        return False
    return len(sig) == 64 and key.verify(sig, sigall_digest(inputs, outputs))


# --- settlement keys (derived from the NFT mint seed) ----------------------


def _derive(seed: bytes, label: bytes) -> bytes:
    return hmac.new(
        seed, b"Cashu_NFT_Settlement_Key_v1\n" + label, hashlib.sha256
    ).digest()


P256_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)


class EscrowKey:
    """P-256 ECDH key of the NFT mint's preimage escrow, one per version.

    Envelope (``esc1``): ephemeral P-256 key, shared secret through ECDH,
    HKDF-SHA256(salt = epk ‖ rpk, info = domain ‖ version ‖ manifest hash),
    AES-256-GCM with AAD = domain ‖ version ‖ manifest hash. The browser
    implements the same with WebCrypto."""

    def __init__(self, seed: bytes, version: str = "esc1"):
        self.version = version
        counter = 0
        scalar = 0
        while not 0 < scalar < P256_ORDER:
            scalar = int.from_bytes(
                _derive(seed, f"escrow/{version}/{counter}".encode()), "big"
            )
            counter += 1
        self._key = ec.derive_private_key(scalar, ec.SECP256R1())
        self.public_key = (
            self._key.public_key()
            .public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
            .hex()
        )

    def open(self, envelope: Dict[str, Any], mhash: bytes) -> bytes:
        if envelope.get("v") != self.version:
            raise ProtocolError("escrow envelope uses an unknown key version")
        try:
            epk = bytes.fromhex(envelope["epk"])
            nonce = bytes.fromhex(envelope["nonce"])
            ct = bytes.fromhex(envelope["ct"])
            peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), epk)
        except (KeyError, ValueError, TypeError) as exc:
            raise ProtocolError("malformed escrow envelope") from exc
        if len(nonce) != 12 or len(ct) != 32 + 16:
            raise ProtocolError("malformed escrow envelope")
        shared = self._key.exchange(ec.ECDH(), peer)
        key = _escrow_kdf(
            shared, epk, bytes.fromhex(self.public_key), self.version, mhash
        )
        try:
            return AESGCM(key).decrypt(nonce, ct, _escrow_aad(self.version, mhash))
        except Exception as exc:
            raise ProtocolError("escrow envelope does not decrypt") from exc


def _escrow_aad(version: str, mhash: bytes) -> bytes:
    return ESCROW_INFO + b"\n" + version.encode() + b"\n" + mhash


def _escrow_kdf(
    shared: bytes, epk: bytes, rpk: bytes, version: str, mhash: bytes
) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=epk + rpk,
        info=_escrow_aad(version, mhash),
    ).derive(shared)


def seal_preimage(
    public_key: str, version: str, preimage: bytes, mhash: bytes
) -> Dict[str, str]:
    """Client side of the escrow envelope (Python wallets and tests)."""
    rpk = bytes.fromhex(public_key)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), rpk)
    eph = ec.generate_private_key(ec.SECP256R1())
    epk = eph.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    key = _escrow_kdf(eph.exchange(ec.ECDH(), peer), epk, rpk, version, mhash)
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, preimage, _escrow_aad(version, mhash))
    return {"v": version, "epk": epk.hex(), "nonce": nonce.hex(), "ct": ct.hex()}


class ReceiptKey:
    """secp256k1 key that signs NFT delivery receipts (``rcpt1``)."""

    def __init__(self, seed: bytes, version: str = "rcpt1"):
        self.version = version
        self._key = SecpPrivateKey(_derive(seed, f"receipt/{version}".encode()))
        self.public_key = PublicKeyXOnly.from_secret(self._key.secret).format().hex()

    def sign(self, receipt: Dict[str, Any]) -> str:
        return self._key.sign_schnorr(receipt_hash(receipt)).hex()


def verify_receipt(receipt: Dict[str, Any], signature: str, public_key: str) -> bool:
    try:
        return PublicKeyXOnly(bytes.fromhex(public_key)).verify(
            bytes.fromhex(signature), receipt_hash(receipt)
        )
    except ValueError:
        return False


def preimage_matches(preimage: bytes, hashlock: str) -> bool:
    return len(preimage) == 32 and hmac.compare_digest(
        hashlib.sha256(preimage).hexdigest(), hashlock
    )
