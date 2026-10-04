// The app moved to its canonical origin (PUBLIC_URL, read from og:url). Collection
// keys live in each browser per origin, so the old origin hands them over with
// postMessage between two windows: the keys never appear in a URL, a server log or
// the browser history. NFT credentials and the ecash wallet are backed up at the
// server, encrypted with the key, so the key is all that has to move.
import React, { useEffect, useState } from 'react';
import { ArrowLeft, ArrowRight, Copy, Download } from 'lucide-react';
import { Toaster } from 'sonner';
import { Button, Spinner, copyText } from './ui.jsx';

// Origins the app used to live on. Only these hand keys over, and only to HOME.
export const MOVED_FROM = ['https://jpg.nocean.xyz'];
export const HOME = (() => {
  try { return new URL(document.querySelector('meta[property="og:url"]')?.content || '', window.location.href).origin; }
  catch { return window.location.origin; }
})();
const here = () => window.location.pathname + window.location.search + window.location.hash;
const hostOf = (origin) => new URL(origin).host;

/** What this page load does: 'redirect' or 'offer' on an old origin; 'receive' in the
 * window an old origin opened, or 'stranded' if a browser opened it without a link
 * back (some in-app browsers); null to run the app. */
export function movePlan(hasKeys) {
  const { origin, search } = window.location;
  if (MOVED_FROM.includes(origin) && HOME !== origin) return hasKeys ? 'offer' : 'redirect';
  const from = new URLSearchParams(search).get('move-from');
  if (from && MOVED_FROM.includes(from)) return window.opener ? 'receive' : 'stranded';
  return null;
}

export function redirectHome() { window.location.replace(HOME + here()); }

function MoveFrame({ children }) {
  return <main className="page move"><div className="move-card">
    <span className="brand"><span className="brand-mark" aria-hidden="true" /><span className="brand-name">Nonfungible.cash</span></span>
    {children}
  </div><Toaster position="bottom-center" toastOptions={{ className: 'toast' }} /></main>;
}

/** On the old origin: send this browser's keys to HOME, then continue there. */
export function MoveOffer({ payload, keysText, onDownload }) {
  const [state, setState] = useState('idle'), [showKey, setShowKey] = useState(false);
  const copy = async () => { if (!(await copyText(keysText(), `Key copied. Paste it into Import a key on ${hostOf(HOME)}.`))) setShowKey(true); };
  const move = () => {
    const win = window.open(`${HOME}/?move-from=${encodeURIComponent(window.location.origin)}`, 'move-collection', 'popup,width=440,height=560');
    if (!win) { setState('blocked'); return; }
    setState('moving');
    const stop = () => { clearTimeout(timer); window.removeEventListener('message', onMessage); };
    const timer = setTimeout(() => { stop(); setState('failed'); }, 30000);
    function onMessage(event) {
      if (event.origin !== HOME || event.source !== win) return;
      if (event.data?.type === 'move-ready') win.postMessage({ type: 'move-keys', ...payload() }, HOME);
      else if (event.data?.type === 'move-done') { stop(); redirectHome(); }
      else if (event.data?.type === 'move-failed') { stop(); setState('failed'); }
    }
    window.addEventListener('message', onMessage);
  };
  const newHost = hostOf(HOME), oldHost = window.location.host;
  return <MoveFrame>
    <h1>We moved to {newHost}</h1>
    <p className="lead">Your collection key is saved in this browser for {oldHost}. Move it to {newHost} to keep using your collection. Your NFTs and wallet come along: they’re backed up, encrypted with your key.</p>
    {state === 'moving' ? <Button variant="primary" size="lg" disabled icon={<Spinner />}>Moving your collection</Button>
      : <Button variant="primary" size="lg" icon={<ArrowRight size={16} />} onClick={move}>Move my collection</Button>}
    {state === 'blocked' && <p className="field-error" role="alert">Your browser blocked the window that does the move. Allow pop-ups for {oldHost} and try again, or download your keys.</p>}
    {state === 'failed' && <p className="field-error" role="alert">The move didn’t finish. Try again, or download your keys and add them on {newHost} with Import a key.</p>}
    <div className="row">
      <Button variant="ghost" size="sm" icon={<Copy size={14} />} onClick={copy}>Copy my key</Button>
      <Button variant="ghost" size="sm" icon={<Download size={14} />} onClick={onDownload}>Download my keys</Button>
    </div>
    {showKey && <><p className="hint">Copy this private key yourself. Keep it secret: it controls your collection.</p><code className="mono move-key">{keysText()}</code></>}
  </MoveFrame>;
}

/** On HOME, opened by an old origin but without a way to talk back to it. */
export function MoveStranded() {
  const from = new URLSearchParams(window.location.search).get('move-from');
  const oldHost = hostOf(from), newHost = hostOf(HOME);
  return <MoveFrame>
    <h1>Bring your key over by hand</h1>
    <p className="lead">This browser can’t move your collection from {oldHost} automatically. Go back to {oldHost}, choose Copy my key, then open {newHost} and paste it into Import a key.</p>
    <Button variant="primary" size="lg" icon={<ArrowLeft size={16} />} onClick={() => window.location.assign(from + '/')}>Back to {oldHost}</Button>
  </MoveFrame>;
}

/** In the window the old origin opened: accept its keys, confirm, close. */
export function MoveReceive({ save }) {
  const [state, setState] = useState('waiting');
  useEffect(() => {
    const from = new URLSearchParams(window.location.search).get('move-from');
    function onMessage(event) {
      if (event.origin !== from || event.source !== window.opener || event.data?.type !== 'move-keys') return;
      const moved = save(event.data);
      setState(moved ? 'done' : 'failed');
      window.opener.postMessage({ type: moved ? 'move-done' : 'move-failed' }, from);
      if (moved) setTimeout(() => window.close(), 400);
    }
    window.addEventListener('message', onMessage);
    window.opener.postMessage({ type: 'move-ready' }, from);
    return () => window.removeEventListener('message', onMessage);
  }, [save]);
  return <MoveFrame>
    {state === 'waiting' && <p className="lead"><Spinner /> Moving your collection…</p>}
    {state === 'done' && <p className="lead">Your collection is here. You can close this window.</p>}
    {state === 'failed' && <p className="field-error" role="alert">This browser couldn’t save your key here. Close this window and download your keys instead.</p>}
  </MoveFrame>;
}
