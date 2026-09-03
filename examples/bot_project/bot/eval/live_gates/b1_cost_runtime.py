"""Live service probes and one-turn dispatch for the B1 cost gate."""

from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path
from typing import Final
from urllib.parse import urlparse
from uuid import uuid4

import anyio
import httpx
from pydantic import BaseModel, ConfigDict

from bot.eval.agent_harness import build_trace_only_services, static_system_prompt
from bot.eval.evalenv import LangfuseCredentials
from bot.eval.experiment_runner import _NoopEmitter
from bot.service.pool.declaration import boot_scope_spec
from bot.workspace.handle import WorkspaceHandle, WorkspaceHandleRootProvider
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.emitter import StopReason
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage, MessageRole
from modex_agent.core.session_id import SessionInfo
from modex_agent.ioc.configs.llm import LLMConfig
from modex_agent.ioc.factories.llm import create_llm_provider
from modex_agent.memory.context import ContextState
from modex_agent.memory.history import ListMessageHistory
from modex_agent.plugins.assembly.single_agent import (
    SingleAgentAssembled,
    SingleAgentInfra,
    assemble_declared_single_agent,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.overlay import AgentOverlay, PoolOverlay, ScopeOverlay, apply_scope_overlay
from modex_agent.tools.presets import ToolPreset
from modex_agent.trace.langfuse_query import _MAX_PAGES, LangfuseClient, ScoreReadData

HEALTH_TIMEOUT_SECONDS: Final = 2.0
SCORE_POLL_ATTEMPTS: Final = 10
SCORE_POLL_INTERVAL_SECONDS: Final = 1.0
SCORE_FIELDS: Final = "core,details,subject"
_BOT_PROJECT: Final = Path(__file__).resolve().parents[3]
_REACT_HARNESS_DECLARATION: Final = (
    _BOT_PROJECT / "config" / "scopes" / "eval" / "agents" / "react-harness.yml"
)
REQUIRED_SCORE_NAMES: Final = frozenset(
    {
        "tool_success_rate",
        "tool_call_count",
        "error_tool_count",
        "iteration_count",
        "llm_call_count",
        "total_input_tokens",
        "total_output_tokens",
        "total_reasoning_tokens",
        "api_latency_avg_s",
        "cache_hit_rate",
        "response_token_ratio",
        "has_reasoning",
        "cost_usd",
    }
)


class GateError(RuntimeError):
    """A bounded B1 contract failure."""


class PreflightEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    langfuse_health: bool
    collector_port: bool
    missing: list[str]


class TurnDispatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    trace_id: str
    output: str


def _probe_langfuse_health(host: str) -> bool:
    try:
        with httpx.Client(timeout=HEALTH_TIMEOUT_SECONDS) as client:
            response = client.get(f"{host.rstrip('/')}/api/public/health")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _probe_collector(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    if parsed.hostname is None:
        return False
    try:
        with socket.create_connection(
            (parsed.hostname, parsed.port or 4318),
            timeout=HEALTH_TIMEOUT_SECONDS,
        ):
            return True
    except OSError:
        return False


def run_preflight() -> PreflightEvidence:
    required_env = (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASIC_AUTH",
        "TEST_LLM_MODEL",
        "TEST_LLM_API_KEY",
        "TEST_LLM_BASE_URL",
    )
    missing = [name for name in required_env if not os.environ.get(name)]
    langfuse_host = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
    collector_endpoint = os.environ.get(
        "OTEL_TRACES_ENDPOINT",
        "http://localhost:4318/v1/traces",
    )
    health_ok = _probe_langfuse_health(langfuse_host)
    collector_ok = _probe_collector(collector_endpoint)
    if not health_ok:
        missing.append("langfuse:/api/public/health")
    if not collector_ok:
        missing.append("collector:4318")
    return PreflightEvidence(
        langfuse_health=health_ok,
        collector_port=collector_ok,
        missing=missing,
    )


async def dispatch_turn() -> TurnDispatch:
    provider = create_llm_provider(
        LLMConfig(
            model=os.environ["TEST_LLM_MODEL"],
            api_key=os.environ["TEST_LLM_API_KEY"],
            base_url=os.environ["TEST_LLM_BASE_URL"],
            temperature=0.0,
            max_output_tokens=64,
        )
    )
    session = SessionInfo(
        session_id=f"b1-cost-smoke.{uuid4().hex}.react",
        agent_name="react",
    )
    identity = TurnIdentity(agent_id="react", session=session, turn_id="turn-1")
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    message_history = ListMessageHistory()
    await message_history.append(
        ChatMessage(role=MessageRole.USER, content="Reply with exactly: b1-cost-smoke")
    )
    with tempfile.TemporaryDirectory(prefix="modex-b1-cost-smoke-") as raw_trace_dir:
        runtime_dir = Path(raw_trace_dir)
        services = build_trace_only_services(
            runtime_dir,
            model=os.environ["TEST_LLM_MODEL"],
        )
        component_registry = ComponentRegistry()
        with PluginRegistrationContext(component_registry) as registration:
            DefaultPlugin().register(registration)
        declaration = load_scope_declaration(_REACT_HARNESS_DECLARATION)
        overlay = ScopeOverlay(
            pools={
                "react-harness": PoolOverlay(
                    agents={"react": AgentOverlay(toolset=ToolPreset.NONE)}
                )
            }
        )
        scope_boot = boot_scope_spec(
            apply_scope_overlay(declaration, overlay),
            project_dir=_BOT_PROJECT,
            data_dir=runtime_dir,
            graphs_dirs=(),
            default_llm_provider="default",
            registry=component_registry,
        )
        hooks = (
            tuple(spec.hook for spec in services.hooks.hook_specs)
            if services.hooks is not None
            else ()
        )
        assembled: SingleAgentAssembled | None = None
        try:
            assembled = await assemble_declared_single_agent(
                scope_boot.compilation.agents[0],
                SingleAgentInfra(
                    llm_provider=provider,
                    safety=RuntimeSafetyPolicy(),
                    root_provider=WorkspaceHandleRootProvider(
                        WorkspaceHandle(target=Path.cwd(), data_root=runtime_dir)
                    ),
                    extra_hooks=hooks,
                    governance_enabled=False,
                ),
                project_dir=_BOT_PROJECT,
                data_dir=runtime_dir,
                component_registry=component_registry,
            )
            pipeline = assembled.instance.pipeline
            assert pipeline is not None
            builder = pipeline._turn_context_builder
            assert builder is not None
            context, _ = builder.build_runtime_and_context(
                session,
                ContextState(
                    system_prompt=static_system_prompt(
                        "Return a concise direct answer. Do not call tools."
                    ),
                    history=message_history,
                ),
                assembled.context_manager,
                workspace=Path.cwd(),
            )
            context.max_iterations = 1
            context.identity = identity
            context.runtime = AgentRuntime(
                services=AgentRuntimeServices(
                    hooks=pipeline.hook_runner,
                    trace_store=services.trace_store,
                ),
                state=state,
            )
            result = await pipeline.agent.run(context, _NoopEmitter())
        finally:
            if assembled is not None:
                await assembled.close()
            elif services.hooks is not None:
                await services.hooks.aclose()
            if services.trace_store is not None:
                services.trace_store.close()

    if result.stop_reason is not StopReason.COMPLETED:
        message = f"agent turn stopped with {result.stop_reason.value}: {result.error or ''}"
        raise GateError(message)
    trace_id = state.custom.get(TurnCustomKey.TRACE_ID)
    if not isinstance(trace_id, str):
        raise GateError("agent turn did not produce a trace id")
    return TurnDispatch(
        session_id=session.session_id,
        trace_id=trace_id,
        output=result.content or "",
    )


async def read_trace_scores(trace_id: str) -> list[ScoreReadData]:
    credentials = LangfuseCredentials.from_env()
    if credentials is None:
        raise GateError("Langfuse credentials are required to read B1 scores")
    client = LangfuseClient(
        credentials.host or "http://localhost:3000",
        credentials.public_key,
        credentials.secret_key,
    )
    latest: list[ScoreReadData] = []
    try:
        for attempt in range(SCORE_POLL_ATTEMPTS):
            latest = []
            cursor: str | None = None
            for _ in range(_MAX_PAGES):
                page, cursor = await client.get_scores(
                    fields=SCORE_FIELDS,
                    trace_id=trace_id,
                    limit=100,
                    cursor=cursor,
                )
                latest.extend(score for score in page if score.name in REQUIRED_SCORE_NAMES)
                if cursor is None:
                    break
            if {score.name for score in latest} == REQUIRED_SCORE_NAMES:
                return latest
            if attempt + 1 < SCORE_POLL_ATTEMPTS:
                await anyio.sleep(SCORE_POLL_INTERVAL_SECONDS)
    finally:
        await client.close()
    return latest
