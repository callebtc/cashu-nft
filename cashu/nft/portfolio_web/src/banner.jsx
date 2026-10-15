import React, { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Minus, Monitor, Plus, Smartphone } from 'lucide-react';
import { Button, Identicon, identiconColor } from './ui.jsx';
import { imageUrl } from './formats.mjs';

// Banners are 3:1 at 1500x500, the size Twitter/X uses (the server re-encodes
// to the same size, portfolio_image.BANNER_SIZE).
export const BANNER_WIDTH = 1500, BANNER_HEIGHT = 500;
const MAX_ZOOM = 5;
const MAX_SOURCE_BYTES = 20 * 1024 * 1024;

export const bannerUrl = (pubkey, version) => `/api/banners/${pubkey}.jpg?v=${version}`;

/** The profile header's banner: an uploaded or cropped banner (`src`), a
 *  legacy NFT cover shown whole (`cover`), or a mosaic of the latest NFTs. */
export function ProfileBanner({ pubkey, src, cover, cards = [], style }) {
  const tint = { '--tint': identiconColor(pubkey), ...style };
  if (src) return <div className="profile-banner is-banner" style={tint}><img src={src} alt="" /></div>;
  if (cover) return <div className="profile-banner banner-1 is-cover" style={tint}><img src={imageUrl(cover)} alt="" /></div>;
  const n = cards.length >= 6 ? 6 : cards.length >= 3 ? 3 : cards.length ? 1 : 0;
  return <div className={`profile-banner banner-${n}`} style={tint}>
    {cards.slice(0, n).map((c) => <img key={c.id} src={imageUrl(c.h)} alt="" />)}
  </div>;
}

// The profile header's geometry in CSS px (style.css: .page, .profile-main,
// .profile-avatar, .profile-banner, .profile-id h1), at the widest desktop
// card and a typical phone. The preview scales it to its own width.
const DEVICES = {
  desktop: { width: 1192, left: 28, top: 16, gap: 20, avatar: 102, overlap: 60, radius: 26, inner: 20, pad: 5, name: 54, auto: 180 },
  phone: { width: 347, left: 16, top: 12, gap: 12, avatar: 78, overlap: 46, radius: 26, inner: 20, pad: 5, name: 32, auto: 120 },
};
export const defaultDevice = () => (window.matchMedia?.('(max-width: 640px)').matches ? 'phone' : 'desktop');

export function DeviceToggle({ value, onChange }) {
  return <div className="tabs device-toggle" role="tablist" aria-label="Preview size">
    {[['desktop', 'Desktop', Monitor], ['phone', 'Phone', Smartphone]].map(([key, label, Icon]) =>
      <button key={key} type="button" role="tab" aria-selected={value === key} className={`tab ${value === key ? 'is-active' : ''}`} onClick={() => onChange(key)}>
        <Icon size={14} />{label}
      </button>)}
  </div>;
}

/** A scaled copy of the profile header: banner, overlapping picture and name.
 *  `banner` fills the banner area; `ratio` is its width / height. */
export function HeaderPreview({ device, banner, ratio, avatar, name }) {
  const d = DEVICES[device];
  const cq = (px) => `calc(${px / d.width} * 100cqw)`;
  return <div className={`header-preview is-${device}`} aria-hidden="true">
    <div className="hp-card" style={{
      '--hp-ratio': ratio ?? d.width / d.auto, '--hp-left': cq(d.left), '--hp-top': cq(d.top), '--hp-gap': cq(d.gap),
      '--hp-avatar': cq(d.avatar), '--hp-overlap': cq(d.overlap), '--hp-radius': cq(d.radius), '--hp-inner': cq(d.inner),
      '--hp-pad': cq(d.pad), '--hp-name': cq(d.name),
    }}>
      <div className="hp-banner">{banner}</div>
      <div className="hp-main">
        <div className="hp-avatar">{avatar}</div>
        <strong className="hp-name ellipsis">{name || 'Collection'}</strong>
      </div>
    </div>
  </div>;
}

/** Decode a picture for cropping. EXIF orientation is applied, as the <img>
 *  in the cropper does. */
export async function loadBannerSource(blob) {
  if (blob.size > MAX_SOURCE_BYTES) throw new Error('Pick a picture up to 20 MB.');
  const bitmap = await createImageBitmap(blob, { imageOrientation: 'from-image' }).catch(() => { throw new Error('This picture can’t be read. Try a JPG or PNG.'); });
  return { bitmap, url: URL.createObjectURL(blob), width: bitmap.width, height: bitmap.height };
}

export function releaseBannerSource(source) {
  if (!source) return;
  source.bitmap.close?.();
  URL.revokeObjectURL(source.url);
}

// A view is the zoom and the image point (in 0..1 image coordinates) at the
// frame's centre. Zoom 1 is the smallest scale that still covers the frame.
function visible(source, z) {
  const r = source.width / source.height;
  return { vw: 1 / (z * Math.max(1, r / 3)), vh: 1 / (z * Math.max(1, 3 / r)) };
}
function clampView(source, { z, cx, cy }) {
  z = Math.min(MAX_ZOOM, Math.max(1, z));
  const { vw, vh } = visible(source, z);
  return { z, cx: Math.min(1 - vw / 2, Math.max(vw / 2, cx)), cy: Math.min(1 - vh / 2, Math.max(vh / 2, cy)) };
}
/** Zoom by `factor`, keeping the image point under (u, v) of the frame still. */
function zoomView(source, view, factor, u = .5, v = .5) {
  const before = visible(source, view.z);
  const z = Math.min(MAX_ZOOM, Math.max(1, view.z * factor));
  const after = visible(source, z);
  return clampView(source, {
    z,
    cx: view.cx + (u - .5) * (before.vw - after.vw),
    cy: view.cy + (v - .5) * (before.vh - after.vh),
  });
}

