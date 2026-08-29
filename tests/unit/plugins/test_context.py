"""TDD tests for the assembly pipeline context types.

Written FIRST to drive the implementation of
``src/modex_agent/plugins/assembly/context.py`` (task 2 of the
scope-converge implementation plan). Asserts the exact field contract for
``PoolRuntimeDeps`` and ``AssemblyContext``:
field names, required vs optional, frozen immutability, and missing-argument
TypeError behavior.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock

import pytest

from modex_agent.plugins.assembly import context as assembly_context
from modex_agent.plugins.assembly.context import (
    AssemblyContext,
    PoolRuntimeDeps,
)

# ---- PoolRuntimeDeps ----


class TestPoolRuntimeDeps:
    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(PoolRuntimeDeps)

    def test_field_names_exact(self) -> None:
        expected = {
            "session_tree_manager",
            "control_channel",
            "notification_service",
            "binding_store",
            "pool_assembly_ctx",
            "root_provider",
            "mcp_registry",
            "emitter_factory",
            "terminal_manager",
            "process_registry",
            "interceptor_chain",
            "command_processor",
            "persistent_bash",
            "capability_supply",
        }
        actual = {f.name for f in dataclasses.fields(PoolRuntimeDeps)}
        assert actual == expected

    def test_session_tree_manager_optional(self) -> None:
        """session_tree_manager defaults to None (built after pipeline runs)."""
        instance = PoolRuntimeDeps()
        assert instance.session_tree_manager is None

    def test_optional_fields_default_none(self) -> None:
        instance = PoolRuntimeDeps()
        assert instance.control_channel is None
        assert instance.notification_service is None
        assert instance.binding_store is None
        assert instance.pool_assembly_ctx is None
        assert instance.session_tree_manager is None
        assert instance.root_provider is None
        assert instance.mcp_registry is None
        assert instance.emitter_factory is None
        assert instance.terminal_manager is None

    def test_frozen_immutability(self) -> None:
        instance = PoolRuntimeDeps(session_tree_manager=MagicMock())
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.root_provider = MagicMock()  # type: ignore[misc]


# ---- AssemblyContext ----


class TestAssemblyContext:
    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(AssemblyContext)

    def test_field_names_exact(self) -> None:
        expected = {
            "registry",
            "workspace_registry",
            "workspace_ctx",
            "workspace_resources",
            "workspace_spec",
            "pool_runtime",
            "infra",
            "llm_provider",
        }
        actual = {f.name for f in dataclasses.fields(AssemblyContext)}
        assert actual == expected

    def test_missing_required_fields_raises_type_error(self) -> None:
        """Adversarial probe: omitting required fields raises TypeError."""
        with pytest.raises(TypeError):
            AssemblyContext()  # type: ignore[call-arg]

    def test_registry_is_required_no_default(self) -> None:
        field = {f.name: f for f in dataclasses.fields(AssemblyContext)}["registry"]
        assert field.default is dataclasses.MISSING

    def test_workspace_registry_is_optional(self) -> None:
        field = {f.name: f for f in dataclasses.fields(AssemblyContext)}["workspace_registry"]
        assert field.default is None

    def test_workspace_ctx_is_required_no_default(self) -> None:
        field = {f.name: f for f in dataclasses.fields(AssemblyContext)}["workspace_ctx"]
        assert field.default is dataclasses.MISSING

    def test_optional_fields_default_none(self) -> None:
        sentinel = MagicMock()
        instance = AssemblyContext(
            registry=sentinel,
            workspace_registry=sentinel,
            workspace_ctx=sentinel,
        )
        assert instance.workspace_resources is None
        assert instance.pool_runtime is None

    def test_resolution_context_places_resolution_dependencies(self) -> None:
        registry = MagicMock()
        workspace_ctx = MagicMock()
        pool_runtime = PoolRuntimeDeps(session_tree_manager=MagicMock())

        instance = assembly_context.resolution_context(
            registry,
            workspace_ctx,
            pool_runtime,
        )

        assert instance.registry is registry
        assert instance.workspace_ctx is workspace_ctx
        assert instance.pool_runtime is pool_runtime
        assert instance.workspace_registry is None

    def test_frozen_immutability(self) -> None:
        sentinel = MagicMock()
        instance = AssemblyContext(
            registry=sentinel,
            workspace_registry=sentinel,
            workspace_ctx=sentinel,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            instance.pool_runtime = PoolRuntimeDeps(session_tree_manager=sentinel)  # type: ignore[misc]
