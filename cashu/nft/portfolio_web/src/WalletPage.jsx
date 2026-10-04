// Ordinary ecash wallet screen: balances per mint, adding mints, Lightning
// and ecash in and out. Wallet logic lives in src/money/wallet.ts.
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { toast } from 'sonner';
import QRCode from 'qrcode';
import { ArrowDownToLine, ArrowLeft, ArrowUpFromLine, ClipboardPaste, Coins, ExternalLink, Plus, Wallet, Zap } from 'lucide-react';
import { getJSON } from './api.mjs';
import { Button, CopyChip, DrawnCheck, HoldButton, Modal, Notice, SkeletonRows, StorageNotice, Spinner, panel } from './ui.jsx';
import { Segmented, imageUrl } from './social.jsx';
import { UR_FRAME_MS, needsAnimatedQr, urFrames } from './qr.mjs';
import { TestBadge, host, sats } from './market.jsx';

const TONES = ['var(--lime)', 'var(--orange)', 'var(--blue)', 'var(--pink)', 'var(--yellow)', 'var(--mint)'];
const toneOf = (url) => TONES[[...url].reduce((n, c) => (n * 31 + c.charCodeAt(0)) >>> 0, 7) % TONES.length];
const MINT_NOTES = {
  'https://testnut.cashu.space': 'Test sats with no value. Invoices pay themselves.',
  'https://mint.minibits.cash/Bitcoin': 'Bitcoin mint run by Minibits.',
  'https://mint.coinos.io': 'Bitcoin mint run by Coinos.',
  'https://mint.macadamia.cash': 'Bitcoin mint run by Macadamia.',
};
const QUICK = [100, 1000, 10000];

/** Mint display names: shortcut names first, then the mint's own /v1/info. */
function useMintNames(mints, shortcuts) {
  const [names, setNames] = useState({});
  useEffect(() => {
    for (const s of shortcuts) setNames((n) => (n[s.url] ? n : { ...n, [s.url]: s.name.replace(/\s*\(.*\)$/, '') }));
    for (const url of mints) {
      if (names[url] || shortcuts.some((s) => s.url === url)) continue;
      fetch(url + '/v1/info').then((r) => r.json()).then((info) => { if (info?.name) setNames((n) => ({ ...n, [url]: String(info.name).slice(0, 40) })); }).catch(() => {});
    }
  }, [mints.join(','), shortcuts.length]); // eslint-disable-line react-hooks/exhaustive-deps
  return (url) => names[url] || host(url);
}

function MintMark({ url, size = 40 }) {
  const label = host(url).replace(/^(mint|www)\./, '').slice(0, 1).toUpperCase();
  return <span className="mint-mark" style={{ '--tone': toneOf(url), width: size, height: size, fontSize: size * 0.42 }} aria-hidden="true">{label}</span>;
}

function Qr({ value, label, onTooLarge }) {
  const [svg, setSvg] = useState(''), [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    setFailed(false);
    QRCode.toString(value.toUpperCase().startsWith('LNBC') ? value.toUpperCase() : value, { type: 'svg', margin: 1, errorCorrectionLevel: 'M' })
      .then((s) => { if (live) setSvg(s); })
      .catch(() => { if (live) { setSvg(''); setFailed(true); onTooLarge?.(); } });
    return () => { live = false; };
  }, [value]); // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="qr" role="img" aria-label={label}>
    {svg ? <div dangerouslySetInnerHTML={{ __html: svg }} /> : failed ? <p className="hint">Too large for a QR code. Copy it instead.</p> : <Spinner />}
  </div>;
}

/** Cycles through UR parts of a token (NUT-16 animated QR code). */
function AnimatedQr({ value, label }) {
  const [frame, setFrame] = useState('');
  useEffect(() => {
    const next = urFrames(value);
    setFrame(next());
    const timer = setInterval(() => setFrame(next()), UR_FRAME_MS);
    return () => clearInterval(timer);
  }, [value]);
  return frame ? <Qr value={frame} label={label} /> : <div className="qr"><Spinner /></div>;
}

