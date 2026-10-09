"""Configuration for the Father Agent.

Settings come from the process environment and an optional ``.env`` file in
the project root. Only free inference endpoints are accepted: a base URL whose
host is not on the allow-list is refused at load time, so a paid API can never
be reached by accident.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigError

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Hosts that serve free inference. Anything else is refused.
FREE_HOSTS: dict[str, str] = {
    "api.groq.com": "Groq free tier",
    "router.huggingface.co": "Hugging Face Inference Providers (free credits)",
    "api-inference.huggingface.co": "Hugging Face Serverless Inference",
}

#: A local llama.cpp / OpenAI-compatible server is allowed only on loopback.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_HF_BASE_URL = "https://router.huggingface.co/v1"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
DEFAULT_HF_MODEL = "Qwen/Qwen2.5-72B-Instruct"

KNOWN_PROVIDERS = ("groq", "huggingface", "local", "mock")


def _is_placeholder(value: str) -> bool:
    """Return True for empty strings and obvious template placeholders."""
    lowered = value.strip().lower()
    return (not lowered or lowered.startswith(("your", "<", "changeme", "replace", "xxx"))
            or lowered in {"none", "null", "placeholder"})


def _secret(name: str) -> str:
    """Read a secret from the environment, treating placeholders as unset."""
    value = os.environ.get(name, "")
    return "" if _is_placeholder(value) else value.strip()


def _int(name: str, default: int, minimum: int = 0) -> int:
    """Read an integer setting, raising ConfigError on garbage."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _float(name: str, default: float) -> float:
    """Read a positive float setting, raising ConfigError on garbage."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be > 0, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    """Read a boolean setting (1/true/yes/on)."""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _path(name: str, default: Path) -> Path:
    """Read a path setting; relative paths resolve against the project root."""
    raw = os.environ.get(name, "").strip()
    path = Path(raw).expanduser() if raw else default
    return path if path.is_absolute() else (PROJECT_ROOT / path)


def check_free_endpoint(url: str, *, allow_local: bool = False) -> str:
    """Return ``url`` if its host serves free inference, else raise ConfigError.

    Args:
        url: The OpenAI-compatible base URL to check.
        allow_local: Accept loopback hosts (for an opt-in llama.cpp server).
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if allow_local and host in LOCAL_HOSTS:
        return url.rstrip("/")
    if parsed.scheme != "https":
        raise ConfigError(f"Refusing {url!r}: remote endpoints must use https.")
    if host not in FREE_HOSTS:
        allowed = ", ".join(sorted(FREE_HOSTS))
        raise ConfigError(
            f"Refusing {url!r}: only free inference endpoints are allowed ({allowed})."
        )
    return url.rstrip("/")


