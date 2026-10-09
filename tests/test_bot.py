"""The Telegram bot, offline: real Update objects into a real Dispatcher.

aiogram ships no test harness (``aiogram.test_utils`` is not part of the
package), so this module carries its own: a :class:`RecordingSession` replaces
the HTTP session of a real ``Bot`` and records every Bot API method instead of
sending it. No token, no network, no .env: the conftest blocks sockets.

The suite skips when the bot extra is not installed, unless
``FATHER_REQUIRE_BOT=1`` (set in CI), where a missing aiogram is a failure.
"""

from __future__ import annotations

import asyncio
import io
import itertools
import logging
import os
import subprocess
import sys
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

if os.environ.get("FATHER_REQUIRE_BOT") == "1":
    import aiogram  # noqa: F401 - CI must run this suite, never skip it
else:
    pytest.importorskip("aiogram", reason="bot extra not installed "
                                          "(pip install -r requirements-bot.txt)")

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import (
    DeleteMessage,
    DeleteWebhook,
    EditMessageText,
    GetUpdates,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Chat, Message, Update, User
from aiohttp.test_utils import make_mocked_request

from father_agent import bot as botmod
from father_agent.config import Config
from father_agent.errors import ConfigError
from father_agent.factory import Factory
from father_agent.logging_setup import PROGRESS_LOGGER
from father_agent.providers import MockProvider, ProviderChain
from father_agent.providers.base import Completion
from father_agent.telegram_progress import TelegramProgress

from .conftest import ROOT, SAMPLE_COMMAND

OWNER = 318668971
FRIEND = 42
STRANGER = 555123456
TOKEN = "123456789:AAtest-not-a-real-token"


# --------------------------------------------------------------------------- #
# The harness
# --------------------------------------------------------------------------- #


class TelegramHTML(HTMLParser):
    """Refuse what Telegram's HTML parse mode refuses: unknown or unbalanced tags."""

    ALLOWED = {"b", "strong", "i", "em", "u", "s", "code", "pre", "a", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        assert tag in self.ALLOWED, f"Telegram would refuse <{tag}>"
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.stack and self.stack.pop() == tag, f"unbalanced </{tag}>"

    @classmethod
    def check(cls, text: str) -> None:
        parser = cls()
        parser.feed(text)
        parser.close()
        assert not parser.stack, f"unclosed {parser.stack}"


class RecordingSession(BaseSession):
    """A Bot session that records each method and answers it locally."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._ids = itertools.count(1000)
        self.fail_polls = False
        #: An exception class DeleteMessage raises, as Telegram does without the right.
        self.refuse_deletes: type[Exception] | None = None

    async def make_request(self, bot: Bot, method: TelegramMethod[Any],
                           timeout: int | None = None):  # noqa: ASYNC109 - BaseSession API
        self.calls.append(method)
        if isinstance(method, GetUpdates):
            if self.fail_polls:
                raise RuntimeError("telegram unreachable")
            return []
        if isinstance(method, DeleteMessage) and self.refuse_deletes is not None:
            raise self.refuse_deletes(method, "Bad Request: message can't be deleted")
        if isinstance(method, SendMessage | EditMessageText | SendDocument):
            chat_id = int(method.chat_id)
            text = getattr(method, "text", None) or getattr(method, "caption", None)
            TelegramHTML.check(text or "")
            message_id = getattr(method, "message_id", None) or next(self._ids)
            return Message(message_id=message_id, date=datetime.now(),
                           chat=Chat(id=chat_id, type="private"), text=text)
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise RuntimeError("no downloads in tests")
        yield b""

    async def close(self) -> None:
        return None

    def of(self, kind: type) -> list[Any]:
        """Recorded calls of one method type, in order."""
        return [c for c in self.calls if isinstance(c, kind)]

    def texts(self) -> list[str]:
        """The text of every SendMessage, in order."""
        return [c.text for c in self.of(SendMessage)]


class Gate(MockProvider):
    """The mock provider, but planning waits until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def complete(self, messages, **kwargs) -> Completion:
        await self.release.wait()
        return await super().complete(messages, **kwargs)


class BrokenTests(MockProvider):
    """Plans and writes agent.py, then writes a test file that fails the gate while ``broken``."""

    broken = True

    async def complete(self, messages, **kwargs) -> Completion:
        if self.broken and "file=test_agent.py" in messages[0]["content"]:
            return Completion("```python\ndef oops(:\n    pass\n```", self.name, self.model)
        return await super().complete(messages, **kwargs)


class Harness:
    """A FatherBot with a real Dispatcher and a recording Bot."""

    def __init__(self, config: Config, tmp_path: Path, provider: MockProvider | None = None,
                 allowed: frozenset[int] = frozenset({OWNER, FRIEND}), password: str = "",
                 notify: bool = True) -> None:
        self.session = RecordingSession()
        self.bot = Bot(TOKEN, session=self.session,
                       default=botmod.DefaultBotProperties(parse_mode="HTML"))
        self.provider = provider or MockProvider()
        settings = botmod.BotSettings(token=TOKEN, allowed=allowed, access_password=password,
                                      auth_notify=notify)
        factory = Factory(config, ProviderChain([self.provider]))
        self.father = botmod.FatherBot(config, settings, factory=factory,
                                       progress=TelegramProgress(min_interval=0),
                                       output_dir=tmp_path / "out")
        self.father.attach(self.bot)
        self.dispatcher = self.father.build_dispatcher()
        self._update_ids = itertools.count(1)

    async def send(self, text: str, user_id: int = OWNER) -> None:
        """Feed one private-chat text message from ``user_id`` through the Dispatcher."""
        user = User(id=user_id, is_bot=False, first_name="Owner" if user_id == OWNER else "Sara",
                    username=None if user_id == OWNER else "sara_k")
        message = Message(message_id=next(self._update_ids), date=datetime.now(),
                          chat=Chat(id=user_id, type="private"), from_user=user, text=text)
        await self.dispatcher.feed_update(self.bot, Update(update_id=next(self._update_ids),
                                                           message=message))

    async def __aenter__(self) -> Harness:
        self.father.start(self.bot)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.wait_for(self.father.wait_idle(), 30)
        await self.father.stop()


def drive(coro_fn) -> Any:
    """Run one async test body."""
    return asyncio.run(coro_fn())


# --------------------------------------------------------------------------- #
# The seven moments
# --------------------------------------------------------------------------- #


def test_start_introduces_the_factory_with_the_keyboard(config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path) as h:
            await h.send("/start")
        return h

    h = drive(body)
    (sent,) = h.session.of(SendMessage)
    assert sent.text.startswith("Send me one line of English and I will plan, write and "
                                "validate an async Python sub-agent")
    assert "hand you the bundle as a ZIP." in sent.text
    assert ("<pre>Nothing generated is ever executed. Files are written only after the gate "
            "passes.</pre>") in sent.text
    buttons = [b.text for row in sent.reply_markup.keyboard for b in row]
    assert buttons == ["/new", "/status", "/help"]
    assert h.bot.default.parse_mode == "HTML"


def test_who_am_i_names_the_id_and_how_it_got_in(config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path) as h:
            await h.send("who am I")
            await h.send("/whoami", user_id=FRIEND)
        return h

    texts = drive(body).session.texts()
    assert texts == [f"<pre>your Telegram id: {id_}\naccess: allowlist, permanent\n"
                     "since: before this session</pre>" for id_ in (OWNER, FRIEND)]


def test_stranger_is_refused_with_their_id_and_nothing_runs(config: Config,
                                                            tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path) as h:
            await h.send("make me a bot that scrapes prices", user_id=STRANGER)
            await h.send("/start", user_id=STRANGER)
        return h

    h = drive(body)
    texts = h.session.texts()
    assert len(texts) == 2
    assert texts[0] == ("Not allowed.\n<pre>your Telegram id: 555123456\nask the operator to "
                        "add it to TELEGRAM_ALLOWED_USERS, then send the line again</pre>\n"
                        "Nothing was planned, nothing was spent.")
    assert h.father.factory.last_spec is None and h.father.current is None
    assert not h.session.of(EditMessageText) and not h.session.of(SendDocument)
    assert not (tmp_path / "out").exists()


def test_empty_allowlist_fails_closed(config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path, allowed=frozenset()) as h:
            await h.send(SAMPLE_COMMAND)
        return h

    h = drive(body)
    (text,) = h.session.texts()
    assert text.startswith("Not allowed.") and f"your Telegram id: {OWNER}" in text
    assert not (tmp_path / "out").exists()


def test_generation_edits_one_status_message_then_sends_the_zip(
        config: Config, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path) as h:
            await h.send(SAMPLE_COMMAND)
        return h

    with caplog.at_level(logging.INFO, logger=PROGRESS_LOGGER):
        h = drive(body)
    sends = h.session.of(SendMessage)
    # exactly two messages: the status message and the done message, never six
    assert len(sends) == 2
    status, done = sends
    assert status.text == "<b>working · 0:00</b>"
    edits = h.session.of(EditMessageText)
    assert edits and all(e.message_id == 1000 for e in edits)
    final = edits[-1].text
    assert final.startswith("<b>done · ")
    lines = final.split("<pre>", 1)[1].removesuffix("</pre>").splitlines()
    assert [line.split(" ", 2)[1] for line in lines] == [f"{n}/6" for n in range(1, 7)]
    assert lines[0] == "father 1/6 planning · provider mock/offline-templates"
    assert "wrote agent.py" in lines[2] and "wrote test_agent.py" in lines[2]
    assert "ruff ok" in lines[3] and "nothing was executed" in lines[3]
    assert lines[5].endswith("wallet_watcher/ · 9 files")
    # the very same lines went through the factory's existing progress logger
    logged = [r.getMessage() for r in caplog.records if r.name == PROGRESS_LOGGER]
    assert "planning · provider mock/offline-templates" in logged
    assert any(m.endswith("wallet_watcher/ · 9 files") for m in logged)

    assert done.text.startswith("<b>wallet_watcher</b> is written and validated. 9 files, "
                                "gate green.\n<pre>")
    assert "ruff ok" in done.text and "nothing was executed" in done.text
    (document,) = h.session.of(SendDocument)
    assert document.document.filename == "wallet_watcher.zip"
    assert document.caption.startswith("<pre>next: ") and "--once" in document.caption
    with zipfile.ZipFile(io.BytesIO(document.document.data)) as archive:
        names = archive.namelist()
        assert "wallet_watcher/agent.py" in names and "wallet_watcher/spec.json" in names
        assert len(names) == 9
        assert archive.testzip() is None
    order = [type(c).__name__ for c in h.session.calls]
    assert order.index("SendDocument") > order.index("EditMessageText")


def test_second_line_queues_with_its_position(config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path, provider=Gate()) as h:
            await h.send(SAMPLE_COMMAND)
            for _ in range(20):  # let the worker pick the first job up
                await asyncio.sleep(0)
            assert h.father.current is not None
            await h.send("now one that watches a GitHub repo for new releases", user_id=FRIEND)
            await h.send("/status")
            h.provider.release.set()
        return h

    h = drive(body)
    texts = h.session.texts()
    queued = next(t for t in texts if t.startswith("One generation at a time."))
    assert queued == ("One generation at a time.\n<pre>the current one is at 1/6 (planning)\n"
                      "queue: 1 · yours starts the moment it lands</pre>")
    status = next(t for t in texts if "queue: 1\n" in t and "working · " in t)
    assert "is at 1/6 (planning)" in status
    documents = h.session.of(SendDocument)
    assert len(documents) == 2  # both ran, one after the other
    assert {int(d.chat_id) for d in documents} == {OWNER, FRIEND}


def test_gate_failure_reports_the_stage_and_writes_nothing(config: Config,
                                                           tmp_path: Path) -> None:
    config = Config.load(env_file=tmp_path / "missing.env", max_repair_attempts=0)

    async def body() -> Harness:
        async with Harness(config, tmp_path, provider=BrokenTests()) as h:
            await h.send(SAMPLE_COMMAND)
        return h

    h = drive(body)
    failure = h.session.texts()[-1]
    head, pre_and_hint = failure.split("\n", 1)
    assert head == "Stopped at 4/6 · validating."
    assert pre_and_hint.startswith("<pre>gate: test_agent.py")
    assert "(1 failure)" in pre_and_hint or "failures)" in pre_and_hint
    assert "nothing was written, nothing was delivered</pre>" in pre_and_hint
    assert pre_and_hint.endswith("/retry replays the spec that did survive.")
    assert h.session.of(EditMessageText)[-1].text.startswith("<b>stopped · ")
    assert not h.session.of(SendDocument)
    assert not (tmp_path / "out" / "wallet_watcher").exists()
    assert OWNER in h.father.last_failed_spec


def test_retry_replays_the_surviving_spec(config: Config, tmp_path: Path) -> None:
    config = Config.load(env_file=tmp_path / "missing.env", max_repair_attempts=0)

    async def body() -> Harness:
        async with Harness(config, tmp_path, provider=BrokenTests()) as h:
            await h.send(SAMPLE_COMMAND)
            await h.father.wait_idle()
            h.provider.broken = False  # the model recovers
            await h.send("/retry")
        return h

    h = drive(body)
    (document,) = h.session.of(SendDocument)
    assert document.document.filename == "wallet_watcher.zip"
    final = h.session.of(EditMessageText)[-1].text
    assert "father 1/6 re-using spec wallet_watcher (planning skipped)" in final
    assert OWNER not in h.father.last_failed_spec


def test_again_replays_the_last_line_and_retry_needs_a_failure(config: Config,
                                                               tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path) as h:
            await h.send("/again")
            await h.send("/retry")
            await h.send(SAMPLE_COMMAND)
            await h.father.wait_idle()
            await h.send("/again")
        return h

    h = drive(body)
    texts = h.session.texts()
    assert texts[0] == "Nothing to repeat yet. Send one line of English first."
    assert texts[1].startswith("Nothing to retry")
    assert len(h.session.of(SendDocument)) == 2


def test_new_without_a_line_and_unknown_commands(config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path) as h:
            await h.send("/new")
            await h.send("/deploy wallet_watcher")
            await h.send("/help")
            await h.send("/status")
        return h

    texts = drive(body).session.texts()
    assert texts[0].startswith("Send the line after /new")
    assert texts[1] == "I do not know that command. /help lists what I do."
    assert "/again" in texts[2] and "/retry" in texts[2] and "/whoami" in texts[2]
    assert texts[3].startswith("Idle. Nothing queued.")


def test_oversized_bundle_falls_back_to_text(config: Config, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(botmod, "UPLOAD_LIMIT", 100)

    async def body() -> Harness:
        async with Harness(config, tmp_path) as h:
            await h.send(SAMPLE_COMMAND)
        return h

    h = drive(body)
    assert not h.session.of(SendDocument)
    fallback = h.session.texts()[-1]
    assert "over Telegram's 0 MB upload cap, so it was not sent." in fallback
    assert "gone at the next restart" in fallback and "agent.py" in fallback


# --------------------------------------------------------------------------- #
# The password door: /auth and /login
# --------------------------------------------------------------------------- #

PASSWORD = "swordfish 42"


class FakeClock:
    """A monotonic clock the throttle reads, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _reply_after_delete(h: Harness) -> tuple[int, int]:
    """Indexes of the first DeleteMessage and of the SendMessage that follows it."""
    order = h.session.calls
    deleted = next(i for i, c in enumerate(order) if isinstance(c, DeleteMessage))
    replied = next(i for i, c in enumerate(order) if isinstance(c, SendMessage) and i > deleted)
    return deleted, replied


def test_auth_argument_takes_the_password_verbatim() -> None:
    assert botmod.auth_argument("/auth swordfish 42") == "swordfish 42"
    assert botmod.auth_argument("/login   pass  word  ") == "pass  word"
    assert botmod.auth_argument("/AUTH@AJ_FatherAgent_BOT pw") == "pw"
    assert botmod.auth_argument("/auth") == ""
    assert botmod.auth_argument("/authorize pw") is None
    assert botmod.auth_argument("auth pw") is None and botmod.auth_argument(None) is None


def test_stranger_is_offered_auth_when_a_password_is_set(config: Config,
                                                         tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path, password=PASSWORD) as h:
            await h.send(SAMPLE_COMMAND, user_id=STRANGER)
        return h

    h = drive(body)
    assert h.session.texts() == [
        f"Not allowed.\n<pre>your Telegram id: {STRANGER}\nsend /auth &lt;password&gt; to "
        "get in, or ask the operator to add your id</pre>\n"
        "Nothing was planned, nothing was spent."]
    assert h.father.factory.last_spec is None and not (tmp_path / "out").exists()


def test_correct_password_admits_deletes_first_and_tells_the_operators(
        config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path, password=PASSWORD) as h:
            await h.send("hello", user_id=STRANGER)
            await h.send(f"/auth {PASSWORD}", user_id=STRANGER)
            await h.send("/whoami", user_id=STRANGER)
        return h

    h = drive(body)
    assert STRANGER in h.father.access.guests and h.father.access.permits(STRANGER)
    (deleted,) = h.session.of(DeleteMessage)
    assert (int(deleted.chat_id), deleted.message_id) == (STRANGER, 3)  # the /auth message
    d, r = _reply_after_delete(h)
    assert d < r
    replies = [m for m in h.session.of(SendMessage) if int(m.chat_id) == STRANGER]
    assert replies[1].text == ("You're in.\n<pre>send one line of English, e.g.\n"
                               f"{botmod.EXAMPLE}</pre>")
    assert replies[2].text.startswith(f"<pre>your Telegram id: {STRANGER}\naccess: password\n"
                                      "since: ")
    assert replies[2].text.endswith("Z this session</pre>\nIf the bot restarts, send /auth again.")
    dms = {int(m.chat_id): m.text for m in h.session.of(SendMessage)
           if int(m.chat_id) in (OWNER, FRIEND)}
    assert set(dms) == {OWNER, FRIEND}
    assert dms[OWNER].startswith(f"Someone used the password.\n<pre>user {STRANGER} "
                                 "(Sara, @sara_k)\ngot in at ")
    assert dms[OWNER].endswith("\n1 guest is in this session</pre>")
    assert all(PASSWORD not in m.text for m in h.session.of(SendMessage))


def test_login_is_the_same_door_and_a_restart_is_named(config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path, password=PASSWORD, notify=False) as h:
            await h.send(f"/login {PASSWORD}", user_id=STRANGER)  # first words this session
        return h

    h = drive(body)
    assert h.session.texts() == ["You're in. The bot had restarted, so access starts again "
                                 "from here."]
    assert len(h.session.of(DeleteMessage)) == 1  # and BOT_AUTH_NOTIFY=0 sent no DM


def test_wrong_password_refuses_deletes_and_counts(config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path, password=PASSWORD) as h:
            await h.send("/auth swordfish-41", user_id=STRANGER)
            await h.send(SAMPLE_COMMAND, user_id=STRANGER)
        return h

    h = drive(body)
    assert not h.father.access.permits(STRANGER) and not h.father.access.guests
    assert len(h.session.of(DeleteMessage)) == 1
    d, r = _reply_after_delete(h)
    assert d < r
    wrong, refused = h.session.texts()
    assert wrong == ("That password is not right.\n"
                     "<pre>4 tries left before /auth pauses for an hour</pre>")
    assert refused.startswith("Not allowed.") and h.father.factory.last_spec is None


def test_five_wrong_pauses_auth_then_it_releases(config: Config, tmp_path: Path) -> None:
    clock = FakeClock()

    async def body() -> Harness:
        async with Harness(config, tmp_path, password=PASSWORD) as h:
            h.father.access.clock = clock
            for n in range(5):
                await h.send(f"/auth wrong-{n}", user_id=STRANGER)
            await h.send(f"/auth {PASSWORD}", user_id=STRANGER)  # right, but paused
            assert not h.father.access.permits(STRANGER)
            clock.now += botmod.AUTH_WINDOW - 60
            await h.send(f"/auth {PASSWORD}", user_id=STRANGER)  # still paused
            clock.now += 61
            await h.send(f"/auth {PASSWORD}", user_id=STRANGER)  # the pause has lifted
        return h

    h = drive(body)
    texts = h.session.texts()
    assert [t.split("<pre>")[1].split("</pre>")[0] for t in texts[:5]] == [
        "4 tries left before /auth pauses for an hour",
        "3 tries left before /auth pauses for an hour",
        "2 tries left before /auth pauses for an hour",
        "1 try left before /auth pauses for an hour",
        "no tries left: /auth is paused for an hour"]
    assert texts[5] == ("Too many wrong passwords.\n<pre>/auth is paused for this id for "
                        "another 60 minutes\nthis one was not checked</pre>")
    assert "for another 1 minute\n" in texts[6]
    assert texts[7].startswith("You're in.")
    assert h.father.access.permits(STRANGER)
    assert len(h.session.of(DeleteMessage)) == 8  # every attempt, paused ones included


def test_throttle_is_per_id_and_old_tries_expire() -> None:
    clock = FakeClock()
    access = botmod.Access(frozenset({OWNER}), PASSWORD, clock=clock)
    for _ in range(4):
        assert access.check(STRANGER, "nope") == "wrong"
    assert access.tries_left(STRANGER) == 1 and access.tries_left(FRIEND) == 5
    clock.now += botmod.AUTH_WINDOW
    assert access.tries_left(STRANGER) == 5  # an hour later the old tries no longer count
    assert access.check(FRIEND, PASSWORD) == "ok" and FRIEND in access.guests
    assert botmod.Access(frozenset(), "").check(STRANGER, "") == "off"


def test_allowlisted_ids_never_need_the_password(config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path, password=PASSWORD) as h:
            await h.send("/whoami")
            await h.send("/status", user_id=FRIEND)
            await h.send("/auth not-the-password")
        return h

    h = drive(body)
    whoami, status, already = h.session.texts()
    assert "access: allowlist, permanent" in whoami and status.startswith("Idle.")
    assert already == ("You are already in.\n<pre>access: allowlist, permanent: no password "
                       "needed</pre>")
    assert len(h.session.of(DeleteMessage)) == 1  # a password-shaped message is still removed
    assert not h.father.access.guests and h.father.access.tries_left(OWNER) == 5


@pytest.mark.parametrize("refusal", [TelegramBadRequest, TelegramForbiddenError],
                         ids=["bad-request", "forbidden"])
@pytest.mark.parametrize("attempt", [PASSWORD, "wrong"], ids=["right", "wrong"])
def test_a_refused_delete_does_not_crash_and_is_said(config: Config, tmp_path: Path,
                                                     refusal: type[Exception],
                                                     attempt: str) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path, password=PASSWORD, notify=False) as h:
            h.session.refuse_deletes = refusal
            await h.send("hi", user_id=STRANGER)
            await h.send(f"/auth {attempt}", user_id=STRANGER)
        return h

    h = drive(body)
    reply = h.session.texts()[-1]
    assert reply.startswith("You're in." if attempt == PASSWORD else "That password is not right.")
    assert reply.endswith("I could not delete your message, so the password is still in this "
                          "chat. Please delete it yourself.")
    assert h.father.access.permits(STRANGER) is (attempt == PASSWORD)


def test_unset_password_is_a_closed_door(config: Config, tmp_path: Path) -> None:
    async def body() -> Harness:
        async with Harness(config, tmp_path) as h:
            await h.send("/auth anything at all", user_id=STRANGER)
            await h.send("/auth", user_id=STRANGER)
        return h

    h = drive(body)
    texts = h.session.texts()
    assert texts == ["Password access is not enabled on this bot.\n<pre>your Telegram id: "
                     f"{STRANGER}\nask the operator to add it to TELEGRAM_ALLOWED_USERS</pre>"] * 2
    assert not h.father.access.guests and not h.father.access.permits(STRANGER)


def test_the_password_never_reaches_a_log_record(config: Config, tmp_path: Path,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    guess = "swordfish 41 typo"

    async def body() -> Harness:
        async with Harness(config, tmp_path, password=PASSWORD) as h:
            h.session.refuse_deletes = TelegramBadRequest
            await h.send(f"/auth {guess}", user_id=STRANGER)
            await h.send(f"/auth {PASSWORD}", user_id=STRANGER)
        return h

    with caplog.at_level(logging.DEBUG):
        h = drive(body)
    records = [r.getMessage() for r in caplog.records]
    assert any(f"wrong /auth password from Telegram id {STRANGER}" in m for m in records)
    assert any(f"Telegram id {STRANGER} got in with the password" in m for m in records)
    for secret in (PASSWORD, guess):
        assert all(secret not in m for m in records)
        assert all(secret not in m.text for m in h.session.of(SendMessage))


def test_password_settings_load_and_stay_secret(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    load = lambda: botmod.BotSettings.load(Config.load(env_file=tmp_path / "x.env"))  # noqa: E731
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    settings = load()
    assert settings.access_password == "" and settings.auth_notify is True
    assert any("off: the allowlist is the only way in" in line for line in settings.describe())
    monkeypatch.setenv("BOT_ACCESS_PASSWORD", f"  {PASSWORD}\n")
    monkeypatch.setenv("BOT_AUTH_NOTIFY", "0")
    settings = load()
    assert settings.access_password == PASSWORD and settings.auth_notify is False
    assert PASSWORD not in repr(settings) and PASSWORD not in "\n".join(settings.describe())
    config = Config.load(env_file=tmp_path / "x.env")
    assert PASSWORD in config.secrets and PASSWORD not in repr(config)


# --------------------------------------------------------------------------- #
# Health, settings and the redaction of the token
# --------------------------------------------------------------------------- #


def test_healthz_reflects_the_poll_loop(config: Config, tmp_path: Path) -> None:
    async def body() -> list[tuple[int, dict[str, Any]]]:
        h = Harness(config, tmp_path)
        app = botmod.health_app(h.father)
        handler = next(r.handler for r in app.router.routes()
                       if r.method == "GET" and r.resource.canonical == "/healthz")

        async def probe() -> tuple[int, dict[str, Any]]:
            response = await handler(make_mocked_request("GET", "/healthz", app=app))
            import json
            return response.status, json.loads(response.body)

        seen = [await probe()]
        await h.bot(GetUpdates(timeout=0))  # through the session middleware
        seen.append(await probe())
        h.session.fail_polls = True
        with pytest.raises(RuntimeError):
            await h.bot(GetUpdates(timeout=0))
        h.father.health.last_ok -= botmod.STALE_AFTER + 1
        seen.append(await probe())
        await h.bot(DeleteWebhook())  # other methods do not count as polls
        await h.father.stop()
        return seen

    (s0, b0), (s1, b1), (s2, b2) = drive(body)
    assert (s0, b0["status"]) == (503, "starting")
    assert (s1, b1["status"], b1["polls"], b1["mode"]) == (200, "ok", 1, "polling")
    assert b1["last_poll_ok"] and b1["queue"] == 0 and b1["busy"] is False
    assert (s2, b2["status"]) == (503, "stale")
    assert "telegram unreachable" in b2["last_error"]


def test_settings_load_check_and_fail_closed(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    load = lambda: botmod.BotSettings.load(Config.load(env_file=tmp_path / "x.env"))  # noqa: E731
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN is not set"):
        load()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not a token")
    with pytest.raises(ConfigError, match="does not look like a bot token"):
        load()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    settings = load()
    assert settings.allowed == frozenset() and settings.mode == "polling"
    assert settings.port == 8080
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", f"{OWNER}, {FRIEND},abc")
    monkeypatch.setenv("PORT", "10000")
    settings = load()
    assert settings.allowed == {OWNER, FRIEND} and settings.port == 10000
    assert all(TOKEN not in line for line in settings.describe())
    monkeypatch.setenv("TELEGRAM_WEBHOOK_URL", "http://insecure.example")
    with pytest.raises(ConfigError, match="https"):
        load()
    # the token is a secret like the provider keys: redacted from every log line
    assert TOKEN in Config.load(env_file=tmp_path / "x.env").secrets


def test_cli_bot_check_prints_masked_settings(monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    from father_agent.cli import main

    assert main(["bot", "--check"]) == 2
    assert "TELEGRAM_BOT_TOKEN is not set" in capsys.readouterr().err
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", str(OWNER))
    assert main(["bot", "--check"]) == 0
    text = capsys.readouterr().out
    assert "nothing was contacted" in text and str(OWNER) in text
    assert TOKEN not in text and "GET /healthz on port 8080" in text


def _entry(args: list[str], **env: str) -> subprocess.CompletedProcess[str]:
    """Run one entry point in a fresh interpreter; the env is the conftest-scrubbed one."""
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=60, env={**os.environ, **env})


@pytest.mark.parametrize("flags", [[], ["--check"]], ids=["run", "check"])
def test_module_entry_without_a_token_fails_like_main_py(flags: list[str]) -> None:
    module = _entry(["-m", "father_agent.bot", *flags])
    script = _entry(["main.py", "bot", *flags])
    assert module.returncode == script.returncode == 2
    assert "configuration error: TELEGRAM_BOT_TOKEN is not set" in module.stderr
    assert "Traceback" not in module.stderr and module.stdout == ""
    assert (module.stdout, module.stderr) == (script.stdout, script.stderr)


def test_module_entry_check_matches_main_py() -> None:
    env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_USERS": str(OWNER)}
    module = _entry(["-m", "father_agent.bot", "--check"], **env)
    script = _entry(["main.py", "bot", "--check"], **env)
    assert module.returncode == script.returncode == 0
    assert "nothing was contacted" in module.stdout and TOKEN not in module.stdout
    assert (module.stdout, module.stderr) == (script.stdout, script.stderr)
