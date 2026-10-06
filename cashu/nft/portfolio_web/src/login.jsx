// Login with Nostr (NOSTR_LOGIN_PLAN.md): a signing extension or a pasted
// Nostr key. The collection's public key is the Nostr key; its wallets derive
// from a vault secret encrypted to that key (nostr.ts). New collections start
// from the user's Nostr name and picture.
import React, { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { KeyRound, Plug } from 'lucide-react';
import { signedRequest } from './api.mjs';
import { identityFor, saveEntry, storedKeys, unlockForTab, waitForExtension } from './identity.ts';
import { uploadHeaders } from './turnstile.mjs';
import { Button, HoldButton, Identicon, Modal, Spinner, setAvatarVersion } from './ui.jsx';
import { shrinkPicture } from './social.jsx';

const loadNostr = () => import('./nostr.ts');

/** The wallet secret this browser already holds for a collection, if any. */
function cachedVault(pubkey) {
  const entry = storedKeys()[pubkey];
  if (!entry || typeof entry === 'string') return undefined;
  return entry.vault ?? identityFor(pubkey, entry)?.root;
}

/** Drives a Nostr login to an identity: `onIdentity(identity, profile)`.
 *  `onStep` runs when the login opens a dialog of its own. */
export function useNostrLogin(onIdentity, onStep) {
  const [flow, setFlow] = useState(null), [busy, setBusy] = useState('');

  const finish = (login, root, profile = null) => {
    const entry = login.method === 'nip07' ? { kind: 'nip07', session: login.session, vault: root }
      : login.method === 'ncryptsec' ? { kind: 'ncryptsec', ncryptsec: login.ncryptsec }
        : { kind: 'nsec', secret: login.secret, vault: root };
    if (login.method === 'ncryptsec') unlockForTab(login.pubkey, login.secret, root);
    saveEntry(login.pubkey, entry);
    setFlow(null);
    onIdentity(identityFor(login.pubkey, entry), profile);
  };

  const proceed = async (login) => {
    const nostr = await loadNostr();
    setBusy('Opening your wallet');
    const vault = await nostr.findVault(login, cachedVault(login.pubkey));
    if (!login.existing) { onStep?.(); setFlow({ step: 'create', login, vault }); return; }
    if (!vault) { onStep?.(); setFlow({ step: 'restore', login }); return; }
    // Found only on the relays: the server lost its copy, put it back.
    if (!vault.onServer) await nostr.storeVault(login, vault, { relays: false });
    finish(login, vault.root);
  };

  const run = async (label, task) => {
    setBusy(label);
    try { return await task(); } catch (e) { toast.error(e.message); return null; } finally { setBusy(''); }
  };
  /** A signing extension (NIP-07). */
  const withExtension = () => run('Waiting for your Nostr extension', async () => {
    await proceed(await (await loadNostr()).connectExtension());
  });
  /** A pasted Nostr key. Resolves 'key' when it opens a collection that uses
   *  its own key: the caller unlocks that the way it always has. */
  const withKey = (secret, ncryptsec) => run('Unlocking', async () => {
    const login = await (await loadNostr()).connectKey(secret, ncryptsec);
    if (login.existing && !login.existing.nostr) return 'key';
    await proceed(login);
    return 'nostr';
  });

  const close = () => { if (!busy) setFlow(null); };
  const dialogs = <>
    <CreateDialog flow={flow?.step === 'create' ? flow : null} close={close} busy={busy} setBusy={setBusy} onCreated={finish} />
    <RestoreDialog flow={flow?.step === 'restore' ? flow : null} close={close} busy={busy} setBusy={setBusy} onRestored={finish} />
  </>;
  return { busy, withExtension, withKey, dialogs };
}

/** "Continue with Nostr extension", or why it isn't available. */
export function ExtensionButton({ onClick, busy, onPaste }) {
  const [found, setFound] = useState(null);
  useEffect(() => { let live = true; waitForExtension().then((ext) => { if (live) setFound(!!ext); }); return () => { live = false; }; }, []);
  if (found === false) return <div className="nostr-missing">
    <span className="hint">No Nostr extension found.</span>
    {onPaste && <button type="button" className="link" onClick={onPaste}>Paste a Nostr key instead</button>}
  </div>;
  return <Button type="button" variant="secondary" size="lg" className="full" icon={busy ? <Spinner /> : <Plug size={16} />} onClick={onClick} disabled={!!busy || found === null}>
    {busy || 'Continue with Nostr extension'}
  </Button>;
}

function CreateDialog({ flow, close, busy, setBusy, onCreated }) {
  const [name, setName] = useState(''), [picture, setPicture] = useState(null), [looking, setLooking] = useState(false);
  const touched = useRef(false);
  useEffect(() => {
    if (!flow) return;
    let live = true;
    touched.current = false; setName(''); setPicture(null); setLooking(true);
    (async () => {
      const nostr = await loadNostr(), found = await flow.login.nostr;
      if (!live) return;
      if (found.name && !touched.current) setName(found.name);
      setLooking(false);
      // A picture this browser can't fetch (CORS, gone, too big) is skipped.
      const blob = await nostr.fetchPicture(found.picture);
      const bytes = blob ? await shrinkPicture(blob).catch(() => null) : null;
      if (live && bytes) setPicture({ bytes, preview: URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' })) });
    })().catch(() => { if (live) setLooking(false); });
    return () => { live = false; };
  }, [flow]);
  useEffect(() => () => { if (picture) URL.revokeObjectURL(picture.preview); }, [picture]);

  const create = async (event) => {
    event.preventDefault();
    const { login } = flow;
    setBusy('Creating collection');
    try {
      const nostr = await loadNostr();
      const vault = flow.vault ?? await nostr.sealVault(login, nostr.newRoot());
      const { profile, published } = await nostr.createCollection(login, name, vault, !!flow.vault);
      if (!published) toast('Your wallet key is saved on this server. Its backup on your Nostr relays didn’t go through; you can download it under “Back up wallet key”.');
      if (picture) {
        try {
          const updated = await (await signedRequest(login.signer, `/api/profiles/${login.pubkey}/avatar`, picture.bytes, 'image/jpeg', '', await uploadHeaders())).json();
          setAvatarVersion(login.pubkey, updated.avatar);
          profile.avatar = updated.avatar;
        } catch { /* a picture the server refuses is skipped too */ }
      }
      onCreated(login, vault.root, profile);
    } catch (e) { toast.error(e.message); } finally { setBusy(''); }
  };

  return <Modal open={!!flow} close={close} title="Create your collection" description="Your Nostr key is your collection key. Pick a name; you can change it, and your picture, later.">
    {flow && <form onSubmit={create} className="stack">
      <div className="avatar-edit">
        <span className="avatar-preview">{picture ? <img src={picture.preview} alt="" /> : <Identicon pubkey={flow.login.pubkey} size={72} />}</span>
        <span className="hint">{looking ? <><Spinner size={13} /> Looking up your Nostr profile…</> : picture ? 'Your Nostr profile picture.' : 'Add a picture later under Edit profile.'}</span>
      </div>
      <label className="field"><span>Collection name</span>
        <input maxLength={40} placeholder="e.g. Field recordings" value={name} autoFocus
          onChange={(e) => { touched.current = true; setName(e.target.value); }} />
      </label>
      <Button variant="primary" size="lg" className="full" type="submit" disabled={!!busy} icon={busy ? <Spinner /> : null}>{busy || 'Create collection'}</Button>
    </form>}
  </Modal>;
}

function RestoreDialog({ flow, close, busy, setBusy, onRestored }) {
  const [key, setKey] = useState(''), [error, setError] = useState('');
  useEffect(() => { setKey(''); setError(''); }, [flow]);
  const restore = async (root, check) => {
    const { login } = flow;
    setBusy('Restoring your wallet'); setError('');
    try {
      const nostr = await loadNostr();
      if (check && !(await nostr.rootMatches(login, root))) { setError('This wallet key doesn’t open this collection’s backups.'); return; }
      const vault = await nostr.sealVault(login, root);
      await nostr.storeVault(login, vault);
      onRestored(login, root);
    } catch (e) { toast.error(e.message); } finally { setBusy(''); }
  };
  const submit = (event) => {
    event.preventDefault();
    const root = key.trim().toLowerCase();
    if (!/^[0-9a-f]{64}$/.test(root)) { setError('A wallet key is 64 hex characters.'); return; }
    restore(root, true);
  };
  return <Modal open={!!flow} close={close} title="Restore your wallet" description="This collection exists, but its wallet key wasn’t found on this server or your relays. Paste the wallet key you backed up.">
    {flow && <form onSubmit={submit} className="stack">
      <label className="field"><span>Wallet key</span>
        <input type="password" autoComplete="off" spellCheck={false} placeholder="64 hex characters" value={key} onChange={(e) => setKey(e.target.value)} autoFocus />
      </label>
      {error && <span className="field-error">{error}</span>}
      <Button variant="primary" size="lg" className="full" type="submit" disabled={!!busy} icon={busy ? <Spinner /> : <KeyRound size={16} />}>{busy || 'Restore wallet'}</Button>
      <p className="hint">No backup? Start with a new wallet key. NFTs and ecash held under the lost key stay out of reach.</p>
      <HoldButton danger disabled={!!busy} onComplete={() => restore((Array.from(crypto.getRandomValues(new Uint8Array(32)), (b) => b.toString(16).padStart(2, '0')).join('')), false)}>Hold to start a new wallet</HoldButton>
    </form>}
  </Modal>;
}
