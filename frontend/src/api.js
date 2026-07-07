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

async function request(method, path, { params, body, formData } = {}) {
  let url = BASE + path;
  if (params) {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') qs.set(k, v);
    });
    const s = qs.toString();
    if (s) url += '?' + s;
  }

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
    throw err;
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

export const get = (path, params) => request('GET', path, { params });
export const post = (path, body, params) => request('POST', path, { body, params });
export const patch = (path, body) => request('PATCH', path, { body });
export const del = (path) => request('DELETE', path);
export const upload = (path, formData) => request('POST', path, { formData });
