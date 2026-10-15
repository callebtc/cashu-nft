// Market browse controls: URL-backed filter state, the filter panel (price
// histogram and range, availability, bids, age, seller), the sort
// menu and the active-filter chips.
import React, { useEffect, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { Menu } from '@base-ui/react/menu';
import { ArrowUpDown, Check, ChevronDown, Search, X } from 'lucide-react';
import { Identicon } from './ui.jsx';

const num = (n) => Number(n).toLocaleString();
const compact = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });

export const SORTS = [['new', 'Newest'], ['old', 'Oldest'], ['price_asc', 'Price: low to high'], ['price_desc', 'Price: high to low'], ['bids', 'Most bids'], ['top_bid', 'Highest bid']];
const SINCE = [['', 'Any time'], ['1d', '24 hours', 86400], ['7d', '7 days', 7 * 86400], ['30d', '30 days', 30 * 86400]];
const BIDS = [['', 'Any'], ['some', 'Has bids'], ['none', 'No bids yet']];
const MAX_SELLERS = 20; // the listings API accepts up to 20
const PRESETS = [['Under 1k', null, 1000], ['1k–10k', 1000, 10000], ['10k–100k', 10000, 100000], ['100k+', 100000, null]];

/* ---------- state ---------- */

const EMPTY = { q: '', sort: 'new', min: null, max: null, bids: '', since: '', available: false, sellers: [] };

function fromUrl() {
  const p = new URLSearchParams(window.location.search);
  const int = (k) => (/^\d{1,10}$/.test(p.get(k) || '') ? Number(p.get(k)) : null);
  const list = (k, ok) => (p.get(k) || '').split(',').filter(ok);
  return {
    q: p.get('q') || '', sort: SORTS.some(([k]) => k === p.get('sort')) ? p.get('sort') : 'new', min: int('min'), max: int('max'),
    bids: BIDS.some(([k]) => k && k === p.get('bids')) ? p.get('bids') : '', since: SINCE.some(([k]) => k && k === p.get('since')) ? p.get('since') : '',
    available: p.get('available') === '1', sellers: list('seller', (v) => /^[0-9a-f]{64}$/.test(v)).slice(0, MAX_SELLERS),
  };
}

function toUrl(f) {
  const p = new URLSearchParams();
  if (f.q) p.set('q', f.q);
  if (f.sort !== 'new') p.set('sort', f.sort);
  if (f.min != null) p.set('min', f.min);
  if (f.max != null) p.set('max', f.max);
  if (f.bids) p.set('bids', f.bids);
  if (f.since) p.set('since', f.since);
  if (f.available) p.set('available', '1');
  if (f.sellers.length) p.set('seller', f.sellers.join(','));
  return p.toString();
}

/** The listings query for a filter state. */
export function listingQuery(f, offset, limit = 24) {
  const p = new URLSearchParams({ sort: f.sort, q: f.q.trim(), limit, offset });
  if (f.min != null) p.set('min_price', f.min);
  if (f.max != null) p.set('max_price', f.max);
  if (f.bids) p.set('bids', f.bids);
  if (f.since) p.set('since', SINCE.find(([k]) => k === f.since)[2]);
  if (f.available) p.set('available', '1');
  if (f.sellers.length) p.set('sellers', f.sellers.join(','));
  return p.toString();
}

/** Filter state mirrored into the address bar, so a filtered view can be shared. */
export function useFilters() {
  const [filters, setFilters] = useState(fromUrl);
  useEffect(() => {
    const query = toUrl(filters);
    window.history.replaceState(window.history.state, '', `/market${query ? `?${query}` : ''}`);
  }, [filters]);
  const set = (patch) => setFilters((f) => ({ ...f, ...patch }));
  const clear = () => setFilters((f) => ({ ...EMPTY, q: f.q, sort: f.sort }));
  const active = (filters.min != null || filters.max != null) + !!filters.bids + !!filters.since + filters.available + !!filters.sellers.length;
  return { filters, set, clear, active };
}

export function useDebouncedValue(value, ms = 250) {
  const [v, setV] = useState(value);
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t); }, [value, ms]);
  return v;
}

export function useWide(query = '(min-width: 961px)') {
  const [wide, setWide] = useState(() => window.matchMedia(query).matches);
  useEffect(() => { const m = window.matchMedia(query), on = () => setWide(m.matches); m.addEventListener('change', on); return () => m.removeEventListener('change', on); }, [query]);
  return wide;
}

/* ---------- price ---------- */

// Prices span orders of magnitude, so the histogram and slider are logarithmic.
const STEPS = 1000;
const nice = (n) => { const p = 10 ** Math.max(0, Math.floor(Math.log10(n)) - 1); return Math.round(n / p) * p; };

