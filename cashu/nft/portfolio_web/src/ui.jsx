import React, { useEffect, useReducer, useRef, useState } from 'react';
import { Dialog } from '@base-ui/react/dialog';
import { AnimatePresence, animate, motion, useMotionValue, useReducedMotion, useSpring, useTransform } from 'motion/react';
import { toast } from 'sonner';
import { Check, CircleAlert, CircleHelp, Copy, LoaderCircle, X, ArrowLeft } from 'lucide-react';
import { onDeviceStorage } from './storage.ts';

export const short = (s, head = 8, tail = 6) => s ? `${s.slice(0, head)}…${s.slice(-tail)}` : '';
export const date = (n) => new Date(n * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
const press = { scale: .97 };
const snappy = { type: 'spring', stiffness: 520, damping: 32 };

export function Button({ variant = 'secondary', size = '', icon = null, children, className = '', ...props }) {
  return <motion.button whileTap={props.disabled ? undefined : press} transition={snappy}
    className={`btn btn-${variant} ${size ? 'btn-' + size : ''} ${className}`} {...props}>
    {icon}{children && <span>{children}</span>}
  </motion.button>;
}

export function Spinner({ size = 16 }) { return <LoaderCircle size={size} className="spin" aria-hidden="true" />; }

export async function copyText(text, message) {
  try { await navigator.clipboard.writeText(text); toast.success(message); return true; }
  catch { toast.error('Clipboard unavailable. Select the text and copy it manually.'); return false; }
}

export function CopyChip({ value, display, message = 'Copied', className = '' }) {
  const [done, setDone] = useState(false);
  useEffect(() => { if (!done) return; const t = setTimeout(() => setDone(false), 1400); return () => clearTimeout(t); }, [done]);
  return <motion.button whileTap={press} transition={snappy} className={`chip mono ${className}`} title={value}
    onClick={async () => setDone(await copyText(value, message))} aria-label={`Copy ${value}`}>
    <span>{display ?? short(value)}</span>
    <span className="chip-icon"><AnimatePresence mode="popLayout" initial={false}>
      <motion.span key={done ? 'ok' : 'copy'} initial={{ scale: .4, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: .4, opacity: 0 }} transition={snappy}>
        {done ? <Check size={13} /> : <Copy size={13} />}
      </motion.span>
    </AnimatePresence></span>
  </motion.button>;
}

// Deterministic 5×5 mirrored identicon with a flat colour from the pubkey.
const IDENTICON_COLORS = ['#1f6feb', '#0f8a5f', '#d9480f', '#c92a2a', '#0b7285', '#b08800', '#171717'];
export const identiconColor = (pubkey) => IDENTICON_COLORS[parseInt(pubkey.slice(0, 2), 16) % IDENTICON_COLORS.length];

// Average colour of an image, used as a flat tint for its card frame. Cached per asset.
const tints = new Map();
export function useTint(key) {
  const [tint, setTint] = useState(() => tints.get(key) || null);
  const onLoad = (event) => {
    if (tints.has(key)) { setTint(tints.get(key)); return; }
    try {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 16;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(event.currentTarget, 0, 0, 16, 16);
      const data = ctx.getImageData(0, 0, 16, 16).data;
      let r = 0, g = 0, b = 0;
      for (let i = 0; i < data.length; i += 4) { r += data[i]; g += data[i + 1]; b += data[i + 2]; }
      const n = data.length / 4, value = `rgb(${Math.round(r / n)} ${Math.round(g / n)} ${Math.round(b / n)})`;
      tints.set(key, value); setTint(value);
    } catch { /* tint is decorative */ }
  };
  return [tint, onLoad];
}

/* Profile pictures: one batched lookup for every identicon on screen. A
 * profile without a picture keeps its generated identicon. */
const avatarVersions = new Map();
const avatarListeners = new Set();
let avatarQueue = new Set(), avatarTimer = null;
async function flushAvatars() {
  const keys = [...avatarQueue]; avatarQueue = new Set(); avatarTimer = null;
  for (let i = 0; i < keys.length; i += 100) {
    const chunk = keys.slice(i, i + 100);
    try {
      const found = await (await fetch(`/api/avatars?pubkeys=${chunk.join(',')}`)).json();
      for (const k of chunk) avatarVersions.set(k, found[k] ?? null);
    } catch { for (const k of chunk) avatarVersions.set(k, null); }
  }
  avatarListeners.forEach((fn) => fn());
}
export function setAvatarVersion(pubkey, version) {
  avatarVersions.set(pubkey, version ?? null);
  avatarListeners.forEach((fn) => fn());
}
function useAvatarVersion(pubkey) {
  const [, rerender] = useReducer((n) => n + 1, 0);
  useEffect(() => {
    avatarListeners.add(rerender);
    if (/^[0-9a-f]{64}$/.test(pubkey || '') && !avatarVersions.has(pubkey) && !avatarQueue.has(pubkey)) {
      avatarQueue.add(pubkey);
      if (!avatarTimer) avatarTimer = setTimeout(flushAvatars, 40);
    }
    return () => { avatarListeners.delete(rerender); };
  }, [pubkey]);
  return avatarVersions.get(pubkey) ?? null;
}

export function Identicon({ pubkey, size = 72 }) {
  const version = useAvatarVersion(pubkey);
  if (version) return <img className="identicon avatar-img" src={`/api/avatars/${pubkey}.jpg?v=${version}`} width={size} height={size} alt="" loading="lazy" decoding="async" />;
  const bytes = pubkey.match(/../g).map((b) => parseInt(b, 16));
  const color = identiconColor(pubkey);
  const cells = [];
  for (let y = 0; y < 5; y++) for (let x = 0; x < 3; x++) {
    if (bytes[1 + y * 3 + x] % 2) { cells.push([x, y]); if (x < 2) cells.push([4 - x, y]); }
  }
  return <svg className="identicon" width={size} height={size} viewBox="-1 -1 7 7" aria-hidden="true">
    <rect x="-1" y="-1" width="7" height="7" fill="var(--surface-2)" />
    {cells.map(([x, y]) => <rect key={`${x}-${y}`} x={x} y={y} width="1.02" height="1.02" fill={color} />)}
  </svg>;
}

// Flat illustrative artwork for the landing page. Clearly labelled as previews.
export function PreviewArt({ variant = 0 }) {
  if (variant === 1) return <svg viewBox="0 0 400 400" role="img" aria-label="Illustrative artwork, preview only">
    <rect width="400" height="400" fill="#dbe7f3" />
    <rect x="60" y="230" width="280" height="110" fill="#1f6feb" />
    <rect x="60" y="150" width="190" height="80" fill="#0b2a52" />
    <rect x="250" y="190" width="90" height="40" fill="#f4f4f1" />
    <circle cx="300" cy="110" r="38" fill="#f4f4f1" />
  </svg>;
  if (variant === 2) return <svg viewBox="0 0 400 400" role="img" aria-label="Illustrative artwork, preview only">
    <rect width="400" height="400" fill="#141414" />
    {Array.from({ length: 49 }, (_, i) => <circle key={i} cx={80 + (i % 7) * 40} cy={80 + Math.floor(i / 7) * 40} r={i % 9 === 0 ? 11 : 5} fill={i % 9 === 0 ? '#37b24d' : '#f4f4f1'} />)}
  </svg>;
  return <svg viewBox="0 0 400 400" role="img" aria-label="Illustrative artwork, preview only">
    <rect width="400" height="400" fill="#efe9df" />
    <circle cx="200" cy="190" r="104" fill="#e8590c" />
    <rect y="250" width="400" height="150" fill="#171717" />
    <rect x="70" y="290" width="260" height="10" fill="#efe9df" />
    <rect x="120" y="320" width="160" height="10" fill="#efe9df" />
  </svg>;
}

// Pointer-driven 3D tilt. Interactive mode also supports drag-to-rotate and flipping.
export function Tilt({ children, interactive = false, flipped = false, className = '', max = 10 }) {
  const reduced = useReducedMotion();
  const spring = { stiffness: 260, damping: 22, mass: .6 };
  const rx = useSpring(0, spring), ry = useSpring(0, spring), turn = useSpring(0, { stiffness: 140, damping: 18 });
  const drag = useRef(null);
  useEffect(() => { reduced ? turn.jump(flipped ? 180 : 0) : turn.set(flipped ? 180 : 0); }, [flipped, reduced, turn]);
  const transform = useTransform([rx, ry, turn], ([x, y, t]) => `rotateX(${x}deg) rotateY(${y + t}deg)`);
  const move = (event) => {
    if (reduced) return;
    if (drag.current) {
      ry.set(Math.max(-70, Math.min(70, (event.clientX - drag.current.x) * .4)));
      rx.set(Math.max(-25, Math.min(25, (drag.current.y - event.clientY) * .15)));
    } else if (event.pointerType === 'mouse') {
      const rect = event.currentTarget.getBoundingClientRect();
      ry.set(((event.clientX - rect.left) / rect.width - .5) * max * 2);
      rx.set(((event.clientY - rect.top) / rect.height - .5) * -max * 1.6);
    }
  };
  const reset = () => { drag.current = null; rx.set(0); ry.set(0); };
  return <div className={`perspective ${className}`}>
    <motion.div className={`tilt ${interactive ? 'tilt-3d' : ''}`} style={{ transform: reduced ? `rotateY(${flipped ? 180 : 0}deg)` : transform }}
      onPointerMove={move} onPointerLeave={() => { if (!drag.current) reset(); }}
      onPointerDown={interactive && !reduced ? (e) => {
        if (!e.isPrimary || drag.current) return;
        drag.current = { x: e.clientX, y: e.clientY };
        e.currentTarget.setPointerCapture(e.pointerId);
      } : undefined}
      onPointerUp={reset} onPointerCancel={reset}>
      {children}
    </motion.div>
  </div>;
}

export function verdict(result, ready = false) {
  if (!result) return { tone: 'muted', text: 'Verifying', busy: true };
  if (result.pending) return { tone: 'muted', text: 'Awaiting signature' };
  if (!result.valid) return { tone: 'bad', text: 'Invalid proof' };
  if (result.state === 'SPENT') return { tone: 'muted', text: 'Transferred' };
  if (result.state !== 'UNSPENT') return { tone: 'warn', text: 'Verification unavailable' };
  return ready ? { tone: 'warn', text: 'Transfer pending' } : { tone: 'good', text: 'Verified owner' };
}

export function StatusBadge({ result, ready = false, large = false }) {
  const v = verdict(result, ready);
  return <span className={`badge badge-${v.tone} ${large ? 'badge-lg' : ''}`}>
    <AnimatePresence mode="popLayout" initial={false}>
      <motion.span key={v.text} className="badge-inner" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: .18 }}>
        {v.busy ? <Spinner size={11} /> : <span className="dot" />}{v.text}
      </motion.span>
    </AnimatePresence>
  </span>;
}

