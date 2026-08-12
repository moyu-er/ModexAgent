"""Tests for CommunicationTarget and CommunicationTargetStore."""

from __future__ import annotations

import pytest

from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
)


def _normal(name: str, desc: str = "") -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=AgentCommKind.NORMAL, description=desc)


def _subagent(name: str, desc: str = "") -> CommunicationTarget:
    return CommunicationTarget(name=name, kind=AgentCommKind.SUBAGENT, description=desc)


class TestCommunicationTarget:
    def test_frozen(self) -> None:
        t = CommunicationTarget(name="a", kind=AgentCommKind.NORMAL)
        with pytest.raises(AttributeError):
            t.name = "b"  # type: ignore[misc]

    def test_defaults(self) -> None:
        t = CommunicationTarget(name="a", kind=AgentCommKind.NORMAL)
        assert t.description == ""


class TestStoreAdd:
    def test_add_target(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding", "Coding expert"))
        assert store.has("coding")

    def test_communication_target_store_rejects_duplicate_name(self) -> None:
        """ValueError MUST include both pool names so cross-pool peer wiring
        can never silently overwrite an existing target."""
        store = CommunicationTargetStore()
        existing = CommunicationTarget(
            name="peer-main",
            kind=AgentCommKind.NORMAL,
            description="local",
            pool_name="local-pool",
        )
        incoming = CommunicationTarget(
            name="peer-main",
            kind=AgentCommKind.NORMAL,
            description="remote",
            pool_name="peer-pool",
        )
        store.add(existing)
        with pytest.raises(ValueError) as excinfo:
            store.add(incoming)
        msg = str(excinfo.value)
        assert "peer-main" in msg
        assert "local-pool" in msg
        assert "peer-pool" in msg
        assert store.get("peer-main") is existing
        assert len(store.list()) == 1


class TestStoreGet:
    def test_communication_target_get_returns_target_or_none(self) -> None:
        store = CommunicationTargetStore()
        registered = CommunicationTarget(
            name="alpha",
            kind=AgentCommKind.NORMAL,
            description="first",
            pool_name="pool-a",
        )
        store.add(registered)
        assert store.get("alpha") is registered
        assert store.get("missing") is None

    def test_get_in_subagent_mode_resolves_parent(self) -> None:
        from modex_agent.core.agent import AgentContext, current_agent_context
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id="conv-1.worker",
                agent_name="worker",
                parent_session_id="conv-1.main",
            ),
        )
        store = CommunicationTargetStore(for_subagent=True)
        token = current_agent_context.set(ctx)
        try:
            resolved = store.get("main")
        finally:
            current_agent_context.reset(token)
        assert resolved is not None
        assert resolved.name == "main"
        assert resolved.kind == AgentCommKind.NORMAL
        assert store.get("other") is None


