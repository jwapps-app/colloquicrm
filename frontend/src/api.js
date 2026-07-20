const BASE = '/api/v1';
const TOKEN_KEY = 'crm_token';

// Auth endpoints where a 401 is an expected outcome (bad credentials, wrong
// code) rather than an expired session — don't redirect for these.
const NO_REDIRECT_PATHS = ['/auth/login', '/auth/setup', '/auth/totp', '/auth/bootstrap'];

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

function buildUrl(path, params) {
  let url = BASE + path;
  if (params) {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.set(k, v);
    });
    const s = qs.toString();
    if (s) url += '?' + s;
  }
  return url;
}

// A 401 outside the auth flows means the session is gone: drop the token,
// leave a breadcrumb for the login page, and send the user there.
function unauthorized(method, path) {
  clearToken();
  try {
    // Breadcrumb for the login page: which request ended the session.
    sessionStorage.setItem('crm_signout_reason', `${method} ${path}`);
  } catch {
    // storage unavailable — the redirect still happens
  }
  if (!window.location.pathname.startsWith('/login')) {
    window.location.assign('/login');
  }
  const err = new Error('Session expired. Please sign in again.');
  err.status = 401;
  return err;
}

async function request(method, path, { params, body, formData } = {}) {
  const url = buildUrl(path, params);

  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const opts = { method, headers, cache: 'no-store' };
  if (formData) {
    opts.body = formData; // browser sets multipart content-type + boundary
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(url, opts);
  } catch {
    throw new Error('Network error — could not reach the server.');
  }

  if (res.status === 401 && !NO_REDIRECT_PATHS.includes(path)) {
    throw unauthorized(method, path);
  }

  if (res.status === 204) return null;

  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    if (data && typeof data === 'object' && data.detail) {
      msg = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } else if (typeof data === 'string' && data) {
      msg = data.slice(0, 200);
    }
    // Proxy/CDN error pages are HTML; show the status instead of markup soup.
    if (msg.trimStart().startsWith('<')) {
      msg = `The server returned an error page (${res.status}) — likely a temporary proxy or tunnel hiccup. Try again.`;
    }
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

export async function download(path, params) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(buildUrl(path, params), { headers, cache: 'no-store' });
  if (res.status === 401) {
    throw unauthorized('GET', path);
  }
  if (!res.ok) {
    let msg = `Download failed (${res.status})`;
    try {
      const d = await res.json();
      if (d.detail) msg = d.detail;
    } catch {
      // non-JSON error body — keep the status message
    }
    throw new Error(msg);
  }
  const blob = await res.blob();
  const dispo = res.headers.get('Content-Disposition') || '';
  const m = dispo.match(/filename="?([^";]+)"?/);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = m ? m[1] : 'export.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
}

// Short-TTL cache for static-ish org lookups (users, contact types, tags,
// custom fields, pipelines) that otherwise re-fetch on every navigation.
// Sharing the in-flight promise also dedupes concurrent identical requests.
const _getCache = new Map();

export function cachedGet(path, params, ttlMs = 60000) {
  const url = buildUrl(path, params);
  const hit = _getCache.get(url);
  if (hit && Date.now() - hit.at < ttlMs) return hit.promise;
  const promise = request('GET', path, { params }).catch((e) => {
    _getCache.delete(url); // don't cache failures
    throw e;
  });
  _getCache.set(url, { at: Date.now(), promise });
  return promise;
}

/** Drop cached GETs whose path starts with `pathPrefix` (query strings
 * included), so the next cachedGet refetches. Call after a mutation that
 * invalidates one of the cached lookups — e.g. bustCache('/tags') after
 * adding a tag, bustCache('/custom-fields') after editing fields. */
export function bustCache(pathPrefix) {
  const prefix = BASE + pathPrefix;
  for (const key of [..._getCache.keys()]) {
    if (key.startsWith(prefix)) _getCache.delete(key);
  }
}

export const get = (path, params) => request('GET', path, { params });
export const post = (path, body, params) => request('POST', path, { body, params });
export const patch = (path, body) => request('PATCH', path, { body });
export const del = (path) => request('DELETE', path);
export const upload = (path, formData) => request('POST', path, { formData });