// An animated check mark drawn with pathLength.
export function DrawnCheck({ on, size = 18 }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
    <motion.path d="M5 12.5l4.2 4.2L19 7" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"
      initial={false} animate={{ pathLength: on ? 1 : 0, opacity: on ? 1 : 0 }} transition={{ duration: .35, ease: 'easeOut' }} />
  </svg>;
}

export function CheckRow({ state, children, detail }) {
  // state: 'ok' | 'fail' | 'pending' | 'unknown'
  return <li className={`check-row is-${state}`}>
    <span className="check-mark">
      {state === 'pending' ? <Spinner size={13} /> : state === 'fail' ? <X size={13} /> : state === 'unknown' ? <CircleHelp size={13} /> : <DrawnCheck on size={14} />}
    </span>
    <span><strong>{children}</strong>{detail && <small className="mono">{detail}</small>}</span>
  </li>;
}

// Press-and-hold confirmation for irreversible asset movements.
export function HoldButton({ onComplete, disabled = false, children, duration = 1400, icon = null, danger = false }) {
  const progress = useMotionValue(0);
  const width = useTransform(progress, (v) => `${v * 100}%`);
  const run = useRef(null);
  const [holding, setHolding] = useState(false);
  const start = () => {
    if (disabled || run.current) return;
    setHolding(true);
    run.current = animate(progress, 1, {
      duration: (1 - progress.get()) * duration / 1000, ease: 'linear',
      onComplete: () => { run.current = null; setHolding(false); progress.set(0); onComplete(); },
    });
  };
  const stop = () => {
    if (!run.current) return;
    run.current.stop(); run.current = null; setHolding(false);
    animate(progress, 0, { duration: .25, ease: 'easeOut' });
  };
  return <motion.button type="button" className={`btn btn-primary btn-lg hold ${danger ? 'hold-danger' : ''} ${holding ? 'is-holding' : ''}`} disabled={disabled}
    animate={{ scale: holding ? .985 : 1 }} transition={snappy}
    onPointerDown={(e) => { if (e.button === 0) start(); }} onPointerUp={stop} onPointerLeave={stop} onPointerCancel={stop}
    onKeyDown={(e) => { if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) { e.preventDefault(); start(); } }}
    onKeyUp={(e) => { if (e.key === ' ' || e.key === 'Enter') stop(); }}
    onClick={(e) => e.preventDefault()} onContextMenu={(e) => e.preventDefault()} aria-describedby="hold-hint">
    <motion.span className="hold-fill" style={{ width }} aria-hidden="true" />
    <span className="hold-label">{icon}{holding ? 'Keep holding…' : children}</span>
  </motion.button>;
}

