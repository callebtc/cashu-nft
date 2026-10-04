import React, { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { ArrowRight, Download, KeyRound, Lock, Plus, ShieldX, ImageOff } from 'lucide-react';
import { checked, getJSON } from './api.mjs';
import { openLink } from './link.mjs';
import { Button, CheckRow, DrawnCheck, Identicon, Spinner, Tilt, useTint } from './ui.jsx';
import { imageUrl } from './formats.mjs';

const transferTools = () => Promise.all([import('./wallet/image.ts'), import('./wallet/ps.ts')]);
const toHex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');

// Decrypt and check a link's credential entirely in the browser before offering to claim it.
async function inspect(link, fragment, password, config) {
  const token = await openLink(link.envelope, { id: link.id, h: link.h, fragment, password });
  const [{ transferImage, splitImage }, ps] = await transferTools();
  const cred = ps.decodeToken(token);
  if (cred.h !== link.h) throw new Error('This link carries a credential for a different picture.');
  ps.verifyCredential(cred, config);
  const image = new Uint8Array(await (await checked(await fetch(imageUrl(link.h)))).arrayBuffer());
  if (splitImage(image).token || toHex(ps.integer(ps.hashAsset(image))) !== cred.h) throw new Error('The public picture doesn’t match this NFT.');
  const state = await (await checked(await fetch('/v1/nft/checkstate', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nullifiers: [ps.nullifier(cred)] }) }))).json();
  return { file: transferImage(image, token), unspent: state.states[0]?.state === 'UNSPENT' };
}

