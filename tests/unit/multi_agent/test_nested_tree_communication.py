"""Nested-tree communication assembly (ticket 12, SPEC §3.2/§5.2).

The mid-level agent of a three-level declared tree gets a per-agent
``CommunicationTargetStore`` holding exactly its DIRECT children (never
grandchildren); the derived ``task`` TOOL-slot factory resolves against
that store. A leaf gets no store at all — its assembly carries no
``task`` entry, and the factory fails loudly if one is requested.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.plugins.assembly.context import (
    AgentContext,
    CommunicationFacilities,
    PoolRuntimeDeps,
)
from modex_agent.plugins.defaults.communication import TaskToolFactory
from modex_agent.scope.spec import AgentSpec
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

_MID = AgentSpec(name="mid", description="middle agent")
_LEAF = AgentSpec(name="leaf", description="leaf agent")
_GRANDCHILD = AgentSpec(name="subsub", description="grandchild agent")


def _deps() -> AgentMaterializeDeps:
    return AgentMaterializeDeps(
        agent_factory=MagicMock(),
        pool=MagicMock(),
        session_factory=MagicMock(),
        broker=MagicMock(),
        tree=MagicMock(spec=SessionTreeManager),
    )


def _chain_ctx(
    facilities: CommunicationFacilities, agent_name: str
) -> AgentContext:
    return AgentContext(
        registry=MagicMock(),
        workspace_ctx=WorkspaceContext(
            target=MagicMock(), paths=WorkspacePaths(root=MagicMock()), is_home=False
        ),
        pool_runtime=PoolRuntimeDeps(communication=facilities),
        agent_name=agent_name,
    )


class TestMidLevelFacilities:
    def test_mid_template_store_lists_direct_children_only(self) -> None:
        # Given — a mid-level template whose declared children are leaf
        # and one sibling leaf (the grandchild belongs to `leaf`, not `mid`)
        template = AgentTemplate(
            spec=_MID,
            children=(_LEAF, AgentSpec(name="leaf2", description="")),
        )

        # When
        facilities = template._comm_facilities(_deps(), "mid")

        # Then — exactly the direct children; the grandchild never joins
        assert facilities.target_store is not None
        assert [t.name for t in facilities.target_store.list_subagents()] == [
            "leaf",
            "leaf2",
        ]
        assert all(
            t.kind == AgentCommKind.SUBAGENT
            for t in facilities.target_store.list_subagents()
        )

    def test_leaf_template_has_no_target_store(self) -> None:
        # Given — a leaf (no declared children)
        template = AgentTemplate(spec=_LEAF)

        # When
        facilities = template._comm_facilities(_deps(), "leaf")

        # Then — no store: the derived send_to_agent entry builds its own
        # subagent-mode store; there is no task entry to resolve
        assert facilities.target_store is None

    async def test_task_tool_factory_resolves_mid_store(self) -> None:
        # Given — the mid's facilities + the factory the compiled `task`
        # entry resolves through
        template = AgentTemplate(spec=_MID, children=(_LEAF,))
        facilities = template._comm_facilities(_deps(), "mid")
        assert facilities.target_store is not None

        # When
        tool = await TaskToolFactory().create(
            _empty_config(), _chain_ctx(facilities, "mid")
        )

        # Then — the tool's target list IS the direct-children store
        assert tool.name == "task"
        assert [t.name for t in tool.list_targets()] == ["leaf"]

    async def test_task_tool_factory_fails_loudly_without_store(self) -> None:
        # Given — facilities without a store (a leaf's) but a derived
        # `task` entry requesting resolution
        service = MagicMock()
        facilities = CommunicationFacilities(service=service, target_store=None)

        # When / Then — loud, not a silently-empty tool
        with pytest.raises(ValueError, match="per-agent target store"):
            await TaskToolFactory().create(
                _empty_config(), _chain_ctx(facilities, "leaf")
            )

    async def test_task_tool_factory_fails_loudly_without_facilities(
        self,
    ) -> None:
        # Given — no pool-layer communication facilities at all
        ctx = AgentContext(
            registry=MagicMock(),
            workspace_ctx=WorkspaceContext(
                target=MagicMock(),
                paths=WorkspacePaths(root=MagicMock()),
                is_home=False,
            ),
            agent_name="mid",
        )

        # When / Then
        with pytest.raises(ValueError, match="communication facilities"):
            await TaskToolFactory().create(_empty_config(), ctx)


def _empty_config() -> Any:
    from modex_agent.plugins.defaults.communication import _ToolConfig

    return _ToolConfig()
