"""Integration tests for external subagent (child session) capture.

End-to-end verification of the child-session discovery + routing pipeline
using :class:`ScriptedStreamingAdapter` + :class:`OpenCodeSSEParser` to
simulate a complete opencode SSE event sequence (task tool call + child
discovery + child text/tool events + main session continuation).

Coverage:

1. **End-to-end** — main text → child text → child tool → main text.
   Verifies: main emitter receives main events; child ``SessionInfo``
   created and registered with correct ``parent_session_id``; child emitter
   receives child events; ``SessionStore.get_children`` returns the child;
   ``buildTree()`` pure function places the child under the parent.
2. **Cross-turn resume** — two turns, same ``provider_child_sid`` →
   ``SessionRegistry.register`` merge (no new record) + child emitter
   maps to the same deterministic modex session id.
3. **Concurrent children** — two different ``provider_child_sid`` events
   interleaved → two independent child emitters, no cross-talk.
4. **``buildTree()`` pure function** — replicates the WebUI
   ``sessionTree.ts`` ``buildTree()`` algorithm in Python and verifies
   parent→child tree construction.

All tests use scripted adapters — no real opencode process is spawned.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from modex_agent.agents.external_coding.agent import (
    ExternalCodingAgent,
    ScriptedStreamingAdapter,
    StreamingProviderBackend,
)
from modex_agent.agents.external_coding.backend_provider import (
    PoolScopedBackendProvider,
)
from modex_agent.agents.external_coding.child_discovery import (
    ExternalChildSessionDiscoverySink,
)
from modex_agent.agents.external_coding.events import ExternalCodingEvent
from modex_agent.agents.external_coding.paths import ExternalPaths, ProviderKind
from modex_agent.agents.external_coding.providers.opencode_sse_parser import (
    OpenCodeSSEParser,
)
from modex_agent.agents.external_coding.scripted_backend import (
    ScriptedProgramme,
    ScriptedProviderBackend,
    ScriptedStep,
)
from modex_agent.agents.external_coding.session_store import (
    LocalFileExternalSessionMapStore,
)
from modex_agent.agents.external_coding.types import (
    BackendStatus,
    ExternalEnvSpec,
)
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionIdFactory, SessionInfo
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.session_store import LocalFileSessionStore
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)
from modex_agent.core.types import MessageRole

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAIN_PROVIDER_SID = "prov-main-sid"
_CHILD_PROVIDER_SID_1 = "prov-child-1"
_CHILD_PROVIDER_SID_2 = "prov-child-2"
_PARENT_MODEX_SID = "pool1.agent1"

# ---------------------------------------------------------------------------
# Recording emitters
# ---------------------------------------------------------------------------


class _RecordingEmitter(ContentEmitter[ExternalCodingEvent]):  # type: ignore[type-arg]
    """Main-session emitter capturing turn events, deltas, completes, errors."""

    def __init__(self) -> None:
        super().__init__()
        self.deltas: list[str] = []
        self.turn_events: list[TurnEvent] = []
        self.completed: AgentResult | None = None
        self.errors: list[str] = []

    def wants_streaming(self) -> bool:
        return False

    async def emit_delta(self, delta: str) -> None:
        self.deltas.append(delta)

    async def emit_turn_event(self, event: TurnEvent) -> None:
        self.turn_events.append(event)

    async def emit_complete(self, result: AgentResult) -> None:
        self.completed = result

    async def emit_error(self, error: str) -> None:
        self.errors.append(error)


class _RecordingChildEmitter(ContentEmitter[ExternalCodingEvent]):  # type: ignore[type-arg]
    """Child-session emitter — records the modex_sid it was created for."""

    def __init__(self, modex_sid: str) -> None:
        super().__init__()
        self.modex_sid = modex_sid
        self.turn_events: list[TurnEvent] = []
        self.errors: list[str] = []
        self.deltas: list[str] = []

    async def emit_delta(self, delta: str) -> None:
        self.deltas.append(delta)

    async def emit_turn_event(self, event: TurnEvent) -> None:
        self.turn_events.append(event)

    async def emit_complete(self, result: AgentResult) -> None:
        pass

    async def emit_error(self, error: str) -> None:
        self.errors.append(error)


# ---------------------------------------------------------------------------
# SSE event JSON builders (opencode SSE wire format)
# ---------------------------------------------------------------------------


def _sse_text_delta(session_id: str, delta: str) -> str:
    """Build an opencode SSE ``message.part.delta`` event JSON line."""
    return json.dumps(
        {
            "type": "message.part.delta",
            "properties": {"sessionID": session_id, "delta": delta},
        }
    )


def _sse_tool_completed(
    session_id: str,
    call_id: str,
    tool_name: str,
    output: str,
    tool_input: str = '{"cmd":"ls"}',
) -> str:
    """Build an opencode SSE ``message.part.updated`` event for a completed tool.

    A single completed tool event produces both ``TOOL_USE`` and
    ``TOOL_RESULT`` emissions from the parser.
    """
    return json.dumps(
        {
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {
                    "type": "tool",
                    "tool": tool_name,
                    "callID": call_id,
                    "state": {
                        "status": "completed",
                        "input": tool_input,
                        "output": output,
                    },
                },
            },
        }
    )


# ---------------------------------------------------------------------------
# Helpers: spec, context, agent, adapter
# ---------------------------------------------------------------------------


def _make_spec(workdir: Path, session_id: str = _PARENT_MODEX_SID) -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=workdir,
        inbox_root=workdir / "inbox",
        workdir=workdir,
        session_id=session_id,
        agent_name="agent1",
        provider_session_id="prov-initial",
        agent_pool_map={"agent1": "pool1", "helper": "pool1"},
        targets=[("helper", "a helper agent")],
        modexctl_bin_dir=workdir / "bin",
    )


def _make_ctx(session_id: str = _PARENT_MODEX_SID) -> AgentContext:
    history = ListMessageHistory([ChatMessage(role=MessageRole.USER, content="run")])
    return AgentContext(
        system_prompt="",
        history=history,
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id),
        current_input="run",
    )


def _make_child_emitter_factory(
    emitters: dict[str, _RecordingChildEmitter],
) -> Callable[[str], ContentEmitter[ExternalCodingEvent]]:
    def factory(modex_sid: str) -> ContentEmitter[ExternalCodingEvent]:
        emitter = _RecordingChildEmitter(modex_sid)
        emitters[modex_sid] = emitter
        return emitter

    return factory


def _make_adapter(steps: list[ScriptedStep], parser: OpenCodeSSEParser) -> ScriptedStreamingAdapter:
    """Construct a ScriptedStreamingAdapter with a fresh parser."""
    programme = ScriptedProgramme(
        steps=tuple(steps),
        status=BackendStatus.COMPLETED,
        session_id=_MAIN_PROVIDER_SID,
    )
    scripted = ScriptedProviderBackend(programme)
    return ScriptedStreamingAdapter(scripted=scripted, parser=parser)


def _make_fresh_parser() -> OpenCodeSSEParser:
    """A fresh SSE parser with the main session pre-set."""
    parser = OpenCodeSSEParser()
    parser.set_main_session(_MAIN_PROVIDER_SID)
    return parser


def _build_agent(
    tmp_path: Path,
    adapter: StreamingProviderBackend,
    *,
    sink: ExternalChildSessionDiscoverySink,
    registry: InMemorySessionRegistry,
    session_store: LocalFileSessionStore,
    emitter_factory: Callable[[str], ContentEmitter[ExternalCodingEvent]],
) -> ExternalCodingAgent:
    return ExternalCodingAgent(
        backend_provider=PoolScopedBackendProvider(adapter),
        session_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
        parser=_make_fresh_parser(),  # unused by agent turn flow; required by ctor
        provider_kind=ProviderKind.OPENCODE,
        spec=_make_spec(tmp_path),
        base_env={"PATH": "/usr/bin"},
        child_discovery_sink=sink,
        session_registry=registry,
        child_emitter_factory=emitter_factory,
    )


def _build_real_sink(
    tmp_path: Path,
    registry: InMemorySessionRegistry,
) -> ExternalChildSessionDiscoverySink:
    """Construct the production sink with real collaborators."""
    return ExternalChildSessionDiscoverySink(
        session_factory=SessionIdFactory(),
        session_registry=registry,
        session_map_store=LocalFileExternalSessionMapStore(ExternalPaths(tmp_path)),
        provider_kind=ProviderKind.OPENCODE,
    )


# ---------------------------------------------------------------------------
# buildTree() pure function (replicates WebUI sessionTree.ts buildTree)
# ---------------------------------------------------------------------------


@dataclass
class _TreeNode:
    """Mirror of the WebUI ``TreeNode`` interface for pure-function testing."""

    session_id: str
    parent_session_id: str | None
    updated_at: int
    pool: str
    children: list[_TreeNode] = field(default_factory=list)


def _build_tree(sessions: list[dict[str, Any]]) -> list[_TreeNode]:
    """Replicate WebUI ``buildTree()`` logic in Python for testing.

    Groups a flat list of conversation dicts (each with ``session_id`` and
    ``parent_session_id``) into a parent→children tree, sorting siblings by
    ``updated_at`` descending — matching ``examples/bot_project/webui/src/lib/
    sessionTree.ts``.
    """
    children_map: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    for s in sessions:
        parent = s.get("parent_session_id")
        if parent:
            children_map.setdefault(parent, []).append(s)
        else:
            roots.append(s)
    # Sort children and roots newest-first (matches TS buildTree).
    for children in children_map.values():
        children.sort(key=lambda c: c.get("updated_at", 0), reverse=True)
    roots.sort(key=lambda r: r.get("updated_at", 0), reverse=True)

    def to_node(s: dict[str, Any]) -> _TreeNode:
        return _TreeNode(
            session_id=s["session_id"],
            parent_session_id=s.get("parent_session_id"),
            updated_at=s.get("updated_at", 0),
            pool=s.get("pool", "main"),
            children=[to_node(c) for c in children_map.get(s["session_id"], [])],
        )

    return [to_node(r) for r in roots]


# ---------------------------------------------------------------------------
# 1. End-to-end child session capture
# ---------------------------------------------------------------------------


class TestEndToEndChildCapture:
    """Full pipeline: SSE JSON → parser → Emission → routing → registration."""

    async def test_main_and_child_events_routed_and_registered(self, tmp_path: Path) -> None:
        session_store = LocalFileSessionStore(tmp_path / "sessions")
        registry = InMemorySessionRegistry(store=session_store)
        sink = _build_real_sink(tmp_path, registry)
        child_emitters: dict[str, _RecordingChildEmitter] = {}

        steps = [
            ScriptedStep(text=_sse_text_delta(_MAIN_PROVIDER_SID, "Starting task...")),
            ScriptedStep(text=_sse_text_delta(_CHILD_PROVIDER_SID_1, "Working on subtask...")),
            ScriptedStep(
                text=_sse_tool_completed(
                    _CHILD_PROVIDER_SID_1, "call-child-1", "bash", "file1\nfile2"
                )
            ),
            ScriptedStep(text=_sse_text_delta(_MAIN_PROVIDER_SID, "Task complete.")),
        ]
        parser = _make_fresh_parser()
        adapter = _make_adapter(steps, parser)
        agent = _build_agent(
            tmp_path,
            adapter,
            sink=sink,
            registry=registry,
            session_store=session_store,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        main_emitter = _RecordingEmitter()

        result = await agent.run(_make_ctx(), main_emitter)

        # --- 1. Main session emitter received main text events ---
        assert result.stop_reason == StopReason.COMPLETED
        main_texts = [e for e in main_emitter.turn_events if isinstance(e, TurnTextEvent)]
        assert [t.text for t in main_texts] == ["Starting task...", "Task complete."]

        # --- 2. Child SessionInfo created and registered ---
        child_modex_sid = sink.resolve_child_modex_session_id(_CHILD_PROVIDER_SID_1)
        child_session = await registry.get(child_modex_sid)
        assert child_session is not None
        assert child_session.session_id == child_modex_sid
        assert child_session.agent_name == "external-subagent"
        assert child_session.parent_session_id == _PARENT_MODEX_SID

        # --- 3. Child emitter received child text + tool events ---
        assert child_modex_sid in child_emitters
        child = child_emitters[child_modex_sid]
        child_texts = [e for e in child.turn_events if isinstance(e, TurnTextEvent)]
        assert len(child_texts) == 1
        assert child_texts[0].text == "Working on subtask..."
        child_tool_calls = [e for e in child.turn_events if isinstance(e, TurnToolCallEvent)]
        child_tool_results = [e for e in child.turn_events if isinstance(e, TurnToolResultEvent)]
        assert len(child_tool_calls) == 1
        assert child_tool_calls[0].call_id == "call-child-1"
        assert child_tool_calls[0].tool_name == "bash"
        assert len(child_tool_results) == 1
        assert child_tool_results[0].call_id == "call-child-1"
        assert child_tool_results[0].output == "file1\nfile2"

        # --- 4. SessionStore.get_children returns the child SessionInfo ---
        stored_children = await session_store.get_children(_PARENT_MODEX_SID)
        assert len(stored_children) == 1
        assert stored_children[0].session_id == child_modex_sid
        assert stored_children[0].parent_session_id == _PARENT_MODEX_SID

        # --- 5. buildTree() pure function places child under parent ---
        conversations = [
            {
                "session_id": _PARENT_MODEX_SID,
                "parent_session_id": None,
                "updated_at": 1000,
                "pool": "opencode",
            },
            {
                "session_id": child_modex_sid,
                "parent_session_id": _PARENT_MODEX_SID,
                "updated_at": 2000,
                "pool": "opencode",
            },
        ]
        tree = _build_tree(conversations)
        assert len(tree) == 1
        assert tree[0].session_id == _PARENT_MODEX_SID
        assert len(tree[0].children) == 1
        assert tree[0].children[0].session_id == child_modex_sid
        assert tree[0].children[0].parent_session_id == _PARENT_MODEX_SID


# ---------------------------------------------------------------------------
# 2. Cross-turn resume — same provider_child_sid → register merge
# ---------------------------------------------------------------------------


class TestCrossTurnResume:
    """Two turns with the same provider_child_sid: deterministic modex_sid + merge."""

    async def test_two_turns_same_child_merges_not_duplicates(self, tmp_path: Path) -> None:
        session_store = LocalFileSessionStore(tmp_path / "sessions")
        registry = InMemorySessionRegistry(store=session_store)
        sink = _build_real_sink(tmp_path, registry)
        child_emitters: dict[str, _RecordingChildEmitter] = {}

        # Turn 1: child text emission.
        steps_1 = [
            ScriptedStep(text=_sse_text_delta(_MAIN_PROVIDER_SID, "main turn 1")),
            ScriptedStep(text=_sse_text_delta(_CHILD_PROVIDER_SID_1, "child turn 1")),
        ]
        parser_1 = _make_fresh_parser()
        adapter_1 = _make_adapter(steps_1, parser_1)
        agent = _build_agent(
            tmp_path,
            adapter_1,
            sink=sink,
            registry=registry,
            session_store=session_store,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        await agent.run(_make_ctx(), _RecordingEmitter())

        child_modex_sid = sink.resolve_child_modex_session_id(_CHILD_PROVIDER_SID_1)
        turn_1_child = child_emitters[child_modex_sid]
        assert len(turn_1_child.turn_events) == 1

        # After turn 1: exactly 1 child in the session store.
        children_after_1 = await session_store.get_children(_PARENT_MODEX_SID)
        assert len(children_after_1) == 1
        assert children_after_1[0].session_id == child_modex_sid

        # Turn 2: same provider_child_sid, fresh parser (avoids seen_tool_calls
        # state leaking). Swap the backend provider on the agent.
        steps_2 = [
            ScriptedStep(text=_sse_text_delta(_MAIN_PROVIDER_SID, "main turn 2")),
            ScriptedStep(text=_sse_text_delta(_CHILD_PROVIDER_SID_1, "child turn 2")),
        ]
        parser_2 = _make_fresh_parser()
        adapter_2 = _make_adapter(steps_2, parser_2)
        agent._backend_provider = PoolScopedBackendProvider(adapter_2)
        await agent.run(_make_ctx(), _RecordingEmitter())

        # Deterministic modex_sid: same key both turns.
        assert child_modex_sid in child_emitters
        turn_2_child = child_emitters[child_modex_sid]
        assert turn_2_child is not turn_1_child  # per-turn dict reinitialized
        assert len(turn_2_child.turn_events) == 1
        turn_2_texts = [e for e in turn_2_child.turn_events if isinstance(e, TurnTextEvent)]
        assert turn_2_texts[0].text == "child turn 2"

        # SessionRegistry.register merged — still exactly 1 child (not 2).
        children_after_2 = await session_store.get_children(_PARENT_MODEX_SID)
        assert len(children_after_2) == 1
        assert children_after_2[0].session_id == child_modex_sid


# ---------------------------------------------------------------------------
# 3. Concurrent children — two different provider_child_sid interleaved
# ---------------------------------------------------------------------------


class TestConcurrentChildren:
    """Two children with different provider sids: independent emitters, no cross-talk."""

    async def test_two_interleaved_children_routed_independently(self, tmp_path: Path) -> None:
        session_store = LocalFileSessionStore(tmp_path / "sessions")
        registry = InMemorySessionRegistry(store=session_store)
        sink = _build_real_sink(tmp_path, registry)
        child_emitters: dict[str, _RecordingChildEmitter] = {}

        steps = [
            ScriptedStep(text=_sse_text_delta(_MAIN_PROVIDER_SID, "dispatching two subtasks")),
            # Child 1 text
            ScriptedStep(text=_sse_text_delta(_CHILD_PROVIDER_SID_1, "child 1 working")),
            # Child 2 text (interleaved before child 1 finishes)
            ScriptedStep(text=_sse_text_delta(_CHILD_PROVIDER_SID_2, "child 2 working")),
            # Child 1 tool
            ScriptedStep(
                text=_sse_tool_completed(_CHILD_PROVIDER_SID_1, "call-c1", "grep", "match found")
            ),
            # Child 2 tool
            ScriptedStep(
                text=_sse_tool_completed(_CHILD_PROVIDER_SID_2, "call-c2", "bash", "done")
            ),
            # Main session continuation
            ScriptedStep(text=_sse_text_delta(_MAIN_PROVIDER_SID, "both subtasks done")),
        ]
        parser = _make_fresh_parser()
        adapter = _make_adapter(steps, parser)
        agent = _build_agent(
            tmp_path,
            adapter,
            sink=sink,
            registry=registry,
            session_store=session_store,
            emitter_factory=_make_child_emitter_factory(child_emitters),
        )
        main_emitter = _RecordingEmitter()

        result = await agent.run(_make_ctx(), main_emitter)

        assert result.stop_reason == StopReason.COMPLETED

        # Two distinct child modex_sids.
        child_1_modex = sink.resolve_child_modex_session_id(_CHILD_PROVIDER_SID_1)
        child_2_modex = sink.resolve_child_modex_session_id(_CHILD_PROVIDER_SID_2)
        assert child_1_modex != child_2_modex

        # Both child emitters created.
        assert child_1_modex in child_emitters
        assert child_2_modex in child_emitters

        # Child 1 received only child 1 events (text + tool call + tool result).
        child_1 = child_emitters[child_1_modex]
        child_1_texts = [e for e in child_1.turn_events if isinstance(e, TurnTextEvent)]
        assert [t.text for t in child_1_texts] == ["child 1 working"]
        child_1_calls = [e for e in child_1.turn_events if isinstance(e, TurnToolCallEvent)]
        assert len(child_1_calls) == 1
        assert child_1_calls[0].call_id == "call-c1"
        assert child_1_calls[0].tool_name == "grep"

        # Child 2 received only child 2 events.
        child_2 = child_emitters[child_2_modex]
        child_2_texts = [e for e in child_2.turn_events if isinstance(e, TurnTextEvent)]
        assert [t.text for t in child_2_texts] == ["child 2 working"]
        child_2_calls = [e for e in child_2.turn_events if isinstance(e, TurnToolCallEvent)]
        assert len(child_2_calls) == 1
        assert child_2_calls[0].call_id == "call-c2"
        assert child_2_calls[0].tool_name == "bash"

        # Main emitter received only main text (no child leakage).
        main_texts = [e for e in main_emitter.turn_events if isinstance(e, TurnTextEvent)]
        assert [t.text for t in main_texts] == ["dispatching two subtasks", "both subtasks done"]

        # SessionStore has exactly 2 children for the parent.
        stored_children = await session_store.get_children(_PARENT_MODEX_SID)
        assert len(stored_children) == 2
        stored_ids = {c.session_id for c in stored_children}
        assert stored_ids == {child_1_modex, child_2_modex}

        # Both children have correct parent_session_id.
        for c in stored_children:
            assert c.parent_session_id == _PARENT_MODEX_SID


# ---------------------------------------------------------------------------
# 4. buildTree() pure function tests
# ---------------------------------------------------------------------------


class TestBuildTreePureFunction:
    """Replicates the WebUI ``sessionTree.ts`` ``buildTree()`` algorithm in Python."""

    def test_parent_with_one_child(self) -> None:
        conversations: list[dict[str, Any]] = [
            {"session_id": "parent", "parent_session_id": None, "updated_at": 1000},
            {"session_id": "child", "parent_session_id": "parent", "updated_at": 2000},
        ]
        tree = _build_tree(conversations)
        assert len(tree) == 1
        assert tree[0].session_id == "parent"
        assert tree[0].parent_session_id is None
        assert len(tree[0].children) == 1
        assert tree[0].children[0].session_id == "child"
        assert tree[0].children[0].parent_session_id == "parent"

    def test_multiple_children_sorted_newest_first(self) -> None:
        conversations: list[dict[str, Any]] = [
            {"session_id": "root", "parent_session_id": None, "updated_at": 100},
            {"session_id": "c1", "parent_session_id": "root", "updated_at": 300},
            {"session_id": "c2", "parent_session_id": "root", "updated_at": 500},
            {"session_id": "c3", "parent_session_id": "root", "updated_at": 200},
        ]
        tree = _build_tree(conversations)
        assert len(tree) == 1
        root = tree[0]
        assert root.session_id == "root"
        assert [c.session_id for c in root.children] == ["c2", "c1", "c3"]

    def test_multiple_roots_sorted_newest_first(self) -> None:
        conversations: list[dict[str, Any]] = [
            {"session_id": "r1", "parent_session_id": None, "updated_at": 100},
            {"session_id": "r2", "parent_session_id": None, "updated_at": 300},
            {"session_id": "r3", "parent_session_id": None, "updated_at": 200},
        ]
        tree = _build_tree(conversations)
        assert [n.session_id for n in tree] == ["r2", "r3", "r1"]

    def test_grandchild_nested_two_levels(self) -> None:
        conversations: list[dict[str, Any]] = [
            {"session_id": "grandparent", "parent_session_id": None, "updated_at": 1},
            {"session_id": "parent", "parent_session_id": "grandparent", "updated_at": 2},
            {"session_id": "child", "parent_session_id": "parent", "updated_at": 3},
        ]
        tree = _build_tree(conversations)
        assert len(tree) == 1
        gp = tree[0]
        assert gp.session_id == "grandparent"
        assert len(gp.children) == 1
        parent = gp.children[0]
        assert parent.session_id == "parent"
        assert len(parent.children) == 1
        assert parent.children[0].session_id == "child"

    def test_orphan_child_without_parent_in_list_is_dropped(self) -> None:
        # A child whose parent_session_id does not match any session_id in
        # the list goes into children_map[parent] but no root has that
        # session_id, so it is never reached by the recursive build and
        # does not appear in the output tree. This matches the WebUI
        # buildTree() behavior — orphans are dropped, not promoted to roots.
        conversations: list[dict[str, Any]] = [
            {"session_id": "orphan", "parent_session_id": "missing-parent", "updated_at": 1},
            {"session_id": "real-root", "parent_session_id": None, "updated_at": 2},
        ]
        tree = _build_tree(conversations)
        root_ids = {n.session_id for n in tree}
        assert root_ids == {"real-root"}
        assert "orphan" not in root_ids

    def test_empty_input_returns_empty_tree(self) -> None:
        assert _build_tree([]) == []

    def test_single_root_no_children(self) -> None:
        conversations: list[dict[str, Any]] = [
            {"session_id": "solo", "parent_session_id": None, "updated_at": 1}
        ]
        tree = _build_tree(conversations)
        assert len(tree) == 1
        assert tree[0].session_id == "solo"
        assert tree[0].children == []
