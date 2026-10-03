// Cloudflare Turnstile for image uploads. The widget runs in invisible mode in
// an off-screen host, so nothing is shown. Each upload gets a fresh token
// (tokens are single-use). Without a configured sitekey this is a no-op.
const SCRIPT = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
const TIMEOUT_MS = 30000;
const FAILED = "We couldn't confirm this upload came from a browser. Reload the page and try again.";

let sitekey = null;
let script = null;

export function setTurnstileSitekey(key) { sitekey = key || null; }

function loadScript() {
  script ??= new Promise((resolve, reject) => {
    const tag = document.createElement('script');
    tag.src = SCRIPT;
    tag.async = true;
    tag.onload = () => (window.turnstile ? resolve(window.turnstile) : reject(new Error(FAILED)));
    tag.onerror = () => { script = null; tag.remove(); reject(new Error(FAILED)); };
    document.head.append(tag);
  });
  return script;
}

async function token() {
  const turnstile = await loadScript();
  return new Promise((resolve, reject) => {
    const host = document.createElement('div');
    host.className = 'turnstile-host';
    host.setAttribute('aria-hidden', 'true');
    document.body.append(host);
    let id = null, settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { if (id !== null) turnstile.remove(id); } catch { /* already gone */ }
      host.remove();
      fn(value);
    };
    const timer = setTimeout(() => finish(reject, new Error(FAILED)), TIMEOUT_MS);
    try {
      id = turnstile.render(host, {
        sitekey,
        action: 'upload',
        callback: (value) => finish(resolve, value),
        'error-callback': () => finish(reject, new Error(FAILED)),
        'expired-callback': () => finish(reject, new Error(FAILED)),
        'timeout-callback': () => finish(reject, new Error(FAILED)),
      });
    } catch { finish(reject, new Error(FAILED)); }
  });
}

/**
 * Headers for an image upload: a fresh Turnstile token when enabled.
 * @returns {Promise<Record<string, string>>}
 */
export async function uploadHeaders() {
  if (!sitekey) return {};
  return { 'X-Turnstile-Token': await token() };
}
