"""LLMRequest — the canonical sampling envelope for LLM calls (ADR-0046)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.core.message import ChatMessage


class ReasoningEffort(StrEnum):
    """Provider-neutral model reasoning effort."""

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class LLMRequest(BaseModel):
    """Canonical request envelope carrying every sampling parameter.

    This envelope is the ONLY carrier of sampling parameters: HTTP headers
    carry auth credentials and user-configured passthrough only — sampling
    knobs never travel as headers (PRD §2.4 / chapter 8).

    The model is fully serializable (``model_dump()`` round-trips through
    ``model_validate()``, both python and JSON modes) so a future cassette
    phase can switch to content-addressed request keys.

    ``extra_body`` is the providerOptions-style escape hatch: the engine
    merges it into the wire request body top level with user-wins
    precedence. The merge itself lives in the provider/engine layer (T13),
    not here — this type is pure data.

    Default-value semantics when a field is ``None`` at construction:
    ``temperature`` falls back to provider config (0.7), ``top_p`` to 0.95,
    ``reasoning_effort=NONE`` means the parameter is not sent at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    messages: list[ChatMessage]
    # OpenAI-shape JSON-schema tool definitions; the payload is a
    # vendor-defined open schema, so it stays dict-valued.
    tools: tuple[dict[str, Any], ...] = ()
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    stop: tuple[str, ...] | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
    prompt_cache_key: str | None = None
    # rule 14 exemption: genuinely open extension payload — providerOptions
    # style keys merged into the wire body top level, user wins.
    extra_body: dict[str, Any] | None = None
