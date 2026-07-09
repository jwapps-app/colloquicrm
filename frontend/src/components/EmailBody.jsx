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

  async function toggle(id) {
    if (open === id) {
      setOpen(null);
      return;
    }
    setOpen(id);
    if (!bodies[id]) {
      setBodies((b) => ({ ...b, [id]: { loading: true } }));
      try {
        const body = await get(`/emails/${id}/body`);
        setBodies((b) => ({ ...b, [id]: { ...body, loading: false } }));
      } catch (e) {
        setBodies((b) => ({ ...b, [id]: { error: e.message, loading: false } }));
      }
    }
  }

  return { open, toggle, bodies, close: () => setOpen(null) };
}

export function EmailBody({ body, children }) {
  return (
    <div className="email-body-wrap">
      {body?.loading && <Loading small />}
      {body?.error && <div className="form-error">{body.error}</div>}
      {body?.body_text && <div className="email-body">{body.body_text}</div>}
      {!body?.body_text && body?.body_html && (
        <iframe title="email" className="email-frame" sandbox="" srcDoc={body.body_html} />
      )}
      {body && !body.loading && !body.error && !body.body_text && !body.body_html && (
        <div className="muted">No body stored for this message.</div>
      )}
      {children}
    </div>
  );
}
