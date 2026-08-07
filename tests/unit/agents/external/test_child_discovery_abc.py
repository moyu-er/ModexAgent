"""Unit tests for the ``ChildSessionDiscoverySink`` ABC.

Covers:
  - ABC cannot be instantiated directly.
  - A concrete subclass implementing both abstract methods instantiates.
  - ``on_child_discovered`` is a coroutine function (async).
  - ``resolve_child_modex_session_id`` is a regular (sync) function.
  - Concrete ``on_child_discovered`` returns the expected string.
  - Concrete ``resolve_child_modex_session_id`` returns the expected string.

The concrete subclass used here is a test-only stub; production
implementations live in the harness and persist mappings through
``ExternalSessionMapStore``. The ABC stays provider-neutral, so no
provider-specific imports appear in this file.
"""

from __future__ import annotations

import inspect

import pytest

from modex_agent.agents.external.child_discovery import (
    ChildSessionDiscoverySink,
)


class _StubSink(ChildSessionDiscoverySink):
    """Minimal in-memory implementation for ABC contract tests."""

    def __init__(self) -> None:
        self._calls: list[tuple[str, str, str | None]] = []

    async def on_child_discovered(
        self,
        provider_child_session_id: str,
        parent_modex_session_id: str,
        provider_agent_type: str | None = None,
    ) -> str:
        self._calls.append(
            (provider_child_session_id, parent_modex_session_id, provider_agent_type)
        )
        return f"modex:{provider_child_session_id}"

    def resolve_child_modex_session_id(self, provider_child_session_id: str) -> str:
        return f"modex:{provider_child_session_id}"


class TestChildSessionDiscoverySinkABC:
    def test_abc_cannot_be_instantiated_directly(self) -> None:
        with pytest.raises(TypeError):
            ChildSessionDiscoverySink()  # type: ignore[abstract]

    def test_concrete_subclass_instantiates(self) -> None:
        sink = _StubSink()
        assert isinstance(sink, ChildSessionDiscoverySink)

    def test_on_child_discovered_is_coroutine_function(self) -> None:
        assert inspect.iscoroutinefunction(ChildSessionDiscoverySink.on_child_discovered)

    def test_resolve_child_modex_session_id_is_sync_function(self) -> None:
        assert not inspect.iscoroutinefunction(
            ChildSessionDiscoverySink.resolve_child_modex_session_id
        )
        assert inspect.isfunction(ChildSessionDiscoverySink.resolve_child_modex_session_id)

    async def test_on_child_discovered_returns_expected_string(self) -> None:
        sink = _StubSink()
        result = await sink.on_child_discovered(
            "provider-child-1",
            "parent-modex-1",
            provider_agent_type="coder",
        )
        assert result == "modex:provider-child-1"
        assert sink._calls == [("provider-child-1", "parent-modex-1", "coder")]

    def test_resolve_child_modex_session_id_returns_expected_string(self) -> None:
        sink = _StubSink()
        assert sink.resolve_child_modex_session_id("provider-child-2") == ("modex:provider-child-2")
