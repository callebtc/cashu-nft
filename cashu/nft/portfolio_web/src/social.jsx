import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { toast } from 'sonner';
import { ArrowDownToLine, Gavel, Heart, Image as ImageIcon, Search, Sparkles, Tag, UserPlus, UserCheck, Users } from 'lucide-react';
import { getJSON, signedRequest } from './api.mjs';
import { uploadHeaders } from './turnstile.mjs';
import { Button, Identicon, Modal, SkeletonCards, SkeletonRows, Spinner, Tilt, identiconColor, setAvatarVersion, useTint } from './ui.jsx';
import { imageUrl } from './formats.mjs';

export { imageUrl };

export function ago(seconds) {
  const d = Math.max(0, Date.now() / 1000 - seconds);
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  if (d < 86400 * 30) return `${Math.floor(d / 86400)}d ago`;
  return new Date(seconds * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

export async function socialPost(identity, path, body) {
  const response = await signedRequest(identity.secret, `/api/profiles/${identity.pubkey}${path}`, JSON.stringify(body), 'application/json');
  return response.json();
}

// The viewer's likes and follows, kept in one place so every button agrees.
export function useRelations(identity) {
  const [relations, setRelations] = useState({ likes: [], following: [] });
  useEffect(() => {
    if (!identity) { setRelations({ likes: [], following: [] }); return; }
    let live = true;
    getJSON(`/api/profiles/${identity.pubkey}/relations`).then((r) => { if (live) setRelations(r); }).catch(() => {});
    return () => { live = false; };
  }, [identity]);
  const toggle = useCallback(async (kind, target, on) => {
    const key = kind === 'likes' ? 'likes' : 'following';
    setRelations((r) => ({ ...r, [key]: on ? [...new Set([...r[key], target])] : r[key].filter((t) => t !== target) }));
    try { return await socialPost(identity, `/${kind}/${target}`, { on }); }
    catch (e) {
      setRelations((r) => ({ ...r, [key]: on ? r[key].filter((t) => t !== target) : [...r[key], target] }));
      throw e;
    }
  }, [identity]);
  return [relations, toggle];
}

export function LikeButton({ liked, count, onToggle, size = '' }) {
  const [burst, setBurst] = useState(0);
  return <motion.button className={`like-btn ${liked ? 'is-liked' : ''} ${size ? 'like-' + size : ''}`} whileTap={{ scale: .9 }}
    onClick={(e) => { e.stopPropagation(); if (!liked) setBurst((b) => b + 1); onToggle(!liked); }} aria-pressed={liked} aria-label={liked ? 'Unlike collection' : 'Like collection'}>
    <span className="like-icon">
      <motion.span key={burst} initial={burst ? { scale: .4 } : false} animate={{ scale: [null, 1.35, 1] }} transition={{ duration: .45 }} style={{ display: 'grid' }}>
        <Heart size={size === 'sm' ? 14 : 17} fill={liked ? 'currentColor' : 'none'} strokeWidth={2.4} />
      </motion.span>
      <AnimatePresence>{burst > 0 && <motion.span key={'b' + burst} className="like-burst" initial={{ scale: .3, opacity: 1 }} animate={{ scale: 2.2, opacity: 0 }} exit={{ opacity: 0 }} transition={{ duration: .5 }} />}</AnimatePresence>
    </span>
    <AnimatePresence mode="popLayout" initial={false}>
      <motion.span key={count} initial={{ y: -10, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: 10, opacity: 0 }} transition={{ type: 'spring', stiffness: 500, damping: 30 }}>{count}</motion.span>
    </AnimatePresence>
  </motion.button>;
}

export function FollowButton({ following, onToggle }) {
  return <Button variant={following ? 'secondary' : 'primary'} icon={following ? <UserCheck size={16} /> : <UserPlus size={16} />} onClick={() => onToggle(!following)}>
    {following ? 'Following' : 'Follow'}
  </Button>;
}

export function CollectionCard({ item, index = 0, onOpen, liked, onLike, rank }) {
  const [tint, onLoad] = useTint(item.cover || item.pubkey);
  const previews = item.previews?.length ? item.previews : [];
  return <motion.div className="card-slot" initial={{ opacity: 0, y: 18 }} whileInView={{ opacity: 1, y: 0 }} viewport={{ once: true }}
    transition={{ delay: Math.min(index, 8) * .05, type: 'spring', stiffness: 260, damping: 26 }}>
    <Tilt max={7}>
      <div role="link" tabIndex={0} className="collection-card" style={{ '--tint': tint || identiconColor(item.pubkey) }}
        onClick={() => onOpen(item.pubkey)} onKeyDown={(e) => { if (e.key === 'Enter') onOpen(item.pubkey); }}>
        {rank && <span className="rank">#{rank}</span>}
        <div className={`collection-cover covers-${item.custom_cover || previews.length < 4 ? 1 : 4}`}>
          {item.cover ? (item.custom_cover || previews.length < 4
            ? <img src={imageUrl(item.cover)} alt="" loading="lazy" onLoad={onLoad} />
            : previews.map((h, i) => <img key={h} src={imageUrl(h)} alt="" loading="lazy" onLoad={i === 0 ? onLoad : undefined} />))
            : <span className="cover-empty"><ImageIcon size={28} /></span>}
        </div>
        <div className="collection-body">
          <Identicon pubkey={item.pubkey} size={40} />
          <div className="collection-text"><strong className="ellipsis">{item.name}</strong><span>{item.nfts} {item.nfts === 1 ? 'NFT' : 'NFTs'} · {item.followers} {item.followers === 1 ? 'follower' : 'followers'}</span></div>
          <LikeButton size="sm" liked={liked} count={item.likes} onToggle={(on) => onLike(item.pubkey, on)} />
        </div>
      </div>
    </Tilt>
  </motion.div>;
}

export function MarketCard({ item, index = 0, onOpen }) {
  const [tint, onLoad] = useTint(item.h);
  return <motion.div className="card-slot" initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }}
    transition={{ delay: Math.min(index, 10) * .035, type: 'spring', stiffness: 260, damping: 26 }}>
    <Tilt>
      <button className="nft-card" style={tint ? { '--tint': tint } : undefined} onClick={() => onOpen(item)} aria-label={`Open ${item.title}`}>
        <div className="nft-media"><img src={imageUrl(item.h)} alt="" loading="lazy" decoding="async" onLoad={onLoad} /></div>
        <div className="nft-body">
          <span className="nft-title">{item.title}</span>
          <span className="nft-meta"><span className="owner-chip"><Identicon pubkey={item.pubkey} size={18} /><span className="ellipsis">{item.owner_name}</span></span><span className="nft-date">{ago(item.created)}</span></span>
        </div>
      </button>
    </Tilt>
  </motion.div>;
}

const VERB = { mint: 'minted', receive: 'received', collection: 'started a collection', like: 'liked', follow: 'followed', sale: 'bought', bid: 'bid' };
const KIND_ICON = { mint: <Sparkles size={15} />, receive: <ArrowDownToLine size={15} />, collection: <ImageIcon size={15} />, like: <Heart size={15} />, follow: <UserPlus size={15} />, sale: <Tag size={15} />, bid: <Gavel size={15} /> };

export function ActivityItem({ event, onProfile, onCard, index = 0 }) {
  const who = (pk, name) => <button className="who" onClick={() => onProfile(pk)}><Identicon pubkey={pk} size={22} /><span>{name || 'A collector'}</span></button>;
  return <motion.li className={`event kind-${event.kind}`} layout initial={{ opacity: 0, x: -12 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: Math.min(index, 12) * .03, type: 'spring', stiffness: 320, damping: 28 }}>
    <span className="event-icon">{KIND_ICON[event.kind]}</span>
    <div className="event-text">
      {who(event.actor, event.actor_name)}
      <span className="verb">{VERB[event.kind]}</span>
      {(event.kind === 'mint' || event.kind === 'receive' || event.kind === 'sale') && <button className="event-title" onClick={() => onCard(event)}>{event.title}</button>}
      {(event.kind === 'receive' || event.kind === 'sale') && event.target && <><span className="verb">from</span>{who(event.target, event.target_name)}</>}
      {event.kind === 'sale' && event.price != null && <><span className="verb">for</span><span className="event-price"><Tag size={12} />{Number(event.price).toLocaleString()} sats</span></>}
      {event.kind === 'bid' && <><span className="event-price"><Tag size={12} />{Number(event.price).toLocaleString()} sats</span><span className="verb">on</span><button className="event-title" onClick={() => onCard(event)}>{event.title}</button></>}
      {(event.kind === 'like' || event.kind === 'follow') && event.target && who(event.target, event.target_name)}
    </div>
    <span className="event-time">{ago(event.created)}</span>
    {event.h && <button className="event-thumb" onClick={() => onCard(event)} aria-label={`Open ${event.title}`}><img src={imageUrl(event.h)} alt="" loading="lazy" /></button>}
  </motion.li>;
}

export function ActivityList({ source, onProfile, onCard, empty, pageSize = 30 }) {
  const [events, setEvents] = useState(null), [more, setMore] = useState(false), [loading, setLoading] = useState(false);
  const run = useRef(0);
  useEffect(() => {
    if (!source) { setEvents([]); return; }
    const id = ++run.current;
    setEvents(null);
    getJSON(`${source}${source.includes('?') ? '&' : '?'}limit=${pageSize}`).then((list) => { if (id === run.current) { setEvents(list); setMore(list.length === pageSize); } })
      .catch((e) => { if (id === run.current) { setEvents([]); toast.error(e.message); } });
  }, [source, pageSize]);
  const loadMore = async () => {
    if (!events?.length) return;
    setLoading(true);
    try {
      const before = events[events.length - 1].created;
      const list = await getJSON(`${source}${source.includes('?') ? '&' : '?'}limit=${pageSize}&before=${before}`);
      const seen = new Set(events.map((e) => e.id));
      setEvents([...events, ...list.filter((e) => !seen.has(e.id))]); setMore(list.length === pageSize);
    } catch (e) { toast.error(e.message); } finally { setLoading(false); }
  };
  if (!events) return <SkeletonRows count={6} />;
  if (!events.length) return <div className="empty"><strong>{empty?.title || 'Quiet for now'}</strong><span className="muted">{empty?.text || 'Nothing has happened here yet.'}</span>{empty?.action}</div>;
  return <>
    <ul className="feed">{events.map((e, i) => <ActivityItem key={e.id} event={e} index={i} onProfile={onProfile} onCard={onCard} />)}</ul>
    {more && <div className="load-more"><Button variant="secondary" onClick={loadMore} disabled={loading} icon={loading ? <Spinner /> : null}>Load more</Button></div>}
  </>;
}

function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

export function Segmented({ options, value, onChange, id }) {
  return <div className="tabs" role="tablist">
    {options.map(([key, label]) => <button key={key} role="tab" aria-selected={value === key} className={`tab ${value === key ? 'is-active' : ''}`} onClick={() => onChange(key)}>
      {label}{value === key && <motion.span layoutId={`seg-${id}`} className="tab-underline" transition={{ type: 'spring', stiffness: 500, damping: 38 }} />}
    </button>)}
  </div>;
}

export function ExplorePage({ tab, navigate, relations, onLike, openCard }) {
  const [sort, setSort] = useState(tab === 'nfts' ? 'new' : 'popular');
  const [query, setQuery] = useState('');
  const q = useDebounced(query.trim());
  const [items, setItems] = useState(null), [more, setMore] = useState(false), [loading, setLoading] = useState(false);
  const run = useRef(0);
  useEffect(() => { setSort(tab === 'nfts' ? 'new' : 'popular'); }, [tab]);
  const endpoint = tab === 'nfts' ? '/api/explore/nfts' : '/api/explore/collections';
  const load = useCallback(async (offset = 0) => {
    const id = ++run.current;
    if (!offset) setItems(null);
    setLoading(true);
    try {
      const data = await getJSON(`${endpoint}?sort=${sort}&q=${encodeURIComponent(q)}&limit=24&offset=${offset}`);
      if (id !== run.current) return;
      setItems((prev) => offset ? [...(prev || []), ...data.items] : data.items); setMore(data.more);
    } catch (e) { toast.error(e.message); setItems((prev) => prev || []); } finally { if (id === run.current) setLoading(false); }
  }, [endpoint, sort, q]);
  useEffect(() => { load(0); }, [load]);
  const like = async (target, on) => {
    try {
      const summary = await onLike(target, on);
      if (summary) setItems((list) => list.map((i) => i.pubkey === target ? { ...i, likes: summary.likes } : i));
    } catch { /* toast shown by caller */ }
  };
  const sorts = tab === 'nfts' ? [['new', 'Newest'], ['old', 'Oldest'], ['title', 'A–Z']] : [['popular', 'Popular'], ['new', 'Newest'], ['largest', 'Biggest']];
  return <main className="page explore">
    <header className="page-head">
      <div><span className="pill">Explore</span><h1>{tab === 'nfts' ? 'Every NFT on the mint' : 'Collections worth a look'}</h1></div>
      <Segmented id="explore" value={tab} onChange={(t) => navigate(t === 'nfts' ? '/explore/nfts' : '/explore')} options={[['collections', 'Collections'], ['nfts', 'NFTs']]} />
    </header>
    <div className="toolbar">
      <label className="search"><Search size={17} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={tab === 'nfts' ? 'Search NFTs by title' : 'Search collections'} aria-label="Search" /></label>
      <div className="sorts">{sorts.map(([key, label]) => <button key={key} className={`sort ${sort === key ? 'is-active' : ''}`} onClick={() => setSort(key)}>{label}</button>)}</div>
    </div>
    {!items ? <SkeletonCards count={tab === 'nfts' ? 8 : 6} variant={tab === 'nfts' ? 'nft' : 'collection'} />
      : !items.length ? <div className="empty"><strong>Nothing found</strong><span className="muted">{q ? 'Try a different search.' : 'Be the first to mint something.'}</span></div>
        : <div className={`grid ${tab === 'collections' ? 'grid-collections' : ''}`}>
          {tab === 'nfts'
            ? items.map((item, i) => <MarketCard key={item.id} item={item} index={i} onOpen={openCard} />)
            : items.map((item, i) => <CollectionCard key={item.pubkey} item={item} index={i} rank={sort === 'popular' && !q && i < 3 && item.likes > 0 ? i + 1 : null} liked={relations.likes.includes(item.pubkey)} onLike={like} onOpen={(pk) => navigate(`/p/${pk}`)} />)}
        </div>}
    {more && <div className="load-more"><Button variant="secondary" onClick={() => load(items.length)} disabled={loading} icon={loading ? <Spinner /> : null}>Load more</Button></div>}
  </main>;
}

export function ActivityPage({ identity, navigate, openCard }) {
  const [scope, setScope] = useState('everyone');
  const source = scope === 'following' ? (identity ? `/api/profiles/${identity.pubkey}/feed` : null) : '/api/activity';
  return <main className="page activity">
    <header className="page-head">
      <div><span className="pill">Live</span><h1>What's happening</h1></div>
      <Segmented id="activity" value={scope} onChange={setScope} options={[['everyone', 'Everyone'], ['following', 'Following']]} />
    </header>
    {scope === 'following' && !identity
      ? <div className="empty"><strong>Follow collectors to fill your feed</strong><span className="muted">Start a collection, then follow the people whose drops you want to see.</span></div>
      : <ActivityList source={source} onProfile={(pk) => navigate(`/p/${pk}`)} onCard={openCard}
        empty={scope === 'following' ? { title: 'Your feed is empty', text: 'Follow a few collectors and their mints and receipts show up here.', action: <Button variant="primary" onClick={() => navigate('/explore')}>Find collections</Button> } : null} />}
  </main>;
}

export function NetworkDialog({ pubkey, open, initial, close, navigate }) {
  const [tab, setTab] = useState(initial || 'followers');
  const [data, setData] = useState(null);
  useEffect(() => { if (open) { setTab(initial || 'followers'); setData(null); getJSON(`/api/profiles/${pubkey}/network`).then(setData).catch((e) => toast.error(e.message)); } }, [open, pubkey, initial]);
  const list = data?.[tab] || [];
  return <Modal open={open} close={close} title="Network">
    <div className="stack">
      <Segmented id="network" value={tab} onChange={setTab} options={[['followers', `Followers${data ? ' ' + data.followers.length : ''}`], ['following', `Following${data ? ' ' + data.following.length : ''}`]]} />
      {!data ? <SkeletonRows count={4} className="sk-compact" />
        : !list.length ? <p className="muted">{tab === 'followers' ? 'No followers yet.' : 'Not following anyone yet.'}</p>
          : <ul className="people">{list.map((p) => <li key={p.pubkey}><button onClick={() => { close(); navigate(`/p/${p.pubkey}`); }}><Identicon pubkey={p.pubkey} size={34} /><strong className="ellipsis">{p.name}</strong><Users size={14} /></button></li>)}</ul>}
    </div>
  </Modal>;
}

const MAX_AVATAR_BYTES = 5 * 1024 * 1024;

/** Downscale in the browser (512 px JPG) so uploads stay small; the server
 *  re-encodes to its final 256 px square. */
async function shrinkPicture(file) {
  if (file.size > MAX_AVATAR_BYTES) throw new Error('Pick a picture up to 5 MB.');
  const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' }).catch(() => { throw new Error('This picture can’t be read. Try a JPG or PNG.'); });
  const scale = Math.min(1, 512 / Math.min(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9));
  return new Uint8Array(await blob.arrayBuffer());
}

export function EditCollectionDialog({ open, close, profile, identity, onSaved }) {
  const [name, setName] = useState(''), [cover, setCover] = useState(''), [saving, setSaving] = useState(false);
  const [picture, setPicture] = useState(null), [removePicture, setRemovePicture] = useState(false), [pictureError, setPictureError] = useState('');
  const fileRef = useRef(null);
  const active = profile?.cards.filter((c) => c.status !== 'sent') || [];
  useEffect(() => {
    if (open && profile) { setName(profile.name); setCover(profile.custom_cover ? profile.cover : ''); setPicture(null); setRemovePicture(false); setPictureError(''); }
  }, [open, profile]);
  useEffect(() => () => { if (picture) URL.revokeObjectURL(picture.preview); }, [picture]);
  const choose = async (file) => {
    if (!file) return;
    setPictureError('');
    try { const bytes = await shrinkPicture(file); setPicture({ bytes, preview: URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' })) }); setRemovePicture(false); }
    catch (e) { setPictureError(e.message); }
  };
  const hasPicture = picture || (profile?.avatar && !removePicture);
  const save = async (event) => {
    event.preventDefault();
    setSaving(true);
    try {
      let updated;
      const base = `/api/profiles/${identity.pubkey}`;
      if (picture) updated = await (await signedRequest(identity.secret, `${base}/avatar`, picture.bytes, 'image/jpeg', '', await uploadHeaders())).json();
      else if (removePicture && profile?.avatar) updated = await (await signedRequest(identity.secret, `${base}/avatar/remove`)).json();
      updated = await socialPost(identity, '/settings', { name: name.trim(), cover });
      setAvatarVersion(identity.pubkey, updated.avatar);
      onSaved(updated); toast.success('Profile updated.'); close();
    } catch (e) { toast.error(e.message); } finally { setSaving(false); }
  };
  return <Modal open={open} close={() => { if (!saving) close(); }} title="Edit profile" description="Your name, picture and cover.">
    <form className="stack" onSubmit={save}>
      <div className="field"><span>Picture</span>
        <div className="avatar-edit">
          <span className="avatar-preview">
            {picture ? <img src={picture.preview} alt="" /> : hasPicture ? <img src={`/api/avatars/${identity.pubkey}.jpg?v=${profile.avatar}`} alt="" />
              : <Identicon pubkey={identity.pubkey} size={72} />}
          </span>
          <div className="avatar-actions">
            <input ref={fileRef} type="file" accept="image/*" hidden onChange={(e) => { choose(e.target.files?.[0]); e.target.value = ''; }} />
            <Button type="button" variant="secondary" size="sm" onClick={() => fileRef.current?.click()} disabled={saving}>{hasPicture ? 'Change picture' : 'Upload picture'}</Button>
            {hasPicture && <Button type="button" variant="ghost" size="sm" onClick={() => { setPicture(null); setRemovePicture(true); }} disabled={saving}>Remove</Button>}
            <span className="hint">JPG, PNG or WebP, up to 5 MB.</span>
          </div>
        </div>
        {pictureError && <span className="field-error">{pictureError}</span>}
      </div>
      <label className="field"><span>Name</span><input maxLength={40} value={name} onChange={(e) => setName(e.target.value)} required /></label>
      <div className="field"><span>Cover</span>
        <div className="cover-picker">
          <button type="button" className={`cover-option cover-default ${cover === '' ? 'is-picked' : ''}`} onClick={() => setCover('')}><span>Auto</span></button>
          {active.map((c) => <motion.button type="button" key={c.id} whileTap={{ scale: .92 }} className={`cover-option ${cover === c.h ? 'is-picked' : ''}`} onClick={() => setCover(c.h)} title={c.title}>
            <img src={imageUrl(c.h)} alt={c.title} />
          </motion.button>)}
        </div>
        <span className="hint">Auto shows your latest NFTs as a mosaic.</span>
      </div>
      <Button variant="primary" size="lg" className="full" type="submit" disabled={saving || !name.trim()} icon={saving ? <Spinner /> : null}>Save changes</Button>
    </form>
  </Modal>;
}
