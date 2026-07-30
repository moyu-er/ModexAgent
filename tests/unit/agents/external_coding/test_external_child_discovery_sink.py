"""Unit tests for :class:`ExternalChildSessionDiscoverySink`.

Covers:
  - ``on_child_discovered`` returns the deterministic modex session_id,
    awaits ``SessionRegistry.register`` with the correct ``SessionInfo``
    (parent + metadata), and awaits ``ExternalSessionMapStore.commit``
    with ``parent_modex_session_id``.
  - ``resolve_child_modex_session_id`` returns the same deterministic id
    with NO side effects (no register/commit calls).
  - Determinism: resolving the same provider child id twice yields the
    same modex session_id.
  - ``on_child_discovered`` without ``provider_agent_type`` produces an
    empty metadata dict (no ``provider_agent`` key).

Uses a real :class:`SessionIdFactory` (deterministic via
:func:`encode_snowflake`) and ``AsyncMock``-backed doubles for the
registry and map store — the sink's contract is what matters, not the
store internals.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modex_agent.agents.external_coding.child_discovery import (
    ExternalChildSessionDiscoverySink,
)
from modex_agent.agents.external_coding.paths import ProviderKind
from modex_agent.agents.external_coding.session_store import ExternalSessionMapStore
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import SessionRegistry


def _make_sink() -> tuple[ExternalChildSessionDiscoverySink, MagicMock, MagicMock]:
    factory = SessionIdFactory()
    registry = MagicMock(spec=SessionRegistry)
    registry.register = AsyncMock(return_value=None)
    store = MagicMock(spec=ExternalSessionMapStore)
    store.commit = AsyncMock(return_value=None)
    sink = ExternalChildSessionDiscoverySink(
        session_factory=factory,
        session_registry=registry,
        session_map_store=store,
        provider_kind=ProviderKind.PI,
    )
    return sink, registry, store


class TestExternalChildSessionDiscoverySink:
    async def test_on_child_discovered_registers_and_commits(self) -> None:
        sink, registry, store = _make_sink()

        result = await sink.on_child_discovered("child1", "parent1", "general")

        expected_id = sink.resolve_child_modex_session_id("child1")
        assert result == expected_id

        registry.register.assert_awaited_once()
        registered = registry.register.call_args.args[0]
        assert registered.session_id == expected_id
        assert registered.agent_name == "external-subagent"
        assert registered.parent_session_id == "parent1"
        assert registered.metadata == {"provider_agent": "general"}

        store.commit.assert_awaited_once_with(
            expected_id,
            "child1",
            ProviderKind.PI,
        )

    async def test_resolve_has_no_side_effects(self) -> None:
        sink, registry, store = _make_sink()

        result = sink.resolve_child_modex_session_id("child1")

        assert result == sink.resolve_child_modex_session_id("child1")
        registry.register.assert_not_called()
        store.commit.assert_not_called()

    def test_resolve_is_deterministic(self) -> None:
        sink, _, _ = _make_sink()
        first = sink.resolve_child_modex_session_id("child1")
        second = sink.resolve_child_modex_session_id("child1")
        assert first == second

    async def test_on_child_discovered_without_provider_agent_type_empty_metadata(
        self,
    ) -> None:
        sink, registry, _ = _make_sink()

        await sink.on_child_discovered("child2", "parent2")

        registered = registry.register.call_args.args[0]
        assert registered.metadata == {}
        assert "provider_agent" not in registered.metadata


def test_sink_is_concrete_subclass_of_abc() -> None:
    from modex_agent.agents.external_coding.child_discovery import (
        ChildSessionDiscoverySink,
    )

    sink, _, _ = _make_sink()
    assert isinstance(sink, ChildSessionDiscoverySink)
