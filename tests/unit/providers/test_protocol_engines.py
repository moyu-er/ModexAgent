"""Protocol-engine registry — the ``interface_format`` resolution face.

Covers the process-level registry the factory resolves through: the
three bundled wire protocols seeded at first access (each resolving to
today's engine class through ``create_llm_provider``), a dummy engine
registered under a new name resolving end-to-end, unknown names failing
loudly with the registered options listed, and the loader's registration
plumbing (attached registry → lands; absent → dropped with a WARNING).
The generic BackendRegistry semantics themselves are W2's tests
(tests/unit/plugins/test_backends.py).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from modex_agent.core.llm_request import LLMRequest
from modex_agent.core.stream_events import LLMStreamEvent
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.providers.factory import create_llm_provider
from modex_agent.providers.http.formats.anthropic import AnthropicProtocol
from modex_agent.providers.http.formats.openai_compat import OpenAICompatProtocol
from modex_agent.providers.http.formats.openai_responses import OpenAIResponsesProtocol
from modex_agent.providers.http.protocol import LLMProtocol, ProtocolConfig
from modex_agent.providers.http.provider import HTTPStreamProvider
from modex_agent.providers.http.sse import SseFrame
from modex_agent.providers.llm_config import LLMConfig
from modex_agent.providers.protocol_engines import (
    ANTHROPIC_FORMAT,
    OPENAI_COMPATIBLE_FORMAT,
    OPENAI_RESPONSE_FORMAT,
    protocol_engine_registry,
)
from modex_agent.scope.component_registry import ComponentRegistry

_BASE = "https://api.example.com/v1"


class _DummyEngine(LLMProtocol):
    """Minimal third-party engine double — a new wire protocol's shape."""

    def build_body(self, request: LLMRequest, cfg: ProtocolConfig) -> dict[str, object]:
        return {}

    def url(self, base_url: str) -> str:
        return f"{base_url}/dummy"

    def auth_headers(self, api_key: str | None) -> dict[str, str]:
        return {}

    async def events(self, frames: AsyncIterator[SseFrame]) -> AsyncIterator[LLMStreamEvent]:
        async for _frame in frames:
            pass

    @property
    def api_key_env(self) -> str:
        return "DUMMY_API_KEY"


class _EmptyConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class _EnginePlugin(Plugin):
    """Registers one named protocol engine (the third-party shape)."""

    config_model = _EmptyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_protocol_engine("plugin-wire", _DummyEngine)


# ── Bundled defaults ────────────────────────────────────────────────────────


def test_registry_seeds_three_bundled_formats_at_first_access() -> None:
    # Superset, not exact equality: third-party registrations landing in the
    # process-level registry are the design, not contamination.
    assert set(protocol_engine_registry().names()) >= {
        OPENAI_COMPATIBLE_FORMAT,
        OPENAI_RESPONSE_FORMAT,
        ANTHROPIC_FORMAT,
    }


@pytest.mark.parametrize(
    ("name", "engine_type"),
    [
        (OPENAI_COMPATIBLE_FORMAT, OpenAICompatProtocol),
        (OPENAI_RESPONSE_FORMAT, OpenAIResponsesProtocol),
        (ANTHROPIC_FORMAT, AnthropicProtocol),
    ],
)
def test_each_default_name_resolves_todays_engine_through_the_factory(
    name: str, engine_type: type
) -> None:
    provider = create_llm_provider(
        LLMConfig(model="m", api_key="sk-test", base_url=_BASE, interface_format=name)
    )
    assert isinstance(provider, HTTPStreamProvider)
    assert type(provider._protocol) is engine_type


# ── Third-party registration + failure mode ─────────────────────────────────


def test_dummy_engine_under_new_name_resolves_end_to_end() -> None:
    protocol_engine_registry().register("dummy-wire", _DummyEngine)

    provider = create_llm_provider(
        LLMConfig(
            model="m",
            api_key="sk-test",
            base_url=_BASE,
            interface_format="dummy-wire",
        )
    )

    assert isinstance(provider, HTTPStreamProvider)
    assert type(provider._protocol) is _DummyEngine
    assert provider._url == f"{_BASE}/dummy"


def test_unknown_name_is_loud_and_lists_options() -> None:
    with pytest.raises(ValueError) as excinfo:
        create_llm_provider(
            LLMConfig(
                model="m",
                api_key="sk-test",
                base_url=_BASE,
                interface_format="gemini",
            )
        )
    message = str(excinfo.value)
    assert "gemini" in message
    assert OPENAI_COMPATIBLE_FORMAT in message
    assert OPENAI_RESPONSE_FORMAT in message
    assert ANTHROPIC_FORMAT in message


# ── Loader registration plumbing ────────────────────────────────────────────


async def test_plugin_engine_registration_lands_in_attached_registry(
    tmp_path: Path,
) -> None:
    registry = protocol_engine_registry()

    await ComponentRegistryLoader.load(
        ComponentRegistry(),
        PluginDiscoveryConfig(
            bundled_factories=(_EnginePlugin(),),
            project_plugin_paths=(),
            user_plugin_path=tmp_path / "absent",
            protocol_engines=registry,
        ),
    )

    assert "plugin-wire" in registry.names()
    assert isinstance(registry.resolve("plugin-wire"), _DummyEngine)


async def test_plugin_engine_registration_dropped_with_warning_without_registry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="modex_agent.plugins.loader"):
        await ComponentRegistryLoader.load(
            ComponentRegistry(),
            PluginDiscoveryConfig(
                bundled_factories=(_EnginePlugin(),),
                project_plugin_paths=(),
                user_plugin_path=tmp_path / "absent",
            ),
        )

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("plugin-wire" in m and "BackendRegistry" in m for m in warnings), warnings
