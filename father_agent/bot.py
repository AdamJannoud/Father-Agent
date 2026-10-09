"""The Father Agent in Telegram: one line in, a validated sub-agent back as a ZIP.

    pip install -r requirements-bot.txt
    python main.py bot            # or: python -m father_agent.bot

One process holds everything: the aiogram 3 polling loop (outbound to
Telegram), an aiohttp server on ``$PORT`` whose ``GET /healthz`` says whether
that loop is alive (what a free host's keepalive pinger calls), and one
:class:`~father_agent.factory.Factory` that runs one generation at a time from
a queue. Progress is the factory's own six-stage feed, drawn by
:class:`~father_agent.telegram_progress.TelegramProgress` as one message edited
in place.

The allowlist fails closed: with ``TELEGRAM_ALLOWED_USERS`` empty every message
is refused, and the refusal names the sender's id so adding it is a copy-paste.
An optional shared ``BOT_ACCESS_PASSWORD`` is the second door: ``/auth`` or
``/login`` with it admits a sender for the life of this process (never on disk),
the message carrying it is deleted before it is checked, and five wrong tries in
an hour pause the command for that id. The allowlist never needs it.
Generated code is never executed here either; the bundle is zipped from the
files the factory wrote after the gate passed.

aiogram is an optional extra. Nothing in the core imports this module; the CLI
imports it only for ``python main.py bot``, and ``python -m father_agent.bot``
is that same command (see :func:`main`).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
import io
import logging
import os
import re
import sys
import time
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    """``python -m father_agent.bot [--check] [-p PROVIDER]``: the CLI's ``bot`` command.

    Delegates to :func:`father_agent.cli.main` so both entry points share one
    dispatcher: same ``--check``, same configuration errors, same exit codes.
    Defined above the aiogram imports so that, under ``-m``, it runs before
    them and a core-only install gets the CLI's install hint, not a traceback.
    """
    from .cli import main as cli_main

    return cli_main(["bot", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.methods import GetUpdates
from aiogram.types import (
    BufferedInputFile,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from aiogram.utils.token import TokenValidationError, validate_token
from aiohttp import web

from .config import Config, mask
from .errors import ConfigError, FatherAgentError, ValidationFailedError
from .factory import Factory, GenerationResult
from .logging_setup import RedactingFilter
from .providers.chain import build_chain
from .spec import SubAgentSpec
from .telegram_progress import STAGE_NAMES, TelegramProgress, elapsed_label
from .validator import gate_summary

logger = logging.getLogger(__name__)

#: Telegram's Bot API refuses a document over 50 MB.
UPLOAD_LIMIT = 50 * 1024 * 1024
#: A queue longer than this is refused rather than left to wait for an hour.
MAX_QUEUE = 5
#: Longest line accepted as a command.
MAX_COMMAND = 1000
#: /healthz turns 503 when no getUpdates has succeeded for this long.
STALE_AFTER = 120.0
#: Wrong /auth passwords per id before the command pauses for that id.
AUTH_TRIES = 5
#: How long those tries are counted for, and how long the pause lasts.
AUTH_WINDOW = 3600.0
EXAMPLE = "a Solana wallet watcher that logs balance changes every 60s and plots them"

START_TEXT = (
    "Send me one line of English and I will plan, write and validate an async Python "
    "sub-agent for it, showing each stage as it runs, then hand you the bundle as a ZIP.\n"
    "<pre>Nothing generated is ever executed. Files are written only after the gate "
    "passes.</pre>"
)
HELP_TEXT = (
    "Send one line of English, e.g.\n"
    f"<pre>{EXAMPLE}</pre>\n"
    "/new &lt;line&gt; the same, spelled out\n"
    "/again build your last line again\n"
    "/retry rebuild from the spec that survived a failed run\n"
    "/status what is running and what is queued\n"
    "/whoami your Telegram id and how you got in\n"
    "Nothing generated is ever executed."
)
_WHO_AM_I = re.compile(r"^\s*who\s*am\s*i\s*\??\s*$", re.IGNORECASE)
_AUTH = re.compile(r"^/(?:auth|login)(?:@\w+)?(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def parse_allowed(raw: str) -> frozenset[int]:
    """``"318668971, 42"`` -> ``{318668971, 42}``; anything not an id is ignored."""
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class BotSettings:
    """What the bot reads from the environment, beside the factory's Config."""

    token: str
    allowed: frozenset[int] = frozenset()
    port: int = 8080
    webhook_url: str = ""
    webhook_secret: str = ""
    access_password: str = field(default="", repr=False)
    auth_notify: bool = True

    @property
    def mode(self) -> str:
        """``polling`` (the default) or ``webhook`` (when TELEGRAM_WEBHOOK_URL is set)."""
        return "webhook" if self.webhook_url else "polling"

    @classmethod
    def load(cls, config: Config) -> BotSettings:
        """Read and check the bot settings; ConfigError says exactly what is wrong."""
        token = config.telegram_bot_token
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is not set: create a bot with @BotFather "
                              "and put its token in .env or the host's environment")
        try:
            validate_token(token)
        except TokenValidationError as exc:
            raise ConfigError("TELEGRAM_BOT_TOKEN does not look like a bot token "
                              "(expected 123456789:AA...)") from exc
        raw_port = os.environ.get("PORT", "").strip() or "8080"
        if not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
            raise ConfigError(f"PORT must be a TCP port number, got {raw_port!r}")
        webhook = os.environ.get("TELEGRAM_WEBHOOK_URL", "").strip()
        if webhook and not webhook.startswith("https://"):
            raise ConfigError("TELEGRAM_WEBHOOK_URL must be an https:// URL")
        return cls(token=token,
                   allowed=parse_allowed(os.environ.get("TELEGRAM_ALLOWED_USERS", "")),
                   port=int(raw_port), webhook_url=webhook,
                   webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip(),
                   access_password=config.bot_access_password,
                   auth_notify=os.environ.get("BOT_AUTH_NOTIFY", "").strip().lower()
                   not in {"0", "false", "no", "off"})

    def describe(self) -> list[str]:
        """Display lines for ``bot --check``; the token is masked, the password never shown."""
        allowed = (", ".join(str(i) for i in sorted(self.allowed)) if self.allowed
                   else "EMPTY: every message is refused with the sender's id")
        if self.access_password:
            notify = "operators get a DM" if self.auth_notify else "BOT_AUTH_NOTIFY=0, no DM"
            password = (f"set: /auth admits a guest until restart · {AUTH_TRIES} wrong "
                        f"an hour pauses it · {notify}")
        else:
            password = "off: the allowlist is the only way in"
        return [f"token       {mask(self.token)}",
                f"allowed     {allowed}",
                f"password    {password}",
                f"mode        {self.mode}" + (f" → {self.webhook_url}" if self.webhook_url
                                              else " (outbound long poll)"),
                f"health      GET /healthz on port {self.port}"]


