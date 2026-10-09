"""Shared fixtures. Every test runs offline: real sockets are blocked."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from father_agent.config import Config  # noqa: E402

KEY_VARS = ("GROQ_API_KEY", "HF_TOKEN", "GROQ_MODEL", "HF_MODEL", "GROQ_BASE_URL", "HF_BASE_URL",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_BASE_URL",
            "FATHER_LOCAL_LLM_URL", "FATHER_PROVIDER_ORDER", "FATHER_ALLOW_MOCK_FALLBACK",
            "FATHER_OUTPUT_DIR", "FATHER_LOG_DIR", "FATHER_REQUEST_TIMEOUT",
            "FATHER_MAX_RETRIES", "FATHER_LOG_LEVEL", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ALLOWED_USERS", "TELEGRAM_WEBHOOK_URL", "TELEGRAM_WEBHOOK_SECRET", "PORT",
            "BOT_ACCESS_PASSWORD", "BOT_AUTH_NOTIFY")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test tries to open an internet socket."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError(f"network access attempted in tests: {address!r}")
        real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("network access attempted in tests")))


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No keys from the developer's shell or .env leak into tests."""
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FATHER_ENV_FILE", str(tmp_path / "no-such.env"))
    monkeypatch.setenv("FATHER_OUTPUT_DIR", str(tmp_path / "subagents"))
    monkeypatch.setenv("FATHER_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """A keyless config writing into the test's temp folder."""
    return Config.load(env_file=tmp_path / "missing.env")


SAMPLE_COMMAND = "a Solana wallet watcher that logs balance changes every 60s and plots them"