/** Static QR code for small tokens, animated one for larger tokens or any that
 *  do not fit a single code. */
function TokenQr({ token }) {
  const [tooLarge, setTooLarge] = useState(false);
  const animated = useMemo(() => needsAnimatedQr(token), [token]);
  useEffect(() => setTooLarge(false), [token]);
  return animated || tooLarge ? <AnimatedQr value={token} label="Animated ecash token QR code" />
    : <Qr value={token} label="Ecash token QR code" onTooLarge={() => setTooLarge(true)} />;
}

function AmountField({ value, onChange, max, autoFocus }) {
  return <div className="amount-field">
    <label className="amount-input"><input inputMode="numeric" value={value} placeholder="0" autoFocus={autoFocus} aria-label="Amount in sats"
      onChange={(e) => onChange(e.target.value.replace(/\D/g, '').replace(/^0+(?=\d)/, ''))} /><span>sats</span></label>
    <div className="amount-quick">
      {QUICK.map((q) => <button type="button" key={q} className="chip" onClick={() => onChange(String(q))}>{q.toLocaleString()}</button>)}
      {max > 0 && <button type="button" className="chip" onClick={() => onChange(String(max))}>Max</button>}
    </div>
  </div>;
}

function MintSelect({ mints, value, onChange, nameOf, balances }) {
  return <label className="field"><span>Mint</span>
    <select value={value} onChange={(e) => onChange(e.target.value)}>
      {mints.map((m) => { const b = balances.find((x) => x.mint === m); return <option key={m} value={m}>{nameOf(m)} · {sats(b?.available ?? 0)}{b?.testValue ? ' · test sats' : ''}</option>; })}
    </select></label>;
}

function Done({ title, detail, children }) {
  return <motion.div className="wallet-done" {...panel}>
    <motion.span className="success-mark" initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ type: 'spring', stiffness: 400, damping: 18 }}><DrawnCheck on size={26} /></motion.span>
    <h3>{title}</h3>{detail && <p className="muted">{detail}</p>}{children}
  </motion.div>;
}

/* ---------- add a mint ---------- */

function MintChooser({ money, shortcuts, known, onAdded }) {
  const [url, setUrl] = useState(''), [busy, setBusy] = useState(''), [error, setError] = useState('');
  const add = async (target) => {
    setBusy(target); setError('');
    try { const c = await money.addMint(target); toast.success(`Added ${c.name || host(c.url)}.`); setUrl(''); onAdded?.(c.url); }
    catch (e) { setError(e.message); } finally { setBusy(''); }
  };
  const options = shortcuts.filter((s) => !known.includes(s.url));
  return <div className="mint-chooser">
    {options.length > 0 && <div className="mint-options">
      {options.map((s) => <button key={s.url} className="mint-option" disabled={!!busy || money.readOnly} onClick={() => add(s.url)}>
        <MintMark url={s.url} />
        <span className="mint-option-text"><strong>{s.name.replace(/\s*\(.*\)$/, '')}{s.test_value && <TestBadge on />}</strong><span className="muted">{MINT_NOTES[s.url] || host(s.url)}</span></span>
        {busy === s.url ? <Spinner /> : <Plus size={18} />}
      </button>)}
    </div>}
    <form className="mint-custom" onSubmit={(e) => { e.preventDefault(); if (url.trim()) add(url.trim()); }}>
      <label className="field"><span>Other mint</span><input value={url} onChange={(e) => { setUrl(e.target.value); setError(''); }} placeholder="https://mint.example.com" spellCheck={false} autoCapitalize="off" /></label>
      <Button type="submit" variant="secondary" disabled={!url.trim() || !!busy || money.readOnly} icon={busy && busy === url.trim() ? <Spinner /> : <Plus size={15} />}>Add</Button>
    </form>
    {error && <p className="field-error">{error}</p>}
    <p className="hint">Each mint holds its own ecash. Only add mints you trust; listed mints are suggestions, not endorsements.</p>
  </div>;
}

/* ---------- receive ---------- */