@dataclass(frozen=True)
class Config:
    """Immutable runtime settings for one Father Agent process."""

    groq_api_key: str = ""
    hf_token: str = ""
    groq_model: str = DEFAULT_GROQ_MODEL
    hf_model: str = DEFAULT_HF_MODEL
    groq_base_url: str = DEFAULT_GROQ_BASE_URL
    hf_base_url: str = DEFAULT_HF_BASE_URL
    local_llm_url: str = ""
    local_llm_model: str = "local"
    provider_order: tuple[str, ...] = ("groq", "huggingface")
    allow_mock_fallback: bool = False
    request_timeout: float = 90.0
    max_retries: int = 3
    max_concurrency: int = 2
    max_repair_attempts: int = 2
    temperature: float = 0.2
    output_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "subagents")
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")
    log_level: str = "INFO"
    prompts_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "prompts")
    env_file: Path | None = None

    def __post_init__(self) -> None:
        """Enforce the free-only rule and sane provider names."""
        check_free_endpoint(self.groq_base_url)
        check_free_endpoint(self.hf_base_url)
        if self.local_llm_url:
            check_free_endpoint(self.local_llm_url, allow_local=True)
        unknown = [p for p in self.provider_order if p not in KNOWN_PROVIDERS]
        if unknown:
            raise ConfigError(
                f"Unknown provider(s) in FATHER_PROVIDER_ORDER: {', '.join(unknown)}. "
                f"Known: {', '.join(KNOWN_PROVIDERS)}."
            )

    @classmethod
    def load(cls, env_file: Path | str | None = None, **overrides: object) -> Config:
        """Build a Config from ``.env`` plus the environment.

        Real environment variables win over ``.env`` values. Keyword overrides
        win over both and are mainly for tests and CLI flags.
        """
        path = Path(env_file) if env_file else PROJECT_ROOT / ".env"
        loaded: Path | None = None
        if path.is_file():
            try:
                from dotenv import load_dotenv

                load_dotenv(path, override=False)
                loaded = path
            except ImportError:
                logger.warning("python-dotenv is not installed; ignoring %s", path)
            except OSError as exc:
                logger.warning("Could not read %s: %s", path, exc)
        order = tuple(
            p.strip().lower()
            for p in os.environ.get("FATHER_PROVIDER_ORDER", "groq,huggingface").split(",")
            if p.strip()
        )
        values: dict[str, object] = {
            "groq_api_key": _secret("GROQ_API_KEY"),
            "hf_token": _secret("HF_TOKEN"),
            "groq_model": os.environ.get("GROQ_MODEL", "").strip() or DEFAULT_GROQ_MODEL,
            "hf_model": os.environ.get("HF_MODEL", "").strip() or DEFAULT_HF_MODEL,
            "groq_base_url": os.environ.get("GROQ_BASE_URL", "").strip() or DEFAULT_GROQ_BASE_URL,
            "hf_base_url": os.environ.get("HF_BASE_URL", "").strip() or DEFAULT_HF_BASE_URL,
            "local_llm_url": os.environ.get("FATHER_LOCAL_LLM_URL", "").strip(),
            "local_llm_model": os.environ.get("FATHER_LOCAL_LLM_MODEL", "").strip() or "local",
            "provider_order": order or ("groq", "huggingface"),
            "allow_mock_fallback": _bool("FATHER_ALLOW_MOCK_FALLBACK", False),
            "request_timeout": _float("FATHER_REQUEST_TIMEOUT", 90.0),
            "max_retries": _int("FATHER_MAX_RETRIES", 3, minimum=1),
            "max_concurrency": _int("FATHER_MAX_CONCURRENCY", 2, minimum=1),
            "max_repair_attempts": _int("FATHER_MAX_REPAIR_ATTEMPTS", 2, minimum=0),
            "temperature": _float("FATHER_TEMPERATURE", 0.2),
            "output_dir": _path("FATHER_OUTPUT_DIR", PROJECT_ROOT / "subagents"),
            "log_dir": _path("FATHER_LOG_DIR", PROJECT_ROOT / "logs"),
            "log_level": os.environ.get("FATHER_LOG_LEVEL", "INFO").strip().upper() or "INFO",
            "prompts_dir": _path("FATHER_PROMPTS_DIR", PROJECT_ROOT / "prompts"),
            "env_file": loaded,
        }
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]

    @property
    def secrets(self) -> list[str]:
        """Return every configured secret, for log redaction."""
        return [s for s in (self.groq_api_key, self.hf_token) if s]

    def has_key(self, provider: str) -> bool:
        """Return True when ``provider`` has what it needs to make a call."""
        if provider == "groq":
            return bool(self.groq_api_key)
        if provider == "huggingface":
            return bool(self.hf_token)
        if provider == "local":
            return bool(self.local_llm_url)
        return provider == "mock"

    @property
    def remote_ready(self) -> bool:
        """True when at least one real (non-mock) provider can be used."""
        return any(self.has_key(p) for p in self.provider_order if p != "mock")


def mask(secret: str) -> str:
    """Return a display-safe form of a secret: never more than its last 4 chars."""
    if not secret:
        return "missing"
    return f"set (…{secret[-4:]})" if len(secret) >= 12 else "set"
