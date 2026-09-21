"""Fire-and-forget background work that must actually finish.

The event loop only holds a weak reference to a task made with
asyncio.create_task — drop the returned handle and the task can be
garbage-collected mid-flight, silently killing an import or sync. spawn()
parks every detached task in a module-level set until it completes, so
"kick it off and return" call sites don't have to manage the reference.

after_commit() is the other half: side effects that must only happen once a
request's writes are durable (notifications that re-read the row from a fresh
session). Callbacks queue on the session and run when — and only when — its
transaction commits; a rollback throws them away.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine

from sqlalchemy import event
from sqlalchemy.orm import Session

log = logging.getLogger("background")

_tasks: set[asyncio.Task] = set()

_AFTER_COMMIT_KEY = "after_commit_callbacks"


def spawn(coro: Coroutine) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


def after_commit(db, callback: Callable[[], None]) -> None:
    """Run `callback` once `db`'s current transaction has committed.

    `db` is the request's AsyncSession (or a plain Session). The callback is a
    plain callable — typically a lambda that spawns a coroutine — and is handed
    nothing: capture ids, not ORM objects, since it runs outside the
    transaction. If the transaction rolls back instead, the callback is
    dropped and never runs. Whoever commits (the route itself, or get_db's
    commit on the way out) triggers it; nobody has to remember to."""
    session = getattr(db, "sync_session", db)
    session.info.setdefault(_AFTER_COMMIT_KEY, []).append(callback)


def pending_after_commit(db) -> int:
    """How many callbacks are waiting on this session's commit (for tests)."""
    session = getattr(db, "sync_session", db)
    return len(session.info.get(_AFTER_COMMIT_KEY, ()))


@event.listens_for(Session, "after_commit")
def _run_after_commit(session: Session) -> None:
    if session.in_nested_transaction():
        # A released SAVEPOINT is not durable yet — wait for the real commit.
        return
    callbacks = session.info.pop(_AFTER_COMMIT_KEY, None)
    for callback in callbacks or ():
        try:
            callback()
        except Exception:
            # The write is committed; a broken notification must not turn the
            # response into a 500.
            log.exception("after-commit callback failed")


@event.listens_for(Session, "after_soft_rollback")
def _drop_after_commit(session: Session, previous_transaction) -> None:
    if previous_transaction.nested:
        # Only a SAVEPOINT went away; the outer transaction may still commit.
        return
    # The queued side effects describe writes that no longer exist.
    session.info.pop(_AFTER_COMMIT_KEY, None)