export default function ClaimPage({ linkId, config, identity, wallet, walletState, navigate, onCreate, onImport, onReceived }) {
  const [fragment] = useState(() => window.location.hash.slice(1));
  const [link, setLink] = useState(null), [error, setError] = useState('');
  const [password, setPassword] = useState(''), [unlocking, setUnlocking] = useState(false), [shake, setShake] = useState(0);
  const [ready, setReady] = useState(null), [badPassword, setBadPassword] = useState('');
  const [claiming, setClaiming] = useState(false), [claimed, setClaimed] = useState(null);
  const [tint, onLoad] = useTint(link?.h || linkId);
  const auto = useRef(false);

  useEffect(() => {
    getJSON(`/api/links/${linkId}`).then(setLink).catch((e) => setError(e.message));
  }, [linkId]);

  const unlock = async (pw) => {
    setUnlocking(true); setBadPassword('');
    try { setReady(await inspect(link, fragment, pw, config)); }
    catch (e) {
      if (link.protected && /password/.test(e.message)) { setBadPassword(e.message); setShake((s) => s + 1); }
      else setError(e.message);
    } finally { setUnlocking(false); }
  };
  useEffect(() => {
    if (!link || !config || link.status !== 'open' || link.protected || auto.current) return;
    auto.current = true;
    unlock('');
  }, [link, config]);

  const claim = async () => {
    setClaiming(true);
    try {
      const asset = await wallet.receive(ready.file, link.title);
      setClaimed(asset); onReceived(asset);
    } catch (e) { setError(e.message); } finally { setClaiming(false); }
  };

  const own = identity && link && identity.pubkey === link.sender;
  const head = link && <>
    <span className="pill">{link.protected ? <><Lock size={13} /> Password-protected NFT</> : 'Incoming NFT'}</span>
    <h1><span className="ellipsis">{link.sender_name || 'A collector'}</span> sent you <em>{link.title}</em></h1>
  </>;

  let body;
  if (error) body = <div className="claim-state is-bad"><ShieldX size={20} /><div><strong>Can’t claim this link</strong><p>{error}</p></div></div>;
  else if (!link) body = <div className="feed-loading"><Spinner /> Opening link…</div>;
  else if (claimed) body = <motion.div className="claim-done" initial={{ scale: .9, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: 'spring', stiffness: 300, damping: 18 }}>
    <span className="success-mark"><DrawnCheck on size={28} /></span>
    <h2>It’s yours.</h2>
    <p>The old credential is spent. <strong>{link.title}</strong> now lives in your collection with a fresh one only you know.</p>
    <Button variant="primary" size="lg" icon={<ArrowRight size={16} />} onClick={() => navigate(`/p/${identity.pubkey}?nft=${claimed.id}`)}>View in my collection</Button>
  </motion.div>;
  else if (link.status === 'claimed') body = <div className="claim-state"><DrawnCheck on size={20} /><div><strong>Already claimed</strong>
    <p>{link.claimed_by ? <>This NFT now belongs to <button className="link" onClick={() => navigate(`/p/${link.claimed_by.pubkey}`)}>{link.claimed_by.name || 'another collector'}</button>.</> : 'Someone already claimed this link.'}</p></div></div>;
  else if (link.status === 'void') body = <div className="claim-state is-bad"><ShieldX size={20} /><div><strong>This link was canceled</strong><p>The sender canceled the transfer, so this link no longer works. Ask them for a new one.</p></div></div>;
  else if (!fragment) body = <div className="claim-state is-bad"><ShieldX size={20} /><div><strong>This link is incomplete</strong><p>The part after the # is missing. Ask the sender to copy the full link again.</p></div></div>;
  else if (link.protected && !ready) body = <motion.form key={shake} className="unlock" onSubmit={(e) => { e.preventDefault(); unlock(password); }}
    animate={shake ? { x: [0, -10, 10, -6, 6, 0] } : undefined} transition={{ duration: .4 }}>
    <label className="field"><span>Password</span>
      <input type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="The password the sender gave you" autoFocus disabled={unlocking} />
    </label>
    <AnimatePresence>{badPassword && <motion.p className="field-error" initial={{ opacity: 0, y: -4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>{badPassword}</motion.p>}</AnimatePresence>
    <Button variant="primary" size="lg" className="full" type="submit" disabled={!password || unlocking} icon={unlocking ? <Spinner /> : <KeyRound size={16} />}>{unlocking ? 'Unlocking…' : 'Unlock NFT'}</Button>
    <p className="hint">Decryption happens in your browser. The password never leaves this page.</p>
  </motion.form>;
  else body = <div className="claim-ready">
    <ul className="checks">
      <CheckRow state={ready ? 'ok' : 'pending'}>Credential decrypted in your browser</CheckRow>
      <CheckRow state={ready ? 'ok' : 'pending'}>Matches this exact picture</CheckRow>
      <CheckRow state={!ready ? 'pending' : ready.unspent ? 'ok' : 'fail'}>{ready && !ready.unspent ? 'Already claimed' : 'Not claimed yet'}</CheckRow>
    </ul>
    {ready && !ready.unspent ? <p className="hint">Someone claimed it moments ago. Refresh to see who.</p>
      : own ? <p className="hint">This is your own link. Share it with the person you’re sending to. Claiming it yourself works like canceling.</p> : null}
    {ready?.unspent && (identity
      ? <Button variant="primary" size="lg" className="full" onClick={claim} disabled={claiming || !wallet} icon={claiming ? <Spinner /> : <Download size={16} />}>
        {claiming ? 'Claiming…' : !wallet ? (walletState === 'error' ? 'Wallet unavailable' : 'Opening your wallet…') : own ? 'Claim it back' : 'Add to my collection'}</Button>
      : <div className="stack">
        <Button variant="primary" size="lg" className="full" icon={<Plus size={16} />} onClick={onCreate}>Start a collection to claim it</Button>
        <button className="link" onClick={onImport}>I already have a key</button>
      </div>)}
  </div>;

  return <main className="page claim">
    <section className="claim-card">
      <div className="claim-art">
        <Tilt max={10}>
          <div className="nft-card is-preview has-tint" style={tint ? { '--tint': tint } : undefined}>
            <div className="nft-media">{link ? <img src={imageUrl(link.h)} alt={link.title} onLoad={onLoad} /> : <span className="media-empty"><ImageOff size={34} /></span>}
              {link?.protected && !ready && !claimed && <span className="media-tag"><Lock size={10} /> Locked</span>}</div>
            <div className="nft-body"><span className="nft-title">{link?.title || ' '}</span>
              <span className="nft-meta">{link && <span className="owner-chip"><Identicon pubkey={link.sender} size={18} /><span className="ellipsis">from {link.sender_name || 'a collector'}</span></span>}</span></div>
          </div>
        </Tilt>
      </div>
      <div className="claim-side">
        {head}
        <AnimatePresence mode="wait">
          <motion.div key={error ? 'e' : claimed ? 'c' : link?.status + String(!!ready)} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }} transition={{ duration: .2 }}>{body}</motion.div>
        </AnimatePresence>
      </div>
    </section>
    <p className="claim-foot muted">Transfer links carry the NFT’s credential encrypted. The key is in the part of the link after #, which your browser never sends to the server.</p>
  </main>;
}
