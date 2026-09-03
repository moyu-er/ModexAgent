"""The FW-bundled ``tracing`` capability — the span-hook family convergence.

Covers (mirroring ``test_todo_capability.py`` structure, ADR-0047):

- **Protocol shape** — ``TracingCapability`` is a pure opt-in bundle
  contributing all 7 span-hook names unconditionally; the tier filter is
  ``bind``'s business (the binding-vouching mechanism is the tier knob).
- **Bind tier matrix** — MINIMAL/STANDARD/FULL vouch exactly the tier's
  subset; ``trace_backend=off`` vouches NOTHING (tracing dark, supply
  skipped via the None path).
- **Supply** — adopts the caller-carried store
  (``pool_data.trace_store`` — the harbor trial seam) when present, else
  builds via ``build_trace_stores`` rooted at ``runtime_dir/"trace"``;
  OFF → ``None`` (no supply entry). ``stop()`` closes ONLY the
  self-built store (the adopted store's lifecycle stays with its owner).
- **Assemble** — builds the vouched hook instances through the family's
  single construction authority (:func:`build_trace_hooks`), ONE
  ``TraceSessionState`` shared across every instance, ordered
  root-first/tool-before-handoff; the artifacts carry the ordered tuple.
- **Factory resolution** — the 7 HOOK-slot factories resolve the
  per-agent wiring artifacts from ``ctx.capability_wirings["tracing"]``
  and each pick their instance by name; a missing wiring (capability not
  effective) raises loudly.
- **Roster end-to-end** — ``capabilities: {tracing: {trace_spans: …}}``
  compiles the vouched names into merged_hooks; ``hooks: [-trace_tool]``
  removes one (component-level veto through the standard merge).
- **Code-wired death** — the retired ``DefaultAgentFactory`` trace
  injection is gone (grep-pinned).
- **BIZ fallback** — the global-observability → effective-capability
  injection reproduces the pre-migration hook set on the shipped tree's
  native agents.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

pytest.importorskip("aiohttp")  # transitive: bot.service → web_ui_service → aiohttp

from modex_agent.hook.abc import HookSpec
from modex_agent.ioc.configs.observability import (
    ObservabilityConfig,
    TraceBackend,
    TraceSpanMode,
)
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps
from modex_agent.plugins.capability import (
    CapabilityBinding,
    FinalRosterView,
    PoolSupplyAgentEntry,
    PoolSupplyView,
    TreePositionView,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.capabilities.tracing import (
    TraceSupply,
    TracingCapability,
    TracingCapabilityConfig,
    require_tracing_supply,
)
from modex_agent.plugins.defaults.hooks import (
    TraceAgentStartHookFactory,
    TraceApprovalHookFactory,
    TraceChatHookFactory,
    TraceHandoffHookFactory,
    TraceIterationHookFactory,
    TraceRootHookFactory,
    TraceToolHookFactory,
)
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.trace.agent_start_hook import AgentStartSpanHook
from modex_agent.trace.approval_span_hook import ApprovalSpanHook
from modex_agent.trace.chat_span_hook import ChatSpanHook
from modex_agent.trace.factory import build_trace_hooks
from modex_agent.trace.handoff_span_hook import HandoffSpanHook
from modex_agent.trace.iteration_span_hook import IterationSpanHook
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.root_span_hook import RootSpanHook
from modex_agent.trace.score_injector import L2ScoreInjector
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.trace.tool_span_hook import ToolSpanHook
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    pass

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOT_PROJECT = _REPO_ROOT / "examples" / "bot_project"

_ALL_HOOK_NAMES = (
    "trace_root",
    "trace_chat",
    "trace_tool",
    "trace_handoff",
    "trace_approval",
    "trace_agent_start",
    "trace_iteration",
)


def _registry() -> ComponentRegistry:
    """A registry carrying the FW defaults (the tracing capability lives
    in DefaultPlugin — the production registration face)."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _tree_view() -> TreePositionView:
    return TreePositionView(
        pool_name="p", agent_name="root", is_root=True, parent=None, children=(), peers=()
    )


def _workspace_ctx(root: Path | None = None) -> WorkspaceContext:
    target = root if root is not None else Path("./test_tracing_capability_ws")
    return WorkspaceContext(
        target=target, paths=WorkspacePaths(root=target / ".modex"), is_home=False
    )


