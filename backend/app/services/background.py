"""Fire-and-forget background work that must actually finish.

The event loop only holds a weak reference to a task made with
asyncio.create_task — drop the returned handle and the task can be
garbage-collected mid-flight, silently killing an import or sync. spawn()
parks every detached task in a module-level set until it completes, so
"kick it off and return" call sites don't have to manage the reference.
"""

import asyncio
from collections.abc import Coroutine

_tasks: set[asyncio.Task] = set()


def spawn(coro: Coroutine) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
