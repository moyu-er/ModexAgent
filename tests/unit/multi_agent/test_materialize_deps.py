from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.core.constants import ReasoningEffort
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager


def test_constructs_with_required_fields() -> None:
    deps = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        safety=RuntimeSafetyPolicy(),
        llm_model="gpt-4o",
        llm_temperature=0.7,
        llm_max_output_tokens=None,
        project_dir=Path("."),
        notification_service=None,
        inbox_consumer=None,
        agent_bus=None,
        output_adapter_factory=None,
        root_provider=None,
        session_registry=None,
        on_subagent_created=None,
    )
    assert deps.llm_model == "gpt-4o"
    assert deps.llm_temperature == 0.7


def test_is_regular_runtime_object() -> None:
    deps = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
    )
    deps.llm_model = "x"

    assert not dataclasses.is_dataclass(deps)
    assert deps.llm_model == "x"


def test_optional_fields_default_none() -> None:
    deps = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
    )
    assert deps.project_dir is None
    assert deps.on_subagent_created is None
    assert deps.default_llm_provider == "default"


def test_context_fork_builder_defaults_none_and_settable() -> None:
    deps_default = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
    )
    assert deps_default.context_fork_builder is None

    fork_builder = MagicMock()
    deps_set = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        context_fork_builder=fork_builder,
    )
    assert deps_set.context_fork_builder is fork_builder


def test_scope_path_defaults_none_and_settable() -> None:
    deps_default = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
    )
    assert deps_default.scope_path is None

    resolver = MagicMock()
    deps_set = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        scope_path=resolver,
    )
    assert deps_set.scope_path is resolver


def test_mcp_registry_defaults_none_and_settable() -> None:
    deps_default = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
    )
    assert deps_default.mcp_registry is None

    registry = MagicMock()
    deps_set = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        mcp_registry=registry,
    )
    assert deps_set.mcp_registry is registry


def test_llm_reasoning_effort_defaults_to_none_and_settable() -> None:
    deps_default = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
    )
    assert deps_default.llm_reasoning_effort == ReasoningEffort.NONE

    deps_set = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        llm_reasoning_effort=ReasoningEffort.HIGH,
    )
    assert deps_set.llm_reasoning_effort == ReasoningEffort.HIGH


def test_execution_strategy_defaults_none_and_settable() -> None:
    deps_default = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
    )
    assert deps_default.execution_strategy is None

    strategy = MagicMock()
    deps_set = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        execution_strategy=strategy,  # type: ignore[arg-type]
    )
    assert deps_set.execution_strategy is strategy


def test_control_origin_defaults_empty_and_settable() -> None:
    deps_default = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
    )
    assert deps_default.control_origin == ""

    deps_set = AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=SessionIdFactory(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
        control_origin="http://127.0.0.1:21800",
    )
    assert deps_set.control_origin == "http://127.0.0.1:21800"
