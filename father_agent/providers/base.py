"""Provider interface shared by every model backend."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

Message = dict[str, str]


@dataclass(frozen=True)
class Completion:
    """A model reply plus where it came from."""

    text: str
    provider: str
    model: str

    @property
    def label(self) -> str:
        """``provider/model`` as shown in progress lines."""
        return f"{self.provider}/{self.model}"


class Provider(ABC):
    """A chat-completion backend (Groq, Hugging Face, local llama.cpp, mock)."""

    #: Short name used in config and logs, e.g. ``"groq"``.
    name: str = "provider"
    #: Model identifier sent to the backend.
    model: str = ""
    #: True for the offline deterministic provider.
    is_mock: bool = False

    @property
    def label(self) -> str:
        """``provider/model`` for logs and progress lines."""
        return f"{self.name}/{self.model}"

    @abstractmethod
    async def complete(self, messages: list[Message], *, temperature: float = 0.2,
                       max_tokens: int = 4096) -> Completion:
        """Return the model's reply to ``messages``.

        Raises:
            ProviderError: On any transport, HTTP or payload failure.
        """

    async def check(self) -> str:
        """Cheap reachability check used by ``doctor --online``; returns a status line."""
        return "not checked"

    async def aclose(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release network resources. The default has none."""
