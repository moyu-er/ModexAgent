"""LLMProtocol ABC plus the WireRequest / ProtocolConfig envelopes (ADR-0046).

This module is the contract between the one concrete provider
(``HTTPStreamProvider``, T18) and the three protocol engines (openai_compat
/ openai_responses / anthropic, T14-T16). The provider owns transport; the
engine owns translation. Everything below the factory talks only to the
:class:`LLMProtocol` ABC.

Two disciplines bind every engine implementer:

1. **Engines are stateless across requests.** ``build_body`` is a pure
   function of ``(LLMRequest, ProtocolConfig)``; ``events(frames)`` returns
   a fresh async generator per request whose closure holds ALL per-request
   translation state (tool stream state, usage buffers, signature caches).
   An engine instance carries no mutable attributes.
2. **Protocol engine files are structurally self-similar**, in this fixed
   section order: common inputs, wire request schema, parse state, body
   building, event parsing, exports. A future engine (e.g. Gemini) is a new
   file following that shape, never an edit to the provider.

Replay state leaves the engine only through ``Finish.replay``
(:class:`~modex_agent.core.stream_events.ReplayFields`); engines expose no
per-response instance methods or attributes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.constants import ReasoningEffort
from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.llm_struct import LLMErrorInfo
from modex_agent.core.stream_events import LLMStreamEvent
from modex_agent.providers.http.errors import classify_http_error as classify_default_http_error
from modex_agent.providers.http.sse import SseFrame


class WireRequest(BaseModel):
    """One translated HTTP request: where to send, engine auth, wire body.

    The URL is the factory-resolved request URL (``endpoint_url`` verbatim
    when set, else the engine's ``url()`` join) — the provider sends it
    unchanged. The provider merges user-configured ``extra_headers`` into
    ``headers`` upstream — this envelope never carries the merged view.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    # Engine auth headers ONLY (Authorization / x-api-key +
    # anthropic-version ...) — user extra_headers are merged upstream by
    # the provider, never here.
    headers: dict[str, str]
    # rule 14 exemption: the wire JSON payload is a vendor-defined open
    # schema the engine builds, so it stays dict-valued.
    body: dict[str, Any]


class ProtocolConfig(BaseModel):
    """Provider-level configuration for one protocol engine.

    Sampling parameters never live here — the
    :class:`~modex_agent.core.llm_request.LLMRequest` envelope is their only
    carrier. This config holds the API key and per-format behavior knobs;
    the request URL is resolved by the factory and carried on the provider,
    not here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: str | None = None
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
    # User-configured passthrough headers; merged with engine auth headers
    # by the provider (user values win on collision).
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # Consumed by the openai_responses engine only: store=false switches
    # reasoning replay from item_reference to full encrypted_content replay.
    store: bool = True
    # Consumed by the openai_compat engine only: strip <think> tags from
    # delta.content while the stream carries no native reasoning.
    parse_think_tags: bool = True
    # rule 14 exemption: providerOptions-style open extension payload —
    # merged into the wire body top level, user wins.
    extra_body: dict[str, Any] | None = None


class LLMProtocol(ABC):
    """Translation contract every protocol engine implements.

    The engine lowers a canonical LLMRequest onto its wire format, joins a
    base URL, mints auth headers, and translates an SSE frame stream into
    LLMStreamEvents. The two module-level disciplines (statelessness,
    self-similar file sections) are part of this contract.
    """

    @abstractmethod
    def build_body(self, request: LLMRequest, cfg: ProtocolConfig) -> dict[str, Any]:
        """Explicitly construct the wire request body from the canonical model."""
        pass

    @abstractmethod
    def url(self, base_url: str) -> str:
        """Join ``base_url`` into this protocol's request endpoint URL."""
        pass

    @abstractmethod
    def auth_headers(self, api_key: str | None) -> dict[str, str]:
        """Engine auth headers for ``api_key``; empty dict when the key is None."""
        pass

    @abstractmethod
    def events(self, frames: AsyncIterator[SseFrame]) -> AsyncIterator[LLMStreamEvent]:
        """Translate an SSE frame stream into LLMStreamEvents.

        Returns a fresh async generator per request; ALL per-request
        translation state lives inside that generator's closure
        (discipline 1 — the engine instance stays stateless).
        """
        pass

    @property
    @abstractmethod
    def api_key_env(self) -> str:
        """Environment-variable fallback name for the API key.

        The provider reads this environment variable when the configured
        ``api_key`` is empty, preserving the legacy SDK-era semantics
        (``OPENAI_API_KEY`` for both OpenAI formats, ``ANTHROPIC_API_KEY``
        for anthropic).
        """
        pass

    def classify_http_error(
        self,
        status: int,
        body: bytes,
        provider: str,
        headers: Mapping[str, str] | None = None,
    ) -> LLMErrorInfo:
        """Classify a non-2xx HTTP response — the shared default classifier.

        Delegates to
        :func:`modex_agent.providers.http.errors.classify_http_error`. An
        engine overrides this only when its wire format carries
        provider-specific signal the status+body scan cannot see.
        """
        return classify_default_http_error(
            status=status, body=body, provider=provider, headers=headers
        )
