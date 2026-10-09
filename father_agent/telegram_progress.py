"""The factory's six-stage progress feed, drawn as one Telegram message edited in place.

:class:`TelegramProgress` *is* a :class:`~father_agent.logging_setup.ProgressReporter`:
the Factory's six ``_progress`` calls reach it exactly as they reach the terminal,
every line still goes to the ``father.progress`` logger (and so the log file),
and the same line is folded into one status message::

    working · 0:12
    father 1/6 planning · provider groq
    father 2/6 wallet_watcher · blockchain · httpx + pandas + matplotlib
    father 3/6 wrote agent.py: 5 classes, 10 methods, docstrings · 288 lines · ...

A stage that reports more than once (three files written at 3/6, two gate lines
at 4/6) stays one line, so the message never holds more than six. Edits are
coalesced: at most one every ``min_interval`` seconds, and the final state is
always drawn. Nothing here imports aiogram; the bot passes an ``edit`` coroutine.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Awaitable, Callable

from .logging_setup import ProgressReporter

logger = logging.getLogger(__name__)

#: What each step is called in a queue or failure message ("at 4/6 (validating)").
STAGE_NAMES = {1: "planning", 2: "spec", 3: "coding", 4: "validating", 5: "delivery",
               6: "writing"}
#: The step that is under way once step N has reported: 2/6 is printed when the
#: plan is done, so a failure after it happened while coding (3/6).
_IN_PROGRESS = {0: 1, 1: 1, 2: 3, 3: 3, 4: 5, 5: 6, 6: 6}

Editor = Callable[[str], Awaitable[object]]


def elapsed_label(seconds: float) -> str:
    """``0:12``, ``3:07``: minutes and seconds."""
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


class TelegramProgress(ProgressReporter):
    """A ProgressReporter that also keeps one Telegram status message up to date."""

    def __init__(self, *, min_interval: float = 1.0) -> None:
        """``min_interval``: the least time between two edits of the status message."""
        super().__init__()
        self.min_interval = min_interval
        self._edit: Editor | None = None
        self._lines: dict[int, list[str]] = {}
        self._extra: list[str] = []
        self._next = ""
        self._step = 0
        self._started = time.monotonic()
        self._dirty = False
        self._task: asyncio.Task[None] | None = None
        self._shown = ""
        self.edits = 0

    # -- the ProgressReporter interface the Factory calls ----------------------

    def step(self, stage: str, step: int | None, message: str) -> None:
        """Log the line as the terminal does, then fold it into the status message."""
        super().step(stage, step, message)
        if self._edit is None:
            return
        if step:
            self._step = max(self._step, step)
            self._lines.setdefault(step, []).append(message)
        else:
            self._extra.append(message)
        self._kick()

    def note(self, message: str) -> None:
        """Log a free-form line; the ``next: ...`` note is kept for the delivery caption."""
        super().note(message)
        if message.startswith("next: "):
            self._next = message.removeprefix("next: ")

    # -- what the bot drives ---------------------------------------------------

    def begin(self, edit: Editor) -> None:
        """Start a new run whose status message ``edit`` redraws."""
        self._edit = edit
        self._lines, self._extra, self._next = {}, [], ""
        self._step, self._dirty, self._task, self.edits = 0, False, None, 0
        self._shown = ""
        self._started = time.monotonic()

    @property
    def active(self) -> bool:
        """True while a run is being drawn."""
        return self._edit is not None

    @property
    def reported_step(self) -> int:
        """The highest step reported so far (0 before the first line)."""
        return self._step

    @property
    def failed_step(self) -> int:
        """The step that was under way, for ``Stopped at N/6``."""
        return _IN_PROGRESS.get(self._step, self._step)

    @property
    def elapsed(self) -> float:
        """Seconds since :meth:`begin`."""
        return time.monotonic() - self._started

    @property
    def next_command(self) -> str:
        """The factory's own ``next:`` line for this run, or ``""``."""
        return self._next

    def lines(self) -> list[str]:
        """One ``father n/6 ...`` line per reported step, then any provider notes."""
        rows = [f"father {n}/6 {' · '.join(self._lines[n])}" for n in sorted(self._lines)]
        return rows + [f"father {m}" for m in self._extra[-2:]]

    def render(self, state: str = "working") -> str:
        """The status message as Telegram HTML: a header, then the lines in a block."""
        header = f"<b>{html.escape(state)} · {elapsed_label(self.elapsed)}</b>"
        body = "\n".join(self.lines())
        return f"{header}\n<pre>{html.escape(body)}</pre>" if body else header

    async def finish(self, state: str) -> None:
        """Stop coalescing and draw the final state (``done``, ``stopped``) once."""
        if self._edit is None:
            return
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._draw(state)
        self._edit = None

    # -- internals -------------------------------------------------------------

    def _kick(self) -> None:
        """Mark the message stale and make sure one flusher is running."""
        self._dirty = True
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._flush())
            except RuntimeError:  # no loop: the Factory was driven synchronously
                self._task = None

    async def _flush(self) -> None:
        """Redraw while lines keep arriving, never faster than ``min_interval``."""
        while self._dirty and self._edit is not None:
            self._dirty = False
            await self._draw("working")
            await asyncio.sleep(self.min_interval)

    async def _draw(self, state: str) -> None:
        """One edit; a failed edit is logged, never raised into the Factory."""
        edit, text = self._edit, self.render(state)
        if edit is None or text == self._shown:  # Telegram refuses a no-op edit
            return
        try:
            await edit(text)
            self._shown = text
            self.edits += 1
        except Exception as exc:  # noqa: BLE001 - a lost edit must not stop a generation
            logger.warning("status message edit failed: %s", exc)
