"""The Father Agent in Telegram: one line in, a validated sub-agent back as a ZIP.

    pip install -r requirements-bot.txt
    python main.py bot

One process holds everything: the aiogram 3 polling loop (outbound to
Telegram), an aiohttp server on ``$PORT`` whose ``GET /healthz`` says whether
that loop is alive (what a free host's keepalive pinger calls), and one
:class:`~father_agent.factory.Factory` that runs one generation at a time from
a queue. Progress is the factory's own six-stage feed, drawn by
:class:`~father_agent.telegram_progress.TelegramProgress` as one message edited
in place.

The allowlist fails closed: with ``TELEGRAM_ALLOWED_USERS`` empty every message
is refused, and the refusal names the sender's id so adding it is a copy-paste.
Generated code is never executed here either; the bundle is zipped from the
files the factory wrote after the gate passed.

aiogram is an optional extra. Nothing in the core imports this module; the CLI
imports it only for ``python main.py bot``.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import logging
import os
import re
import sys
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    "/whoami your Telegram id and whether it is allowed\n"
    "Nothing generated is ever executed."
)
_WHO_AM_I = re.compile(r"^\s*who\s*am\s*i\s*\??\s*$", re.IGNORECASE)


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
                   webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip())

    def describe(self) -> list[str]:
        """Display lines for ``bot --check``; the token is masked."""
        allowed = (", ".join(str(i) for i in sorted(self.allowed)) if self.allowed
                   else "EMPTY: every message is refused with the sender's id")
        return [f"token       {mask(self.token)}",
                f"allowed     {allowed}",
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


class AllowList(BaseMiddleware):
    """Refuse every message whose sender is not on the list (an empty list refuses all)."""

    def __init__(self, allowed: frozenset[int]) -> None:
        """Remember the allowed Telegram user ids."""
        self.allowed = allowed

    async def __call__(self, handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        """Hand the message on, or answer with the caller's id and stop."""
        user = getattr(event, "from_user", None)
        if user is not None and user.id in self.allowed:
            return await handler(event, data)
        if isinstance(event, Message):
            logger.warning("refused a message from Telegram id %s (not in "
                           "TELEGRAM_ALLOWED_USERS)", user.id if user else "?")
            await event.answer(refusal_text(user.id if user else None))
        return None


def refusal_text(user_id: int | None) -> str:
    """The "Not allowed." message, naming the id to add."""
    return ("Not allowed.\n"
            f"<pre>your Telegram id: {user_id if user_id is not None else 'unknown'}\n"
            "ask the operator to add it to TELEGRAM_ALLOWED_USERS, then send the line "
            "again</pre>\n"
            "Nothing was planned, nothing was spent.")


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
        """A Dispatcher with the allowlist in front of every message handler."""
        dispatcher = Dispatcher()
        dispatcher.message.outer_middleware(AllowList(self.settings.allowed))
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
            await message.answer(f"Your id: <b>{user_id}</b> · allowed. "
                                 f"Allowed users: {len(self.settings.allowed)}.")

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
    if not settings.allowed:
        logger.warning("TELEGRAM_ALLOWED_USERS is empty: every message is refused with the "
                       "sender's id. Add yours and restart.")
    logger.info("bot up: %s · health on :%s/healthz · allowed users %s", settings.mode,
                settings.port, len(settings.allowed))
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
