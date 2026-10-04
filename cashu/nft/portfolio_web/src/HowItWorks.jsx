import React from 'react';
import { motion } from 'motion/react';
import { ArrowRight } from 'lucide-react';
import { Button } from './ui.jsx';
import { Segmented } from './social.jsx';
import Basics from './HowBasics.jsx';

const SECTIONS = [
  ['jpg', 'The NFT is the JPG'],
  ['mint', 'Minting with a blind signature'],
  ['show', 'Proving ownership'],
  ['send', 'Transferring in zero knowledge'],
  ['trust', 'Keys, custody and trust'],
];

const Eq = ({ children }) => <pre className="eq mono">{children}</pre>;
const reveal = { initial: { opacity: 0, y: 14 }, whileInView: { opacity: 1, y: 0 }, viewport: { once: true, margin: '-60px' }, transition: { duration: .4, ease: 'easeOut' } };

function Cryptography({ onStart }) {
  return <>
    <div className="how-layout">
      <nav className="how-toc" aria-label="On this page">
        {SECTIONS.map(([id, label], i) => <a key={id} href={`#${id}`}><span className="mono">{String(i + 1).padStart(2, '0')}</span>{label}</a>)}
      </nav>
      <article className="how-article">
        <motion.section id="jpg" {...reveal}>
          <h2><span className="mono">01</span>The NFT is the JPG</h2>
          <p>An asset is identified by the hash of its exact bytes, reduced into the BLS12-381 scalar field:</p>
          <Eq>{'h = SHA-256("Cashu_PS_Asset_v1" ‖ len ‖ jpg) mod r'}</Eq>
          <p>Before minting, the app normalizes the file once: it applies the orientation flag, strips EXIF and XMP metadata and keeps the colour profile. The resulting bytes are what get hashed, stored and shown.</p>
          <p>Ownership is a <em>Pointcheval–Sanders</em> (PS) signature over two attributes: the asset hash <span className="mono">h</span> and an owner secret <span className="mono">s</span> that only the owner knows.</p>
          <Eq>{'σ = (u, v),   v = (x + yₕ·h + yₛ·s) · u'}</Eq>
          <p>Knowing <span className="mono">(σ, s)</span> means owning the NFT. To send it, the app writes the credential into one extra EXIF segment of the JPG. Remove that segment and you get back the exact bytes that hash to <span className="mono">h</span>, so the file proves which picture the credential belongs to.</p>
          <p className="aside">This is byte identity, not visual identity. A screenshot or re-encode of the same picture has a different <span className="mono">h</span> and carries no credential.</p>
        </motion.section>

        <motion.section id="mint" {...reveal}>
          <h2><span className="mono">02</span>Minting with a blind signature</h2>
          <p>The mint signs without learning the owner secret, and without receiving <span className="mono">h</span> in the clear.</p>
          <ol className="steps">
            <li>The mint opens a session and hands out a base <span className="mono">u = k·G₁</span>.</li>
            <li>Your browser picks <span className="mono">s</span> and a blinding scalar <span className="mono">t</span>, then sends three commitments and a single Schnorr proof that it knows <span className="mono">h, t, s</span> behind all of them:
              <Eq>{'D = h·G_asset          duplicate tag\nB = h·u + t·G₁         blinded asset\nS = s·G₁               owner commitment'}</Eq>
            </li>
            <li>The mint checks the proof, refuses a tag <span className="mono">D</span> it has seen before, and signs the blinded values:
              <Eq>{"v' = x·u + yₕ·B + k·yₛ·S"}</Eq>
            </li>
            <li>Your browser removes the blinding and verifies the result before storing it:
              <Eq>{"v = v' − t·Yₕ = (x + yₕ·h + yₛ·s)·u"}</Eq>
            </li>
          </ol>
          <p className="aside">The duplicate tag is deterministic, which is what lets the mint reject exact duplicates. It also means anyone holding a candidate JPG can test it against a tag. This portfolio publishes the image anyway, so here blinding protects the owner secret, not the picture.</p>
        </motion.section>

        <motion.section id="show" {...reveal}>
          <h2><span className="mono">03</span>Proving ownership</h2>
          <p>Profiles display a <em>showing</em>: a fresh, unlinkable presentation of the credential that anyone can verify, bound to the profile it appears on.</p>
          <ol className="steps">
            <li>Re-randomize the signature with a random <span className="mono">ρ</span>: <span className="mono">(u′, v′) = (ρ·u, ρ·v)</span>.</li>
            <li>Reveal <span className="mono">h</span>, the nullifier <span className="mono">N = s·G_null</span> and <span className="mono">s·u′</span>, with a Chaum–Pedersen proof that the same <span className="mono">s</span> sits behind both. The Fiat–Shamir challenge binds the proof to <span className="mono">profile ‖ h ‖ keyset</span>.</li>
            <li>The verifier checks the PS pairing equation:
              <Eq>{"e(v′, g̃) = e(u′, X̃ + h·Ỹₕ) · e(s·u′, Ỹₛ)"}</Eq>
            </li>
            <li>The profile key signs the showing with a BIP-340 Schnorr signature. Without that signature, a showing copied to another profile is rejected.</li>
            <li>Finally the browser asks the mint one question: is <span className="mono">N</span> spent? Unspent means <strong>Verified owner</strong>; spent means <strong>Transferred</strong>.</li>
          </ol>
          <p>Every check except the last runs in your browser, in a background worker. The mint never sees which profile you're looking at, only a nullifier.</p>
        </motion.section>

        <motion.section id="send" {...reveal}>
          <h2><span className="mono">04</span>Transferring in zero knowledge</h2>
          <p>Sending creates a <em>transfer JPG</em>: the image plus its credential. It's a bearer instrument. Whoever redeems it first owns the NFT.</p>
          <ol className="steps">
            <li>The receiving browser extracts the credential and checks <span className="mono">H(jpg without the segment) = h</span> before contacting anyone.</li>
            <li>It presents the old credential with <span className="mono">h</span> hidden inside a Pedersen-style commitment, and blinds the signature to match:
              <Eq>{"κ = h·Ỹₕ + o·g̃        v″ = v′ + o·u′\ne(v″, g̃) = e(u′, X̃ + κ) · e(s·u′, Ỹₛ)"}</Eq>
            </li>
            <li>A zero-knowledge proof shows that the <span className="mono">h</span> inside <span className="mono">κ</span> is the same <span className="mono">h</span> inside a new blinded request <span className="mono">B = h·u₂ + t·G₁</span>, for a fresh owner commitment <span className="mono">S′ = s′·G₁</span>.</li>
            <li>The mint verifies, marks the old <span className="mono">N</span> as spent in the same database transaction, and blind-signs the new credential. The first valid redemption wins; every later one fails.</li>
          </ol>
          <p>The mint learns neither the asset nor the new owner secret. On the sender's profile, the old nullifier now reads spent and the card moves to <strong>Sent</strong>.</p>
          <h3 className="how-sub">Sending with a link</h3>
          <p>Instead of a file you can send a link. Your browser encrypts the credential with AES-256-GCM under a fresh random key <span className="mono">K</span>, which is placed only in the link's <span className="mono">#fragment</span>. Browsers never send fragments to servers, so the platform stores ciphertext it cannot open.</p>
          <Eq>{'key = HKDF(K ‖ PBKDF2(password, salt, 600 000), "link id ‖ h")\nlink = /claim/<id>#base64url(K)'}</Eq>
          <p>With a password, the receiver needs both the link and the password. The ciphertext is bound to the link ID and the asset hash, and the receiving browser re-checks <span className="mono">H(jpg) = h</span> and the PS signature before claiming.</p>
          <p className="aside">Canceling is a transfer to yourself: a fresh <span className="mono">s′</span>, the old <span className="mono">N</span> spent, every exported file and link void.</p>
        </motion.section>

        <motion.section id="trust" {...reveal}>
          <h2><span className="mono">05</span>Keys, custody and trust</h2>
          <dl className="facts">
            <div><dt>Profile key</dt><dd>A secp256k1 key generated in your browser. It signs every owner request against a single-use challenge, and signs every showing. It is never sent to the server.</dd></div>
            <div><dt>Credentials</dt><dd>Kept in your browser, encrypted with AES-256-GCM under a key derived by HKDF from your profile key and the mint keyset. The mint stores only ciphertext backups; importing your key restores them.</dd></div>
            <div><dt>Mint identity</dt><dd>The keyset ID is a SHA-256 commitment to the mint's public parameters. Your browser pins it on first visit and refuses to continue if it changes.</dd></div>
            <div><dt>What you still trust</dt><dd>The mint can issue credentials it shouldn't and can refuse service. The JavaScript served to you could be malicious. This is experimental, unaudited cryptography.</dd></div>
          </dl>
        </motion.section>
        <div className="how-cta"><p>Ready to mint your first one?</p><Button variant="primary" icon={<ArrowRight size={16} />} onClick={onStart}>Create a collection</Button></div>
      </article>
    </div>
  </>;
}

export default function HowItWorks({ onStart, view = 'basics', onView }) {
  const crypto = view === 'cryptography';
  return <main className="page how">
    <header className="how-head">
      <span className="kicker">How it works</span>
      <h1>The NFT is the JPG.</h1>
      <p className="lead">{crypto
        ? 'Most NFTs are a token on a ledger that points to an image hosted somewhere else. An NFT on Nonfungible.cash is a credential carried inside the image file itself. This page explains the cryptography that makes that work, with just enough math to check it.'
        : 'Most NFTs are an entry on a public ledger that points to a picture stored somewhere else. An NFT on Nonfungible.cash is different: the picture carries its own ownership.'}</p>
      <Segmented id="how" value={view} onChange={(v) => onView?.(v)} options={[['basics', 'The basics'], ['cryptography', 'Cryptography']]} />
    </header>
    {crypto ? <Cryptography onStart={onStart} /> : <Basics onStart={onStart} onCrypto={() => { onView?.('cryptography'); window.scrollTo({ top: 0, behavior: 'smooth' }); }} />}
  </main>;
}
