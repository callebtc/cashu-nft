// "Get started": pick how to begin, then one focused step. A new collection
// (with a private key made here), the private key of an existing collection,
// or Nostr (a signing extension, or a pasted nsec / ncryptsec). The steps
// share the App's onboarding state and submit through its `onboard`.
import React, { useEffect, useState } from 'react';
import { ChevronRight, Download, KeyRound, Plug, Plus } from 'lucide-react';
import { download } from './api.mjs';
import { BackButton, Button, CopyChip, DrawnCheck, Modal, Spinner } from './ui.jsx';
import { ExtensionButton, useExtensionFound } from './login.jsx';

export const START_STEPS = ['start', 'create', 'key', 'nostr'];

const isNcryptsec = (text) => /^\s*(?:nostr:)?ncryptsec1/i.test(text);

export function StartDialog({ step, setStep, close, busy, nostr, create, pasted, onSubmit, moved }) {
  const found = useExtensionFound();
  const open = START_STEPS.includes(step);
  const working = !!busy || !!nostr.busy;
  const back = <div><BackButton onClick={() => setStep('start')} disabled={working}>All options</BackButton></div>;
  const [title, description] = {
    create: ['Create a collection', 'Your collection is controlled by a private key made in this browser. Save it now: there is no password reset.'],
    key: ['Paste a private key', `Paste the private key you saved when you created the collection. It stays in this browser.${moved}`],
    nostr: ['Continue with Nostr', 'Your Nostr key becomes your collection key. Use a signing extension, or paste your key.'],
  }[step] ?? ['Get started', 'Start a new collection, or open one you already have.'];

  return <Modal open={open} close={close} title={title} description={description}>
    {step === 'create' ? <form onSubmit={onSubmit} className="stack">
      {back}
      <label className="field"><span>Collection name</span><input maxLength={40} placeholder="e.g. Field recordings" value={create.name} onChange={(e) => create.setName(e.target.value)} autoFocus /></label>
      <div className="keybox">
        <div className="keybox-head"><span><KeyRound size={14} /> Private key</span><span className="muted">Never share this</span></div>
        <code className="mono">{create.generated}</code>
        <div className="row">
          <CopyChip value={create.generated} display="Copy" message="Private key copied" />
          <Button type="button" variant="ghost" size="sm" icon={<Download size={14} />} onClick={() => download(new Blob([create.generated + '\n'], { type: 'text/plain' }), 'nonfungible-cash-private-key.txt')}>Download</Button>
        </div>
      </div>
      <label className="ack">
        <input type="checkbox" checked={create.backedUp} onChange={(e) => create.setBackedUp(e.target.checked)} />
        <span className="ack-box"><DrawnCheck on={create.backedUp} size={14} /></span>
        <span>I saved my private key. Without it this collection can't be recovered.</span>
      </label>
      <Button variant="primary" size="lg" className="full" type="submit" disabled={working || !create.backedUp} icon={busy ? <Spinner /> : null}>{busy || 'Create collection'}</Button>
    </form>

      : step === 'key' ? <form onSubmit={onSubmit} className="stack">
        {back}
        <label className="field"><span>Private key</span><input type="password" autoComplete="off" placeholder="64 hex characters" spellCheck={false} value={pasted.value} onChange={(e) => pasted.setValue(e.target.value)} required autoFocus /></label>
        <Button variant="primary" size="lg" className="full" type="submit" disabled={working || !pasted.value.trim()} icon={busy ? <Spinner /> : <KeyRound size={16} />}>{busy || 'Unlock'}</Button>
      </form>

        : step === 'nostr' ? <NostrStep back={back} found={found} busy={busy} working={working} nostr={nostr} keyState={pasted} onSubmit={onSubmit} />

          : <div className="choices">
            <Choice icon={<Plus size={20} />} tone="lime" title="Create a new collection" detail="We make a private key for you to save." onClick={() => setStep('create')} />
            <Choice icon={<KeyRound size={20} />} tone="yellow" title="I have a private key" detail="Open a collection you made before." onClick={() => setStep('key')} />
            <Choice icon={<Plug size={20} />} tone="pink" title="Continue with Nostr" detail={found ? 'Use your signing extension or Nostr key.' : 'Paste your Nostr key (nsec).'} onClick={() => setStep('nostr')} />
          </div>}
  </Modal>;
}

function Choice({ icon, tone, title, detail, onClick }) {
  return <button type="button" className="choice" onClick={onClick}>
    <span className={`choice-icon choice-${tone}`}>{icon}</span>
    <span className="choice-text"><strong>{title}</strong><span>{detail}</span></span>
    <ChevronRight size={18} className="choice-chevron" aria-hidden="true" />
  </button>;
}

/** A signing extension when there is one; a pasted Nostr key always works. */
function NostrStep({ back, found, busy, working, nostr, keyState, onSubmit }) {
  // A stored ncryptsec comes prefilled: only its password is missing.
  const [paste, setPaste] = useState(!!keyState.value);
  useEffect(() => { if (found === false) setPaste(true); }, [found]);
  const encrypted = isNcryptsec(keyState.value);
  const passwordFirst = encrypted && !keyState.password;
  return <div className="stack">
    {back}
    {found === false
      ? <p className="hint">No Nostr extension found in this browser. Paste your Nostr key instead.</p>
      : <ExtensionButton onClick={nostr.withExtension} busy={nostr.busy} found={found} />}
    {found && !paste && <button type="button" className="link choice-alt" onClick={() => setPaste(true)}>Paste your Nostr key instead</button>}
    {paste && <form onSubmit={onSubmit} className="stack">
      {found && <div className="or-rule" role="separator"><span>or</span></div>}
      <label className="field"><span>Nostr key</span><input type="password" autoComplete="off" placeholder="nsec1… or ncryptsec1…" spellCheck={false} value={keyState.value} onChange={(e) => keyState.setValue(e.target.value)} required autoFocus={!passwordFirst} /></label>
      {encrypted && <label className="field"><span>Password</span><input type="password" autoComplete="current-password" value={keyState.password} onChange={(e) => keyState.setPassword(e.target.value)} required autoFocus={passwordFirst} /></label>}
      {/^\s*(?:nostr:)?nsec1/i.test(keyState.value) && <span className="hint">Prefer a signing extension: your nsec never touches this site.</span>}
      <Button variant={found ? 'secondary' : 'primary'} size="lg" className="full" type="submit" disabled={working || !keyState.value.trim()} icon={busy ? <Spinner /> : null}>{busy || 'Continue with Nostr'}</Button>
    </form>}
  </div>;
}