function PriceRange({ facet, min, max, onChange }) {
  const lo = facet?.min ?? 1, hi = Math.max(facet?.max ?? 1_000_000, lo + 1);
  const toPos = (v) => Math.round(Math.min(1, Math.max(0, (Math.log(v) - Math.log(lo)) / (Math.log(hi) - Math.log(lo)))) * STEPS);
  const toValue = (pos) => nice(Math.exp(Math.log(lo) + (pos / STEPS) * (Math.log(hi) - Math.log(lo))));
  const a = min == null ? 0 : toPos(min), b = max == null ? STEPS : toPos(max);
  const buckets = facet?.buckets || [], peak = Math.max(1, ...buckets);
  const edge = (i) => Math.exp(Math.log(lo) + (i / buckets.length) * (Math.log(hi) - Math.log(lo)));
  const inRange = (i) => (min == null || edge(i + 1) >= min) && (max == null || edge(i) <= max);
  const field = (value, key, placeholder) => <label className="price-input"><input inputMode="numeric" value={value == null ? '' : num(value)} placeholder={placeholder} aria-label={`${placeholder} price in sats`}
    onChange={(e) => { const d = e.target.value.replace(/\D/g, '').slice(0, 10); onChange({ [key]: d ? Number(d) : null }); }} /></label>;
  return <div className="price-range">
    <div className="histogram" aria-hidden="true">{buckets.map((n, i) =>
      <span key={i} className={inRange(i) ? 'is-in' : ''} style={{ height: n ? `${12 + (n / peak) * 88}%` : 0 }} />)}</div>
    <div className="dual-range" style={{ '--a': `${(a / STEPS) * 100}%`, '--b': `${(b / STEPS) * 100}%` }}>
      <span className="dual-track" />
      <input type="range" min={0} max={STEPS} value={a} aria-label="Minimum price" aria-valuetext={min == null ? 'No minimum' : `${num(min)} sats`}
        onChange={(e) => { const pos = Math.min(Number(e.target.value), b - 10); onChange({ min: pos <= 0 ? null : toValue(pos) }); }} />
      <input type="range" min={0} max={STEPS} value={b} aria-label="Maximum price" aria-valuetext={max == null ? 'No maximum' : `${num(max)} sats`}
        onChange={(e) => { const pos = Math.max(Number(e.target.value), a + 10); onChange({ max: pos >= STEPS ? null : toValue(pos) }); }} />
    </div>
    <div className="range-scale muted"><span>{compact.format(lo)}</span><span>{compact.format(hi)}</span></div>
    <div className="price-inputs">{field(min, 'min', 'Min')}<span className="muted">to</span>{field(max, 'max', 'Max')}</div>
    <div className="opts">{PRESETS.map(([label, pmin, pmax]) =>
      <button key={label} className={`opt ${min === pmin && max === pmax ? 'is-active' : ''}`} onClick={() => onChange(min === pmin && max === pmax ? { min: null, max: null } : { min: pmin, max: pmax })}>{label}</button>)}</div>
  </div>;
}

/* ---------- panel ---------- */

function Section({ title, action, children }) {
  return <section className="filter-section"><header><h3>{title}</h3>{action}</header>{children}</section>;
}

function Options({ options, value, onChange, label }) {
  return <div className="opts" role="radiogroup" aria-label={label}>{options.map(([k, l]) =>
    <button key={k} role="radio" aria-checked={value === k} className={`opt ${value === k ? 'is-active' : ''}`} onClick={() => onChange(k)}>{l}</button>)}</div>;
}

function CheckOption({ checked, onChange, children, count }) {
  return <label className={`check-opt ${checked ? 'is-checked' : ''} ${count === 0 && !checked ? 'is-empty' : ''}`}>
    <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
    <span className="check-box">{checked && <Check size={13} strokeWidth={3.5} />}</span>
    <span className="check-label">{children}</span>
    {count != null && <span className="check-count">{count}</span>}
  </label>;
}

const toggle = (list, v, on) => (on ? [...list, v] : list.filter((x) => x !== v));