class TestStorePop:
    def test_pop_by_name(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.pop_by_name("coding")
        assert not store.has("coding")

    def test_pop_nonexistent_is_noop(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.pop_by_name("nonexistent")
        assert len(store.list()) == 1


class TestStoreList:
    def test_returns_copy(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        copy = store.list()
        copy.clear()
        assert len(store.list()) == 1


class TestStoreDescription:
    def test_normal_description_contains_targets(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding", "Coding expert"))
        store.add(_subagent("scout", "Fast recon"))
        desc = store.description
        assert "coding" in desc
        assert "Coding expert" in desc
        assert "scout" in desc
        assert "Fast recon" in desc
        assert "peer" in desc.lower()
        assert "subagent" in desc

    def test_normal_description_shows_kind_labels(self) -> None:
        """Normal description MUST separate targets by kind into labeled sections."""
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.add(_subagent("scout"))
        desc = store.description
        assert "Subagents" in desc
        assert "Peer targets" in desc
        assert "scout" in desc
        assert "coding" in desc

    def test_normal_description_empty_targets(self) -> None:
        store = CommunicationTargetStore()
        desc = store.description
        assert "No targets currently available" in desc

    def test_description_cached(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        first = store.description
        second = store.description
        assert first is second

    def test_description_refreshed_after_add(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        first = store.description
        store.add(_subagent("scout", "Recon"))
        second = store.description
        assert first is not second
        assert "scout" in second

    def test_description_refreshed_after_pop(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        store.add(_subagent("scout"))
        first = store.description
        store.pop_by_name("scout")
        second = store.description
        assert "scout" not in second


class TestNormalDescriptionTwoKindContract:
    """Description MUST distinguish subagent vs normal targets so the LLM
    picks the right relationship: subagent = session to continue;
    normal = peer to message as an equal."""

    def test_empty_store_silent_on_kind(self) -> None:
        store = CommunicationTargetStore()
        desc = store.description.lower()
        assert "subagent" not in desc
        assert "normal" not in desc

    def test_with_targets_explains_two_kinds(self) -> None:
        """Description MUST distinguish subagent vs normal targets so the LLM
        picks the right relationship: subagent = session to continue;
        normal = peer to message as an equal."""
        store = CommunicationTargetStore()
        store.add(_subagent("scout"))
        store.add(_normal("coding"))
        desc = store.description.lower()
        assert "subagents" in desc
        assert "peer targets" in desc
        assert "continuing" in desc
        assert "as an equal" in desc

    def test_with_targets_emphasizes_only_channel(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        desc = store.description
        assert "ONLY channel" in desc

    def test_with_targets_warns_against_acknowledgement_spam(self) -> None:
        store = CommunicationTargetStore()
        store.add(_normal("coding"))
        desc = store.description.lower()
        assert "acknowledge" in desc

    def test_content_param_hint_differs_per_kind(self) -> None:
        """The description MUST mention both subagent continuation (invocation_id)
        and peer messaging, so the LLM understands the two relationship kinds."""
        store = CommunicationTargetStore()
        store.add(_subagent("scout"))
        store.add(_normal("coding"))
        desc = store.description.lower()
        assert "pass invocation_id" in desc
        assert "peer agent" in desc
        assert "`task` tool" in desc


class TestStoreSubagentDescription:
    def test_subagent_description_shows_parent_name_only(self) -> None:
        """Subagent description echoes the parent NAME resolved from the
        contextvar (not a static add()), and never leaks kind/description."""
        from modex_agent.core.agent import AgentContext, current_agent_context
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id="conv-1.worker",
                agent_name="worker",
                parent_session_id="conv-1.main",
            ),
        )
        store = CommunicationTargetStore(for_subagent=True)
        # Static adds must NOT influence the subagent description.
        store.add(_normal("main", "AI assistant"))

        token = current_agent_context.set(ctx)
        try:
            desc = store.description
        finally:
            current_agent_context.reset(token)

        assert "'main'" in desc  # parent name echoed from contextvar
        # Must NOT leak kind or the static description
        assert "normal" not in desc
        assert "AI assistant" not in desc

    def test_subagent_description_no_parent_available(self) -> None:
        store = CommunicationTargetStore(for_subagent=True)
        desc = store.description
        assert "No parent" in desc


class TestStoreSubagentDynamicParent:
    """In subagent mode the store resolves its single target (the parent) at
    call time from ``current_agent_context``, ignoring the static ``_targets``
    dict. The parent must never be baked at materialize time — the instance is
    reused across different invokers, so a static parent would go stale."""

    def test_list_targets_returns_parent_only(self) -> None:
        from modex_agent.core.agent import AgentContext, current_agent_context
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id="conv-1.worker",
                agent_name="worker",
                parent_session_id="conv-1.main",
            ),
        )
        store = CommunicationTargetStore(for_subagent=True)
        # Static adds must be ignored in subagent mode.
        store.add(_normal("sibling"))
        store.add(_normal("main"))

        token = current_agent_context.set(ctx)
        try:
            targets = store.list()
        finally:
            current_agent_context.reset(token)

        assert len(targets) == 1
        assert targets[0].name == "main"
        assert targets[0].kind == AgentCommKind.NORMAL

    def test_has_target_matches_parent_only(self) -> None:
        from modex_agent.core.agent import AgentContext, current_agent_context
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id="conv-1.worker",
                agent_name="worker",
                parent_session_id="conv-1.main",
            ),
        )
        store = CommunicationTargetStore(for_subagent=True)
        token = current_agent_context.set(ctx)
        try:
            assert store.has("main") is True
            assert store.has("other") is False
        finally:
            current_agent_context.reset(token)

    def test_no_context_returns_empty(self) -> None:
        store = CommunicationTargetStore(for_subagent=True)
        # No contextvar set → no resolvable parent.
        assert store.list() == []
        assert store.has("main") is False

    def test_no_parent_session_id_returns_empty(self) -> None:
        from modex_agent.core.agent import AgentContext, current_agent_context
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.memory.history import ListMessageHistory

        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id="conv-1.worker",
                agent_name="worker",
                parent_session_id=None,
            ),
        )
        store = CommunicationTargetStore(for_subagent=True)
        token = current_agent_context.set(ctx)
        try:
            assert store.list() == []
            assert store.has("main") is False
        finally:
            current_agent_context.reset(token)


