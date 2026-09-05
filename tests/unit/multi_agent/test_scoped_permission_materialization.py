"""Real native factory -> delegation services -> ToolNode, without an LLM."""

from pathlib import Path
from unittest.mock import patch

import pytest

from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.approval.constants import ApprovalAuditSource, ApprovalTier
from modex_agent.core.message import ChatMessage, MessageRole, ToolCall
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.factory import DefaultAgentFactory
from modex_agent.runtime.approval_decision import ApprovalAuditStore
from modex_agent.sandbox.settings import SandboxBackend, SandboxPolicy
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.scope.spec import AgentSpec
from modex_agent.workspace.scope_path import ScopePath
from tests.unit.approval.test_tool_node_single_classification import _make_graph_ctx
from tests.unit.multi_agent.test_delegation_envelope_wiring import (
    _compiled_template,
    _deps,
    _pool_assembly,
)


async def materialize(
    tmp_path: Path, policy: str | None = None, *, ast: bool = False,
    approval_audit: ApprovalAuditStore | None = None,
) -> AgentInstance:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    root = AgentSpec(name="main", interceptor_configs={"sandbox_guard": {"sandbox": {
        "backend": "host", "policy": policy, "writable_roots": ["../shared"],
    }}}) if policy else AgentSpec(name="main")
    deps = await _deps(tmp_path, _pool_assembly(tmp_path, root), ws)
    deps.agent_factory = DefaultAgentFactory(default_llm_provider=deps.llm_provider)
    deps.scope_path = ScopePath(workspace_root=ws, pool_name="main")
    deps.approval_audit = approval_audit
    template = _compiled_template("scout", allowed_dirs=[Path("../shared")] if policy else [],
                                  tools=["+ast_grep_replace"] if ast else None)
    with patch("modex_agent.plugins.defaults.hooks.resolve_modexctl_bin_dir", return_value=tmp_path):
        return await template.materialize(None, "inv", deps)


@pytest.mark.parametrize("policy", [None, "workspace-write", "read-only"])
async def test_real_materialization_reports_effective_capabilities(tmp_path: Path, policy: str | None) -> None:
    instance = await materialize(tmp_path, policy)
    try:
        snapshot = instance.delegation
        assert snapshot is not None
        assert snapshot.backend is SandboxBackend.HOST
        assert snapshot.enforcement is EnforcementLevel.NONE
        assert snapshot.source is ApprovalAuditSource.DELEGATION
        assert snapshot.file_guards
        assert snapshot.limitations
        assert snapshot.policy is (SandboxPolicy.READ_ONLY if policy == "read-only" else SandboxPolicy.WORKSPACE_WRITE)
        assert all(root.is_absolute() for root in snapshot.envelope)
        assert instance.pipeline is not None
        builder = instance.pipeline._turn_runner.turn_context_builder
        assert builder is not None
        services = builder.runtime_services
        assert services is not None and services.approval is not None
        assert services.delegation is snapshot
        ctx = _make_graph_ctx(services)
        shared_read = ToolCall(tool_name="read", arguments={"path": "../shared/file"}, call_id="shared")
        if policy:
            assert services.approval.classifier.classify(shared_read, ctx.agent_ctx).tier is ApprovalTier.NORMAL
        write = ToolCall(tool_name="write", arguments={"path": "file", "content": "x"}, call_id="write")
        expected = ApprovalTier.HARDLINE if policy == "read-only" else ApprovalTier.NORMAL
        assert services.approval.classifier.classify(write, ctx.agent_ctx).tier is expected
    finally:
        await instance.stop()


