"""Provider-only contract for OpenAI-compatible model gateways."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    messages: tuple[ProviderMessage, ...]
    temperature: float | None = None


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class AIProvider(Protocol):
    """The only model contract the core knows about."""

    def complete(self, request: ProviderRequest) -> ProviderResponse: ...


def messages_from_dicts(messages: Sequence[dict[str, str]]) -> tuple[ProviderMessage, ...]:
    """Convert OpenAI-compatible message dictionaries into domain values."""

    return tuple(ProviderMessage(role=item["role"], content=item["content"]) for item in messages)