def _config(**overrides: object) -> TracingCapabilityConfig:
    return TracingCapabilityConfig.model_validate(overrides)


def _view(
    pool_name: str = "p",
    *,
    entry_config: dict[str, object] | None = None,
    entries: tuple[PoolSupplyAgentEntry, ...] | None = None,
    **kwargs: object,
) -> PoolSupplyView:
    return PoolSupplyView(
        pool_name=pool_name,
        entries=entries
        if entries is not None
        else (PoolSupplyAgentEntry(agent_name="root", config=dict(entry_config or {})),),
        **kwargs,  # type: ignore[arg-type]
    )


def _compile_hooks(agent: AgentSpec) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compile one agent; return (final tools, merged hooks)."""
    spec = ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=[agent]))
    compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())
    compiled = compilation.agents[0]
    return tuple(compiled.spec.tools), tuple(compiled.spec.hooks)


def _final(hooks: tuple[str, ...] = _ALL_HOOK_NAMES) -> FinalRosterView:
    """The C2 input shape the real compiler produces: contributed hook
    names enter merged_hooks at C1, so a clean (veto-less) final roster
    carries all seven."""
    return FinalRosterView(tools=(), hooks=hooks)


def _store(tmp_path: Path) -> OtelSpanTraceStore:
    return OtelSpanTraceStore(base_dir=tmp_path / "trace", backend=TraceBackend.FILE)


# ─── Protocol shape ─────────────────────────────────────────────────────────


class TestProtocolShape:
    def test_registered_in_capability_slot(self) -> None:
        registry = _registry()
        assert registry.resolve(ComponentSlot.CAPABILITY, "tracing") is not None
        assert isinstance(registry.resolve_capability("tracing"), TracingCapability)

    def test_seven_hook_factories_registered(self) -> None:
        registry = _registry()
        for name in _ALL_HOOK_NAMES:
            assert registry.resolve(ComponentSlot.HOOK, name) is not None

    def test_applies_default_false(self) -> None:
        assert TracingCapability().applies(MagicMock()) is False

    def test_contribute_shape_all_seven_unconditionally(self) -> None:
        for backend in TraceBackend:
            for tier in TraceSpanMode:
                contribution = TracingCapability().contribute(
                    _tree_view(), _config(trace_backend=backend.value, trace_spans=tier.value)
                )
                assert contribution.hooks == _ALL_HOOK_NAMES
                assert contribution.tools == ()
                assert contribution.sections == ()
                assert contribution.tool_replacements == ()

    def test_config_mirrors_agent_declarable_subset(self) -> None:
        config = _config(
            trace_spans="full",
            trace_backend="otel_http",
            prompt_capture="full",
            retain_reasoning_content=False,
            capture_tools=True,
            environment="production",
            version="v2",
            tags=["a", "b"],
            eval_score_injection=True,
            eval_ingestion_url="https://lf.example.invalid/api/public/ingestion",
        )
        assert config.trace_spans is TraceSpanMode.FULL
        assert config.trace_backend is TraceBackend.OTEL_HTTP
        assert config.environment == "production"
        assert config.tags == ["a", "b"]

    def test_config_defaults_match_observability_defaults(self) -> None:
        config = TracingCapabilityConfig()
        assert config.trace_spans is TraceSpanMode.STANDARD
        assert config.trace_backend is TraceBackend.FILE
        assert config.prompt_capture == ObservabilityConfig().prompt_capture.value

    def test_config_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValidationError):
            TracingCapabilityConfig.model_validate({"run_logging": True})

    def test_config_from_observability_projects_the_subset(self) -> None:
        obs = ObservabilityConfig(
            trace_spans=TraceSpanMode.FULL,
            trace_backend=TraceBackend.OTEL_HTTP,
            environment="production",
            tags=["x"],
        )
        config = TracingCapabilityConfig.from_observability(obs)
        assert config.trace_spans is TraceSpanMode.FULL
        assert config.trace_backend is TraceBackend.OTEL_HTTP
        assert config.environment == "production"
        assert config.tags == ["x"]


# ─── Bind tier matrix ───────────────────────────────────────────────────────


class TestBindTierMatrix:
    def test_minimal_vouches_root_only(self) -> None:
        binding = TracingCapability().bind(
            _tree_view(), _config(trace_spans="minimal", trace_backend="file"), _final()
        )
        assert binding.hooks == ("trace_root",)

    def test_standard_vouches_five(self) -> None:
        binding = TracingCapability().bind(
            _tree_view(), _config(trace_spans="standard", trace_backend="file"), _final()
        )
        assert binding.hooks == (
            "trace_root",
            "trace_chat",
            "trace_tool",
            "trace_handoff",
            "trace_approval",
        )

    def test_full_vouches_all_seven(self) -> None:
        binding = TracingCapability().bind(
            _tree_view(), _config(trace_spans="full", trace_backend="file"), _final()
        )
        assert binding.hooks == _ALL_HOOK_NAMES

    def test_off_backend_vouches_nothing(self) -> None:
        """``trace_backend=off`` is tracing-dark: no vouched hooks (the
        contributed names die at the binding gate) and no supply entry."""
        binding = TracingCapability().bind(
            _tree_view(), _config(trace_spans="full", trace_backend="off"), _final()
        )
        assert binding.hooks == ()

    def test_off_backend_supply_returns_none(self) -> None:
        assert TracingCapability().supply(_view(entry_config={"trace_backend": "off"})) is None

    def test_no_anchor_error_on_vetoed_hook(self) -> None:
        """A component-vetoed hook (``hooks: [-trace_tool]``) is silently
        dropped from the vouch set — the binding never raises for a
        missing hook name (veto semantics preserved)."""
        final = _final(
            ("trace_root", "trace_chat", "trace_handoff", "trace_approval", "trace_tool_x")
        )
        binding = TracingCapability().bind(
            _tree_view(), _config(trace_spans="standard", trace_backend="file"), final
        )
        assert "trace_tool" not in binding.hooks
        assert "trace_root" in binding.hooks


# ─── Supply ─────────────────────────────────────────────────────────────────


class TestSupply:
    def test_supply_adopts_caller_carried_store(self, tmp_path: Path) -> None:
        """The pool_data.trace_store carrier (the harbor trial seam): the
        supply adopts the store instance; it never builds a second one."""
        carried = _store(tmp_path)
        supply = TracingCapability().supply(
            _view(entry_config={"trace_backend": "file"}, trace_store=carried)
        )
        assert isinstance(supply, TraceSupply)
        assert supply.store is carried

    def test_supply_builds_from_runtime_dir_when_no_carried_store(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime_state" / "p"
        supply = TracingCapability().supply(
            _view(entry_config={"trace_backend": "file"}, runtime_dir=runtime_dir)
        )
        assert isinstance(supply, TraceSupply)
        assert supply.store is not None
        assert supply.store._base_dir == runtime_dir / "trace"  # noqa: SLF001

    def test_supply_builds_from_data_dir_fallback(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "ws"
        supply = TracingCapability().supply(
            _view(entry_config={"trace_backend": "file"}, data_dir=data_dir)
        )
        assert isinstance(supply, TraceSupply)
        assert supply.store is not None  # noqa: SLF001
        assert supply.store._base_dir == data_dir / "runtime_state" / "p" / "trace"  # noqa: SLF001

    def test_supply_raises_when_no_path_no_store(self) -> None:
        with pytest.raises(ValueError, match="tracing"):
            TracingCapability().supply(_view(entry_config={"trace_backend": "file"}))

    def test_supply_builds_score_injector_when_configured(self, tmp_path: Path) -> None:
        supply = TracingCapability().supply(
            _view(
                trace_store=_store(tmp_path),
                entries=(
                    PoolSupplyAgentEntry(
                        agent_name="root",
                        config={
                            "trace_backend": "otel_http",
                            "eval_score_injection": True,
                            "otel_endpoint": "https://lf.example.invalid/v1/traces",
                            "eval_ingestion_url": "https://lf.example.invalid/api/public/ingestion",
                        },
                    ),
                ),
            )
        )
        assert isinstance(supply, TraceSupply)
        assert isinstance(supply.score_injector, L2ScoreInjector)
        assert (
            supply.score_injector._ingestion_url  # noqa: SLF001
            == "https://lf.example.invalid/api/public/ingestion"
        )

    def test_supply_derives_ingestion_url_from_endpoint(self, tmp_path: Path) -> None:
        supply = TracingCapability().supply(
            _view(
                trace_store=_store(tmp_path),
                entries=(
                    PoolSupplyAgentEntry(
                        agent_name="root",
                        config={
                            "trace_backend": "otel_http",
                            "eval_score_injection": True,
                            "otel_endpoint": "https://lf.example.invalid/api/public/otel/v1/traces",
                        },
                    ),
                ),
            )
        )
        assert isinstance(supply, TraceSupply)
        assert supply.score_injector is not None
        assert (
            supply.score_injector._ingestion_url  # noqa: SLF001
            == "https://lf.example.invalid/api/public/ingestion"
        )

    def test_supply_no_injector_when_eval_injection_off(self, tmp_path: Path) -> None:
        supply = TracingCapability().supply(
            _view(entry_config={"trace_backend": "otel_http"}, trace_store=_store(tmp_path))
        )
        assert isinstance(supply, TraceSupply)
        assert supply.score_injector is None

    def test_supply_no_injector_without_endpoint(self, tmp_path: Path) -> None:
        supply = TracingCapability().supply(
            _view(
                trace_store=_store(tmp_path),
                entries=(
                    PoolSupplyAgentEntry(
                        agent_name="root",
                        config={
                            "trace_backend": "file",
                            "eval_score_injection": True,
                        },
                    ),
                ),
            )
        )
        assert isinstance(supply, TraceSupply)
        assert supply.score_injector is None

    async def test_stop_closes_self_built_store_not_adopted(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime_state" / "p"
        supply = TracingCapability().supply(
            _view(entry_config={"trace_backend": "file"}, runtime_dir=runtime_dir)
        )
        assert isinstance(supply, TraceSupply)
        built = supply.store
        assert built is not None
        supply.store._closed = True  # noqa: SLF001 — pretend-close; stop() must not double-close

        carried = _store(tmp_path)
        adopted = TracingCapability().supply(
            _view(entry_config={"trace_backend": "file"}, trace_store=carried)
        )
        assert isinstance(adopted, TraceSupply)
        await adopted.stop()
        assert carried._closed is False  # noqa: SLF001 — adopted store stays with its owner

    async def test_stop_closes_self_built_store(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime_state" / "p"
        supply = TracingCapability().supply(
            _view(entry_config={"trace_backend": "file"}, runtime_dir=runtime_dir)
        )
        assert isinstance(supply, TraceSupply)
        built = supply.store
        assert built is not None
        await supply.stop()
        assert built._closed is True  # noqa: SLF001


# ─── Assemble + factory resolution ──────────────────────────────────────────


def _supply_ctx(tmp_path: Path, config: TracingCapabilityConfig | None = None) -> AgentContext:
    """A full-chain ctx carrying the pool's tracing supply + llm_defaults."""
    supply = TraceSupply(store=_store(tmp_path))
    return AgentContext(
        registry=_registry(),
        workspace_ctx=_workspace_ctx(),
        pool_runtime=PoolRuntimeDeps(capability_supply={"tracing": supply}),
        agent_name="root",
        spec=MagicMock(),
        llm_defaults=MagicMock(model="prov/model-x", temperature=0.7, max_output_tokens=4096),
    )