function ReceiveDialog({ open, close, money, mints, initialMint, nameOf, balances, onReceived, onAddMint }) {
  const [tab, setTab] = useState('lightning'), [mint, setMint] = useState(''), [amount, setAmount] = useState('');
  const [invoice, setInvoice] = useState(null), [token, setToken] = useState(''), [busy, setBusy] = useState(''), [done, setDone] = useState(null);
  const run = useRef(0);
  useEffect(() => { if (open) { setMint(initialMint || mints[0] || ''); setTab(open === 'token' ? 'token' : 'lightning'); setAmount(''); setInvoice(null); setToken(''); setDone(null); } }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { run.current++; }, []);
  const shut = () => { run.current++; close(); };
  const createInvoice = async () => {
    setBusy('Creating invoice');
    try {
      const r = await money.topUp(mint, Number(amount)); setInvoice({ ...r, amount: Number(amount), mint });
      // Coco claims the quote in the background; this only follows it while open.
      const id = ++run.current;
      while (id === run.current) {
        const state = await money.topUpState(r.operationId).catch(() => 'pending');
        if (state === 'finalized') { setDone({ amount: Number(amount) }); onReceived(Number(amount)); return; }
        if (state === 'failed') { toast.error('The mint could not issue this top-up.'); return; }
        await new Promise((res) => setTimeout(res, 2000));
      }
    } catch (e) { toast.error(e.message); } finally { setBusy(''); }
  };
  const receiveToken = async () => {
    setBusy('Receiving');
    try { const r = await money.receive(token); setDone({ amount: r.amount }); onReceived(r.amount); } catch (e) { toast.error(e.message); } finally { setBusy(''); }
  };
  const paste = async () => { try { setToken((await navigator.clipboard.readText()).trim()); } catch { toast('Paste the token into the field.'); } };
  return <Modal open={!!open} close={shut} title="Receive" size="wallet">
    {done ? <Done title={`Received ${sats(done.amount)}`}><Button variant="primary" className="full" onClick={shut}>Done</Button></Done>
      : invoice ? <motion.div className="stack invoice-view" {...panel}>
        <div className="invoice-amount"><strong>{sats(invoice.amount)}</strong><span className="muted">{nameOf(invoice.mint)}</span></div>
        <Qr value={invoice.invoice} label="Lightning invoice QR code" />
        <div className="row invoice-actions">
          <CopyChip value={invoice.invoice} display="Copy invoice" message="Invoice copied" />
          <a className="btn btn-ghost btn-sm" href={`lightning:${invoice.invoice}`}><ExternalLink size={14} />Open wallet app</a>
        </div>
        <p className="wait-line"><span className="pulse" />Waiting for payment</p>
        <p className="hint">You can close this. The wallet finishes the top-up when the invoice is paid.</p>
      </motion.div>
        : <div className="stack">
          <Segmented id="receive" value={tab} onChange={setTab} options={[['lightning', 'Lightning'], ['token', 'Ecash token']]} />
          {tab === 'lightning' ? (!mints.length
            ? <Notice action={<Button size="sm" variant="secondary" onClick={() => { shut(); onAddMint(); }}>Add a mint</Button>}>Add a mint to receive over Lightning.</Notice>
            : <form className="stack" onSubmit={(e) => { e.preventDefault(); createInvoice(); }}>
              <MintSelect mints={mints} value={mint} onChange={setMint} nameOf={nameOf} balances={balances} />
              <AmountField value={amount} onChange={setAmount} autoFocus />
              <Button type="submit" variant="primary" size="lg" className="full" disabled={!Number(amount) || !mint || !!busy} icon={busy ? <Spinner /> : <Zap size={16} />}>{busy || 'Create invoice'}</Button>
            </form>)
            : <form className="stack" onSubmit={(e) => { e.preventDefault(); receiveToken(); }}>
              <label className="field"><span>Cashu token</span><textarea rows={5} value={token} onChange={(e) => setToken(e.target.value)} placeholder="cashuB…" spellCheck={false} /></label>
              <div className="row"><Button type="button" variant="ghost" size="sm" icon={<ClipboardPaste size={14} />} onClick={paste}>Paste</Button></div>
              <Button type="submit" variant="primary" size="lg" className="full" disabled={!token.trim() || !!busy} icon={busy ? <Spinner /> : <ArrowDownToLine size={16} />}>{busy || 'Receive'}</Button>
            </form>}
        </div>}
  </Modal>;
}