@pytest.mark.parametrize("policy", [None, "workspace-write"])
async def test_native_materialization_records_denial_in_shared_audit(
    tmp_path: Path, policy: str | None,
) -> None:
    from modex_agent.agents.react.context import ReActGraphContext
    from modex_agent.agents.react.runtime import ReactGraphRuntime
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.approval.constants import ApprovalAuditDecision
    from modex_agent.core.scope import RecordScope
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.memory.context import ContextState
    from modex_agent.persistence import ConnectionManager, DatabaseKind
    from modex_agent.persistence.adapters.approval_audit_store import SqliteApprovalAuditStore
    from modex_agent.runtime.approval_decision import DecisionActor
    from modex_agent.runtime.enums import TurnCustomKey

    manager = ConnectionManager(tmp_path / "audit.db", DatabaseKind.WORKSPACE)
    await manager.open()
    try:
        audit = SqliteApprovalAuditStore(manager, RecordScope())
        instance = await materialize(tmp_path, policy, approval_audit=audit)
        try:
            assert instance.pipeline is not None
            builder = instance.pipeline._turn_runner.turn_context_builder
            assert builder is not None
            session = SessionInfo.from_str("inv.scout")
            agent_ctx, _ = builder.build_runtime_and_context(
                session, ContextState(), instance.context_manager,
            )
            assert agent_ctx.runtime is not None
            state = agent_ctx.runtime.state
            assert isinstance(state, ReActTurnState)
            # TurnRunner normally sets this before entering the ReAct graph.
            state.custom[TurnCustomKey.TURN_UUID] = "native-audit-turn"
            harness = _make_graph_ctx(agent_ctx.runtime.services)
            ctx = ReActGraphContext(
                state=state, user_data=agent_ctx, runtime=ReactGraphRuntime(),
                coordinator=harness.coordinator,
            )
            outside = tmp_path / "outside.txt"
            call = ToolCall(tool_name="write", arguments={"path": str(outside), "content": "x"}, call_id="denied")
            await agent_ctx.history.append(ChatMessage(role=MessageRole.ASSISTANT, tool_calls=[call]))
            await ToolNode(ToolExecutor()).run(ctx)
            result = state.tool_batches[-1].calls[0].result
            assert result is not None and result.error is not None
            assert state.approval is None
            assert not outside.exists()
            rows = await audit.query(
                session.session_id, source=ApprovalAuditSource.DELEGATION,
                decided_by=DecisionActor.SANDBOX_GUARD,
            )
            assert len(rows) == 1
            assert rows[0].decision is ApprovalAuditDecision.DENIED
            assert rows[0].tool_call_id == call.call_id
            assert rows[0].agent_id == "scout"
            assert rows[0].deny_reason == result.error
            assert agent_ctx.runtime.services.approval_audit is audit
        finally:
            await instance.stop()
    finally:
        await manager.close()


async def test_ast_mutation_uses_the_same_workspace_as_its_guard(tmp_path: Path, monkeypatch) -> None:
    instance = await materialize(tmp_path, ast=True)
    target = tmp_path / "ws" / "sample.py"
    target.write_text("before", encoding="utf-8")
    # Only replace the optional parser, not file access or the actual tool.
    monkeypatch.setattr("modex_agent.tools.ast.ast_replace.is_ast_available", lambda: True)
    monkeypatch.setattr("modex_agent.tools.ast.ast_replace.replace_in_file", lambda *args: ("after", 1))
    try:
        assert instance.pipeline is not None and instance.pipeline.tool_manager is not None
        tool = instance.pipeline.tool_manager.get_tool("ast_grep_replace")
        assert tool is not None
        await tool.execute(path="sample.py", pattern="x", replacement="after", language="python", dry_run=False)
        assert target.read_text(encoding="utf-8") == "after"
    finally:
        await instance.stop()


@pytest.mark.parametrize("policy", [None, "workspace-write"])
async def test_host_bash_executes_through_tool_node(tmp_path: Path, policy: str | None) -> None:
    from dataclasses import replace

    instance = await materialize(tmp_path, policy)
    try:
        assert instance.pipeline is not None and instance.pipeline.tool_manager is not None
        builder = instance.pipeline._turn_runner.turn_context_builder
        assert builder is not None and builder.runtime_services is not None
        ctx = _make_graph_ctx(replace(builder.runtime_services, interceptors=instance.pipeline.interceptor_chain))
        ctx.agent_ctx.tool_manager = instance.pipeline.tool_manager
        call = ToolCall(tool_name="bash", arguments={"command": "echo scoped-host-ok"}, call_id="host")
        await ctx.agent_ctx.history.append(ChatMessage(role=MessageRole.ASSISTANT, tool_calls=[call]))
        await ToolNode(ToolExecutor()).run(ctx)
        result = ctx.state.tool_batches[-1].calls[0].result
        assert result is not None and result.error is None
        assert "scoped-host-ok" in str(result.content)
        assert ctx.state.approval is None
    finally:
        await instance.stop()


