import { useState } from 'react';
import { get } from '../api';
import { Loading } from './ui';

/**
 * The email-body expand machine, shared by the Feed, contact timelines, and
 * email search: tracks which message is open, lazily fetches and caches its
 * body, and renders text or sandboxed HTML.
 */
export function useEmailBodies() {
  const [open, setOpen] = useState(null);
  const [bodies, setBodies] = useState({});

  async function load(id) {
    setBodies((b) => ({ ...b, [id]: { loading: true } }));
    try {
      const body = await get(`/emails/${id}/body`);
      setBodies((b) => ({ ...b, [id]: { ...body, loading: false } }));
    } catch (e) {
      // 410: the full text is gone for good (archived from a mailbox that is
      // no longer connected) — a terminal state, not a failure to retry.
      const failed = e.status === 410 ? { gone: e.message } : { error: e.message };
      setBodies((b) => ({ ...b, [id]: { ...failed, loading: false } }));
    }
  }

  async function toggle(id) {
    if (open === id) {
      setOpen(null);
      return;
    }
    setOpen(id);
    // Only a settled body is a cache hit (a 410 "gone" is settled too) — a
    // failed fetch is tried again on the next open instead of pinning its
    // error for the page's lifetime.
    if (!bodies[id] || bodies[id].error) await load(id);
  }

  /** Refetch a body whose load failed (the Retry button in <EmailBody>). */
  const retry = (id) => {
    if (!bodies[id]?.loading && !bodies[id]?.gone) load(id);
  };

  return { open, toggle, bodies, retry, close: () => setOpen(null) };
}

export function EmailBody({ body, onRetry, children }) {
  return (
    <div className="email-body-wrap">
      {body?.loading && <Loading small />}
      {body?.error && (
        <div className="form-error">
          {body.error}
          {onRetry && (
            <>
              {' '}
              <button type="button" className="linklike" onClick={onRetry}>
                Retry
              </button>
            </>
          )}
        </div>
      )}
      {body?.gone && <div className="muted">{body.gone}</div>}
      {body?.body_text && <div className="email-body">{body.body_text}</div>}
      {!body?.body_text && body?.body_html && (
        <iframe title="email" className="email-frame" sandbox="" srcDoc={body.body_html} />
      )}
      {body && !body.loading && !body.error && !body.gone && !body.body_text && !body.body_html && (
        <div className="muted">No body stored for this message.</div>
      )}
      {children}
    </div>
  );
}