def _binding(config: TracingCapabilityConfig) -> CapabilityBinding:
    return TracingCapability().bind(_tree_view(), config, _final())


class TestAssemble:
    async def test_full_tier_builds_seven_instances_one_session(self, tmp_path: Path) -> None:
        wiring = await TracingCapability().assemble(
            _binding(_config(trace_spans="full", trace_backend="file")), _supply_ctx(tmp_path)
        )
        hooks = wiring.artifacts["hooks"]
        assert isinstance(hooks, tuple)
        assert [type(h) for h in hooks] == [
            RootSpanHook,
            ChatSpanHook,
            ToolSpanHook,
            HandoffSpanHook,
            ApprovalSpanHook,
            AgentStartSpanHook,
            IterationSpanHook,
        ]
        sessions = {h._session for h in hooks}  # noqa: SLF001
        assert len(sessions) == 1
        assert isinstance(next(iter(sessions)), TraceSessionState)

    async def test_standard_tier_builds_five(self, tmp_path: Path) -> None:
        wiring = await TracingCapability().assemble(
            _binding(_config(trace_spans="standard", trace_backend="file")),
            _supply_ctx(tmp_path),
        )
        hooks = wiring.artifacts["hooks"]
        assert [type(h) for h in hooks] == [  # type: ignore[attr-defined]
            RootSpanHook,
            ChatSpanHook,
            ToolSpanHook,
            HandoffSpanHook,
            ApprovalSpanHook,
        ]

    async def test_off_binding_builds_nothing(self, tmp_path: Path) -> None:
        wiring = await TracingCapability().assemble(
            _binding(_config(trace_spans="full", trace_backend="off")), _supply_ctx(tmp_path)
        )
        assert wiring.artifacts == {}

    async def test_stores_and_injector_threaded_from_supply(self, tmp_path: Path) -> None:
        injector = L2ScoreInjector(
            ingestion_url="https://lf.example.invalid/api/public/ingestion", headers={}
        )
        store = _store(tmp_path)
        ctx = _supply_ctx(tmp_path)
        ctx = AgentContext(
            registry=ctx.registry,
            workspace_ctx=ctx.workspace_ctx,
            pool_runtime=PoolRuntimeDeps(
                capability_supply={"tracing": TraceSupply(store=store, score_injector=injector)}
            ),
            agent_name="root",
            spec=MagicMock(),
            llm_defaults=MagicMock(model="prov/model-x"),
        )
        wiring = await TracingCapability().assemble(
            _binding(_config(trace_spans="standard", trace_backend="file")), ctx
        )
        root = wiring.artifacts["hooks"][0]
        assert root._store is store  # noqa: SLF001
        assert root._score_injector is injector  # noqa: SLF001

    async def test_model_and_provider_derived_from_chain(self, tmp_path: Path) -> None:
        ctx = AgentContext(
            registry=_registry(),
            workspace_ctx=_workspace_ctx(),
            pool_runtime=PoolRuntimeDeps(
                capability_supply={"tracing": TraceSupply(store=_store(tmp_path))}
            ),
            agent_name="root",
            spec=MagicMock(),
            llm_defaults=MagicMock(model="openai/gpt-5", temperature=0.7, max_output_tokens=4096),
        )
        wiring = await TracingCapability().assemble(
            _binding(_config(trace_spans="minimal", trace_backend="file")), ctx
        )
        root = wiring.artifacts["hooks"][0]
        assert root._model == "openai/gpt-5"  # noqa: SLF001
        assert root._provider_name == "openai"  # noqa: SLF001
        assert root._request_params == {"temperature": 0.7, "max_tokens": 4096}  # noqa: SLF001

    async def test_assemble_requires_supply(self, tmp_path: Path) -> None:
        ctx = AgentContext(
            registry=_registry(),
            workspace_ctx=_workspace_ctx(),
            pool_runtime=PoolRuntimeDeps(),
            agent_name="root",
            spec=MagicMock(),
            llm_defaults=MagicMock(model=None),
        )
        with pytest.raises(ValueError, match="tracing"):
            await TracingCapability().assemble(
                _binding(_config(trace_spans="standard", trace_backend="file")), ctx
            )