async def test_external_real_runner_records_limits_without_fake_classifier(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from modex_agent.agents.external.agent import ExternalAgent
    from modex_agent.agents.external.turn_runner import ExternalTurnRunner
    from modex_agent.core.agent import ExecutionStrategyKind, ProviderKind
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.memory.context import InMemoryContextManager
    from modex_agent.multi_agent.address import AgentAddress
    from modex_agent.multi_agent.descriptor import AgentDescriptor
    from modex_agent.multi_agent.execution_strategy import (
        ExecutionStrategy,
        ExecutionStrategyRegistry,
        SubagentAssembly,
    )
    from modex_agent.pipeline.pipeline import AgentPipeline
    from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry

    agent = MagicMock(spec=ExternalAgent)
    registry = TurnSessionRegistry()
    runner = ExternalTurnRunner(agent=agent, emitter_factory=None, output_adapter=MagicMock(),
                                registry=registry, safety=RuntimeSafetyPolicy())
    descriptor = AgentDescriptor(address=AgentAddress(name="scout"), execution_strategy=ExecutionStrategyKind.EXTERNAL)
    product = AgentInstance(descriptor=descriptor, context_manager=InMemoryContextManager(),
                            pipeline=AgentPipeline(agent=agent, turn_runner=runner, input_adapter=MagicMock(),
                                                   output_adapter=MagicMock(), registry=registry))

    class Strategy(ExecutionStrategy):
        @property
        def name(self):
            return "external"

        async def assemble_main(self, ctx):
            raise AssertionError("not the main path")

        def validate_pool_spec(self, pool):
            pass

        async def assemble_sub(self, ctx, deps):
            return SubagentAssembly(descriptor=descriptor, instance=product)

    deps = await _deps(tmp_path, _pool_assembly(tmp_path, AgentSpec(name="main")), tmp_path)
    deps.strategy_registry = ExecutionStrategyRegistry()
    deps.strategy_registry.register(Strategy())
    template = _compiled_template("scout", execution_strategy=ExecutionStrategyKind.EXTERNAL, provider_kind=ProviderKind.OPENCODE)
    instance = await template.materialize(None, "external", deps)
    assert instance is product
    assert runner.turn_context_builder is None
    snapshot = instance.delegation
    assert snapshot is not None
    assert not snapshot.file_guards
    assert snapshot.backend is None and snapshot.enforcement is None
    assert snapshot.policy is SandboxPolicy.WORKSPACE_WRITE
    assert snapshot.envelope == (tmp_path,)
    assert snapshot.limitations


@pytest.mark.parametrize("tool", ["read", "write"])
async def test_outside_file_is_error_without_approval(tmp_path: Path, tool: str) -> None:
    instance = await materialize(tmp_path, "workspace-write")
    try:
        assert instance.pipeline is not None
        builder = instance.pipeline._turn_runner.turn_context_builder
        assert builder is not None
        services = builder.runtime_services
        assert services is not None and services.delegation is not None
        ctx = _make_graph_ctx(services)
        outside = tmp_path / "outside.txt"
        call = ToolCall(tool_name=tool, arguments={"path": str(outside), "content": "x"}, call_id="outside")
        await ctx.agent_ctx.history.append(ChatMessage(role=MessageRole.ASSISTANT, content="", tool_calls=[call]))
        await ToolNode(ToolExecutor()).run(ctx)
        results = [m for m in await ctx.agent_ctx.history.to_list() if m.role is MessageRole.TOOL]
        assert len(results) == 1
        batch = ctx.state.tool_batches[-1]
        assert batch.calls[0].result is not None
        assert batch.calls[0].result.error is not None
        assert isinstance(results[0].content, str)
        assert ctx.state.approval is None
        assert not outside.exists()
        assert str(outside) in results[0].content
        for root in services.delegation.envelope:
            assert str(root) in results[0].content
    finally:
        await instance.stop()
