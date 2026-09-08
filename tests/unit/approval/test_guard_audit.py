"""unified-security Ticket 06 — guard decisions on the unified audit timeline.

Coverage matrix (tickets.md 06 验证项):

- guard 拒 (HARDLINE) → audit row with ``decided_by="sandbox_guard"`` and
  ``deny_reason`` carrying the verdict reason;
- guard 升 (BOUNDARY escalate) → one ``sandbox_guard`` row (the escalation
  fact) while the human decision later lands as its own ``user`` row;
- CLEAN → zero rows (noise control: only deviations from NORMAL recorded);
- inner-tier DANGEROUS (no guard verdict) → zero rows — the audit
  attributes the guard, not the tier classifier;
- plain (non-guard) deployments → zero rows, byte-identical behavior;
- audit sink failure never breaks the turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.agents.react.context import ReActGraphContext
from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.config import AgentApprovalConfig, ToolApprovalConfig
from modex_agent.approval.constants import ApprovalTier
from modex_agent.approval.runtime import ApprovalRuntime, TieredToolApprovalClassifier
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.core.scope import RecordScope
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import ExecutionMode, Tool
from modex_agent.memory.history import ListMessageHistory
from modex_agent.persistence import ConnectionManager, DatabaseKind
from modex_agent.persistence.adapters.approval_audit_store import (
    SqliteApprovalAuditStore,
)
from modex_agent.runtime.approval_decision import (
    SANDBOX_GUARD_DECIDED_BY,
    ApprovalAuditDecision,
    DecisionActor,
)
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.sandbox.decision import SecurityDecisionService
from modex_agent.sandbox.security_classifier import SecurityClassifier
from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    GuardSettings,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.tools.manager import InMemoryToolManager
from modex_graph import (
    GraphPersistenceCoordinator,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
    NullNodeStateStore,
)

WS = Path("/ws/project")


class _FixedRoot:
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


class _AuditEmitter(ContentEmitter):
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


def _settings(
    write_surface: WriteSurface = WriteSurface.WORKSPACE,
) -> SandboxSettings:
    return SandboxSettings(
        backend=SandboxBackend.HOST,
        exclusive=ExclusiveConfig(write_surface=write_surface),
        guard=GuardSettings(),
    )


def _guard_classifier(
    *,
    escalate: bool,
    inner: TieredToolApprovalClassifier | None = None,
    settings: SandboxSettings | None = None,
) -> SecurityClassifier:
    return SecurityClassifier(
        decision=SecurityDecisionService(
            settings=settings or _settings(),
            workspace_root_provider=_FixedRoot(WS),  # type: ignore[arg-type]
        ),
        inner=inner
        or TieredToolApprovalClassifier(
            config=AgentApprovalConfig(enabled=False),
        ),
        escalate_enabled=escalate,
    )


class _RecordingAuditStore(SqliteApprovalAuditStore):
    """SQLite store wrapper exposing a failure-injection toggle."""

    def __init__(self, connection: ConnectionManager, scope: RecordScope) -> None:
        super().__init__(connection, scope)
        self.fail_record = False

    async def record(self, entry) -> None:  # type: ignore[no-untyped-def]
        if self.fail_record:
            raise RuntimeError("injected audit sink failure")
        await super().record(entry)


def _make_graph_ctx(
    services: AgentRuntimeServices,
    tool_calls: list[ToolCall],
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
    agent_ctx.history = ListMessageHistory()
    coordinator: GraphPersistenceCoordinator = GraphPersistenceCoordinator(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=NullNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )
    for node_id in ("tool", "llm", "after", "start"):
        coordinator.register_node(node_id)
    ctx = ReActGraphContext(
        state=state,
        runtime=ReactGraphRuntime(emitter=_AuditEmitter()),  # type: ignore[arg-type]
        user_data=agent_ctx,
        coordinator=coordinator,
    )
    return ctx


async def _run_tool_node(
    classifier,
    audit_store,
    tool_calls: list[ToolCall],
) -> ReActGraphContext:
    services = AgentRuntimeServices(
        approval=ApprovalRuntime(classifier=classifier),
        approval_audit=audit_store,
    )
    ctx = _make_graph_ctx(services, tool_calls)
    for tc in tool_calls:
        await ctx.agent_ctx.history.append(
            ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])
        )
    node = ToolNode(ToolExecutor())
    await node.run(ctx)
    return ctx


def _deny_rule_call() -> ToolCall:
    return ToolCall(tool_name="write", arguments={"path": ".git/config", "content": "x"}, call_id="c1")


def _boundary_call() -> ToolCall:
    return ToolCall(tool_name="write", arguments={"path": "/etc/hosts"}, call_id="c1")


def _clean_call() -> ToolCall:
    return ToolCall(tool_name="bash", arguments={"command": f"ls {WS}"}, call_id="c1")


@pytest.fixture
async def audit_db(tmp_path: Path):  # type: ignore[no-untyped-def]
    manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await manager.open()
    yield manager
    await manager.close()


@pytest.fixture
def audit_store(audit_db: ConnectionManager) -> _RecordingAuditStore:
    return _RecordingAuditStore(audit_db, RecordScope(session_id="s1.main"))


class TestGuardDenyAudited:
    async def test_hardline_deny_writes_sandbox_guard_row(
        self, audit_store: _RecordingAuditStore
    ) -> None:
        await _run_tool_node(_guard_classifier(escalate=True), audit_store, [_deny_rule_call()])

        rows = await audit_store.query("s1.main")
        assert len(rows) == 1
        row = rows[0]
        assert row.decided_by == SANDBOX_GUARD_DECIDED_BY
        assert row.decision == "denied"
        assert row.deny_reason is not None
        assert row.tool_name == "write"
        assert row.tool_call_id == "c1"
        assert row.turn_uuid == "turn-uuid-1"
        assert row.agent_id == "agent"

    async def test_boundary_deny_without_escalation_audited(
        self, audit_store: _RecordingAuditStore
    ) -> None:
        await _run_tool_node(_guard_classifier(escalate=False), audit_store, [_boundary_call()])

        rows = await audit_store.query("s1.main")
        assert len(rows) == 1
        assert rows[0].decided_by == SANDBOX_GUARD_DECIDED_BY
        assert rows[0].decision == "denied"
        assert rows[0].deny_reason is not None
        assert "/etc/hosts" in rows[0].deny_reason

    async def test_query_filters_by_decided_by(self, audit_store: _RecordingAuditStore) -> None:
        await _run_tool_node(_guard_classifier(escalate=True), audit_store, [_deny_rule_call()])
        guard_rows = await audit_store.query("s1.main", decided_by=DecisionActor.SANDBOX_GUARD)
        assert len(guard_rows) == 1
        user_rows = await audit_store.query("s1.main", decided_by=DecisionActor.USER)
        assert user_rows == []


class TestGuardEscalationAudited:
    async def test_boundary_escalation_writes_escalated_guard_row(
        self, audit_store: _RecordingAuditStore
    ) -> None:
        # DANGEROUS (PENDING) — the call suspends; the escalation itself is
        # the guard decision recorded here, the human row comes later via
        # the coordinator (unchanged path).
        await _run_tool_node(_guard_classifier(escalate=True), audit_store, [_boundary_call()])

        rows = await audit_store.query("s1.main")
        assert len(rows) == 1
        row = rows[0]
        assert row.decided_by == SANDBOX_GUARD_DECIDED_BY
        assert row.decision is ApprovalAuditDecision.ESCALATED
        assert row.deny_reason is None


class TestCleanNotAudited:
    async def test_clean_call_writes_zero_rows(self, audit_store: _RecordingAuditStore) -> None:
        await _run_tool_node(_guard_classifier(escalate=True), audit_store, [_clean_call()])
        assert await audit_store.query("s1.main") == []

    async def test_inner_tier_dangerous_without_guard_verdict_zero_rows(
        self, audit_store: _RecordingAuditStore
    ) -> None:
        # CLEAN guard verdict + inner tier DANGEROUS (empty allowed_paths)
        # — the tier classifier's judgment is NOT a guard decision: no row.
        inner = TieredToolApprovalClassifier(
            config=AgentApprovalConfig(
                enabled=True,
                tools={"bash": ToolApprovalConfig(allowed_paths=[])},
            ),
        )
        await _run_tool_node(
            _guard_classifier(escalate=True, inner=inner),
            audit_store,
            [_clean_call()],
        )
        assert await audit_store.query("s1.main") == []


class TestPlainDeploymentUnchanged:
    async def test_non_guard_classifier_writes_zero_rows(
        self, audit_store: _RecordingAuditStore
    ) -> None:
        plain = TieredToolApprovalClassifier(
            config=AgentApprovalConfig(enabled=False),
        )
        await _run_tool_node(plain, audit_store, [_deny_rule_call()])
        assert await audit_store.query("s1.main") == []

    async def test_guard_classifier_without_sink_still_decides(
        self, audit_store: _RecordingAuditStore
    ) -> None:
        services = AgentRuntimeServices(
            approval=ApprovalRuntime(classifier=_guard_classifier(escalate=True)),
            approval_audit=None,
        )
        tc = _deny_rule_call()
        ctx = _make_graph_ctx(services, [tc])
        await ctx.agent_ctx.history.append(
            ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[tc])
        )
        node = ToolNode(ToolExecutor())
        await node.run(ctx)
        assert ctx.agent_ctx.runtime is not None
        assert await audit_store.query("s1.main") == []

    async def test_failing_sink_does_not_break_turn(
        self, audit_store: _RecordingAuditStore
    ) -> None:
        audit_store.fail_record = True
        ctx = await _run_tool_node(
            _guard_classifier(escalate=True), audit_store, [_deny_rule_call()]
        )
        # The denied call still produced its error ToolResult — the turn
        # completed (routed to LLM), the audit failure was only logged.
        messages = await ctx.agent_ctx.history.to_list()
        tool_msgs = [m for m in messages if m.role == MessageRole.TOOL]
        assert len(tool_msgs) == 1
        assert tool_msgs[0].tool_call_id == "c1"


class TestClassifierEscalationResult:
    def test_each_result_carries_its_own_reason(self) -> None:
        classifier = _guard_classifier(escalate=True)
        from tests.unit.sandbox.test_security_classifier import _ctx

        denied = _deny_rule_call()
        result = classifier.classify(denied, _ctx())
        assert result.tier is ApprovalTier.HARDLINE
        assert result.deny_reason is not None

        boundary = _boundary_call()
        result = classifier.classify(boundary, _ctx())
        assert result.tier is ApprovalTier.DANGEROUS
        assert result.reason is not None
        assert result.deny_reason is None

        clean = _clean_call()
        result = classifier.classify(clean, _ctx())
        assert result.tier is ApprovalTier.NORMAL
        assert result.reason is None
        assert result.deny_reason is None