export function Modal({ open, close, title, description, children, size = '' }) {
  return <Dialog.Root open={open} onOpenChange={(value) => { if (!value) close(); }}>
    <Dialog.Portal>
      <Dialog.Backdrop className="modal-backdrop" />
      <Dialog.Popup className={`modal ${size ? 'modal-' + size : ''}`}>
        <Dialog.Close className="icon-btn modal-close" aria-label="Close"><X size={18} /></Dialog.Close>
        {title && <Dialog.Title className="modal-title">{title}</Dialog.Title>}
        {description && <Dialog.Description className="modal-description">{description}</Dialog.Description>}
        {children}
      </Dialog.Popup>
    </Dialog.Portal>
  </Dialog.Root>;
}

/* Loading placeholders shaped like the content they stand in for. They fade
 * in after a short delay (fast loads never flash) and breathe gently. */
const Bone = ({ className = '', style }) => <span className={`sk ${className}`} style={style} />;
export function SkeletonCards({ count = 8, variant = 'nft', className = '' }) {
  return <div className={`grid sk-wrap ${variant === 'collection' ? 'grid-collections' : ''} ${className}`} role="status" aria-label="Loading">
    {Array.from({ length: count }, (_, i) => variant === 'collection'
      ? <div key={i} className="sk-card sk-collection"><Bone className="sk-cover" /><div className="sk-row"><Bone className="sk-avatar" /><div className="sk-lines"><Bone className="sk-line w-60" /><Bone className="sk-line w-40 thin" /></div></div></div>
      : <div key={i} className="sk-card sk-nft"><Bone className="sk-media" /><div className="sk-lines"><Bone className="sk-line w-70" /><Bone className="sk-line w-40 thin" /></div></div>)}
  </div>;
}
export function SkeletonRows({ count = 5, thumb = false, avatar = true, className = '' }) {
  return <div className={`sk-wrap sk-rows ${className}`} role="status" aria-label="Loading">
    {Array.from({ length: count }, (_, i) => <div key={i} className="sk-list-row">
      {thumb && <Bone className="sk-thumb" />}
      {avatar && !thumb && <Bone className="sk-dot" />}
      <div className="sk-lines"><Bone className="sk-line" style={{ width: `${62 - (i % 3) * 12}%` }} /><Bone className="sk-line w-30 thin" /></div>
    </div>)}
  </div>;
}
export function SkeletonDetail() {
  return <div className="detail sk-wrap" role="status" aria-label="Loading">
    <div className="sk-card sk-nft sk-detail-art"><Bone className="sk-media" /><div className="sk-lines"><Bone className="sk-line w-50" /><Bone className="sk-line w-30 thin" /></div></div>
    <div className="sk-panel"><Bone className="sk-line w-20 thin" /><Bone className="sk-title" /><Bone className="sk-pill" /><Bone className="sk-line w-70" /><Bone className="sk-line w-60" /><Bone className="sk-button" /></div>
  </div>;
}