class TestFactoryResolution:
    async def test_factories_pick_instance_by_name(self, tmp_path: Path) -> None:
        capability = TracingCapability()
        wiring = await capability.assemble(
            _binding(_config(trace_spans="full", trace_backend="file")), _supply_ctx(tmp_path)
        )
        ctx = AgentContext(
            registry=_registry(),
            workspace_ctx=_workspace_ctx(),
            pool_runtime=PoolRuntimeDeps(),
            agent_name="root",
            spec=MagicMock(),
            llm_defaults=MagicMock(model=None),
            capability_wirings={"tracing": wiring},
        )
        pairs = [
            (TraceRootHookFactory(), "trace_root", RootSpanHook),
            (TraceChatHookFactory(), "trace_chat", ChatSpanHook),
            (TraceToolHookFactory(), "trace_tool", ToolSpanHook),
            (TraceHandoffHookFactory(), "trace_handoff", HandoffSpanHook),
            (TraceApprovalHookFactory(), "trace_approval", ApprovalSpanHook),
            (TraceAgentStartHookFactory(), "trace_agent_start", AgentStartSpanHook),
            (TraceIterationHookFactory(), "trace_iteration", IterationSpanHook),
        ]
        for factory, name, expected_type in pairs:
            hook = await factory.create(factory.config_model(), ctx)
            assert isinstance(hook, expected_type)
            assert wiring.artifacts["by_name"][name] is hook

    async def test_factory_raises_loud_when_capability_not_effective(self) -> None:
        ctx = AgentContext(
            registry=_registry(),
            workspace_ctx=_workspace_ctx(),
            pool_runtime=PoolRuntimeDeps(),
            agent_name="root",
            spec=MagicMock(),
            llm_defaults=MagicMock(model=None),
        )
        with pytest.raises(ValueError, match="tracing"):
            await TraceRootHookFactory().create(TraceRootHookFactory.config_model(), ctx)

    async def test_factory_raises_when_tier_dropped_the_hook(self, tmp_path: Path) -> None:
        """A MINIMAL-tier wiring carries only trace_root — the other six
        factories raise loudly when their instance is absent (the tier
        filter and the roster agree, so this is defense-in-depth)."""
        capability = TracingCapability()
        wiring = await capability.assemble(
            _binding(_config(trace_spans="minimal", trace_backend="file")),
            _supply_ctx(tmp_path),
        )
        ctx = AgentContext(
            registry=_registry(),
            workspace_ctx=_workspace_ctx(),
            pool_runtime=PoolRuntimeDeps(),
            agent_name="root",
            spec=MagicMock(),
            llm_defaults=MagicMock(model=None),
            capability_wirings={"tracing": wiring},
        )
        with pytest.raises(ValueError, match="trace_tool"):
            await TraceToolHookFactory().create(TraceToolHookFactory.config_model(), ctx)

    def test_factories_declare_negative_priority(self) -> None:
        # The retired registration order put the trace family FIRST on the
        # runner; the priority reproduces execution-order parity through
        # the roster (HookRunner sorts by priority, stable).
        assert TraceRootHookFactory.priority == -500
        assert TraceChatHookFactory().priority == -500
        assert TraceToolHookFactory.priority == -500
        assert TraceHandoffHookFactory.priority == -500
        assert TraceApprovalHookFactory.priority == -500
        assert TraceAgentStartHookFactory.priority == -500
        assert TraceIterationHookFactory.priority == -500