# --------------------------------------------------------------------------- #
# Health: is the poll loop actually alive?
# --------------------------------------------------------------------------- #


@dataclass
class PollHealth:
    """When getUpdates last succeeded, fed by :class:`PollWatch`."""

    mode: str = "polling"
    started: float = field(default_factory=time.monotonic)
    last_ok: float | None = None
    last_ok_at: str = ""
    last_error: str = ""
    polls: int = 0

    def polled(self) -> None:
        """Record one successful getUpdates (or, in webhook mode, one delivered update)."""
        self.last_ok = time.monotonic()
        self.last_ok_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.last_error = ""
        self.polls += 1

    def failed(self, exc: BaseException) -> None:
        """Record why the last poll failed."""
        self.last_error = f"{type(exc).__name__}: {exc}"[:200]

    def report(self, *, stale_after: float = STALE_AFTER) -> tuple[bool, dict[str, Any]]:
        """``(healthy, body)`` for /healthz."""
        now = time.monotonic()
        age = None if self.last_ok is None else round(now - self.last_ok, 1)
        if self.mode == "webhook":
            healthy = True
        else:
            healthy = age is not None and age <= stale_after
        state = "ok" if healthy else ("starting" if self.last_ok is None else "stale")
        return healthy, {"status": state, "mode": self.mode, "last_poll_ok": self.last_ok_at,
                         "seconds_since_poll": age, "polls": self.polls,
                         "last_error": self.last_error,
                         "uptime_seconds": round(now - self.started, 1)}