function Sellers({ facet, selected, onChange }) {
  const [find, setFind] = useState(''), [all, setAll] = useState(false);
  const rows = useMemo(() => {
    const known = facet || [];
    // A chosen seller stays visible even when other filters leave them no listings.
    const missing = selected.filter((s) => !known.some((k) => k.pubkey === s)).map((pubkey) => ({ pubkey, name: null, count: 0 }));
    return [...known, ...missing].filter((s) => !find || (s.name || 'Collector').toLowerCase().includes(find.toLowerCase()));
  }, [facet, selected, find]);
  const shown = all || find ? rows : rows.slice(0, 6);
  return <>
    {(facet?.length || 0) > 6 && <label className="mini-search"><Search size={14} /><input value={find} onChange={(e) => setFind(e.target.value)} placeholder="Find a seller" aria-label="Find a seller" /></label>}
    <div className="check-list">{shown.map((s) =>
      <CheckOption key={s.pubkey} checked={selected.includes(s.pubkey)} count={s.count} onChange={(on) => (!on || selected.length < MAX_SELLERS) && onChange(toggle(selected, s.pubkey, on))}>
        <Identicon pubkey={s.pubkey} size={20} /><span className="ellipsis">{s.name || 'Collector'}</span>
      </CheckOption>)}
      {!rows.length && <p className="muted small">No sellers match.</p>}
    </div>
    {!find && rows.length > 6 && <button className="link" onClick={() => setAll((v) => !v)}>{all ? 'Show fewer' : `Show all ${rows.length}`}</button>}
  </>;
}

export function FilterPanel({ filters, set, facets }) {
  const priceSet = filters.min != null || filters.max != null;
  const reset = (patch) => <button className="link" onClick={() => set(patch)}>Reset</button>;
  return <div className="filter-panel">
    <Section title={<>Price <span className="unit">sats</span></>} action={priceSet && reset({ min: null, max: null })}>
      <PriceRange facet={facets?.price} min={filters.min} max={filters.max} onChange={set} />
    </Section>
    <Section title="Availability">
      <CheckOption checked={filters.available} onChange={(available) => set({ available })}>Hide sales in progress</CheckOption>
    </Section>
    <Section title="Bids"><Options label="Bids" options={BIDS} value={filters.bids} onChange={(bids) => set({ bids })} /></Section>
    <Section title="Listed"><Options label="Listed" options={SINCE} value={filters.since} onChange={(since) => set({ since })} /></Section>
    <Section title="Seller" action={filters.sellers.length > 0 && reset({ sellers: [] })}>
      <Sellers facet={facets?.sellers} selected={filters.sellers} onChange={(sellers) => set({ sellers })} />
    </Section>
  </div>;
}

/* ---------- toolbar ---------- */

export function SortMenu({ value, onChange }) {
  const label = SORTS.find(([k]) => k === value)?.[1];
  return <Menu.Root>
    <Menu.Trigger className="sort-trigger" aria-label={`Sort by ${label}`}><ArrowUpDown size={15} /><span className="sort-trigger-label"><span className="muted">Sort:</span> {label}</span><ChevronDown size={15} /></Menu.Trigger>
    <Menu.Portal><Menu.Positioner className="menu-layer" sideOffset={6} align="end"><Menu.Popup className="menu">
      {SORTS.map(([k, l]) => <Menu.Item key={k} className={`menu-item ${value === k ? 'is-current' : ''}`} onClick={() => onChange(k)}>
        <span className="menu-check">{value === k && <Check size={15} strokeWidth={3} />}</span>{l}</Menu.Item>)}
    </Menu.Popup></Menu.Positioner></Menu.Portal>
  </Menu.Root>;
}

function chips(f, facets) {
  const out = [];
  const name = (pk) => facets?.sellers?.find((s) => s.pubkey === pk)?.name || 'Collector';
  if (f.min != null || f.max != null) out.push(['price', f.min != null && f.max != null ? `${num(f.min)}–${num(f.max)} sats` : f.min != null ? `From ${num(f.min)} sats` : `Up to ${num(f.max)} sats`, { min: null, max: null }]);
  if (f.available) out.push(['available', 'For sale now', { available: false }]);
  if (f.bids) out.push(['bids', BIDS.find(([k]) => k === f.bids)[1], { bids: '' }]);
  if (f.since) out.push(['since', `Listed in ${SINCE.find(([k]) => k === f.since)[1]}`, { since: '' }]);
  for (const s of f.sellers) out.push([`seller-${s}`, name(s), { sellers: f.sellers.filter((x) => x !== s) }, s]);
  return out;
}

export function ActiveFilters({ filters, set, clear, facets, total }) {
  const list = chips(filters, facets);
  return <div className="results-bar">
    <span className="results-count">{total == null ? ' ' : <><strong>{num(total)}</strong> {total === 1 ? 'result' : 'results'}</>}</span>
    <AnimatePresence initial={false}>{list.map(([key, label, patch, pubkey]) =>
      <motion.button key={key} layout className="filter-chip" onClick={() => set(patch)} aria-label={`Remove filter ${label}`}
        initial={{ opacity: 0, scale: .8 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: .8 }} transition={{ type: 'spring', stiffness: 500, damping: 30 }}>
        {pubkey && <Identicon pubkey={pubkey} size={16} />}{label}<X size={13} strokeWidth={3} />
      </motion.button>)}</AnimatePresence>
    {list.length > 1 && <button className="link" onClick={clear}>Clear all</button>}
  </div>;
}
