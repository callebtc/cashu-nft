import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Menu } from '@base-ui/react/menu';
import { AnimatePresence, MotionConfig, motion } from 'motion/react';
import { Toaster, toast } from 'sonner';
import { ArrowLeft, ArrowRight, Check, Compass, Monitor, Moon, Sun, Image as ImageIcon, Download, Ellipsis, Eye, EyeOff, FileJson, HandCoins, ImageDown, Info, KeyRound, Link2, Menu as MenuIcon, Plus,
  Pencil, Radio, RefreshCw, RotateCcw, Send, ShieldX, Store, Trash2, Upload, Undo2, Wallet } from 'lucide-react';
import '@fontsource-variable/inter';
import '@fontsource-variable/bricolage-grotesque';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import './style.css';
import { newPrivateKey, parseShowing, profileKey, validateKeyset } from './crypto.mjs';
import { checked, download, getJSON, signedRequest } from './api.mjs';
import { setTurnstileSitekey } from './turnstile.mjs';
import { BackButton, Button, CheckRow, SkeletonCards, CopyChip, DrawnCheck, HoldButton, Identicon, Modal, Notice, PreviewArt, StorageNotice, Spinner, StatusBadge,
  Tilt, copyText, date, identiconColor, panel, short, useTint, verdict } from './ui.jsx';
import HowItWorks from './HowItWorks.jsx';
import ClaimPage from './claim.jsx';
import { linkUrl, newLinkId, sealLink } from './link.mjs';
import { ListingControls, ListingPage, MarketPage, OffersPage, PendingPurchases, useMarket } from './market.jsx';
import { WalletPage } from './WalletPage.jsx';
import { local } from './storage.ts';
import { HOME, MOVED_FROM, MoveOffer, MoveReceive, MoveStranded, movePlan, redirectHome } from './move.jsx';
import { ActivityItem, ActivityList, ActivityPage, CollectionCard, EditCollectionDialog, ExplorePage, FollowButton, LikeButton, MarketCard,
  NetworkDialog, Segmented, useRelations } from './social.jsx';

const openWallet = (...args) => import('./wallet/index.ts').then((module) => module.openWallet(...args));
const transferTools = () => Promise.all([import('./wallet/jpg.ts'), import('./wallet/ps.ts')]);

const KEYRING = 'cashu-nft-keys-v1', ACTIVE = 'cashu-nft-active-v1', MINT_PIN = 'cashu-nft-mint-v1';
function storedKeys() {
  try { const keys = JSON.parse(local.get(KEYRING) || '{}'); return keys && typeof keys === 'object' && !Array.isArray(keys) ? keys : {}; }
  catch { return {}; }
}
function initialIdentity() {
  try {
    const keys = storedKeys(), active = local.get(ACTIVE);
    const secret = keys[active];
    return secret && profileKey(secret) === active ? { secret, pubkey: active } : null;
  } catch { return null; }
}
// Polling returns a fresh object even when nothing changed; keep the old one so
// cards don't re-render and proofs aren't re-verified every 15 seconds.
const keepSame = (prev, next) => prev && JSON.stringify(prev) === JSON.stringify(next) ? prev : next;
function routeKey() { return window.location.pathname.match(/^\/p\/([0-9a-f]{64})\/?$/)?.[1] || null; }
// Where onboarding/top-up returns to: only internal listing paths (no open redirect).
const safeReturn = (v) => (/^\/market\/[0-9a-f]{32}$/.test(v || '') ? v : null);
function readRoute() {
  const path = window.location.pathname, params = new URLSearchParams(window.location.search), nft = params.get('nft');
  const pubkey = routeKey();
  if (pubkey) return { page: 'profile', pubkey, nft };
  if (/^\/how-it-works\/?$/.test(path)) return { page: 'how', view: params.get('view') === 'cryptography' ? 'cryptography' : 'basics' };
  if (/^\/explore\/nfts\/?$/.test(path)) return { page: 'explore', tab: 'nfts' };
  if (/^\/explore\/?$/.test(path)) return { page: 'explore', tab: 'collections' };
  if (/^\/activity\/?$/.test(path)) return { page: 'activity' };
  if (/^\/market\/?$/.test(path)) return { page: 'market' };
  const listing = path.match(/^\/market\/([0-9a-f]{32})\/?$/);
  if (listing) return { page: 'listing', id: listing[1], offer: params.get('offer') === '1' };
  if (/^\/wallet\/?$/.test(path)) return { page: 'wallet', then: safeReturn(params.get('then')) };
  if (/^\/offers\/?$/.test(path)) return { page: 'offers', tab: ['received', 'made'].includes(params.get('tab')) ? params.get('tab') : null, offer: /^[0-9a-f]{32}$/.test(params.get('offer') || '') ? params.get('offer') : null };
  const claim = path.match(/^\/claim\/([0-9a-f]{32})\/?$/);
  if (claim) return { page: 'claim', id: claim[1] };
  return { page: 'home' };
}
const imageUrl = (h) => `/api/images/${h}.jpg`;
// A deleted NFT's JPG is gone, but sales, offers and old listings still name
// it: show a neutral placeholder rather than a broken image.
const MISSING_JPG = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="#ebe6d9"/><path d="M38 38 L62 62 M62 38 L38 62" stroke="#5d5a52" stroke-width="5" stroke-linecap="round"/></svg>');
window.addEventListener('error', (e) => {
  const img = e.target;
  if (img instanceof HTMLImageElement && img.src.includes('/api/images/')) img.src = MISSING_JPG;
}, true);
const tileIn = (i) => ({ initial: { opacity: 0, y: 16 }, whileInView: { opacity: 1, y: 0 }, viewport: { once: true, margin: '-40px' }, transition: { delay: i * .06, type: 'spring', stiffness: 220, damping: 26 } });

function useVerification(profile, config, refresh) {
  const [results, setResults] = useState({});
  const worker = useRef(null), run = useRef(0);
  useEffect(() => {
    worker.current = new Worker(new URL('./verify.worker.js', import.meta.url), { type: 'module' });
    const current = worker.current;
    return () => { current.terminate(); worker.current = null; };
  }, []);
  useEffect(() => {
    if (!profile || !config || !worker.current) { setResults({}); return; }
    const id = ++run.current, proofs = {}, controller = new AbortController();
    worker.current.onmessage = async ({ data }) => {
      if (data.id !== id || run.current !== id || controller.signal.aborted) return;
      if (!data.done) { proofs[data.cardId] = data.result; return; }
      const valid = Object.entries(proofs).filter(([, r]) => r.valid);
      try {
        for (let i = 0; i < valid.length; i += 1000) {
          const chunk = valid.slice(i, i + 1000);
          const response = await checked(await fetch('/v1/nft/checkstate', { method: 'POST', cache: 'no-store', signal: controller.signal,
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nullifiers: chunk.map(([, r]) => r.nullifier) }) }));
          const payload = await response.json();
          const byNullifier = new Map(payload.states.map((s) => [s.nullifier, s.state]));
          for (const [, result] of chunk) result.state = byNullifier.get(result.nullifier) || 'unknown';
        }
      } catch { for (const [, result] of valid) result.state = 'unknown'; }
      if (run.current === id && !controller.signal.aborted) setResults({ ...proofs });
    };
    // Remove previous proof badges immediately if the credential/context changed.
    setResults({});
    worker.current.postMessage({ id, cards: profile.cards, profile: profile.pubkey, config });
    return () => { controller.abort(); };
  }, [profile, config, refresh]);
  return results;
}

function Marquee({ items }) {
  const row = items.map((text, i) => <span key={i} className="marquee-item">{text}<span className="star" aria-hidden="true">✺</span></span>);
  return <div className="marquee" aria-label={items.join(', ')}><div className="marquee-track" aria-hidden="true">{row}{row}</div></div>;
}

/* ---------- Cards ---------- */

function NFTCard({ card, verification, onOpen, index = 0, isNew = false }) {
  const sent = card.status === 'sent';
  const [tint, onLoad] = useTint(card.h);
  return <motion.div className="card-slot" layout initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }}
    transition={{ delay: Math.min(index, 10) * .04, type: 'spring', stiffness: 260, damping: 26 }}>
    <Tilt className={isNew ? 'is-new' : ''}>
      <button className={`nft-card ${sent ? 'is-sent' : ''} ${tint ? 'has-tint' : ''}`} style={tint ? { '--tint': tint } : undefined}
        onClick={() => onOpen(card.id)} aria-label={`Open ${card.title}`}>
        <div className="nft-media">
          <img src={imageUrl(card.h)} alt="" loading="lazy" decoding="async" onLoad={onLoad} />
          {sent && <span className="media-tag">Sent</span>}
        </div>
        <div className="nft-body">
          <span className="nft-title">{card.title}</span>
          <span className="nft-meta"><StatusBadge result={verification} ready={card.status === 'ready'} /><span className="nft-date">{date(sent && card.sent ? card.sent : card.created)}</span></span>
        </div>
      </button>
    </Tilt>
  </motion.div>;
}

const PREVIEW_TINTS = ['#e8590c', '#1f6feb', '#2f9e44'];
function PreviewCard({ variant, title, note }) {
  return <Tilt max={12}>
    <div className="nft-card is-preview has-tint" style={{ '--tint': PREVIEW_TINTS[variant] }}>
      <div className="nft-media"><PreviewArt variant={variant} /><span className="media-tag">Preview</span></div>
      <div className="nft-body">
        <span className="nft-title">{title}</span>
        <span className="nft-meta"><span className="badge badge-good"><span className="badge-inner"><span className="dot" />Verified owner</span></span><span className="nft-date">{note}</span></span>
      </div>
    </div>
  </Tilt>;
}