class PollWatch(BaseRequestMiddleware):
    """A session middleware that marks every successful getUpdates in :class:`PollHealth`."""

    def __init__(self, health: PollHealth) -> None:
        """Feed ``health``."""
        self.health = health

    async def __call__(self, make_request, bot, method):  # type: ignore[override]
        """Pass the request on; note the outcome when it is a getUpdates."""
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        try:
            response = await make_request(bot, method)
        except Exception as exc:
            self.health.failed(exc)
            raise
        self.health.polled()
        return response


@dataclass
class Guest:
    """An id that got in with the password, for this process only."""

    since: datetime
    name: str = ""


class Access:
    """Who may use the bot: the permanent allowlist, plus guests admitted by password.

    Guests live in memory, so a restart clears them. Wrong passwords are counted
    per id; the :data:`AUTH_TRIES`-th wrong one inside :data:`AUTH_WINDOW` pauses
    ``/auth`` for that id for another window. The password is only ever compared
    (in constant time), never stored anywhere but here, logged or echoed.
    """

    def __init__(self, allowed: frozenset[int], password: str = "", *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        """Remember the allowlist and the shared password ("" turns /auth off)."""
        self.allowed = allowed
        self._password = password.encode("utf-8")
        self.clock = clock
        self.guests: dict[int, Guest] = {}
        self.seen: set[int] = set()
        self._wrong: dict[int, list[float]] = {}
        self._paused_until: dict[int, float] = {}

    @property
    def enabled(self) -> bool:
        """Whether a password is set at all; an unset one is a closed door."""
        return bool(self._password)

    def permits(self, user_id: int | None) -> bool:
        """Allowlisted, or admitted by password in this process."""
        return user_id is not None and (user_id in self.allowed or user_id in self.guests)

    def paused_for(self, user_id: int) -> float:
        """Seconds left on this id's pause (0 when it may try)."""
        until = self._paused_until.get(user_id)
        if until is None:
            return 0.0
        left = until - self.clock()
        if left > 0:
            return left
        del self._paused_until[user_id]
        return 0.0

    def tries_left(self, user_id: int) -> int:
        """Wrong passwords this id may still send before the pause."""
        now = self.clock()
        recent = [t for t in self._wrong.get(user_id, []) if now - t < AUTH_WINDOW]
        self._wrong[user_id] = recent
        return AUTH_TRIES - len(recent)

    def check(self, user_id: int, attempt: str, name: str = "") -> str:
        """``"ok"``, ``"wrong"``, ``"paused"`` or ``"off"``; ``"ok"`` admits the id."""
        if not self.enabled:
            return "off"
        if self.paused_for(user_id):
            return "paused"
        if hmac.compare_digest(attempt.encode("utf-8"), self._password):
            self._wrong.pop(user_id, None)
            if user_id not in self.allowed:
                self.guests[user_id] = Guest(since=datetime.now(UTC), name=name)
            return "ok"
        self.tries_left(user_id)  # drop tries older than the window
        self._wrong.setdefault(user_id, []).append(self.clock())
        if len(self._wrong[user_id]) >= AUTH_TRIES:
            del self._wrong[user_id]
            self._paused_until[user_id] = self.clock() + AUTH_WINDOW
        return "wrong"


def auth_argument(text: str | None) -> str | None:
    """The password after ``/auth`` or ``/login`` (``""`` when missing), else None.

    Everything after the command is the password, inner spaces and all; only the
    whitespace around it is dropped. ``/auth@SomeBot`` (a group mention) counts.
    """
    match = _AUTH.match(text or "")
    return None if match is None else (match.group(1) or "").strip()


class AllowList(BaseMiddleware):
    """Refuse every message whose sender is not let in (an empty list refuses all).

    The one exception is ``/auth`` or ``/login`` from an id that is not in yet:
    that goes to ``authenticate`` instead of the refusal.
    """

    def __init__(self, access: Access,
                 authenticate: Callable[[Message, str], Awaitable[None]] | None = None) -> None:
        """Check senders against ``access``; hand password attempts to ``authenticate``."""
        self.access = access
        self.authenticate = authenticate

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Hand the message on, take a password attempt, or answer with the caller's id."""
        user = getattr(event, "from_user", None)
        if user is not None and self.access.permits(user.id):
            self.access.seen.add(user.id)
            return await handler(event, data)
        if isinstance(event, Message):
            attempt = auth_argument(event.text) if user is not None else None
            if attempt is not None and self.authenticate is not None:
                await self.authenticate(event, attempt)
                self.access.seen.add(user.id)
                return None
            logger.warning("refused a message from Telegram id %s (not in "
                           "TELEGRAM_ALLOWED_USERS)", user.id if user else "?")
            if user is not None:
                self.access.seen.add(user.id)
            await event.answer(refusal_text(user.id if user else None,
                                            password=self.access.enabled))
        return None


def refusal_text(user_id: int | None, *, password: bool = False) -> str:
    """The "Not allowed." message, naming the id, and offering /auth when it is on."""
    how = ("send /auth &lt;password&gt; to get in, or ask the operator to add your id"
           if password else
           "ask the operator to add it to TELEGRAM_ALLOWED_USERS, then send the line again")
    return ("Not allowed.\n"
            f"<pre>your Telegram id: {user_id if user_id is not None else 'unknown'}\n"
            f"{how}</pre>\n"
            "Nothing was planned, nothing was spent.")


def _minutes(seconds: float) -> str:
    """``59 minutes`` / ``1 minute``, rounded up."""
    n = max(1, int(-(-seconds // 60)))
    return f"{n} minute{'' if n == 1 else 's'}"


def _who(user: Any) -> str:
    """``Sara, @sara_k`` for the operator DM."""
    parts = [user.full_name.strip()] if user.full_name.strip() else []
    if user.username:
        parts.append(f"@{user.username}")
    return ", ".join(parts)


DELETE_FAILED = ("\nI could not delete your message, so the password is still in this chat. "
                 "Please delete it yourself.")


def _clock(moment: datetime) -> str:
    """``18:47Z``."""
    return moment.astimezone(UTC).strftime("%H:%MZ")


# --------------------------------------------------------------------------- #
# The controller: one generation at a time
# --------------------------------------------------------------------------- #


@dataclass
class Job:
    """One queued generation: a line of English, or a spec that survived a failure."""

    chat_id: int
    user_id: int
    command: str
    spec: SubAgentSpec | None = None


def zip_bundle(result: GenerationResult) -> bytes:
    """The written sub-agent as a ZIP, every file under ``<slug>/``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(result.files.items()):
            archive.write(path, f"{result.spec.slug}/{name}")
    return buffer.getvalue()


def done_text(result: GenerationResult) -> str:
    """``<slug> is written and validated. 9 files, gate green.`` plus the gate line."""
    needs = [e.name for e in result.spec.env_vars if e.required]
    keys = f"needs {', '.join(needs)}" if needs else "no keys needed"
    gate = f"{gate_summary(result.reports)} · nothing was executed · {keys}"
    return (f"<b>{html.escape(result.spec.slug)}</b> is written and validated. "
            f"{len(result.files)} files, gate green.\n<pre>{html.escape(gate)}</pre>")


def failure_text(exc: FatherAgentError, step: int, *, can_retry: bool) -> str:
    """``Stopped at 4/6 · validating.``, what stopped it, and what to send next."""
    if isinstance(exc, ValidationFailedError):
        problems = exc.problems or [str(exc)]
        plural = "" if len(problems) == 1 else "s"
        where = f"{exc.filename}: " if exc.filename and exc.filename not in problems[0] else ""
        cause = f"gate: {where}{problems[0]} ({len(problems)} failure{plural})"
    else:
        cause = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    hint = ("/retry replays the spec that did survive." if can_retry
            else "/again sends the same line again.")
    return (f"Stopped at {step}/6 · {STAGE_NAMES.get(step, 'working')}.\n"
            f"<pre>{html.escape(cause[:600])}\n"
            "nothing was written, nothing was delivered</pre>\n"
            f"{hint}")


class FatherBot:
    """The bot's state: the factory, the queue, the allowlist and the poll health."""

    def __init__(self, config: Config, settings: BotSettings, *, factory: Factory | None = None,
                 provider: str = "auto", progress: TelegramProgress | None = None,
                 output_dir: Path | None = None) -> None:
        """Build the one Factory every generation shares.

        Args:
            config: Factory settings (providers, output folder, limits).
            settings: Token, allowlist, port and mode.
            factory: A ready Factory (tests); built from ``config`` when None.
            provider: Force one provider, as ``new --provider`` does.
            progress: The reporter the factory reports to; one is made when None.
            output_dir: Where bundles are written; ``config.output_dir`` by default.
        """
        self.config = config
        self.settings = settings
        self.progress = progress or TelegramProgress()
        if factory is None:
            factory = Factory(config, build_chain(config, provider), reporter=self.progress)
        else:
            factory.reporter = self.progress
        self.factory = factory
        self.output_dir = output_dir or config.output_dir
        self.health = PollHealth(mode=settings.mode)
        self.access = Access(settings.allowed, settings.access_password)
        self.waiting: deque[Job] = deque()
        self.current: Job | None = None
        self.last_command: dict[int, str] = {}
        self.last_failed_spec: dict[int, SubAgentSpec] = {}
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._worker: asyncio.Task[None] | None = None

    # -- wiring ------------------------------------------------------------------

    def attach(self, bot: Bot) -> None:
        """Watch ``bot``'s getUpdates calls so /healthz can tell a live poll loop."""
        bot.session.middleware(PollWatch(self.health))

    def build_dispatcher(self) -> Dispatcher:
        """A Dispatcher with the allowlist (and the /auth door) in front of every handler."""
        dispatcher = Dispatcher()
        dispatcher.message.outer_middleware(AllowList(self.access, self.authenticate))
        dispatcher.include_router(self._router())
        return dispatcher

    def start(self, bot: Bot) -> None:
        """Start the queue worker (idempotent)."""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._work(bot))

    async def stop(self) -> None:
        """Cancel the worker and close the factory's provider connections."""
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        await self.factory.aclose()

    async def wait_idle(self) -> None:
        """Return once nothing is running or queued (tests; graceful shutdown)."""
        while not (self._idle.is_set() and not self.waiting):
            await self._idle.wait()
            await asyncio.sleep(0)

    # -- handlers ------------------------------------------------------------------

    def _router(self) -> Router:
        """Every command of the seven moments in the plan."""
        router = Router(name="father")
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="/new"), KeyboardButton(text="/status"),
                       KeyboardButton(text="/help")]],
            resize_keyboard=True, is_persistent=False)

        @router.message(CommandStart())
        async def start(message: Message) -> None:
            await message.answer(START_TEXT, reply_markup=keyboard)

        @router.message(Command("help"))
        async def help_(message: Message) -> None:
            await message.answer(HELP_TEXT)

        @router.message(Command("whoami"))
        @router.message(F.text.regexp(_WHO_AM_I))
        async def whoami(message: Message) -> None:
            user_id = message.from_user.id if message.from_user else 0
            guest = self.access.guests.get(user_id)
            if guest is None:
                await message.answer(f"<pre>your Telegram id: {user_id}\n"
                                     "access: allowlist, permanent\n"
                                     "since: before this session</pre>")
                return
            await message.answer(f"<pre>your Telegram id: {user_id}\naccess: password\n"
                                 f"since: {_clock(guest.since)} this session</pre>\n"
                                 "If the bot restarts, send /auth again.")

        @router.message(F.text.regexp(_AUTH))
        async def auth_again(message: Message) -> None:
            # Only someone already in gets here; the AllowList takes everyone else's.
            deleted = await self._delete(message)
            how = ("allowlist, permanent: no password needed"
                   if message.from_user.id in self.access.allowed
                   else "password, until the bot restarts")
            await message.answer(f"You are already in.\n<pre>access: {how}</pre>"
                                 + ("" if deleted else DELETE_FAILED))

        @router.message(Command("status"))
        async def status(message: Message) -> None:
            await message.answer(self.status_text())

        @router.message(Command("new"))
        async def new(message: Message, command: CommandObject) -> None:
            line = (command.args or "").strip()
            if not line:
                await message.answer("Send the line after /new, or just send it on its own, "
                                     f"e.g.\n<pre>{EXAMPLE}</pre>")
                return
            await self.submit(message, Job(message.chat.id, message.from_user.id, line))

        @router.message(Command("again"))
        async def again(message: Message) -> None:
            line = self.last_command.get(message.from_user.id)
            if not line:
                await message.answer("Nothing to repeat yet. Send one line of English first.")
                return
            await self.submit(message, Job(message.chat.id, message.from_user.id, line))

        @router.message(Command("retry"))
        async def retry(message: Message) -> None:
            spec = self.last_failed_spec.get(message.from_user.id)
            if spec is None:
                await message.answer("Nothing to retry: no failed run left a spec behind. "
                                     "/again sends your last line again.")
                return
            await self.submit(message, Job(message.chat.id, message.from_user.id,
                                           spec.command, spec=spec))

        @router.message(F.text.startswith("/"))
        async def unknown(message: Message) -> None:
            await message.answer("I do not know that command. /help lists what I do.")

        @router.message(F.text)
        async def line(message: Message) -> None:
            await self.submit(message, Job(message.chat.id, message.from_user.id,
                                           message.text.strip()))

        @router.message()
        async def other(message: Message) -> None:
            await message.answer("Send one line of English as text. /help shows an example.")

        return router

    # -- the password door ----------------------------------------------------------

    async def authenticate(self, message: Message, attempt: str) -> None:
        """``/auth <password>`` from an id that is not in: delete it, check it, answer.

        The delete comes first, whatever the outcome, so the answer never sits
        under a visible password. A delete Telegram refuses is said in the answer.
        """
        user = message.from_user
        if not self.access.enabled:
            if attempt:
                await self._delete(message)
            await message.answer("Password access is not enabled on this bot.\n"
                                 f"<pre>your Telegram id: {user.id}\n"
                                 "ask the operator to add it to TELEGRAM_ALLOWED_USERS</pre>")
            return
        if not attempt:
            await message.answer("Send the password after the command:\n"
                                 "<pre>/auth &lt;password&gt;</pre>")
            return
        first_contact = user.id not in self.access.seen
        deleted = await self._delete(message)
        outcome = self.access.check(user.id, attempt, name=_who(user))
        if outcome == "ok":
            logger.info("Telegram id %s got in with the password (%s guests this session)",
                        user.id, len(self.access.guests))
            text = ("You're in. The bot had restarted, so access starts again from here."
                    if first_contact else
                    f"You're in.\n<pre>send one line of English, e.g.\n{EXAMPLE}</pre>")
        elif outcome == "paused":
            text = ("Too many wrong passwords.\n"
                    f"<pre>/auth is paused for this id for another "
                    f"{_minutes(self.access.paused_for(user.id))}\n"
                    "this one was not checked</pre>")
        else:
            logger.warning("wrong /auth password from Telegram id %s", user.id)
            left = 0 if self.access.paused_for(user.id) else self.access.tries_left(user.id)
            text = ("That password is not right.\n<pre>"
                    + (f"{left} tr{'y' if left == 1 else 'ies'} left before /auth pauses "
                       "for an hour" if left else "no tries left: /auth is paused for an hour")
                    + "</pre>")
        await message.answer(text + ("" if deleted else DELETE_FAILED))
        if outcome == "ok":
            await self._notify_operators(message.bot, user.id)

    async def _delete(self, message: Message) -> bool:
        """Delete a message carrying a password; False (never raised) when Telegram refuses."""
        try:
            await message.delete()
        except Exception as exc:  # noqa: BLE001 - the answer matters more than the cleanup
            logger.warning("could not delete a /auth message from Telegram id %s (%s)",
                           message.from_user.id if message.from_user else "?",
                           type(exc).__name__)
            return False
        return True

    async def _notify_operators(self, bot: Bot, user_id: int) -> None:
        """One DM to each allowlisted operator: the shared password was just used."""
        guest = self.access.guests.get(user_id)
        if not self.settings.auth_notify or guest is None:
            return
        count = len(self.access.guests)
        label = f" ({html.escape(guest.name)})" if guest.name else ""
        text = ("Someone used the password.\n"
                f"<pre>user {user_id}{label}\ngot in at {_clock(guest.since)}\n"
                f"{count} guest{' is' if count == 1 else 's are'} in this session</pre>")
        for operator in sorted(self.access.allowed):
            try:
                await bot.send_message(operator, text)
            except Exception as exc:  # noqa: BLE001 - one unreachable operator is not fatal
                logger.warning("could not tell operator %s about a password login (%s)",
                               operator, type(exc).__name__)

    def status_text(self) -> str:
        """``/status``: the running generation, the queue and the poll loop."""
        healthy, body = self.health.report()
        poll = (f"polling ok · last poll {body['seconds_since_poll']}s ago" if healthy
                else f"polling {body['status']}")
        if self.health.mode == "webhook":
            poll = "webhook mode"
        if self.current is None:
            return f"Idle. Nothing queued.\n<pre>{poll}</pre>"
        return (f"{html.escape(self._current_line())}\n"
                f"<pre>working · {elapsed_label(self.progress.elapsed)}\n"
                f"queue: {len(self.waiting)}\n{poll}</pre>")

    def _current_line(self) -> str:
        """``solana_wallet_watcher is at 4/6 (validating)``."""
        spec = self.factory.last_spec or (self.current.spec if self.current else None)
        name = spec.slug if spec else "the current one"
        step = max(1, self.progress.reported_step)
        return f"{name} is at {step}/6 ({STAGE_NAMES[step]})"

    # -- the queue -------------------------------------------------------------------

    async def submit(self, message: Message, job: Job) -> None:
        """Queue one generation and say where it stands."""
        if not job.command and job.spec is None:
            await message.answer("Send one line of English as text. /help shows an example.")
            return
        if len(job.command) > MAX_COMMAND:
            await message.answer(f"Keep it to one line, under {MAX_COMMAND} characters.")
            return
        if len(self.waiting) >= MAX_QUEUE:
            await message.answer(f"The queue is full ({MAX_QUEUE} waiting). "
                                 "Send it again once one lands.")
            return
        if job.spec is None:
            self.last_command[job.user_id] = job.command
        busy = self.current is not None or bool(self.waiting)
        self.waiting.append(job)
        self._idle.clear()
        self._wake.set()
        if busy:
            await message.answer(
                "One generation at a time.\n"
                f"<pre>{html.escape(self._current_line())}\n"
                f"queue: {len(self.waiting)} · yours starts the moment it lands</pre>")

    async def _work(self, bot: Bot) -> None:
        """Run queued jobs one after another, forever."""
        while True:
            if not self.waiting:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
                continue
            self.current = self.waiting.popleft()
            try:
                await self._run(bot, self.current)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad job must not stop the queue
                logger.exception("bot job crashed")
            finally:
                self.current = None

    async def _run(self, bot: Bot, job: Job) -> None:
        """One generation: a status message edited in place, then the ZIP or the reason."""
        status = await bot.send_message(job.chat_id, "<b>working · 0:00</b>")

        async def edit(text: str) -> None:
            await bot.edit_message_text(text=text, chat_id=job.chat_id,
                                        message_id=status.message_id)

        self.progress.begin(edit)
        try:
            result = await self.factory.generate(job.command, output_dir=self.output_dir,
                                                 force=True, spec=job.spec)
        except FatherAgentError as exc:
            # The gate is step 4 whether it refused one file while coding or the bundle.
            step = 4 if isinstance(exc, ValidationFailedError) else self.progress.failed_step
            await self.progress.finish("stopped")
            spec = self.factory.last_spec or job.spec
            if spec is not None:
                self.last_failed_spec[job.user_id] = spec
            logger.warning("bot generation stopped at %s/6: %s", step, exc)
            await bot.send_message(job.chat_id, failure_text(exc, step,
                                                             can_retry=spec is not None))
            return
        await self.progress.finish("done")
        self.last_failed_spec.pop(job.user_id, None)
        await self.deliver(bot, job.chat_id, result)

    async def deliver(self, bot: Bot, chat_id: int, result: GenerationResult) -> None:
        """Send the done message and the bundle as ``<slug>.zip`` (text when too big)."""
        await bot.send_message(chat_id, done_text(result))
        data = zip_bundle(result)
        next_line = self.progress.next_command or result.spec.run_example
        if len(data) > UPLOAD_LIMIT:
            await bot.send_message(
                chat_id,
                f"The ZIP is {len(data) / 1_048_576:.1f} MB, over Telegram's "
                f"{UPLOAD_LIMIT // 1_048_576} MB upload cap, so it was not sent.\n"
                f"<pre>on the server: {html.escape(str(result.target_dir))} "
                f"(gone at the next restart)\n"
                f"files: {', '.join(html.escape(n) for n in sorted(result.files))}\n"
                f"next: {html.escape(next_line)}</pre>")
            return
        await bot.send_document(chat_id,
                                BufferedInputFile(data, filename=f"{result.spec.slug}.zip"),
                                caption=f"<pre>next: {html.escape(next_line)}</pre>")