async function renderBanner(source, view) {
  const { vw, vh } = visible(source, view.z);
  const canvas = document.createElement('canvas');
  canvas.width = BANNER_WIDTH; canvas.height = BANNER_HEIGHT;
  const ctx = canvas.getContext('2d');
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(source.bitmap,
    (view.cx - vw / 2) * source.width, (view.cy - vh / 2) * source.height, vw * source.width, vh * source.height,
    0, 0, BANNER_WIDTH, BANNER_HEIGHT);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9));
  if (!blob) throw new Error('This picture can’t be cropped here. Try a smaller one.');
  return { bytes: new Uint8Array(await blob.arrayBuffer()), preview: URL.createObjectURL(blob) };
}

/** Drag to position, pinch, scroll or use the slider to zoom. The frame sits
 *  in a scaled profile header, so the picture and name show where they will. */
export function BannerCropper({ source, avatar, name, device, setDevice, onCancel, onApply }) {
  const [view, setView] = useState({ z: 1, cx: .5, cy: .5 });
  const [dragging, setDragging] = useState(false), [applying, setApplying] = useState(false);
  const frame = useRef(null), pointers = useRef(new Map());
  const { vw, vh } = visible(source, view.z);

  const pan = useCallback((dx, dy) => setView((prev) => {
    const { vw: w, vh: h } = visible(source, prev.z);
    return clampView(source, { ...prev, cx: prev.cx - dx * w, cy: prev.cy - dy * h });
  }), [source]);
  const zoom = useCallback((factor, u, v) => setView((prev) => zoomView(source, prev, factor, u, v)), [source]);

  useEffect(() => {
    const el = frame.current;
    const onWheel = (event) => {
      event.preventDefault();
      const rect = el.getBoundingClientRect();
      zoom(Math.exp(-event.deltaY * 0.0025), (event.clientX - rect.left) / rect.width, (event.clientY - rect.top) / rect.height);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [zoom]);

  const down = (event) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    setDragging(true);
  };
  const move = (event) => {
    const all = pointers.current, last = all.get(event.pointerId);
    if (!last) return;
    const rect = frame.current.getBoundingClientRect();
    const now = { x: event.clientX, y: event.clientY };
    if (all.size === 1) pan((now.x - last.x) / rect.width, (now.y - last.y) / rect.height);
    else if (all.size === 2) {
      const [other] = [...all].filter(([id]) => id !== event.pointerId).map(([, p]) => p);
      const before = Math.hypot(last.x - other.x, last.y - other.y), after = Math.hypot(now.x - other.x, now.y - other.y);
      if (before > 0) zoom(after / before, ((now.x + other.x) / 2 - rect.left) / rect.width, ((now.y + other.y) / 2 - rect.top) / rect.height);
    }
    all.set(event.pointerId, now);
  };
  const up = (event) => {
    pointers.current.delete(event.pointerId);
    if (!pointers.current.size) setDragging(false);
  };
  const key = (event) => {
    const step = event.shiftKey ? .2 : .04;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (moves[event.key]) pan(...moves[event.key]);
    else if (event.key === '+' || event.key === '=') zoom(1.1);
    else if (event.key === '-' || event.key === '_') zoom(1 / 1.1);
    else return;
    event.preventDefault();
  };
  const apply = async () => {
    setApplying(true);
    try { onApply(await renderBanner(source, view)); } catch (e) { setApplying(false); toast.error(e.message); }
  };

  const crop = <div ref={frame} className={`crop-frame ${dragging ? 'is-dragging' : ''}`} tabIndex={0} role="application"
    aria-label="Banner crop. Drag or use the arrow keys to move the picture, scroll or press plus and minus to zoom."
    onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={up} onKeyDown={key}>
    <img src={source.url} alt="" draggable={false} style={{
      width: `${100 / vw}%`, height: `${100 / vh}%`,
      left: `${-(view.cx - vw / 2) / vw * 100}%`, top: `${-(view.cy - vh / 2) / vh * 100}%`,
    }} />
    <span className="crop-grid" />
  </div>;

  return <div className="stack banner-cropper">
    <HeaderPreview device={device} banner={crop} ratio={3} avatar={avatar} name={name} />
    <div className="crop-controls">
      <button type="button" className="icon-btn icon-btn-bordered" aria-label="Zoom out" onClick={() => zoom(1 / 1.2)} disabled={view.z <= 1}><Minus size={16} /></button>
      <input type="range" className="crop-zoom" aria-label="Zoom" min={1} max={MAX_ZOOM} step={0.01} value={view.z}
        onChange={(e) => zoom(Number(e.target.value) / view.z)} />
      <button type="button" className="icon-btn icon-btn-bordered" aria-label="Zoom in" onClick={() => zoom(1.2)} disabled={view.z >= MAX_ZOOM}><Plus size={16} /></button>
    </div>
    <div className="crop-footer">
      <DeviceToggle value={device} onChange={setDevice} />
      <span className="hint">Drag to reposition.</span>
    </div>
    <div className="crop-actions">
      <Button type="button" variant="secondary" onClick={onCancel} disabled={applying}>Cancel</Button>
      <Button type="button" variant="primary" onClick={apply} disabled={applying}>Apply</Button>
    </div>
  </div>;
}

/** The picture in a header preview: a pending upload, the plain identicon
 *  when the picture is being removed, or the saved one. */
export function PreviewAvatar({ pubkey, picture, removed }) {
  if (picture) return <img src={picture} alt="" />;
  return <Identicon pubkey={pubkey} size={88} plain={removed} />;
}