# ─── Roster end-to-end (compile) ────────────────────────────────────────────


class TestRosterEndToEnd:
    def test_standard_tier_compiles_exactly_five_vouched_names(self) -> None:
        tools, hooks = _compile_hooks(
            AgentSpec(name="main", capabilities={"tracing": {"trace_spans": "standard"}})
        )
        assert "trace_root" in hooks
        assert "trace_chat" in hooks
        assert "trace_tool" in hooks
        assert "trace_handoff" in hooks
        assert "trace_approval" in hooks
        assert "trace_agent_start" not in hooks
        assert "trace_iteration" not in hooks
        assert tools == () or tools  # tools untouched by this capability

    def test_full_tier_compiles_all_seven(self) -> None:
        _, hooks = _compile_hooks(
            AgentSpec(name="main", capabilities={"tracing": {"trace_spans": "full"}})
        )
        assert all(name in hooks for name in _ALL_HOOK_NAMES)

    def test_hook_veto_removes_one(self) -> None:
        _, hooks = _compile_hooks(
            AgentSpec(
                name="main",
                capabilities={"tracing": {"trace_spans": "standard"}},
                hooks=["-trace_tool"],
            )
        )
        assert "trace_tool" not in hooks
        assert "trace_root" in hooks
        assert "trace_chat" in hooks

    def test_capability_false_disables_whole_family(self) -> None:
        _, hooks = _compile_hooks(AgentSpec(name="main", capabilities={"tracing": False}))
        assert all(name not in hooks for name in _ALL_HOOK_NAMES)

    def test_off_backend_compiles_no_hooks(self) -> None:
        _, hooks = _compile_hooks(
            AgentSpec(
                name="main",
                capabilities={"tracing": {"trace_backend": "off", "trace_spans": "full"}},
            )
        )
        assert all(name not in hooks for name in _ALL_HOOK_NAMES)

    def test_binding_payload_carries_no_sections(self) -> None:
        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    # skills auto-applies to every native agent (plan
                    # §11.3) — veto it so the tracing binding is the
                    # only compiled capability here.
                    AgentSpec(
                        name="main",
                        capabilities={"tracing": {}, "skills": False},
                    )
                ],
            ),
        )
        compilation = compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_registry())
        assert len(compilation.agents[0].spec.capabilities) == 1
        binding = compilation.agents[0].spec.capabilities[0].binding
        assert binding.active_sections == ()