# --------------------------------------------------------------------------- #
# The health server and the entry point
# --------------------------------------------------------------------------- #


def health_app(father: FatherBot) -> web.Application:
    """``GET /healthz`` (200 while the poll loop is alive, 503 otherwise) and ``GET /``."""

    async def healthz(request: web.Request) -> web.Response:
        healthy, body = father.health.report()
        body["busy"] = father.current is not None
        body["queue"] = len(father.waiting)
        return web.json_response(body, status=200 if healthy else 503)

    async def index(request: web.Request) -> web.Response:
        return web.Response(text="Father Agent Telegram bot. Health: /healthz\n")

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", index)
    return app


def make_bot(settings: BotSettings) -> Bot:
    """A Bot that sends Telegram HTML by default."""
    return Bot(settings.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def _redact_aiogram_logs(config: Config) -> None:
    """Send aiogram's own log lines to stderr through the same key redaction."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("father %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter(config.secrets))
    aiogram_logger = logging.getLogger("aiogram")
    aiogram_logger.handlers = [handler]
    aiogram_logger.setLevel(logging.INFO)
    aiogram_logger.propagate = False


async def serve(config: Config, settings: BotSettings, *, provider: str = "auto") -> None:
    """Run the health server, the queue worker and the poll loop until SIGTERM/SIGINT."""
    _redact_aiogram_logs(config)
    father = FatherBot(config, settings, provider=provider)
    bot = make_bot(settings)
    father.attach(bot)
    dispatcher = father.build_dispatcher()
    app = health_app(father)
    if settings.mode == "webhook":
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

        SimpleRequestHandler(dispatcher, bot, secret_token=settings.webhook_secret or None
                             ).register(app, path="/telegram")
        setup_application(app, dispatcher, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.port).start()
    if not settings.allowed and not settings.access_password:
        logger.warning("TELEGRAM_ALLOWED_USERS is empty: every message is refused with the "
                       "sender's id. Add yours and restart.")
    logger.info("bot up: %s · health on :%s/healthz · allowed users %s · password access %s",
                settings.mode, settings.port, len(settings.allowed),
                "on" if settings.access_password else "off")
    father.start(bot)
    try:
        if settings.mode == "webhook":
            await bot.set_webhook(settings.webhook_url.rstrip("/") + "/telegram",
                                  secret_token=settings.webhook_secret or None,
                                  drop_pending_updates=False)
            father.health.polled()
            await asyncio.Event().wait()  # until cancelled by the host's SIGTERM
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            await dispatcher.start_polling(bot, handle_signals=True, close_bot_session=True)
    finally:
        await father.stop()
        await runner.cleanup()
        await bot.session.close()


def run(config: Config, *, provider: str = "auto") -> int:
    """``python main.py bot``: load the settings and serve until stopped."""
    settings = BotSettings.load(config)
    try:
        asyncio.run(serve(config, settings, provider=provider))
    except TelegramUnauthorizedError as exc:
        raise ConfigError("Telegram refused TELEGRAM_BOT_TOKEN (unauthorized): copy it again "
                          "from @BotFather") from exc
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return 0
