"""External-transport registry — the ``provider_kind`` resolution face.

Covers the process-level registry the external strategy resolves
through: the bundled ``opencode`` transport seeded at first access, a
dummy transport under a new provider kind resolving through the
strategy's transport construction seam, unknown kinds failing loudly
with the registered kinds listed (the same assembly point the former
single-kind rejection raised at), and the loader's registration
plumbing. The generic BackendRegistry semantics themselves are W2's
tests (tests/unit/plugins/test_backends.py).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import BaseModel

from modex_agent.agents.external.transports.abc import (
    ChildTurnEventCallbackFactory,
    ExternalTransport,
    TurnEventCallback,
)
from modex_agent.agents.external.transports.cli_transport import OpenCodeTransport
from modex_agent.agents.external.types import BackendResult, BackendStatus, ExecOptions
from modex_agent.core.agent import ProviderKind
from modex_agent.plugins.assembly.strategies.external import ExternalExecutionStrategy
from modex_agent.plugins.external_transports import (
    OPENCODE_TRANSPORT_KIND,
    external_transport_registry,
)
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.scope.component_registry import ComponentRegistry


class _DummyTransport(ExternalTransport):
    """Minimal third-party transport double — a new coding agent's shape."""

    async def execute(
        self,
        opts: ExecOptions,
        env: Mapping[str, str],
        on_event: TurnEventCallback,
        on_child_event: ChildTurnEventCallbackFactory | None = None,
    ) -> BackendResult:
        return BackendResult(status=BackendStatus.COMPLETED, session_id="acme-1")


class _EmptyConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class _TransportPlugin(Plugin):
    """Registers one named external transport (the third-party shape)."""

    config_model = _EmptyConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_external_transport("plugin-agent", _DummyTransport)


# ── Bundled default ─────────────────────────────────────────────────────────


def test_registry_seeds_opencode_at_first_access() -> None:
    # Superset, not exact equality: third-party registrations landing in the
    # process-level registry are the design, not contamination.
    assert OPENCODE_TRANSPORT_KIND in external_transport_registry().names()


def test_opencode_resolves_to_opencode_transport_through_the_strategy() -> None:
    transport = ExternalExecutionStrategy()._build_external_backend(
        ProviderKind.OPENCODE.value
    )

    assert isinstance(transport, OpenCodeTransport)


# ── Third-party registration + failure mode ─────────────────────────────────


def test_dummy_transport_under_new_kind_resolves_through_the_strategy() -> None:
    external_transport_registry().register("acme", _DummyTransport)

    transport = ExternalExecutionStrategy()._build_external_backend("acme")

    assert type(transport) is _DummyTransport


def test_unknown_kind_is_loud_and_lists_options() -> None:
    with pytest.raises(ValueError) as excinfo:
        ExternalExecutionStrategy()._build_external_backend("cursor")

    message = str(excinfo.value)
    assert "cursor" in message
    assert OPENCODE_TRANSPORT_KIND in message


# ── Loader registration plumbing ────────────────────────────────────────────


async def test_plugin_transport_registration_lands_in_attached_registry(
    tmp_path: Path,
) -> None:
    registry = external_transport_registry()

    await ComponentRegistryLoader.load(
        ComponentRegistry(),
        PluginDiscoveryConfig(
            bundled_factories=(_TransportPlugin(),),
            project_plugin_paths=(),
            user_plugin_path=tmp_path / "absent",
            external_transports=registry,
        ),
    )

    assert "plugin-agent" in registry.names()
    assert isinstance(registry.resolve("plugin-agent"), _DummyTransport)


async def test_plugin_transport_registration_dropped_with_warning_without_registry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="modex_agent.plugins.loader"):
        await ComponentRegistryLoader.load(
            ComponentRegistry(),
            PluginDiscoveryConfig(
                bundled_factories=(_TransportPlugin(),),
                project_plugin_paths=(),
                user_plugin_path=tmp_path / "absent",
            ),
        )

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("plugin-agent" in m and "BackendRegistry" in m for m in warnings), warnings