# ─── Supply read helper ─────────────────────────────────────────────────────


class TestRequireTracingSupply:
    def test_returns_supply(self, tmp_path: Path) -> None:
        supply = TraceSupply(store=_store(tmp_path))
        runtime = PoolRuntimeDeps(capability_supply={"tracing": supply})
        assert require_tracing_supply(runtime) is supply

    def test_raises_on_missing(self) -> None:
        with pytest.raises(ValueError, match="tracing"):
            require_tracing_supply(PoolRuntimeDeps())

    def test_raises_on_wrong_type(self) -> None:
        runtime = PoolRuntimeDeps(capability_supply={"tracing": MagicMock()})
        with pytest.raises(ValueError, match="TraceSupply"):
            require_tracing_supply(runtime)

    def test_raises_on_none_runtime(self) -> None:
        with pytest.raises(ValueError, match="tracing"):
            require_tracing_supply(None)  # type: ignore[arg-type]


# ─── Code-wired death (grep pins) ───────────────────────────────────────────


class TestCodeWiredDeath:
    def test_factory_py_trace_injection_is_gone(self) -> None:
        source = (_REPO_ROOT / "src" / "modex_agent" / "multi_agent" / "factory.py").read_text(
            encoding="utf-8"
        )
        assert "build_trace_hooks" not in source
        assert "L2ScoreInjector" not in source

    def test_observability_config_param_is_gone(self) -> None:
        source = (_REPO_ROOT / "src" / "modex_agent" / "multi_agent" / "factory.py").read_text(
            encoding="utf-8"
        )
        assert "observability_config" not in source

    def test_grep_no_build_trace_hooks_in_multi_agent(self) -> None:
        result = subprocess.run(
            ["git", "grep", "-l", "build_trace_hooks", "--", "src/modex_agent/multi_agent/"],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        assert result.stdout == "", f"build_trace_hooks survived: {result.stdout}"

    def test_pool_data_no_longer_builds_stores(self) -> None:
        source = (_BOT_PROJECT / "bot" / "workspace" / "pool_data.py").read_text(encoding="utf-8")
        assert "build_trace_stores" not in source


# ─── BIZ fallback (global config → effective capability) ────────────────────


def _bot_project_on_path() -> None:
    if str(_BOT_PROJECT) not in sys.path:
        sys.path.insert(0, str(_BOT_PROJECT))


def _shipped_spec() -> object:
    _bot_project_on_path()
    from bot.service.pool.declaration import load_scope_declaration
    from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER  # noqa: F401

    return load_scope_declaration(_BOT_PROJECT / "config" / "scopes" / "bot.yml")


def _fallback_spec(observability: ObservabilityConfig) -> object:
    """Apply the BIZ fallback mutation (the production seam under test)."""
    _bot_project_on_path()
    from bot.service.pool.declaration import apply_tracing_fallback

    spec = _shipped_spec()
    return apply_tracing_fallback(spec, observability, registry=_registry())  # type: ignore[no-any-return]


class TestBizFallback:
    def test_off_injects_nothing(self) -> None:
        spec = _fallback_spec(ObservabilityConfig(trace_backend=TraceBackend.OFF))
        pools = spec.workspace.pools if spec.workspace is not None else [spec.pool]  # type: ignore[union-attr]
        for pool in pools:
            assert pool is not None
            for agent in pool.agents:
                if agent.execution_strategy == "external":
                    continue
                assert agent.capabilities is None or "tracing" not in agent.capabilities

    def test_registry_less_boot_injects_nothing(self) -> None:
        """T17's hermetic discipline: a registry-less compile (hand-built
        harness boots) cannot carry injected capabilities — the fallback
        is a no-op without the registry the capability protocol needs."""
        _bot_project_on_path()
        from bot.service.pool.declaration import apply_tracing_fallback

        spec = apply_tracing_fallback(
            _shipped_spec(),  # type: ignore[arg-type]
            ObservabilityConfig(trace_backend=TraceBackend.OTEL_HTTP),
        )
        pools = spec.workspace.pools if spec.workspace is not None else [spec.pool]  # type: ignore[union-attr]
        for pool in pools:
            assert pool is not None
            for agent in pool.agents:
                assert agent.capabilities is None or "tracing" not in agent.capabilities

    def test_on_injects_tracing_on_every_native_agent(self) -> None:
        spec = _fallback_spec(
            ObservabilityConfig(
                trace_backend=TraceBackend.OTEL_HTTP, trace_spans=TraceSpanMode.FULL
            )
        )
        pools = spec.workspace.pools if spec.workspace is not None else [spec.pool]  # type: ignore[union-attr]
        native_agents = [
            agent
            for pool in pools
            if pool is not None
            for agent in pool.agents
            if agent.execution_strategy != "external"
        ]
        assert native_agents
        for agent in native_agents:
            assert agent.capabilities is not None
            override = agent.capabilities.get("tracing")
            assert isinstance(override, dict)
            assert override["trace_spans"] == "full"
            assert override["trace_backend"] == "otel_http"

    def test_external_agents_never_get_the_capability(self) -> None:
        spec = _fallback_spec(ObservabilityConfig(trace_backend=TraceBackend.OTEL_HTTP))
        pools = spec.workspace.pools if spec.workspace is not None else [spec.pool]  # type: ignore[union-attr]
        for pool in pools:
            if pool is None:
                continue
            for agent in pool.agents:
                if agent.execution_strategy == "external":
                    assert not (agent.capabilities and "tracing" in agent.capabilities)

    def test_declared_override_wins_over_fallback(self) -> None:
        """An agent that already declares tracing keeps its own config —
        the fallback never overwrites a declaration."""
        from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec

        _bot_project_on_path()
        from bot.service.pool.declaration import apply_tracing_fallback

        spec = ScopeSpec(
            kind=ScopeKind.POOL,
            pool=PoolSpec(
                name="p",
                agents=[
                    AgentSpec(
                        name="main",
                        capabilities={"tracing": {"trace_spans": "minimal"}},
                    )
                ],
            ),
        )
        mutated = apply_tracing_fallback(
            spec,
            ObservabilityConfig(trace_backend=TraceBackend.OTEL_HTTP),
            registry=_registry(),
        )
        agent = mutated.pool.agents[0]  # type: ignore[union-attr]
        assert agent.capabilities is not None
        assert agent.capabilities["tracing"] == {"trace_spans": "minimal"}

    def test_fallback_produces_same_hook_set_as_pre_migration(self) -> None:
        """The parity assertion: the shipped tree's global-config tracing
        (``trace_spans: full`` via bot_config.yml default) produces, through
        the fallback + compile, exactly the 7 span hooks the retired
        code-wired path registered (build_trace_hooks FULL tier)."""
        spec = _fallback_spec(ObservabilityConfig(trace_spans=TraceSpanMode.FULL))
        registry = _registry()
        compilation = compile_scope(
            spec,  # type: ignore[arg-type]
            workspace_ctx=_workspace_ctx(),
            registry=registry,
        )
        pre_migration_types = {
            type(spec.hook)
            for spec in build_trace_hooks(
                ObservabilityConfig(trace_spans=TraceSpanMode.FULL),
                model=None,
                provider_name=None,
                request_params=None,
                score_injector=None,
                store=_store(Path(".")),
            )
        }
        registration_by_type = {
            RootSpanHook: "trace_root",
            ChatSpanHook: "trace_chat",
            ToolSpanHook: "trace_tool",
            HandoffSpanHook: "trace_handoff",
            ApprovalSpanHook: "trace_approval",
            AgentStartSpanHook: "trace_agent_start",
            IterationSpanHook: "trace_iteration",
        }
        pre_migration = {registration_by_type[t] for t in pre_migration_types}
        for compiled in compilation.agents:
            if compiled.spec.agent_type.value not in ("native_main", "native_sub"):
                continue
            roster_trace_hooks = {name for name in compiled.spec.hooks if name in _ALL_HOOK_NAMES}
            assert roster_trace_hooks == pre_migration, (
                f"{compiled.spec.agent_name}: {roster_trace_hooks} != {pre_migration}"
            )


# ─── Eval harnesses keep the direct-construction surface ────────────────────


class TestEvalHarnessSurface:
    def test_build_trace_hooks_survives_as_library_function(self) -> None:
        """(e) — agent_harness.py and harbor/entry.py construct trace
        hooks directly (hand-built harnesses, not the assembly path);
        build_trace_hooks stays as their (and the capability's) builder."""
        specs = build_trace_hooks(
            ObservabilityConfig(trace_spans=TraceSpanMode.STANDARD),
            model=None,
            provider_name=None,
            request_params=None,
            score_injector=None,
            store=_store(Path(".")),
        )
        assert len(specs) == 5
        assert all(isinstance(spec, HookSpec) for spec in specs)
