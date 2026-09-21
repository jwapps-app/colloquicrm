import { useEffect, useId, useRef } from 'react';

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Open modals, innermost last — only the top one answers Escape / traps Tab,
// so a dialog stacked on another doesn't close both.
const stack = [];

export default function Modal({ title, onClose, children }) {
  const titleId = useId();
  const dialogRef = useRef(null);

  // Callers pass inline arrows for onClose; route Escape through a ref so the
  // keydown listener registers once instead of on every parent render.
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  });

  useEffect(() => {
    const dialog = dialogRef.current;
    // Whatever had focus when the dialog opened gets it back on close.
    const opener = document.activeElement;
    const token = {};
    stack.push(token);

    // Visible, enabled controls inside the dialog, in tab order.
    const focusables = () => [...dialog.querySelectorAll(FOCUSABLE)].filter((el) => el.getClientRects().length > 0);

    // Move focus inside — unless a child already claimed it (autoFocus). The
    // first real control beats the × so Enter doesn't immediately close.
    if (!dialog.contains(document.activeElement)) {
      const first = focusables().find((el) => !el.classList.contains('modal-close'));
      (first || dialog).focus();
    }

    function onKey(e) {
      if (stack[stack.length - 1] !== token) return;
      if (e.key === 'Escape') {
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab') return;
      const items = focusables();
      if (items.length === 0) {
        e.preventDefault();
        dialog.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (!dialog.contains(active)) {
        // Focus got out (a click on the backdrop, say) — pull it back in.
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
      } else if (e.shiftKey && (active === first || active === dialog)) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      const i = stack.indexOf(token);
      if (i !== -1) stack.splice(i, 1);
      if (opener && typeof opener.focus === 'function' && document.contains(opener)) opener.focus();
    };
  }, []);

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={titleId} tabIndex={-1} ref={dialogRef}>
        <div className="modal-head">
          <h3 id={titleId}>{title}</h3>
          <button type="button" className="icon-btn modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}