/* ---------- send ---------- */

function SendDialog({ open, close, money, balances, initialMint, nameOf, onSent }) {
  const funded = balances.filter((b) => b.available > 0).map((b) => b.mint);
  const [tab, setTab] = useState('token'), [mint, setMint] = useState(''), [amount, setAmount] = useState(''), [invoice, setInvoice] = useState('');
  const [token, setToken] = useState(''), [quote, setQuote] = useState(null), [busy, setBusy] = useState(''), [done, setDone] = useState(null);
  useEffect(() => { if (open) { setMint(initialMint && funded.includes(initialMint) ? initialMint : funded[0] || ''); setTab(open === 'lightning' ? 'lightning' : 'token'); setAmount(''); setInvoice(''); setToken(''); setQuote(null); setDone(null); } }, [open]); // eslint-disable-line react-hooks/exhaustive-deps
  const available = balances.find((b) => b.mint === mint)?.available ?? 0;
  const act = async (label, fn) => { setBusy(label); try { await fn(); } catch (e) { toast.error(e.message); } finally { setBusy(''); } };
  const createToken = () => act('Creating token', async () => { setToken(await money.send(mint, Number(amount))); onSent(); });
  const getQuote = () => act('Getting a quote', async () => setQuote(await money.withdrawQuote(mint, invoice)));
  const pay = () => act('Paying invoice', async () => { await money.withdraw(quote.operation.id); setDone(true); onSent(); });
  const qAmount = quote ? Number(String(quote.quote.amount)) : 0, qFee = quote ? Number(String(quote.quote.fee_reserve ?? quote.quote.feeReserve ?? 0)) : 0;
  return <Modal open={!!open} close={close} title="Send" size="wallet">
    {done ? <Done title="Invoice paid" detail={`${sats(qAmount)} sent over Lightning.`}><Button variant="primary" className="full" onClick={close}>Done</Button></Done>
      : token ? <motion.div className="stack invoice-view" {...panel}>
        <div className="invoice-amount"><strong>{sats(Number(amount))}</strong><span className="muted">{nameOf(mint)}</span></div>
        <TokenQr token={token} />
        <div className="row invoice-actions"><CopyChip value={token} display="Copy token" message="Token copied" /></div>
        <p className="hint">Anyone with this token can claim it. Share it only with the recipient.</p>
        <Button variant="secondary" className="full" onClick={close}>Done</Button>
      </motion.div>
        : !funded.length ? <Notice>Nothing to send yet. Receive ecash first.</Notice>
          : <div className="stack">
            <Segmented id="send" value={tab} onChange={(t) => { setTab(t); setQuote(null); }} options={[['token', 'Ecash token'], ['lightning', 'Lightning']]} />
            <MintSelect mints={funded} value={mint} onChange={(m) => { setMint(m); setQuote(null); }} nameOf={nameOf} balances={balances} />
            {tab === 'token' ? <form className="stack" onSubmit={(e) => { e.preventDefault(); createToken(); }}>
              <AmountField value={amount} onChange={setAmount} max={available} autoFocus />
              {Number(amount) > available && <p className="field-error">This mint holds {sats(available)}.</p>}
              <Button type="submit" variant="primary" size="lg" className="full" disabled={!Number(amount) || Number(amount) > available || !!busy} icon={busy ? <Spinner /> : <Coins size={16} />}>{busy || 'Create token'}</Button>
            </form>
              : !quote ? <form className="stack" onSubmit={(e) => { e.preventDefault(); getQuote(); }}>
                <label className="field"><span>Lightning invoice</span><textarea rows={4} value={invoice} onChange={(e) => setInvoice(e.target.value)} placeholder="lnbc…" spellCheck={false} /></label>
                <Button type="submit" variant="primary" size="lg" className="full" disabled={!invoice.trim() || !!busy} icon={busy ? <Spinner /> : null}>{busy || 'Review payment'}</Button>
              </form>
                : <div className="stack">
                  <dl className="fee-table">
                    <div><dt>Invoice</dt><dd>{sats(qAmount)}</dd></div>
                    <div><dt>Fee reserve</dt><dd>{sats(qFee)}</dd></div>
                    <div className="total"><dt>Up to</dt><dd>{sats(qAmount + qFee)}</dd></div>
                  </dl>
                  <p className="hint">Unused fee reserve returns to this mint balance.</p>
                  {qAmount + qFee > available ? <p className="field-error">This mint holds {sats(available)}.</p>
                    : busy ? <Button variant="primary" size="lg" className="full" disabled icon={<Spinner />}>{busy}</Button>
                      : <HoldButton onComplete={pay} icon={<Zap size={16} />}>Hold to pay</HoldButton>}
                  <Button variant="ghost" onClick={() => setQuote(null)}>Back</Button>
                </div>}
          </div>}
  </Modal>;
}

