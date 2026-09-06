"""ToolNode classifies each ToolCall exactly once and derives everything else.

The pre-refactor ToolNode called ``classifier.classify`` 3-4 times per batch
(``_classify_all`` + ``_classification_deny_reasons`` + ``_guard_audit_facts``
+ tier lookup in suspension) and recovered the deny reason through a mutable
side channel. These tests pin the single-classification contract: one
``classify`` per tool call, decisions / denial copy / audit rows all derived
from the stored classification.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.agents.react.context import ReActGraphContext
from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import (
    ReActRuntimeStateCodec,
    ReActSnapshotPolicy,
    ReActTurnState,
)
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.classification import ToolClassification
from modex_agent.approval.constants import ApprovalAuditSource, ApprovalDecision
from modex_agent.approval.runtime import ApprovalClassifier, ApprovalRuntime
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ExecutionMode, Tool
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.memory.history import ListMessageHistory
from modex_agent.messaging.models import ApprovalAction
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.approval_audit_store import SqliteApprovalAuditStore
from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore
from modex_agent.persistence.coordinator import SqliteDecisionCoordinator
from modex_agent.pipeline.approval_resumer import ApprovalResumer
from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.runtime.approval_decision import (
    ApprovalAuditDecision,
    ApprovalAuditEntry,
    ApprovalAuditStore,
    DecisionActor,
)
from modex_agent.runtime.codec import RuntimeStateCodecRegistry
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import InMemoryTurnStateStore
from modex_agent.sandbox.delegation import DelegationSnapshot
from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_graph import (
    GraphPersistenceCoordinator,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
    NullNodeStateStore,
)
from modex_graph.exceptions import GraphInterrupt

WS = Path("/ws/project")


class _CountingClassifier(ApprovalClassifier):
    """Wraps another classifier and counts classify calls per call_id."""

    def __init__(self, inner: ApprovalClassifier) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def classify(self, tool_call: ToolCall, ctx: AgentContext) -> ToolClassification:
        self.calls.append(tool_call.call_id or "")
        return self._inner.classify(tool_call, ctx)


class _Emitter(ContentEmitter):
    event_enum = object

    async def emit(self, event, data=None): ...
    async def emit_delta(self, delta: str): ...
    async def emit_content(self, full_content: str): ...
    async def emit_stream_end(self, resuming: bool = False): ...
    async def emit_complete(self, result: AgentResult): ...
    async def emit_error(self, error: str): ...

    def wants_streaming(self) -> bool:
        return False


class _NoopTool(Tool):
    def __init__(self) -> None:
        super().__init__(name="bash", description="run a command", parameters={})

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.EXCLUSIVE

    async def execute(self, **kwargs):
        return "ok"


class _RecordingAuditStore(ApprovalAuditStore):
    """In-memory audit sink capturing recorded entries."""

    def __init__(self) -> None:
        self.entries: list[ApprovalAuditEntry] = []
        self.fail_record = False

    async def record(self, entry: ApprovalAuditEntry) -> None:
        if self.fail_record:
            raise RuntimeError("injected audit sink failure")
        self.entries.append(entry)

    async def query(
        self,
        session_id: str,
        since: datetime | None = None,
        limit: int = 100,
        decided_by: DecisionActor | None = None,
        source: ApprovalAuditSource | None = None,
    ) -> list[ApprovalAuditEntry]:
        return list(self.entries)


def _guard_classifier(*, escalate: bool) -> ApprovalClassifier:
    from modex_agent.approval.config import AgentApprovalConfig
    from modex_agent.approval.runtime import TieredToolApprovalClassifier
    from modex_agent.sandbox.decision import SecurityDecisionService
    from modex_agent.sandbox.security_classifier import SecurityClassifier
    from modex_agent.sandbox.settings import (
        GuardSettings,
        SandboxBackend,
        SandboxSettings,
    )

    class _FixedRoot(WorkspaceRootProvider):
        def __init__(self, root: Path) -> None:
            self._root = root

        def current(self) -> Path:
            return self._root

    return SecurityClassifier(
        decision=SecurityDecisionService(
            settings=SandboxSettings.model_validate(
                {
                    "backend": SandboxBackend.HOST,
                    "exclusive": {"write_surface": "workspace"},
                    "guard": GuardSettings(),
                }
            ),
            workspace_root_provider=_FixedRoot(WS),
        ),
        inner=TieredToolApprovalClassifier(
            config=AgentApprovalConfig(enabled=False),
        ),
        escalate_enabled=escalate,
    )


def _make_graph_ctx(
    services: AgentRuntimeServices,
) -> ReActGraphContext:
    identity = TurnIdentity(
        agent_id="agent",
        session=SessionInfo.from_str("s1.main"),
        turn_id="t1",
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    state.custom[TurnCustomKey.TURN_UUID] = "turn-uuid-1"
    tool_manager = InMemoryToolManager()
    tool_manager.register(_NoopTool())
    agent_ctx = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=tool_manager,
        identity=identity,
        runtime=AgentRuntime(services=services, state=state),
        session=SessionInfo.from_str("s1.main"),
    )
    coordinator: GraphPersistenceCoordinator = GraphPersistenceCoordinator(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=NullNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )
    for node_id in ("tool", "llm", "after", "start"):
        coordinator.register_node(node_id)
    return ReActGraphContext(
        state=state,
        runtime=ReactGraphRuntime(
            emitter=_Emitter(),  # type: ignore[arg-type]
            snapshot_policy=ReActSnapshotPolicy(),
            turn_state_store=services.turn_store,
        ),
        user_data=agent_ctx,
        coordinator=coordinator,
    )


async def _run_tool_node(
    classifier: ApprovalClassifier,
    audit_store: _RecordingAuditStore | None,
    tool_calls: list[ToolCall],
    *,
    expect_pending: bool = False,
) -> tuple[ReActGraphContext, _RecordingAuditStore | None]:
    services = AgentRuntimeServices(
        approval=ApprovalRuntime(classifier=classifier),
        approval_audit=audit_store,
        turn_store=InMemoryTurnStateStore(),
    )
    ctx = _make_graph_ctx(services)
    await ctx.agent_ctx.history.append(
        ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=tool_calls)
    )
    node = ToolNode(ToolExecutor())
    if expect_pending:
        with pytest.raises(GraphInterrupt):
            await node.run(ctx)
        assert ctx.state.phase is TurnPhase.SUSPENDED
    else:
        await node.run(ctx)
    return ctx, audit_store


def _deny_rule_call() -> ToolCall:
    return ToolCall(tool_name="write", arguments={"path": ".git/config", "content": "x"}, call_id="c1")


def _boundary_call() -> ToolCall:
    return ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")


def _clean_call() -> ToolCall:
    return ToolCall(tool_name="bash", arguments={"command": f"ls {WS}"}, call_id="c1")


class TestSingleClassification:
    @pytest.mark.parametrize("configured", [False, True])
    async def test_approval_off_factory_denies_without_pending(self, configured: bool) -> None:
        from modex_agent.ioc.configs.approval import ApprovalConfig, ToolApprovalEntry
        from modex_agent.ioc.factories.approval import build_approval_runtime
        from modex_agent.sandbox.settings import SandboxBackend, SandboxSettings

        class Root(WorkspaceRootProvider):
            def current(self) -> Path:
                return WS

        runtime = build_approval_runtime(
            ApprovalConfig(enabled=False, tools={"bash": ToolApprovalEntry(allowed_paths=["./*"])})
            if configured
            else None,
            sandbox=SandboxSettings(backend=SandboxBackend.HOST),
            root_provider=Root(),
        )
        assert runtime is not None
        counting = _CountingClassifier(runtime.classifier)
        ctx, _ = await _run_tool_node(
            counting,
            None,
            [_boundary_call(), _deny_rule_call().model_copy(update={"call_id": "c2"})],
        )
        assert counting.calls == ["c1", "c2"]
        assert ctx.state.approval is None
        assert ctx.state.phase is not TurnPhase.SUSPENDED
        assert all(
            call.result is not None and call.result.error
            for call in ctx.state.tool_batches[-1].calls
        )

    @pytest.mark.parametrize("action", [ApprovalAction.ALLOW, ApprovalAction.DENY])
    async def test_human_resume_and_audit_roundtrip(
        self, tmp_path: Path, action: ApprovalAction
    ) -> None:
        manager = ConnectionManager(tmp_path / "turn.db", DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            codecs = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
            store = SqliteTurnStateStore(manager, codecs)
            audit = SqliteApprovalAuditStore(manager, RecordScope(session_id="s1.main"))
            pool_data = PoolDataSnapshot(
                context_manager=InMemoryContextManager(),
                turn_store=store,
                trace_store=None,
                memory_dir=None,
                runtime_dir=None,
                pruned_manager=None,
                decision_coordinator=SqliteDecisionCoordinator(manager, codecs),
            )
            counting = _CountingClassifier(_guard_classifier(escalate=True))
            ctx = _make_graph_ctx(
                AgentRuntimeServices(
                    approval=ApprovalRuntime(counting), turn_store=store, approval_audit=audit
                )
            )
            call = ToolCall(
                tool_name="bash", arguments={"command": "touch /etc/passwd"}, call_id="c1"
            )
            await ctx.agent_ctx.history.append(
                ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[call])
            )
            with pytest.raises(GraphInterrupt):
                await ToolNode(ToolExecutor()).run(ctx)
            resumer = ApprovalResumer(agent=MagicMock(), turn_store=store, user_interface=None)
            snapshot = await resumer.load_pending("s1.main")
            assert snapshot is not None
            assert (
                await resumer.apply_resume(
                    snapshot,
                    action=action,
                    session_id="s1.main",
                    pool_data=pool_data,
                    agent_context=ctx.agent_ctx,
                    tool_call_id="c1",
                )
                is store
            )
            persisted = await store.load_turn(snapshot.identity)
            assert persisted is not None
            restored = ReActSnapshotPolicy.state_from_snapshot(persisted)
            assert ctx.agent_ctx.runtime is not None
            ctx.agent_ctx.runtime.state = restored
            ctx.state = restored
            await ToolNode(ToolExecutor()).run(ctx)
            assert counting.calls == ["c1"]
            result = ctx.state.tool_batches[-1].calls[0].result
            assert result is not None
            if action is ApprovalAction.ALLOW:
                assert result.error is None
                assert result.message_content() == "ok"
            else:
                assert result.error is not None and "Denied by user" in result.error
            rows = await audit.query("s1.main")
            assert [row.decision for row in rows] == [
                ApprovalAuditDecision.ESCALATED,
                ApprovalAuditDecision.APPROVED
                if action is ApprovalAction.ALLOW
                else ApprovalAuditDecision.DENIED,
            ]
            assert [row.decided_by for row in rows] == [
                DecisionActor.SANDBOX_GUARD,
                DecisionActor.USER,
            ]
            assert all(row.source is ApprovalAuditSource.RUNTIME for row in rows)
        finally:
            await manager.close()

    async def test_resume_preserves_hard_denial_without_reclassification(self) -> None:
        counting = _CountingClassifier(_guard_classifier(escalate=True))
        store = InMemoryTurnStateStore()
        audit = _RecordingAuditStore()
        services = AgentRuntimeServices(
            approval=ApprovalRuntime(counting), turn_store=store, approval_audit=audit
        )
        ctx = _make_graph_ctx(services)
        calls = [
            _deny_rule_call(),
            ToolCall(tool_name="bash", arguments={"command": "touch /etc/passwd"}, call_id="c2"),
            ToolCall(tool_name="bash", arguments={"command": "pwd"}, call_id="c3"),
        ]
        await ctx.agent_ctx.history.append(
            ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=calls)
        )
        with pytest.raises(GraphInterrupt):
            await ToolNode(ToolExecutor()).run(ctx)
        assert counting.calls == ["c1", "c2", "c3"]
        resumer = ApprovalResumer(agent=MagicMock(), turn_store=store, user_interface=None)
        snapshot = await resumer.load_pending("s1.main")
        assert snapshot is not None
        assert (
            await resumer.apply_resume(
                snapshot,
                action=ApprovalAction.ALLOW,
                session_id="s1.main",
                pool_data=None,
                agent_context=ctx.agent_ctx,
                tool_call_id="c2",
            )
            is store
        )
        assert ctx.agent_ctx.runtime is not None
        restored = ctx.agent_ctx.runtime.state
        assert isinstance(restored, ReActTurnState)
        ctx.state = restored
        await ToolNode(ToolExecutor()).run(ctx)
        assert counting.calls == ["c1", "c2", "c3"]
        batch = ctx.state.tool_batches[-1]
        assert batch.calls[0].decision is ApprovalDecision.DENIED
        assert all(call.result is not None and call.result.error for call in batch.calls)
        denied_result = batch.calls[0].result
        assert denied_result is not None and denied_result.error is not None
        assert "Denied by user" not in denied_result.error
        assert denied_result.error == audit.entries[0].deny_reason
        assert len(audit.entries) == 2

    async def test_one_classify_call_per_tool(self) -> None:
        from modex_agent.approval.config import AgentApprovalConfig
        from modex_agent.approval.runtime import TieredToolApprovalClassifier

        counting = _CountingClassifier(
            TieredToolApprovalClassifier(config=AgentApprovalConfig(enabled=False))
        )
        await _run_tool_node(counting, None, [_clean_call()])
        assert counting.calls == ["c1"]

    async def test_guard_batch_one_classify_per_tool_no_bleed(self) -> None:
        counting = _CountingClassifier(_guard_classifier(escalate=True))
        calls = [
            ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1"),
            ToolCall(tool_name="write", arguments={"path": ".git/config", "content": "x"}, call_id="c2"),
            ToolCall(tool_name="bash", arguments={"command": f"ls {WS}"}, call_id="c3"),
        ]
        await _run_tool_node(counting, None, calls, expect_pending=True)
        assert counting.calls == ["c1", "c2", "c3"]


class TestGuardAuditFromClassification:
    @pytest.mark.parametrize("delegated", [False, True])
    async def test_runtime_source_reaches_sqlite_query(
        self, tmp_path: Path, delegated: bool
    ) -> None:
        manager = ConnectionManager(tmp_path / "audit.db", DatabaseKind.WORKSPACE)
        await manager.open()
        try:
            audit = SqliteApprovalAuditStore(manager, RecordScope(session_id="s1.main"))
            ctx = _make_graph_ctx(
                AgentRuntimeServices(
                    approval=ApprovalRuntime(_guard_classifier(escalate=False)),
                    approval_audit=audit,
                    delegation=(
                        DelegationSnapshot(
                            workspace_root=WS,
                            settings=SandboxSettings(backend=SandboxBackend.HOST),
                        )
                        if delegated
                        else None
                    ),
                )
            )
            await ctx.agent_ctx.history.append(
                ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[_boundary_call()])
            )
            await ToolNode(ToolExecutor()).run(ctx)
            source = ApprovalAuditSource.DELEGATION if delegated else ApprovalAuditSource.RUNTIME
            rows = await audit.query(
                "s1.main", source=source, decided_by=DecisionActor.SANDBOX_GUARD
            )
            assert len(rows) == 1
            assert rows[0].source is source
            assert rows[0].decision is ApprovalAuditDecision.DENIED
        finally:
            await manager.close()

    async def test_hardline_deny_records_denied_row(self) -> None:
        _, recording = await _run_tool_node(
            _guard_classifier(escalate=True), _RecordingAuditStore(), [_deny_rule_call()]
        )
        assert recording is not None
        assert len(recording.entries) == 1
        entry = recording.entries[0]
        assert entry.decision is ApprovalAuditDecision.DENIED
        assert entry.decided_by is DecisionActor.SANDBOX_GUARD
        assert entry.deny_reason is not None
        assert entry.tool_call_id == "c1"
        assert entry.turn_uuid == "turn-uuid-1"

    async def test_boundary_escalation_records_escalated_not_approved(self) -> None:
        _, recording = await _run_tool_node(
            _guard_classifier(escalate=True),
            _RecordingAuditStore(),
            [_boundary_call()],
            expect_pending=True,
        )
        assert recording is not None
        assert len(recording.entries) == 1
        entry = recording.entries[0]
        assert entry.decision is ApprovalAuditDecision.ESCALATED
        assert entry.decided_by is DecisionActor.SANDBOX_GUARD
        assert entry.deny_reason is None

    async def test_clean_call_writes_zero_rows(self) -> None:
        _, recording = await _run_tool_node(
            _guard_classifier(escalate=True), _RecordingAuditStore(), [_clean_call()]
        )
        assert recording is not None
        assert recording.entries == []

    async def test_no_sink_still_decides(self) -> None:
        ctx, _ = await _run_tool_node(_guard_classifier(escalate=True), None, [_deny_rule_call()])
        messages = await ctx.agent_ctx.history.to_list()
        tool_msgs = [m for m in messages if m.role == MessageRole.TOOL]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_call_id == "c1"

    async def test_failing_sink_cannot_bypass_decision(self) -> None:
        store = _RecordingAuditStore()
        store.fail_record = True
        ctx, _ = await _run_tool_node(_guard_classifier(escalate=True), store, [_deny_rule_call()])
        messages = await ctx.agent_ctx.history.to_list()
        tool_msgs = [m for m in messages if m.role == MessageRole.TOOL]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_call_id == "c1"


class TestDenialCopyFromClassification:
    async def test_classification_denial_renders_reason_not_user_copy(self) -> None:
        ctx, _ = await _run_tool_node(_guard_classifier(escalate=False), None, [_boundary_call()])
        messages = await ctx.agent_ctx.history.to_list()
        tool_msgs = [m for m in messages if m.role == MessageRole.TOOL]
        assert len(tool_msgs) == 1
        content = str(tool_msgs[0].content)
        assert "/etc/hosts" in content
        assert "Denied by user" not in content