/** Page and panel back navigation, one design everywhere. */
export function BackButton({ onClick, children = 'Back', disabled = false }) {
  return <button type="button" className="back" onClick={onClick} disabled={disabled}>
    <span className="back-icon" aria-hidden="true"><ArrowLeft size={15} strokeWidth={2.6} /></span>{children}
  </button>;
}

export function Notice({ tone = 'warn', children, action = null }) {
  return <motion.div className={`notice notice-${tone}`} role="status" initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }}>
    <CircleAlert size={16} /><div>{children}</div>{action}
  </motion.div>;
}

// Shown when the browser keeps no on-device storage (e.g. Lockdown Mode).
export function StorageNotice() {
  const [ephemeral, setEphemeral] = useState(false);
  useEffect(() => { onDeviceStorage().then((ok) => setEphemeral(!ok)); }, []);
  if (!ephemeral) return null;
  return <Notice>This browser doesn't allow on-device storage, so your wallets load from their encrypted server backup on each visit. Keep your collection key saved.</Notice>;
}

// Shared panel transition for multi-step dialogs.
export const panel = {
  initial: { opacity: 0, x: 16 }, animate: { opacity: 1, x: 0 }, exit: { opacity: 0, x: -16 },
  transition: { type: 'spring', stiffness: 380, damping: 34 },
};