/* ---------- onboarding banner ---------- */

function Guide({ goal, balances, onContinue }) {
  const ready = goal && balances.some((b) => b.available > goal.price);
  return <motion.section className="guide" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}>
    {goal && <img src={imageUrl(goal.h)} alt="" />}
    <div className="guide-text">
      <strong>{ready ? 'Ready to make your offer' : `Fund your offer${goal ? ` on ${goal.title || 'this NFT'}` : ''}`}</strong>
      {goal && <span>{ready ? `Your balance covers the ${sats(goal.price)} asking price.` : `Asking price ${sats(goal.price)}, plus a small mint fee.`}</span>}
    </div>
    <Button variant={ready ? 'primary' : 'secondary'} icon={<ArrowLeft size={15} />} onClick={onContinue}>{ready ? 'Continue to offer' : 'Back to the NFT'}</Button>
  </motion.section>;
}

/* ---------- page ---------- */

export function WalletPage({ market, navigate, then }) {
  const money = market.money;
  const [receive, setReceive] = useState(null), [send, setSend] = useState(null), [adding, setAdding] = useState(false);
  const [goal, setGoal] = useState(null);
  useEffect(() => {
    setGoal(null);
    if (then) getJSON(`/api/${then.slice(1).replace('market/', 'market/listings/')}`).then(setGoal).catch(() => setGoal(null));
  }, [then]);
  const shortcuts = market.config?.mint_shortcuts || [];
  const mints = market.balances.map((b) => b.mint);
  const nameOf = useMintNames(mints, shortcuts);
  const totals = useMemo(() => {
    const t = { real: 0, test: 0, locked: 0, pending: 0 };
    for (const b of market.balances) {
      t[b.testValue ? 'test' : 'real'] += b.available;
      t.locked += b.offerLocked; t.pending += b.pendingRefund + b.pendingClaim;
    }
    return t;
  }, [market.balances]);
  const backToGoal = () => { if (then) navigate(`${then}?offer=1`); };
  const received = async (amount) => {
    await market.refresh();
    if (then) { toast.success(`Received ${sats(amount)}.`); setReceive(null); backToGoal(); }
  };

  if (market.state === 'opening' || market.state === 'closed') return <main className="page wallet-page" aria-busy="true">
    <div className="wallet-hero sk-wrap"><div className="wallet-total"><span className="sk sk-line w-20 thin" /><span className="sk sk-amount" /></div><div className="wallet-actions"><span className="sk sk-button" /><span className="sk sk-button" /></div></div>
    <SkeletonRows count={2} className="sk-mints" />
  </main>;
  if (!money) return <main className="page wallet-page"><Notice tone="bad">{market.error || 'Your wallet is unavailable.'}</Notice></main>;
  const readOnly = money.readOnly;

  return <main className="page wallet-page">
    <StorageNotice />
    {market.state === 'elsewhere' && <Notice action={<Button size="sm" variant="secondary" onClick={market.takeOver}>Use it here</Button>}>This wallet is open on another device.</Notice>}
    {market.error && <Notice tone="bad">{market.error}</Notice>}
    {then && <Guide goal={goal} balances={market.balances} onContinue={backToGoal} />}

    <section className="wallet-hero">
      <div className="wallet-total">
        <span className="kicker"><Wallet size={14} /> Wallet</span>
        <strong className="wallet-amount">{totals.real.toLocaleString()}<span>sats</span></strong>
        <div className="wallet-sub">
          {totals.test > 0 && <span className="badge badge-warn">+ {sats(totals.test)} test</span>}
          {totals.locked > 0 && <span className="badge">{sats(totals.locked)} in offers</span>}
          {totals.pending > 0 && <span className="badge">{sats(totals.pending)} pending</span>}
        </div>
      </div>
      <div className="wallet-actions">
        <Button variant="primary" size="lg" icon={<ArrowDownToLine size={17} />} disabled={readOnly} onClick={() => setReceive('lightning')}>Receive</Button>
        <Button variant="secondary" size="lg" icon={<ArrowUpFromLine size={17} />} disabled={readOnly || !market.balances.some((b) => b.available > 0)} onClick={() => setSend('token')}>Send</Button>
      </div>
    </section>

    {!mints.length ? <section className="wallet-empty">
      <h2>Add your first mint</h2>
      <p className="muted">A mint issues the ecash you hold. Pick one to get started.</p>
      <MintChooser money={money} shortcuts={shortcuts} known={mints} onAdded={() => market.refresh()} />
    </section> : <section className="mint-list">
      <header className="mint-list-head"><h2>Mints</h2><Button variant="secondary" size="sm" icon={<Plus size={15} />} disabled={readOnly} onClick={() => setAdding(true)}>Add mint</Button></header>
      <AnimatePresence initial={false}>
        {market.balances.map((b) => <motion.article key={b.mint} className="mint-row" layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
          <MintMark url={b.mint} />
          <div className="mint-id">
            <strong>{nameOf(b.mint)} <TestBadge on={b.testValue} /></strong>
            <span className="mono muted ellipsis" title={b.mint}>{host(b.mint)}</span>
          </div>
          <div className="mint-balance">
            <strong>{sats(b.available)}</strong>
            {(b.offerLocked > 0 || b.pendingRefund > 0 || b.pendingClaim > 0) && <span className="muted small">
              {[b.offerLocked && `${sats(b.offerLocked)} in offers`, b.pendingRefund && `${sats(b.pendingRefund)} refund pending`, b.pendingClaim && `${sats(b.pendingClaim)} payment pending`].filter(Boolean).join(' · ')}</span>}
          </div>
          <div className="mint-actions">
            <Button size="sm" variant="secondary" icon={<Zap size={14} />} disabled={readOnly} onClick={() => setReceive({ mint: b.mint })}>Top up</Button>
            <Button size="sm" variant="ghost" icon={<ArrowUpFromLine size={14} />} disabled={readOnly || !b.available} onClick={() => setSend({ mint: b.mint })}>Send</Button>
          </div>
        </motion.article>)}
      </AnimatePresence>
    </section>}

    <ReceiveDialog open={receive && (receive.mint ? 'lightning' : receive)} close={() => setReceive(null)} money={money} mints={mints} initialMint={receive?.mint}
      nameOf={nameOf} balances={market.balances} onReceived={received} onAddMint={() => setAdding(true)} />
    <SendDialog open={send && (send.mint ? 'token' : send)} close={() => setSend(null)} money={money} balances={market.balances} initialMint={send?.mint} nameOf={nameOf} onSent={() => market.refresh()} />
    <Modal open={adding} close={() => setAdding(false)} title="Add a mint" size="wallet">
      <MintChooser money={money} shortcuts={shortcuts} known={mints} onAdded={() => { market.refresh(); setAdding(false); }} />
    </Modal>
  </main>;
}