class TestStoreGraphModeFiltersPeers:
    """In graph mode (``graph_instance_id`` set on AgentContext), the store
    filters out NORMAL (peer) targets — only SUBAGENT targets remain visible.

    Graph nodes communicate via ``deliver`` (graph edges), not peer messaging.
    The agent must not perceive peers: they disappear from ``list()``,
    ``has()``, ``get()``, and the description. Attempting to reach a peer
    returns ``None`` (the tool surfaces "not a valid target").
    """

    @staticmethod
    def _graph_ctx() -> AgentContext:
        from modex_agent.core.agent import AgentContext
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.memory.history import ListMessageHistory

        return AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("conv-1.researcher"),
            graph_instance_id=42,
        )

    @staticmethod
    def _non_graph_ctx() -> AgentContext:
        from modex_agent.core.agent import AgentContext
        from modex_agent.core.session_id import SessionInfo
        from modex_agent.core.tool_manager import InMemoryToolManager
        from modex_agent.memory.history import ListMessageHistory

        return AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("conv-1.researcher"),
        )

    def test_list_filters_out_normal_targets_in_graph_mode(self) -> None:
        from modex_agent.core.agent import current_agent_context

        store = CommunicationTargetStore()
        store.add(_normal("peer-main", "Planning partner"))
        store.add(_subagent("scout", "Fast recon"))

        token = current_agent_context.set(self._graph_ctx())
        try:
            names = [t.name for t in store.list()]
        finally:
            current_agent_context.reset(token)

        assert "scout" in names
        assert "peer-main" not in names

    def test_has_returns_false_for_peer_in_graph_mode(self) -> None:
        from modex_agent.core.agent import current_agent_context

        store = CommunicationTargetStore()
        store.add(_normal("peer-main"))
        store.add(_subagent("scout"))

        token = current_agent_context.set(self._graph_ctx())
        try:
            assert store.has("scout") is True
            assert store.has("peer-main") is False
        finally:
            current_agent_context.reset(token)

    def test_get_returns_none_for_peer_in_graph_mode(self) -> None:
        from modex_agent.core.agent import current_agent_context

        store = CommunicationTargetStore()
        store.add(_normal("peer-main", "Planning partner"))
        store.add(_subagent("scout", "Fast recon"))

        token = current_agent_context.set(self._graph_ctx())
        try:
            assert store.get("scout") is not None
            assert store.get("peer-main") is None
        finally:
            current_agent_context.reset(token)

    def test_description_omits_peer_section_in_graph_mode(self) -> None:
        from modex_agent.core.agent import current_agent_context

        store = CommunicationTargetStore()
        store.add(_normal("peer-main", "Planning partner"))
        store.add(_subagent("scout", "Fast recon"))

        token = current_agent_context.set(self._graph_ctx())
        try:
            desc = store.description
        finally:
            current_agent_context.reset(token)

        assert "scout" in desc
        assert "peer-main" not in desc
        assert "Peer targets" not in desc

    def test_peers_visible_again_when_graph_mode_cleared(self) -> None:
        from modex_agent.core.agent import current_agent_context

        store = CommunicationTargetStore()
        store.add(_normal("peer-main"))
        store.add(_subagent("scout"))

        token = current_agent_context.set(self._graph_ctx())
        store.description  # populate cache in graph mode
        current_agent_context.reset(token)

        # Non-graph context — peers visible again
        names = [t.name for t in store.list()]
        assert "peer-main" in names
        assert "scout" in names