/* ---------- Card detail with guarded send flow ---------- */

function CardDetail({ card, result, owner, busy, config, canSend: walletCanSend, onSend, onCancel, onDelete, onRename, isNew, market, nftWallet, onChanged, navigate }) {
  const [listed, setListed] = useState(null);
  const canSend = walletCanSend && !listed;
  const [flipped, setFlipped] = useState(false);
  const [view, setView] = useState('info');
  const [ack, setAck] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [method, setMethod] = useState('link'), [locked, setLocked] = useState(false);
  const [password, setPassword] = useState(''), [repeat, setRepeat] = useState(''), [result2, setResult2] = useState(null);
  const [unlisting, setUnlisting] = useState(false);
  const [naming, setNaming] = useState(null), [savingName, setSavingName] = useState(false);
  const saveName = async (event) => {
    event.preventDefault();
    const title = naming.trim();
    if (!title || title === card.title) { setNaming(null); return; }
    setSavingName(true);
    if (await onRename(card, title)) setNaming(null);
    setSavingName(false);
  };
  const unlist = async () => {
    setUnlisting(true);
    try { await market.money.api.unlist(listed.id); setListed(null); onChanged(); toast.success('Unlisted. Pending offers were declined and refund at their deadlines.'); }
    catch (e) { toast.error(e.message); } finally { setUnlisting(false); }
  };
  const resetSend = () => { setAck(false); setPassword(''); setRepeat(''); setLocked(false); setMethod('link'); };
  useEffect(() => { setView('info'); resetSend(); setResult2(null); setFlipped(false); setConfirmCancel(false); setListed(null); setNaming(null); }, [card.id]);
  useEffect(() => { if (!confirmCancel) return; const t = setTimeout(() => setConfirmCancel(false), 4000); return () => clearTimeout(t); }, [confirmCancel]);
  const sent = card.status === 'sent', ready = card.status === 'ready';
  const v = verdict(result, ready);
  const proofState = !result ? 'pending' : result.pending ? 'unknown' : result.valid ? 'ok' : 'fail';
  const liveState = !result?.valid ? (result ? 'unknown' : 'pending') : result.state === 'UNSPENT' ? 'ok' : result.state === 'SPENT' ? 'fail' : 'unknown';
  const nullifier = (() => { try { return short(Array.from(parseShowing(card.showing).presentation.slice(209, 257), (b) => b.toString(16).padStart(2, '0')).join(''), 10, 8); } catch { return ''; } })();

  const passwordOk = !locked || (password.length >= 6 && password === repeat);
  const send = async () => {
    const outcome = await onSend(card, { method, password: locked ? password : '' });
    if (outcome) { setResult2(outcome); setView('sent'); setPassword(''); setRepeat(''); }
  };
  const share = async () => {
    try { await navigator.share({ title: card.title, text: `I sent you "${card.title}" on Nonfungible.cash`, url: result2.url }); } catch { /* dismissed */ }
  };

  return <div className="detail">
    <div className="detail-art">
      <Tilt interactive flipped={flipped} className={isNew ? 'is-new' : ''} max={8}>
        <div className={`nft-card detail-face ${sent ? 'is-sent' : ''}`}>
          <div className="nft-media"><img src={imageUrl(card.h)} alt={card.title} /></div>
          <div className="nft-body"><span className="nft-title">{card.title}</span><span className="nft-meta"><span className="mono">{short(card.h, 6, 4)}</span><StatusBadge result={result} ready={ready} /></span></div>
        </div>
        <div className="detail-back" aria-hidden={!flipped}>
          <span className="kicker">Ownership proof</span>
          <dl className="mono">
            <div><dt>asset h</dt><dd>{short(card.h, 12, 10)}</dd></div>
            <div><dt>nullifier N</dt><dd>{nullifier}</dd></div>
            <div><dt>keyset</dt><dd>{short(config?.keyset_id || '', 10, 8)}</dd></div>
            <div><dt>profile sig</dt><dd>{card.signature ? short(card.signature, 10, 8) : 'pending'}</dd></div>
          </dl>
          <p>PS signature on (h, s), shown in zero knowledge and signed by the collector's profile key.</p>
        </div>
      </Tilt>
      <Button variant="ghost" size="sm" icon={<RotateCcw size={14} />} onClick={() => setFlipped((f) => !f)}>{flipped ? 'Show front' : 'Flip card'}</Button>
    </div>

    <div className="detail-side">
      <AnimatePresence mode="wait" initial={false}>
        {view === 'info' && <motion.div key="info" className="detail-panel" {...panel}>
          <div className="detail-head">
            <StatusBadge result={result} ready={ready} large />
            {naming !== null
              ? <form className="rename" onSubmit={saveName}>
                <input className="input rename-input" aria-label="NFT name" value={naming} maxLength={80} autoFocus disabled={savingName}
                  onChange={(e) => setNaming(e.target.value)} onFocus={(e) => e.target.select()}
                  onKeyDown={(e) => { if (e.key === 'Escape') { e.stopPropagation(); setNaming(null); } }} />
                <div className="row">
                  <Button type="submit" variant="primary" size="sm" disabled={savingName || !naming.trim()} icon={savingName ? <Spinner size={14} /> : <Check size={14} />}>Save</Button>
                  <Button type="button" variant="ghost" size="sm" disabled={savingName} onClick={() => setNaming(null)}>Cancel</Button>
                </div>
              </form>
              : <div className="title-row">
                <h2>{card.title}</h2>
                {owner && !sent && <button type="button" className="icon-btn rename-btn" aria-label="Rename" title="Rename" disabled={!!busy} onClick={() => setNaming(card.title)}><Pencil size={15} /></button>}
              </div>}
            <p className="muted">{sent ? `Transferred ${card.sent ? date(card.sent) : ''}. Ownership has moved to a new credential.` : `Minted ${date(card.created)}`}</p>
          </div>
          <ul className="checks">
            <CheckRow state={proofState}>Ownership proof is valid</CheckRow>
            <CheckRow state={proofState}>Signed by this collector</CheckRow>
            <CheckRow state={liveState} detail={result?.state === 'SPENT' ? 'This copy has been passed on' : 'Checked live with the mint'}>{result?.state === 'SPENT' ? 'No longer held here' : 'Still held by this collector'}</CheckRow>
          </ul>
          <dl className="props">
            <div><dt>Asset hash</dt><dd><CopyChip value={card.h} message="Asset hash copied" /></dd></div>
            <div><dt>Collector</dt><dd><CopyChip value={card.pubkey} message="Public key copied" /></dd></div>
          </dl>

          {owner && !sent && ready && <div className="pending-box">
            <strong>Transfer pending</strong>
            <p>A transfer link or JPG exists for this NFT. Whoever claims it first becomes the owner. Cancel to void every link and file.</p>
            <div className="row">
              <Button variant="secondary" icon={<Send size={15} />} disabled={!!busy || !canSend} onClick={() => { resetSend(); setView('send'); }}>Send again</Button>
              <Button variant={confirmCancel ? 'danger' : 'secondary'} icon={<Undo2 size={15} />} disabled={!!busy}
                onClick={() => { if (confirmCancel) { setConfirmCancel(false); onCancel(card); } else setConfirmCancel(true); }}>
                {confirmCancel ? 'Confirm cancel' : 'Cancel transfer'}
              </Button>
            </div>
          </div>}
          {owner && !sent && !ready && !listed && <Button variant="primary" size="lg" className="full" icon={<Send size={16} />} disabled={!!busy || !canSend} onClick={() => setView('send')}>Send NFT</Button>}
          {owner && !sent && !walletCanSend && !busy && <p className="hint">Your wallet is still syncing this NFT.</p>}
          {owner && !sent && market && <ListingControls card={card} market={market} nftWallet={nftWallet} busy={busy} onChanged={onChanged} navigate={navigate} onListing={setListed} />}

          <div className="detail-links">
            <Button variant="ghost" size="sm" icon={<ImageDown size={14} />} onClick={() => { const a = document.createElement('a'); a.href = imageUrl(card.h); a.download = `cashu-${card.h.slice(0, 12)}.jpg`; a.click(); }}>Save image</Button>
            {owner && !sent && <Button variant="ghost" size="sm" className="delete-link" icon={<Trash2 size={14} />} disabled={!!busy} onClick={() => setView('delete')}>Delete</Button>}
            <Button variant="ghost" size="sm" icon={<FileJson size={14} />} onClick={() => download(new Blob([JSON.stringify({ ...card, mint: { keyset_id: config.keyset_id, public_key: config.public_key } }, null, 2)], { type: 'application/json' }), `cashu-proof-${card.h.slice(0, 12)}.json`)}>Public proof</Button>
          </div>
          <p className="hint">Saved images and proofs contain no transfer credential.</p>
        </motion.div>}

        {view === 'send' && <motion.div key="send" className="detail-panel" {...panel}>
          <BackButton onClick={() => setView('info')} disabled={!!busy} />
          <h2>Send this NFT</h2>
          <Segmented id={`send-${card.id}`} value={method} onChange={(m) => { if (!busy) setMethod(m); }} options={[['link', 'Share a link'], ['file', 'Transfer JPG']]} />
          <AnimatePresence mode="wait" initial={false}>
            <motion.div key={method} className="stack" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: .18 }}>
              {method === 'link' ? <>
                <p className="muted">Creates a link anyone can open in a browser. The NFT’s credential travels inside it, encrypted. There’s no recipient address: whoever claims the link first owns the NFT.</p>
                <label className="ack">
                  <input type="checkbox" checked={locked} onChange={(e) => setLocked(e.target.checked)} disabled={!!busy} />
                  <span className="ack-box"><DrawnCheck on={locked} size={14} /></span>
                  <span><strong>Protect with a password.</strong> The receiver needs the link and the password. Share them over different channels.</span>
                </label>
                <AnimatePresence initial={false}>{locked && <motion.div className="pw-fields" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }}>
                  <label className="field"><span>Password</span><input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="At least 6 characters" disabled={!!busy} /></label>
                  <label className="field"><span>Repeat password</span><input type="password" autoComplete="new-password" value={repeat} onChange={(e) => setRepeat(e.target.value)} disabled={!!busy} /></label>
                  {repeat && password !== repeat && <p className="field-error">The passwords don’t match.</p>}
                  <p className="hint">There’s no way to recover a forgotten password, but you can always cancel the transfer.</p>
                </motion.div>}</AnimatePresence>
              </> : <>
                <p className="muted">Creates a transfer JPG: this picture with its ownership credential inside. Whoever adds the file to their collection first owns the NFT.</p>
                <ul className="send-facts">
                  <li><span className="mono">01</span>Send it as a file or document. Screenshots, edits and chat-app compression remove the credential.</li>
                  <li><span className="mono">02</span>Treat the file like cash. Anyone who gets a copy can claim it.</li>
                </ul>
              </>}
            </motion.div>
          </AnimatePresence>
          <label className="ack">
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} disabled={!!busy} />
            <span className="ack-box"><DrawnCheck on={ack} size={14} /></span>
            <span>I understand that anyone holding this {method === 'link' ? (locked ? 'link and password' : 'link') : 'file'} can take ownership of <strong>{card.title}</strong> until I cancel.</span>
          </label>
          {busy ? <Button variant="primary" size="lg" className="full" disabled icon={<Spinner />}>{busy}</Button>
            : <HoldButton disabled={!ack || !canSend || !passwordOk} onComplete={send} icon={method === 'link' ? <Link2 size={16} /> : <Send size={16} />}>{method === 'link' ? 'Hold to create link' : 'Hold to create transfer JPG'}</HoldButton>}
          <p className="hint" id="hold-hint">Press and hold to confirm. Releasing early cancels.</p>
        </motion.div>}

        {view === 'delete' && <motion.div key="delete" className="detail-panel" {...panel}>
          <BackButton onClick={() => setView('info')} disabled={!!busy} />
          <h2>Delete this NFT</h2>
          <p className="muted">The mint burns it for good and the picture is removed from Nonfungible.cash, including earlier owners’ history.{ready ? ' The pending link or transfer JPG stops working.' : ''} You can’t mint this exact JPG again.</p>
          {listed ? <Notice action={listed.state === 'active' ? <Button size="sm" variant="secondary" disabled={!!busy || unlisting || !market?.money}
              icon={unlisting ? <Spinner /> : null} onClick={unlist}>Unlist</Button> : null}>
              {listed.state === 'active' ? 'This NFT is listed on the market. Unlist it first: pending offers are declined and refund at their deadlines.' : 'A sale of this NFT is settling, so it can’t be deleted.'}
            </Notice>
            : !walletCanSend ? <p className="hint">Your wallet is still syncing this NFT.</p> : null}
          <HoldButton danger disabled={!!busy || !!listed || !walletCanSend} onComplete={() => onDelete(card)} icon={<Trash2 size={16} />}>Hold to delete</HoldButton>
        </motion.div>}

        {view === 'sent' && <motion.div key="sent" className="detail-panel" {...panel}>
          <motion.span className="success-mark" initial={{ scale: .6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: 'spring', stiffness: 400, damping: 18 }}><DrawnCheck on size={26} /></motion.span>
          {result2?.url ? <>
            <h2>{result2.protected ? 'Your locked link is ready' : 'Your link is ready'}</h2>
            <div className="link-box">
              <code className="mono">{result2.url}</code>
              <div className="row">
                <CopyChip value={result2.url} display="Copy link" message="Link copied" />
                {typeof navigator !== 'undefined' && navigator.share && <Button variant="ghost" size="sm" icon={<Send size={14} />} onClick={share}>Share</Button>}
              </div>
            </div>
            <p className="muted">{result2.protected ? 'Send the password separately, ideally through a different app. ' : 'Anyone with this link can claim the NFT. '}Copy it now: for safety the link isn’t stored anywhere, not even here.</p>
          </> : <>
            <h2>Transfer JPG saved</h2>
            <p className="muted">Send <span className="mono">cashu-transfer-{card.h.slice(0, 12)}.jpg</span> to the new owner as a file. They open their collection, choose <strong>Add JPG</strong> and drop it in.</p>
          </>}
          <p className="muted">This card stays in your collection as <strong>Transfer pending</strong> until it’s claimed.</p>
          <Button variant="secondary" className="full" onClick={() => setView('info')}>Done</Button>
        </motion.div>}
      </AnimatePresence>
    </div>
  </div>;
}

