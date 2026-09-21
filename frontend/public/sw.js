/* Minimal service worker: network-first for navigations, cache-first for built assets. */

// Stamped per build by vite.config.js (derived from the hashed asset list), so
// every build gets its own caches and `activate` can drop the previous build's.
// Unstamped (the placeholder survives) only if this file is served raw.
const STAMP = '__CRM_BUILD_ID__';
const BUILD_ID = STAMP.startsWith('__') ? 'dev' : STAMP;
const SHELL_CACHE = `crm-shell-${BUILD_ID}`;
const ASSET_CACHE = `crm-assets-${BUILD_ID}`;
const SHELL_KEY = '/index.html';

self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  // Old builds' caches (hashed bundles nothing references any more) go here.
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== SHELL_CACHE && k !== ASSET_CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

/** Paths the SPA shell answers for. Public lead forms (/f/…) are separate
 * server-rendered pages; /api and /assets are never documents. */
function isAppRoute(pathname) {
  return !(
    pathname === '/f' ||
    pathname.startsWith('/f/') ||
    pathname.startsWith('/api/') ||
    pathname.startsWith('/assets/')
  );
}

/** Keep a navigation response as the offline shell only if it IS the shell:
 * a plain 200, same-origin, un-redirected HTML document that mounts the app.
 * Error pages, proxy/captive-portal pages and redirects never qualify. */
async function rememberShell(url, res) {
  if (!isAppRoute(url.pathname)) return;
  if (res.status !== 200 || res.type !== 'basic' || res.redirected) return;
  if (!(res.headers.get('content-type') || '').toLowerCase().includes('text/html')) return;
  const copy = res.clone();
  const html = await copy.clone().text();
  if (!html.includes('id="root"')) return;
  const cache = await caches.open(SHELL_CACHE);
  await cache.put(SHELL_KEY, copy);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Never intercept API calls.
  if (url.pathname.startsWith('/api/')) return;

  // Navigations: network-first, fall back to cached shell when offline.
  // no-store bypasses the HTTP cache — a heuristically-cached index.html
  // can point at bundles from weeks ago.
  if (req.mode === 'navigate') {
    // Public forms are not the app: no shell caching, and no CRM shell served
    // in their place when offline. The browser handles them on its own.
    if (!isAppRoute(url.pathname)) return;
    event.respondWith(
      fetch(req, { cache: 'no-store' })
        .then((res) => {
          event.waitUntil(rememberShell(url, res).catch(() => {}));
          return res;
        })
        .catch(async (err) => {
          const cache = await caches.open(SHELL_CACHE);
          const shell = await cache.match(SHELL_KEY);
          if (shell) return shell;
          throw err;
        })
    );
    return;
  }

  // Built, hashed assets: cache-first.
  if (url.pathname.startsWith('/assets/') || url.pathname.match(/\.(png|svg|ico|woff2?)$/)) {
    event.respondWith(
      caches.open(ASSET_CACHE).then(async (cache) => {
        const hit = await cache.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res && res.status === 200 && res.type === 'basic') cache.put(req, res.clone());
        return res;
      })
    );
  }
});