/* ---------- Unified add dialog: mint a new JPG or receive a transfer JPG ---------- */

function AddDialog({ open, close, config, wallet, onAdded }) {
  const [file, setFile] = useState(null), [preview, setPreview] = useState('');
  const [bytes, setBytes] = useState(null), [kind, setKind] = useState(null), [check, setCheck] = useState(null);
  const [title, setTitle] = useState(''), [dragging, setDragging] = useState(false), [working, setWorking] = useState('');
  const inspection = useRef(0);
  const reset = () => { setFile(null); setBytes(null); setKind(null); setCheck(null); setTitle(''); setWorking(''); };
  useEffect(() => { if (!open) reset(); }, [open]);
  useEffect(() => {
    if (!file) { setPreview(''); return; }
    const url = URL.createObjectURL(file); setPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  const limit = config?.max_jpg_bytes || 10485760;

  const choose = async (next) => {
    if (!next || working) return;
    if (!/\.jpe?g$/i.test(next.name) && next.type !== 'image/jpeg') { toast.error('Choose a JPG file.'); return; }
    if (next.size > limit + 65536) { toast.error(`Choose a JPG under ${Math.round(limit / 1048576)} MB.`); return; }
    const id = ++inspection.current;
    const data = new Uint8Array(await next.arrayBuffer());
    let found;
    try { const [{ splitJpg }] = await transferTools(); found = splitJpg(data); }
    catch (e) { toast.error(e.message || 'This file is not a readable JPG.'); return; }
    if (id !== inspection.current) return;
    setFile(next); setBytes(data); setTitle(next.name.replace(/\.jpe?g$/i, '').replace(/^cashu-transfer-[0-9a-f]+$/, 'Received JPG').slice(0, 80));
    if (!found.token) {
      if (data.length > limit) { toast.error(`Choose a JPG under ${Math.round(limit / 1048576)} MB.`); setFile(null); return; }
      setKind('mint'); setCheck(null); return;
    }
    setKind('receive'); setCheck({ state: 'checking' });
    try {
      const [, ps] = await transferTools();
      const cred = ps.decodeToken(found.token);
      if (cred.h !== Array.from(ps.integer(ps.hashAsset(found.jpg)), (b) => b.toString(16).padStart(2, '0')).join('')) { setCheck({ state: 'mismatch' }); return; }
      try { ps.verifyCredential(cred, config); } catch (e) { setCheck({ state: 'invalid', error: e.message }); return; }
      const response = await checked(await fetch('/v1/nft/checkstate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ nullifiers: [ps.nullifier(cred)] }) }));
      const state = (await response.json()).states[0]?.state;
      if (id === inspection.current) setCheck({ state: state === 'UNSPENT' ? 'claimable' : 'claimed', h: cred.h });
    } catch (e) { if (id === inspection.current) setCheck({ state: 'invalid', error: e.message }); }
  };

  const submit = async (event) => {
    event.preventDefault();
    if (!bytes || !kind || !wallet || working) return;
    setWorking(kind === 'mint' ? 'Minting' : 'Receiving');
    try {
      const asset = await wallet[kind](bytes, title.trim() || (kind === 'mint' ? 'Untitled' : 'Received JPG'));
      onAdded(asset, kind);
    } catch (e) { toast.error(e.message); setWorking(''); }
  };

  const receiveBlocked = kind === 'receive' && check?.state !== 'claimable';
  return <Modal open={open} close={() => { if (!working) close(); }} title="Add a JPG"
    description="Drop any JPG to mint it as a new NFT. Drop a transfer JPG to receive the NFT inside it.">
    <form onSubmit={submit} className="add-form">
      <AnimatePresence mode="wait" initial={false}>
        {!file ? <motion.label key="drop" {...panel} className={`dropzone ${dragging ? 'is-dragging' : ''}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)}
          onDrop={(e) => { e.preventDefault(); setDragging(false); choose(e.dataTransfer.files[0]); }}>
          <input type="file" accept="image/jpeg,.jpg,.jpeg" onChange={(e) => choose(e.target.files[0])} aria-label="Choose a JPG" />
          <motion.span className="dropzone-icon" animate={{ y: dragging ? -4 : 0, scale: dragging ? 1.08 : 1 }} transition={{ type: 'spring', stiffness: 400, damping: 20 }}><Upload size={20} /></motion.span>
          <strong>{dragging ? 'Release to add' : 'Drop a JPG here'}</strong>
          <span className="muted">or click to browse · up to {Math.round(limit / 1048576)} MB</span>
        </motion.label>
          : <motion.div key="review" {...panel} className="review">
            <div className="review-file">
              <motion.img src={preview} alt="" initial={{ scale: .92, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: 'spring', stiffness: 300, damping: 24 }} />
              <div>
                <span className={`kind-tag ${kind === 'receive' ? 'is-receive' : ''}`}>{kind === 'receive' ? 'Transfer JPG detected' : 'New JPG'}</span>
                <strong className="ellipsis">{file.name}</strong>
                <span className="muted">{(file.size / 1024).toFixed(0)} KB</span>
                {!working && <button type="button" className="link" onClick={reset}>Choose another file</button>}
              </div>
            </div>
            {kind === 'receive' && <div className={`receive-check is-${check?.state}`}>
              {check?.state === 'checking' && <><Spinner size={14} /><span>Checking the credential against this picture…</span></>}
              {check?.state === 'claimable' && <><Check size={14} /><span>Credential matches this picture and hasn't been claimed. Receiving moves it to a fresh credential only you know.</span></>}
              {check?.state === 'claimed' && <><ShieldX size={14} /><span>This transfer was already claimed or canceled. Nothing can be received.</span></>}
              {check?.state === 'mismatch' && <><ShieldX size={14} /><span>The embedded credential belongs to a different picture. The file was edited or re-encoded.</span></>}
              {check?.state === 'invalid' && <><ShieldX size={14} /><span>{check.error || 'The embedded credential is invalid.'}</span></>}
            </div>}
            {kind === 'mint' && <p className="hint">Minting is free. The app strips metadata, then commits to the exact bytes. An identical file can only be minted once.</p>}
            <label className="field"><span>Title</span>
              <input maxLength={80} value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Name this NFT" disabled={!!working} />
            </label>
            <Button variant="primary" size="lg" className="full" type="submit" disabled={!!working || !wallet || receiveBlocked}
              icon={working ? <Spinner /> : kind === 'receive' ? <Download size={16} /> : <Plus size={16} />}>
              {working ? `${working}…` : kind === 'receive' ? 'Receive NFT' : 'Mint NFT'}
            </Button>
          </motion.div>}
      </AnimatePresence>
    </form>
  </Modal>;
}

/* ---------- App ---------- */

/* System / light / dark. theme.js applies the choice before first paint. */
const THEMES = [['system', 'System', Monitor], ['light', 'Light', Sun], ['dark', 'Dark', Moon]];
// Top bar destinations: [href, label, route page, icon]. Market always stays in the bar; the rest
// fold into a menu on compact screens and on collection pages.
const NAV = [['/market', 'Market', 'market', Store], ['/explore', 'Explore', 'explore', Compass], ['/activity', 'Activity', 'activity', Radio], ['/how-it-works', 'How it works', 'how', Info]];
function useTheme() {
  const api = typeof window !== 'undefined' ? window.cashuTheme : null;
  const [state, setState] = useState(() => ({ mode: api?.get() || 'system', dark: api?.isDark() || false }));
  useEffect(() => {
    const on = (e) => setState({ mode: e.detail.mode, dark: e.detail.dark });
    window.addEventListener('cashu-theme', on);
    return () => window.removeEventListener('cashu-theme', on);
  }, []);
  return [state, (mode) => api?.set(mode)];
}
// On phones the theme button gives its room to the wordmark; the choices move into the menu.
function ThemeItems({ theme, setTheme }) {
  return <>
    <Menu.Separator className="menu-sep menu-phone" />
    {THEMES.map(([mode, label, Icon]) => <Menu.Item key={mode} className="menu-item menu-phone" onClick={() => setTheme(mode)}>
      <Icon size={15} />{label} theme{theme.mode === mode && <Check size={15} className="menu-check" />}
    </Menu.Item>)}
  </>;
}

function ThemeMenu({ theme, setTheme }) {
  const Current = THEMES.find(([m]) => m === theme.mode)?.[2] || Monitor;
  return <Menu.Root>
    <Menu.Trigger className="icon-btn theme-trigger" aria-label={`Theme: ${theme.mode}`}><Current size={17} /></Menu.Trigger>
    <Menu.Portal><Menu.Positioner className="menu-layer" sideOffset={6} align="end"><Menu.Popup className="menu menu-compact">
      {THEMES.map(([mode, label, Icon]) => <Menu.Item key={mode} className="menu-item" onClick={() => setTheme(mode)}>
        <Icon size={15} />{label}{theme.mode === mode && <Check size={15} className="menu-check" />}
      </Menu.Item>)}
    </Menu.Popup></Menu.Positioner></Menu.Portal>
  </Menu.Root>;
}

function SignInFirst({ onStart }) {
  return <main className="page"><div className="empty"><strong>Start a collection first</strong><span className="muted">Your wallet and offers belong to your collection key.</span><Button variant="primary" onClick={onStart}>Get started</Button></div></main>;
}

function App() {
  const [identity, setIdentity] = useState(initialIdentity);
  const [route, setRoute] = useState(readRoute);
  const pubkey = route.page === 'profile' ? route.pubkey : null;
  const [config, setConfig] = useState(null), [fatal, setFatal] = useState('');
  const [profile, setProfile] = useState(null), [loading, setLoading] = useState(false), [profileError, setProfileError] = useState('');
  const [dialog, setDialog] = useState(null), [selected, setSelected] = useState(null), [tab, setTab] = useState('collection');
  const [busy, setBusy] = useState(''), [refresh, setRefresh] = useState(0), [visitor, setVisitor] = useState(false);
  const [generated, setGenerated] = useState(''), [inputKey, setInputKey] = useState(''), [name, setName] = useState(''), [backedUp, setBackedUp] = useState(false);
  const [openInput, setOpenInput] = useState(''), [fresh, setFresh] = useState(null);
  const [localWallet, setLocalWallet] = useState(null), [walletState, setWalletState] = useState('opening'), [walletError, setWalletError] = useState('');
  const [relations, toggleRelation] = useRelations(identity);
  const [theme, setTheme] = useTheme();
  const navRef = useRef(null);
  const market = useMarket(identity, localWallet, (path) => navRef.current?.(path));
  const [home, setHome] = useState(null);
  const migrationAttempts = useRef(new Set());
  const recovery = useRef(false), reloadRef = useRef(null);
  const owner = Boolean(identity && identity.pubkey === pubkey && !visitor);
  const verification = useVerification(profile, config, refresh);
  const card = profile?.cards.find((c) => c.id === selected);
  const active = profile?.cards.filter((c) => c.status !== 'sent') || [];
  const sent = profile?.cards.filter((c) => c.status === 'sent') || [];
  const shown = tab === 'sent' ? sent : active;
  const openCard = (item) => navigate(item.kind === 'bid' ? `/market/${item.listing_id}` : `/p/${item.pubkey || item.actor}?nft=${item.card_id || item.id}`);
  const [myCards, setMyCards] = useState(null);
  useEffect(() => {
    if (!identity || route.page !== 'offers') return;
    getJSON(`/api/profiles/${identity.pubkey}`).then((p) => setMyCards(p.cards)).catch(() => {});
  }, [identity, route.page, market.version]);

  const navigate = useCallback((path) => {
    window.history.pushState({}, '', path);
    setRoute(readRoute()); setProfile(null); setSelected(null); setVisitor(false); setTab('collection');
    window.scrollTo({ top: 0, behavior: 'instant' });
  }, []);
  navRef.current = navigate;
  useEffect(() => { const pop = () => { setRoute(readRoute()); setProfile(null); setSelected(null); }; window.addEventListener('popstate', pop); return () => window.removeEventListener('popstate', pop); }, []);
  useEffect(() => {
    const titles = { how: 'How it works', explore: route.tab === 'nfts' ? 'Explore NFTs' : 'Explore collections', activity: 'Activity', claim: 'Claim an NFT', market: 'Market', listing: 'For sale', wallet: 'Wallet', offers: 'Offers' };
    document.title = titles[route.page] ? `${titles[route.page]} · Nonfungible.cash` : profile?.name ? `${profile.name} · Nonfungible.cash` : 'Nonfungible.cash · The NFT is the JPG';
  }, [route, profile?.name]);
  useEffect(() => {
    if (!route.nft || !profile) return;
    const target = profile.cards.find((c) => c.id === route.nft);
    if (target) { setSelected(target.id); setTab(target.status === 'sent' ? 'sent' : 'collection'); }
    setRoute((r) => ({ ...r, nft: null }));
  }, [route.nft, route.pubkey, profile]);
  // The address follows the open NFT, so a copied or shared link opens it (with its own preview).
  useEffect(() => {
    if (route.page !== 'profile' || !profile || route.nft) return;
    const path = selected ? `/p/${route.pubkey}?nft=${selected}` : `/p/${route.pubkey}`;
    if (window.location.pathname + window.location.search !== path) window.history.replaceState({}, '', path);
  }, [selected, route.page, route.pubkey, route.nft, profile]);
  useEffect(() => {
    if (route.page !== 'home') return;
    let live = true;
    Promise.all([getJSON('/api/explore/collections?sort=popular&limit=6'), getJSON('/api/explore/nfts?sort=new&limit=8'), getJSON('/api/activity?limit=14')])
      .then(([collections, nfts, activity]) => { if (live) setHome({ collections: collections.items, nfts: nfts.items, activity }); }).catch(() => {});
    return () => { live = false; };
  }, [route.page]);
  useEffect(() => {
    getJSON('/api/config').then((data) => {
      validateKeyset(data);
      const pinned = local.get(MINT_PIN);
      const buildPin = import.meta.env.VITE_MINT_KEYSET_ID;
      if ((pinned && pinned !== data.keyset_id) || (buildPin && buildPin !== data.keyset_id)) throw new Error('The mint identity has changed. Restore the original mint before using this app.');
      local.set(MINT_PIN, data.keyset_id);
      setTurnstileSitekey(data.turnstile_sitekey);
      setConfig(data);
    }).catch((e) => setFatal(e.message));
  }, []);
  // Public profile, plus (for your own) which cards have a pending transfer:
  // the server only tells the owner that a transfer JPG or link exists.
  const identityRef = useRef(identity);
  identityRef.current = identity;
  const fetchProfile = useCallback(async (key) => {
    const data = await getJSON(`/api/profiles/${key}`);
    const me = identityRef.current;
    if (me?.pubkey === key) {
      try {
        const { ids } = await (await signedRequest(me.secret, `/api/profiles/${key}/cards/pending`)).json();
        const pending = new Set(ids);
        data.cards = data.cards.map((c) => (pending.has(c.id) ? { ...c, status: 'ready' } : c));
      } catch { /* shown as owned until the next refresh */ }
    }
    return data;
  }, []);
  const reload = useCallback(async (key = pubkey) => {
    if (!key) return;
    const data = await fetchProfile(key);
    if (routeKey() === key) { setProfile((prev) => keepSame(prev, data)); setProfileError(''); }
    return data;
  }, [pubkey, fetchProfile]);
  reloadRef.current = reload;
  useEffect(() => { if (pubkey && market.version) reloadRef.current(pubkey).catch(() => {}); }, [market.version, pubkey]);
  useEffect(() => {
    if (!identity || !config) { setLocalWallet(null); return; }
    let disposed = false;
    setWalletState('opening'); setWalletError(''); setLocalWallet(null);
    openWallet(identity.secret, config).then(async ({ wallet }) => {
      await wallet.recover();
      if (!disposed) { setLocalWallet(wallet); setWalletState('ready'); if (routeKey() === identity.pubkey) await reloadRef.current(identity.pubkey); }
    }).catch((e) => { if (!disposed) { setWalletState('error'); setWalletError(e.message); } });
    return () => { disposed = true; };
  }, [identity, config]);
  useEffect(() => {
    if (!pubkey) return;
    let disposed = false;
    setLoading(true); setProfileError('');
    fetchProfile(pubkey).then((data) => { if (!disposed) setProfile(data); })
      .catch((e) => { if (!disposed) setProfileError(e.message); }).finally(() => { if (!disposed) setLoading(false); });
    const timer = setInterval(() => { if (document.visibilityState === 'visible') reloadRef.current(pubkey).catch(() => { setProfileError('Connection lost. Ownership status will refresh when the mint is reachable.'); setRefresh((r) => r + 1); }); }, 15000);
    return () => { disposed = true; clearInterval(timer); };
  }, [pubkey, fetchProfile]);

  useEffect(() => {
    const missing = owner && localWallet && profile?.cards.filter((c) => c.status !== 'sent' && c.custody !== 'browser' && !migrationAttempts.current.has(c.id));
    if (!missing?.length || recovery.current || busy || localWallet.pubkey !== pubkey) return;
    recovery.current = true;
    for (const c of missing) migrationAttempts.current.add(c.id);
    setBusy('Moving NFTs into your browser wallet');
    (async () => {
      try { for (const asset of missing) await localWallet.migrate(asset); await reload(); toast.success('Your NFTs now live in this browser’s wallet.'); }
      catch (e) { setWalletError(e.message); toast.error(`Wallet migration needs another try: ${e.message}`); }
      finally { recovery.current = false; setBusy(''); }
    })();
  }, [profile, owner, localWallet, busy, reload, pubkey]);

  const recoverWallet = async () => {
    setBusy('Syncing wallet'); setWalletError('');
    try {
      const { wallet } = await openWallet(identity.secret, config);
      await wallet.recover(); setLocalWallet(wallet); setWalletState('ready');
      migrationAttempts.current.clear(); await reload(); toast.success('Wallet synced.');
    } catch (e) { setWalletError(e.message); toast.error(e.message); }
    finally { setBusy(''); }
  };

  const closeDialog = () => { if (busy) return; setDialog(null); setGenerated(''); setInputKey(''); setBackedUp(false); };
  const openCreate = () => { setName(''); setGenerated(newPrivateKey()); setBackedUp(false); setDialog('create'); };
  const saveIdentity = (secret) => {
    const p = profileKey(secret), keys = storedKeys(); keys[p] = secret;
    local.set(KEYRING, JSON.stringify(keys)); local.set(ACTIVE, p);
    const next = { secret, pubkey: p }; setIdentity(next); return next;
  };
  const onboard = async (event) => {
    event.preventDefault(); setBusy(dialog === 'create' ? 'Creating collection' : 'Unlocking');
    try {
      const secret = dialog === 'create' ? generated : inputKey.trim().toLowerCase();
      const p = profileKey(secret);
      const response = await signedRequest(secret, `/api/profiles/${p}`, JSON.stringify({ name: name.trim() || 'Untitled collection' }), 'application/json');
      const data = await response.json(); saveIdentity(secret);
      setDialog(null); setGenerated(''); setInputKey(''); setBackedUp(false);
      const here = readRoute();
      if (here.page === 'claim') toast.success('Collection ready. Now claim your NFT.');
      // Came for an NFT: fund the wallet next, then return to that listing.
      else if (here.page === 'listing') { navigate(`/wallet?then=/market/${here.id}`); toast.success('Collection ready. Add ecash to make your offer.'); }
      else if (here.page === 'wallet' || here.page === 'offers') { navigate(window.location.pathname + window.location.search); toast.success('Collection ready.'); }
      else { navigate(`/p/${p}`); setProfile(data); toast.success(dialog === 'create' ? 'Collection created.' : 'Collection unlocked.'); }
    } catch (e) { toast.error(e.message); } finally { setBusy(''); }
  };
  const added = async (asset, kind) => {
    await reload().catch(() => {});
    setDialog(null); setTab('collection'); setFresh(asset.id); setSelected(asset.id);
    toast.success(kind === 'mint' ? 'Minted. The JPG is now an NFT.' : 'Received. The old credential is spent and the NFT is yours.');
    setTimeout(() => setFresh(null), 2200);
  };
  const sendCard = async (target, { method = 'file', password = '' } = {}) => {
    setBusy(method === 'link' ? (password ? 'Encrypting link' : 'Creating link') : 'Creating transfer JPG');
    try {
      if (method === 'link') {
        const { token, nullifier } = await localWallet.sendToken(target);
        const id = newLinkId();
        const { fragment, envelope } = await sealLink(token, { id, h: target.h, password });
        await signedRequest(identity.secret, `/api/profiles/${identity.pubkey}/links`, JSON.stringify({ id, card_id: target.id, nullifier, envelope }), 'application/json');
        await reload().catch(() => {});
        toast.success('Link created. Copy it before closing.');
        return { url: linkUrl(window.location.origin, id, fragment), protected: !!password };
      }
      const jpg = await localWallet.send(target);
      download(new Blob([jpg], { type: 'image/jpeg' }), `cashu-transfer-${target.h.slice(0, 12)}.jpg`);
      await reload(); toast.success('Transfer JPG saved. Send it as a file.');
      return { file: true };
    } catch (e) { toast.error(e.message); await reload().catch(() => {}); return null; } finally { setBusy(''); }
  };
  const deleteCard = async (target) => {
    setBusy('Deleting');
    try { await localWallet.destroy(target); setSelected(null); await reload(); toast.success('Deleted. The NFT is burned and its picture is gone.'); }
    catch (e) { toast.error(e.message); await reload().catch(() => {}); } finally { setBusy(''); }
  };
  const renameCard = async (target, title) => {
    try {
      await signedRequest(identity.secret, `/api/profiles/${identity.pubkey}/cards/${target.id}/title`, JSON.stringify({ title }), 'application/json');
      await reload(); toast.success('Renamed.'); return true;
    } catch (e) { toast.error(e.message); return false; }
  };
  const cancelTransfer = async (target) => {
    setBusy('Canceling transfer');
    try { await localWallet.cancel(target); await reload(); toast.success('Transfer canceled. Every link and transfer JPG for it is now void.'); }
    catch (e) { toast.error(e.message); await reload().catch(() => {}); } finally { setBusy(''); }
  };
  const openProfile = (event) => {
    event.preventDefault();
    const match = openInput.trim().match(/(?:^|\/p\/)([0-9a-f]{64})(?:\/?$)/);
    if (!match) { toast.error('Enter a public key or a profile link.'); return; }
    setDialog(null); navigate(`/p/${match[1]}`);
  };
  const canAdd = owner && !!localWallet && !!config && !busy;
  const needsIdentity = () => { toast('Start a collection first, then you can like and follow.'); openCreate(); };
  const applySummary = (target, summary) => {
    if (profile?.pubkey === target) setProfile((p) => p && ({ ...p, likes: summary.likes, followers: summary.followers }));
    setHome((h) => h && ({ ...h, collections: h.collections.map((c) => c.pubkey === target ? { ...c, likes: summary.likes, followers: summary.followers } : c) }));
  };
  const likeCollection = async (target, on) => {
    if (!identity) { needsIdentity(); return null; }
    try { const summary = await toggleRelation('likes', target, on); applySummary(target, summary); return summary; }
    catch (e) { toast.error(e.message); throw e; }
  };
  const followCollection = async (target, on) => {
    if (!identity) { needsIdentity(); return; }
    try { const summary = await toggleRelation('follows', target, on); applySummary(target, summary); toast.success(on ? `Following ${profile?.name || 'this collection'}.` : 'Unfollowed.'); }
    catch (e) { toast.error(e.message); }
  };
  const navActive = (page) => route.page === page || (page === 'market' && route.page === 'listing');
  const verbs = { mint: 'minted', receive: 'received', collection: 'joined', like: 'liked', follow: 'followed', sale: 'bought' };
  const ticker = home?.activity?.length ? home.activity.map((e) => `${e.actor_name || 'Someone'} ${verbs[e.kind]} ${e.title || e.target_name || ''}`.trim())
    : ['The NFT is the JPG', 'Free mint', 'Send it like a meme', 'No seed phrase drama', 'Your key, your vibes', '1 of 1 by default', 'gm'];

  return <MotionConfig reducedMotion="user">
    <header className={`topbar ${identity ? '' : 'is-guest'}`}>
      <div className="topbar-inner">
        <a className="brand" href="/" onClick={(e) => { e.preventDefault(); navigate('/'); }}><span className="brand-mark" aria-hidden="true" /><span className="brand-name">Nonfungible.cash</span></a>
        <nav className="topnav" aria-label="Main">
          {(route.page === 'profile' ? NAV.slice(0, 1) : NAV).map(([href, label, page]) =>
            <a key={href} className={`nav-link ${navActive(page) ? 'is-active' : ''} ${page === 'market' ? 'nav-market' : ''}`} aria-current={navActive(page) ? 'page' : undefined}
              href={href} onClick={(e) => { e.preventDefault(); navigate(href); }}>{label}</a>)}
        </nav>
        <div className="topbar-actions">
          {/* Guests have no profile menu, so the folded links get their own. */}
          {!identity && <Menu.Root>
            <Menu.Trigger className={`icon-btn ${route.page === 'profile' ? '' : 'menu-mobile'}`} aria-label="Menu"><MenuIcon size={18} /></Menu.Trigger>
            <Menu.Portal><Menu.Positioner className="menu-layer" sideOffset={6} align="end"><Menu.Popup className="menu">
              {NAV.map(([href, label, page, Icon]) => <Menu.Item key={href} className={`menu-item ${page === 'market' ? 'menu-guest-market' : ''}`} onClick={() => navigate(href)}>
                <Icon size={15} />{label}{navActive(page) && <Check size={15} className="menu-check" />}
              </Menu.Item>)}
              <ThemeItems theme={theme} setTheme={setTheme} />
            </Menu.Popup></Menu.Positioner></Menu.Portal>
          </Menu.Root>}
          <ThemeMenu theme={theme} setTheme={setTheme} />
          {identity
            ? <Menu.Root>
              <Menu.Trigger className="profile-pill" aria-label="Profile"><Identicon pubkey={identity.pubkey} size={24} /><span className="profile-label">Profile</span>
                {market.unread > 0 && <motion.span key={market.unread} className="unread" initial={{ scale: .5 }} animate={{ scale: 1 }} aria-label={`${market.unread} unread`}>{market.unread > 9 ? '9+' : market.unread}</motion.span>}</Menu.Trigger>
              <Menu.Portal><Menu.Positioner className="menu-layer" sideOffset={6} align="end"><Menu.Popup className="menu">
                <Menu.Item className="menu-item" onClick={() => navigate(`/p/${identity.pubkey}`)}><ImageIcon size={15} />My collection</Menu.Item>
                <Menu.Item className="menu-item" onClick={() => navigate('/wallet')}><Wallet size={15} />Wallet</Menu.Item>
                <Menu.Item className="menu-item" onClick={() => navigate('/offers')}><HandCoins size={15} />Offers{market.unread > 0 && <span className="unread unread-inline">{market.unread}</span>}</Menu.Item>
                {/* On collection pages these move here from the top bar; on compact screens they always live here. */}
                <Menu.Separator className={`menu-sep ${route.page === 'profile' ? '' : 'menu-mobile'}`} />
                {NAV.slice(1).map(([href, label, page, Icon]) =>
                  <Menu.Item key={href} className={`menu-item ${route.page === 'profile' ? '' : 'menu-mobile'}`} onClick={() => navigate(href)}><Icon size={15} />{label}{navActive(page) && <Check size={15} className="menu-check" />}</Menu.Item>)}
                <Menu.Separator className="menu-sep" />
                <Menu.Item className="menu-item" onClick={openCreate} disabled={!config}><Plus size={15} />Start another collection</Menu.Item>
                <Menu.Item className="menu-item" onClick={() => { setName(''); setDialog('import'); }}><KeyRound size={15} />Import a key</Menu.Item>
                <Menu.Item className="menu-item" onClick={() => setDialog('open')}><Link2 size={15} />Open by public key</Menu.Item>
                <ThemeItems theme={theme} setTheme={setTheme} />
              </Menu.Popup></Menu.Positioner></Menu.Portal>
            </Menu.Root>
            : <Button variant="primary" size="sm" className="topbar-cta" onClick={openCreate} disabled={!config}>Get started</Button>}
        </div>
      </div>
    </header>

    {fatal ? <main className="page error-page"><ShieldX size={32} /><h1>The mint is unavailable</h1><p className="muted">{fatal}</p><Button variant="primary" onClick={() => window.location.reload()}>Try again</Button></main>

      : route.page === 'explore' ? <ExplorePage tab={route.tab} navigate={navigate} relations={relations} onLike={likeCollection} openCard={openCard} />

      : route.page === 'claim' ? <ClaimPage linkId={route.id} config={config} identity={identity} wallet={localWallet} walletState={walletState} navigate={navigate}
        onCreate={openCreate} onImport={() => { setName(''); setDialog('import'); }} onReceived={() => toast.success('Claimed. The NFT is in your collection.')} />

      : route.page === 'activity' ? <ActivityPage identity={identity} navigate={navigate} openCard={openCard} />

      : route.page === 'market' ? <MarketPage navigate={navigate} />

      : route.page === 'listing' ? <ListingPage id={route.id} navigate={navigate} identity={identity} market={market} onStart={openCreate} startOffer={route.offer} />

      : route.page === 'wallet' ? (identity ? <WalletPage market={market} navigate={navigate} then={route.then} /> : <SignInFirst onStart={openCreate} />)

      : route.page === 'offers' ? (identity ? <OffersPage navigate={navigate} market={market} nftWallet={localWallet} cards={myCards} initialTab={route.tab} focus={route.offer} /> : <SignInFirst onStart={openCreate} />)

      : route.page === 'how' ? <HowItWorks view={route.view} onStart={() => identity ? navigate(`/p/${identity.pubkey}`) : openCreate()}
        onView={(view) => { window.history.replaceState({}, '', view === 'cryptography' ? '/how-it-works?view=cryptography' : '/how-it-works'); setRoute(readRoute()); }} />

      : route.page === 'home' ? <main className="page home">
        <section className="hero-card tone-cream">
          <motion.div className="hero-copy" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: .45, ease: 'easeOut' }}>
            <span className="pill">gm. Minting is free</span>
            <h1>The NFT <span className="hl">is</span> the JPG.</h1>
            <p className="lead">Turn any picture into a collectible. Its proof of ownership lives inside the file, so sending the JPG sends the NFT.</p>
            <div className="hero-actions">
              {identity ? <Button variant="primary" size="lg" icon={<ArrowRight size={16} />} onClick={() => navigate(`/p/${identity.pubkey}`)}>Open my collection</Button>
                : <Button variant="primary" size="lg" icon={<Plus size={16} />} onClick={openCreate} disabled={!config}>Start a collection</Button>}
              <Button variant="secondary" size="lg" onClick={() => navigate('/how-it-works')}>How it works</Button>
            </div>
            {!identity && <button className="link" onClick={() => { setName(''); setDialog('import'); }}>Already collecting? Import your key</button>}
          </motion.div>
          <div className="hero-stage" aria-label="Illustrative preview cards">
            <span className="sticker st-1" aria-hidden="true">Free mint</span>
            <span className="sticker st-2" aria-hidden="true">1 of 1</span>
            <span className="sticker st-3" aria-hidden="true">No gas</span>
            <motion.div className="stage-card stage-a" initial={{ opacity: 0, y: 30, rotate: -12 }} animate={{ opacity: 1, y: 0, rotate: -7 }} transition={{ delay: .1, type: 'spring', stiffness: 150, damping: 18 }}><PreviewCard variant={1} title="Harbour, 6am" note="Edition of 1" /></motion.div>
            <motion.div className="stage-card stage-b" initial={{ opacity: 0, y: 30, rotate: 12 }} animate={{ opacity: 1, y: 0, rotate: 6 }} transition={{ delay: .2, type: 'spring', stiffness: 150, damping: 18 }}><PreviewCard variant={2} title="Grid study 04" note="Edition of 1" /></motion.div>
            <motion.div className="stage-card stage-c" initial={{ opacity: 0, y: 30 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: .3, type: 'spring', stiffness: 150, damping: 18 }}><PreviewCard variant={0} title="Sunset over nothing" note="Edition of 1" /></motion.div>
          </div>
        </section>

        <Marquee items={ticker} />

        {!home && <section className="home-section">
          <div className="section-head"><h2>Popular collections</h2></div>
          <SkeletonCards count={3} variant="collection" />
        </section>}

        {home?.collections.length > 0 && <section className="home-section">
          <div className="section-head"><h2>Popular collections</h2><Button variant="secondary" size="sm" icon={<ArrowRight size={14} />} onClick={() => navigate('/explore')}>See all</Button></div>
          <div className="grid grid-collections">{home.collections.map((item, i) => <CollectionCard key={item.pubkey} item={item} index={i} rank={i < 3 && item.likes > 0 ? i + 1 : null}
            liked={relations.likes.includes(item.pubkey)} onLike={(pk, on) => likeCollection(pk, on).catch(() => {})} onOpen={(pk) => navigate(`/p/${pk}`)} />)}</div>
        </section>}

        {home?.nfts.length > 0 && <section className="home-section home-split">
          <div className="fresh">
            <div className="section-head"><h2>Fresh mints</h2><Button variant="secondary" size="sm" icon={<ArrowRight size={14} />} onClick={() => navigate('/explore/nfts')}>Browse all</Button></div>
            <div className="grid grid-tight">{home.nfts.slice(0, 6).map((item, i) => <MarketCard key={item.id} item={item} index={i} onOpen={openCard} />)}</div>
          </div>
          <aside className="happening">
            <div className="section-head"><h2>Happening now</h2></div>
            <ul className="feed feed-compact">{home.activity.slice(0, 7).map((e, i) => <ActivityItem key={e.id} event={e} index={i} onProfile={(pk) => navigate(`/p/${pk}`)} onCard={openCard} />)}</ul>
            <Button variant="secondary" className="full" icon={<ArrowRight size={15} />} onClick={() => navigate('/activity')}>All activity</Button>
          </aside>
        </section>}

        <section className="bento">
          <motion.article className="tile tone-peach tile-wide" {...tileIn(0)}>
            <div className="tile-text"><h3>Drop a JPG. Get an NFT.</h3><p>Pick a photo, a drawing, a meme. A few seconds later it's a one-of-one in your collection.</p></div>
            <div className="mini mini-drop" aria-hidden="true"><span className="mini-zone"><Upload size={18} /></span><span className="mini-card"><span /></span></div>
          </motion.article>
          <motion.article className="tile tone-sky" {...tileIn(1)}>
            <div className="tile-text"><h3>Show it off</h3><p>Your collection gets its own page. Anyone can see that every piece is really yours.</p></div>
            <div className="mini mini-badges" aria-hidden="true"><span className="badge badge-good"><span className="badge-inner"><span className="dot" />Verified owner</span></span></div>
          </motion.article>
          <motion.article className="tile tone-mint" {...tileIn(2)}>
            <div className="tile-text"><h3>Send it like a photo</h3><p>Share a link or attach the JPG. Lock it with a password if you like. Whoever claims it first owns it.</p></div>
            <div className="mini mini-send" aria-hidden="true"><span className="mini-file"><ImageDown size={14} />sunset.jpg</span><Send size={16} /></div>
          </motion.article>
          <motion.article className="tile tone-butter tile-wide" {...tileIn(3)}>
            <div className="tile-text"><h3>Yours, not the platform's</h3><p>No account, no password, no gas fees. Your key lives in your browser, and only you can move your NFTs.</p></div>
            <div className="mini mini-key" aria-hidden="true"><KeyRound size={22} /></div>
          </motion.article>
        </section>

        <motion.section className="story-card" {...tileIn(0)}>
          <span className="pill pill-dark">Why it's different</span>
          <h2>Most NFTs are a receipt for a picture. This one <em>is</em> the picture.</h2>
          <p>Elsewhere, the token and the image live in different places, and the image can vanish. Here they travel together in a single file you can save, share and pass on.</p>
          <Button variant="inverse" size="lg" icon={<ArrowRight size={16} />} onClick={() => navigate('/how-it-works')}>See how it works</Button>
        </motion.section>

        <section className="home-cta">
          <h2>Your first NFT is one JPG away.</h2>
          <div className="hero-actions">
            <Button variant="primary" size="lg" onClick={() => identity ? navigate(`/p/${identity.pubkey}`) : openCreate()} disabled={!config}>{identity ? 'Open my collection' : 'Start a collection'}</Button>
          </div>
        </section>
      </main>

      : <main className="page profile">
        <section className="profile-card">
          {profile?.custom_cover
            ? <div className="profile-banner banner-1 is-cover" style={{ '--tint': identiconColor(pubkey) }}><img src={imageUrl(profile.cover)} alt="" /></div>
            : <div className={`profile-banner banner-${active.length >= 6 ? 6 : active.length >= 3 ? 3 : active.length ? 1 : 0}`} style={{ '--tint': identiconColor(pubkey) }}>
              {active.slice(0, active.length >= 6 ? 6 : active.length >= 3 ? 3 : 1).map((c) => <img key={c.id} src={imageUrl(c.h)} alt="" />)}
            </div>}
          <div className="profile-main">
            <div className="profile-avatar"><Identicon pubkey={pubkey} size={88} /></div>
            <div className="profile-id">
              <h1>{profile?.name || (loading ? ' ' : 'Collection')}</h1>
              <div className="profile-sub">
                <CopyChip value={pubkey} message="Public key copied" />
                {owner && <span className={`wallet-state is-${walletError ? 'error' : walletState}`} title={walletError || ''}>
                  {walletState === 'opening' ? <Spinner size={11} /> : <span className="dot" />}
                  {busy || (walletState === 'opening' ? 'Opening wallet' : walletError ? 'Wallet needs attention' : 'Wallet ready')}
                </span>}
              </div>
            </div>
            <div className="profile-actions">
              {identity?.pubkey !== pubkey && profile && <LikeButton liked={relations.likes.includes(pubkey)} count={profile.likes ?? 0} onToggle={(on) => likeCollection(pubkey, on).catch(() => {})} />}
              {identity?.pubkey !== pubkey && profile && <FollowButton following={relations.following.includes(pubkey)} onToggle={(on) => followCollection(pubkey, on)} />}
              <Button variant="secondary" icon={<Link2 size={15} />} onClick={() => copyText(window.location.href, 'Profile link copied')}>Share</Button>
              {owner && <Button variant="primary" icon={<Plus size={16} />} onClick={() => setDialog('add')} disabled={!canAdd}>Add JPG</Button>}
              {identity?.pubkey === pubkey && <Menu.Root>
                <Menu.Trigger className="icon-btn icon-btn-bordered" aria-label="More actions"><Ellipsis size={17} /></Menu.Trigger>
                <Menu.Portal><Menu.Positioner className="menu-layer" sideOffset={6} align="end"><Menu.Popup className="menu">
                  <Menu.Item className="menu-item" onClick={() => setDialog('edit')} disabled={!profile}><Pencil size={15} />Edit profile</Menu.Item>
                  <Menu.Item className="menu-item" onClick={() => setVisitor((v) => !v)}>{visitor ? <EyeOff size={15} /> : <Eye size={15} />}{visitor ? 'Back to owner view' : 'View as visitor'}</Menu.Item>
                  <Menu.Item className="menu-item" onClick={() => setDialog('backup')}><KeyRound size={15} />Back up private key</Menu.Item>
                  <Menu.Item className="menu-item" onClick={recoverWallet} disabled={!!busy || !config}><RefreshCw size={15} />Sync wallet from backup</Menu.Item>
                </Menu.Popup></Menu.Positioner></Menu.Portal>
              </Menu.Root>}
              {!identity && <Button variant="secondary" icon={<KeyRound size={15} />} onClick={() => { setName(''); setDialog('import'); }}>Unlock</Button>}
            </div>
          </div>
          <div className="profile-stats">
            <div className="stat"><strong>{active.length}</strong><span>Collected</span></div>
            <div className="stat"><strong>{sent.length}</strong><span>Sent</span></div>
            <div className="stat"><strong>{profile?.likes ?? 0}</strong><span>Likes</span></div>
            <button className="stat" onClick={() => setDialog('followers')}><strong>{profile?.followers ?? 0}</strong><span>Followers</span></button>
            <button className="stat" onClick={() => setDialog('following')}><strong>{profile?.following ?? 0}</strong><span>Following</span></button>
          </div>
        </section>

        {profileError && <Notice action={!profile && identity?.pubkey === pubkey ? <Button size="sm" variant="secondary" onClick={() => { setInputKey(identity.secret); setDialog('import'); }}>Create it</Button> : null}>{profileError}</Notice>}
        {owner && <StorageNotice />}
        {owner && walletError && <Notice tone="bad" action={<Button size="sm" variant="secondary" onClick={recoverWallet} disabled={!!busy}>Sync wallet</Button>}>{walletError}</Notice>}

        <div className="tabs-row">
          <div className="tabs" role="tablist">
            {[['collection', 'Collection', active.length], ['sent', 'Sent', sent.length], ['activity', 'Activity', null]].map(([id, label, count]) =>
              <button key={id} role="tab" aria-selected={tab === id} className={`tab ${tab === id ? 'is-active' : ''}`} onClick={() => setTab(id)}>
                {label}{count !== null && <motion.span key={count} className="tab-count" initial={{ scale: .7 }} animate={{ scale: 1 }} transition={{ type: 'spring', stiffness: 500, damping: 20 }}>{count}</motion.span>}
                {tab === id && <motion.span layoutId="tab-underline" className="tab-underline" transition={{ type: 'spring', stiffness: 500, damping: 38 }} />}
              </button>)}
          </div>
          <button className="link" onClick={() => { setRefresh((r) => r + 1); reload().catch((e) => toast.error(e.message)); }}><RefreshCw size={13} /> Re-verify</button>
        </div>

        {tab === 'activity' ? <ActivityList source={`/api/activity?actor=${pubkey}`} onProfile={(pk) => navigate(`/p/${pk}`)} onCard={openCard}
          empty={{ title: 'No activity yet', text: 'Mints, receipts, likes and follows by this collector show up here.' }} />
          : loading && !profile ? <SkeletonCards count={4} />
          : <motion.div className="grid" key={tab}>
            {shown.map((asset, i) => <NFTCard key={asset.id} index={i} card={asset} verification={verification[asset.id]} isNew={asset.id === fresh} onOpen={setSelected} />)}
            {tab === 'collection' && owner && <PendingPurchases market={market} />}
            {tab === 'collection' && owner && <motion.button className="nft-card add-tile" onClick={() => setDialog('add')} disabled={!canAdd}
              whileHover={canAdd ? { y: -3 } : undefined} whileTap={canAdd ? { scale: .98 } : undefined} transition={{ type: 'spring', stiffness: 400, damping: 26 }}>
              <span className="add-icon"><Plus size={20} /></span><strong>Add JPG</strong><span className="muted">Mint a new one or receive a transfer</span>
            </motion.button>}
            {!shown.length && !(tab === 'collection' && owner) && <div className="empty">
              <strong>{tab === 'sent' ? 'Nothing sent yet' : 'No NFTs yet'}</strong>
              <span className="muted">{tab === 'sent' ? 'NFTs appear here once their transfer JPG has been claimed.' : 'This collection is empty.'}</span>
            </div>}
          </motion.div>}
      </main>}

    <footer className="footer">
      <div className="footer-inner">
        <span className="brand small"><span className="brand-mark" aria-hidden="true" />Nonfungible.cash</span>
        <span className="muted">Experimental, unaudited cryptography. Pointcheval–Sanders credentials on BLS12-381.</span>
        <a className="nav-link" href="/explore" onClick={(e) => { e.preventDefault(); navigate('/explore'); }}>Explore</a>
        <a className="nav-link" href="/activity" onClick={(e) => { e.preventDefault(); navigate('/activity'); }}>Activity</a>
        <a className="nav-link" href="/how-it-works" onClick={(e) => { e.preventDefault(); navigate('/how-it-works'); }}>How it works</a>
        <button className="nav-link" onClick={() => setDialog('open')}>Open by public key</button>
      </div>
    </footer>

    <Modal open={dialog === 'create' || dialog === 'import'} close={closeDialog}
      title={dialog === 'create' ? 'Create a collection' : 'Unlock a collection'}
      description={dialog === 'create' ? 'Your collection is controlled by a private key generated in this browser. Save it now: there is no password reset.' : 'Paste the private key of an existing collection. It stays in this browser.' + (!HOME.startsWith('https://') || MOVED_FROM.includes(HOME) ? '' : ` Used ${MOVED_FROM.map((o) => new URL(o).host).join(' or ')} before? Open it in this browser once and your collection moves here.`)}>
      <form onSubmit={onboard} className="stack">
        {dialog === 'create' ? <>
          <label className="field"><span>Collection name</span><input maxLength={40} placeholder="e.g. Field recordings" value={name} onChange={(e) => setName(e.target.value)} autoFocus /></label>
          <div className="keybox">
            <div className="keybox-head"><span><KeyRound size={14} /> Private key</span><span className="muted">Never share this</span></div>
            <code className="mono">{generated}</code>
            <div className="row">
              <CopyChip value={generated} display="Copy" message="Private key copied" />
              <Button type="button" variant="ghost" size="sm" icon={<Download size={14} />} onClick={() => download(new Blob([generated + '\n'], { type: 'text/plain' }), 'nonfungible-cash-private-key.txt')}>Download</Button>
            </div>
          </div>
          <label className="ack">
            <input type="checkbox" checked={backedUp} onChange={(e) => setBackedUp(e.target.checked)} />
            <span className="ack-box"><DrawnCheck on={backedUp} size={14} /></span>
            <span>I saved my private key. Without it this collection can't be recovered.</span>
          </label>
        </> : <label className="field"><span>Private key</span><input type="password" autoComplete="off" placeholder="64 hex characters" spellCheck={false} value={inputKey} onChange={(e) => setInputKey(e.target.value)} required autoFocus /></label>}
        <Button variant="primary" size="lg" className="full" type="submit" disabled={!!busy || (dialog === 'create' && !backedUp)} icon={busy ? <Spinner /> : null}>
          {busy || (dialog === 'create' ? 'Create collection' : 'Unlock')}
        </Button>
      </form>
    </Modal>

    <Modal open={dialog === 'open'} close={closeDialog} title="Find a profile" description="Paste a collector's public key or profile link.">
      <form onSubmit={openProfile} className="stack">
        <label className="field"><span>Public key or link</span><input value={openInput} onChange={(e) => setOpenInput(e.target.value)} placeholder="https://…/p/… or 64 hex characters" required autoFocus /></label>
        <Button variant="primary" size="lg" className="full" type="submit" icon={<ArrowRight size={16} />}>Open profile</Button>
      </form>
    </Modal>

    <Modal open={dialog === 'backup'} close={closeDialog} title="Back up your private key" description="This key signs for your collection and decrypts your wallet backups. Keep a copy somewhere other than this browser.">
      <div className="keybox">
        <code className="mono">{identity?.secret}</code>
        <div className="row">
          <CopyChip value={identity?.secret || ''} display="Copy" message="Private key copied" />
          <Button variant="ghost" size="sm" icon={<Download size={14} />} onClick={() => download(new Blob([identity.secret + '\n'], { type: 'text/plain' }), 'nonfungible-cash-private-key.txt')}>Download</Button>
        </div>
      </div>
    </Modal>

    {identity && <EditCollectionDialog open={dialog === 'edit'} close={() => setDialog(null)} profile={profile} identity={identity} onSaved={(p) => setProfile((prev) => ({ ...prev, ...p }))} />}
    {pubkey && <NetworkDialog pubkey={pubkey} open={dialog === 'followers' || dialog === 'following'} initial={dialog} close={() => setDialog(null)} navigate={navigate} />}
    <AddDialog open={dialog === 'add'} close={() => setDialog(null)} config={config} wallet={owner ? localWallet : null} onAdded={added} />

    <Modal open={!!card} close={() => { if (!busy) setSelected(null); }} size="wide" title={card?.title}>
      {card && <CardDetail card={card} result={verification[card.id]} owner={owner} busy={busy} config={config}
        canSend={!!localWallet && !!card.signature && card.custody === 'browser'} onSend={sendCard} onCancel={cancelTransfer} onDelete={deleteCard} onRename={renameCard} isNew={card.id === fresh}
        market={owner ? market : null} nftWallet={localWallet} onChanged={() => reload()} navigate={navigate} />}
    </Modal>
    <Toaster theme={theme.dark ? 'dark' : 'light'} position="bottom-center" toastOptions={{ className: 'toast' }} />
  </MotionConfig>;
}

/** Keys handed over from an old origin (move.jsx). Returns how many were saved. */
function receiveKeys({ keys, active, mint }) {
  const valid = Object.entries(keys && typeof keys === 'object' ? keys : {}).filter(([pubkey, secret]) => {
    try { return typeof secret === 'string' && /^[0-9a-f]{64}$/.test(secret) && profileKey(secret) === pubkey; } catch { return false; }
  });
  if (!valid.length) return 0;
  const mine = storedKeys();
  local.set(KEYRING, JSON.stringify({ ...Object.fromEntries(valid), ...mine }));
  const saved = storedKeys();
  if (!valid.every(([pubkey]) => saved[pubkey])) return 0;
  if (!mine[local.get(ACTIVE)]) local.set(ACTIVE, valid.some(([pubkey]) => pubkey === active) ? active : valid[0][0]);
  if (typeof mint === 'string' && !local.get(MINT_PIN)) local.set(MINT_PIN, mint);
  return valid.length;
}
const movePayload = () => ({ keys: storedKeys(), active: local.get(ACTIVE), mint: local.get(MINT_PIN) });
const keysText = () => Object.values(storedKeys()).join('\n');
const downloadKeys = () => download(new Blob([keysText() + '\n'], { type: 'text/plain' }), 'nonfungible-cash-private-keys.txt');

const plan = movePlan(Object.keys(storedKeys()).length > 0);
const root = createRoot(document.getElementById('root'));
if (plan === 'redirect') redirectHome();
else if (plan === 'offer') root.render(<MoveOffer payload={movePayload} keysText={keysText} onDownload={downloadKeys} />);
else if (plan === 'receive') root.render(<MoveReceive save={receiveKeys} />);
else if (plan === 'stranded') root.render(<MoveStranded />);
else root.render(<App />);
