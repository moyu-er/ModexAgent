"""The FW-bundled ``tracing`` capability — the OTel/Langfuse span-hook family.

Bundles the seven trace span hooks (:func:`~modex_agent.trace.factory.build_trace_hooks`
— RootSpanHook / ChatSpanHook / ToolSpanHook / HandoffSpanHook /
ApprovalSpanHook / AgentStartSpanHook / IterationSpanHook) as an opt-in
capability: declaring ``capabilities: {tracing: {…}}`` on an agent
contributes ALL SEVEN hook names into the roster merge base
unconditionally; the ``trace_spans`` tier (MINIMAL/STANDARD/FULL) is
``bind``'s business — the binding-vouching mechanism vouches exactly the
tier's subset, and the compiler's post-bind gate removes the contributed
names no binding vouches (ADR-0047 W6, the ``experience`` review-hook
precedent). ``hooks: [-trace_tool]`` vetoes a single span hook through
the standard merge (the veto removes the name before ``bind`` sees it;
``bind`` never raises on a missing hook — veto semantics preserved).

The hooks are HOOK-slot registrations owned by
``plugins/defaults/hooks.py`` (seven thin factories, ``priority=-500``);
this module owns the enablement + roster contribution (P2 — single
component-resolution path; the roster dispatch in
``assemble_native_agent`` is the SINGLE registration path — the retired
code-wired ``DefaultAgentFactory.create_agent`` injection died with this
migration), the pool-level supply (:meth:`TracingCapability.supply`), and
the per-agent construction authority (:meth:`TracingCapability.assemble`
builds every hook instance through :func:`build_trace_hooks` — ONE
:class:`~modex_agent.trace.session_state.TraceSessionState` shared
across the family, root first, tool before handoff).

``applies`` defaults False: the global-config default-on tracing of the
shipped bot is a BIZ decision expressed through the deployment's
fallback injection (``bot.service.pool.declaration.apply_tracing_fallback``
— global ``observability`` config → a per-native-agent ``capabilities:
{tracing: {…}}`` override on the compile input), NOT a framework
auto-apply predicate (SPEC P4: the framework knows no default enable
set).

Supply shape (SPEC §7.1):

- The store — ONE :class:`~modex_agent.trace.otel_store.OtelSpanTraceStore`
  per pool. The caller-carried ``pool_data.trace_store`` (the BIZ
  snapshot field; the harbor trial injects its own ``PoolTraceStore``
  through it) is ADOPTED when present — its lifecycle stays with its
  owner. Otherwise the supply builds via
  :func:`~modex_agent.trace.otel_store.build_trace_stores` rooted at
  ``runtime_dir/"trace"`` (the workspace pool-data runtime dir when
  materialized, else the ``<data>/runtime_state/<pool>/trace`` fallback
  — byte-identical to the retired BIZ ``build_pool_data`` block) and
  ``stop()`` closes it (the OTEL_HTTP daemon sender's graceful flush).
- The L2 score injector — built iff the FIRST entry's config enables
  ``eval_score_injection`` with ``trace_backend=otel_http`` AND an
  endpoint configured (the retired ``DefaultAgentFactory`` construction,
  migrated: ``eval_ingestion_url`` wins, else derived from
  ``otel_endpoint``).
- The config snapshot — the first entry's validated config (OQ1
  first-wins arbitration, the retired single-config-per-pool semantics).

Assembly-time model facts (SPEC §7.2): the chain's
``AgentContext.llm_defaults`` (threaded by ``assemble_native_agent``
from ``NativeAssemblyInputs``) supplies the model name, temperature, and
max-output-tokens the retired factory read off the descriptor's
``llm_config``; ``provider_name`` follows the ``model.split("/")[0]``
pattern. The capability contributes NO prompt sections.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, ConfigDict

from modex_agent.ioc.configs.observability import (
    ObservabilityConfig,
    TraceBackend,
    TraceSpanMode,
)
from modex_agent.plugins.capability import (
    Capability,
    CapabilityBinding,
    CapabilityContribution,
    CapabilitySupply,
    CapabilityWiring,
    FinalRosterView,
    PoolSupplyView,
    TreePositionView,
)

if TYPE_CHECKING:
    from modex_agent.hook import Hook
    from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps
    from modex_agent.trace.otel_store import OtelSpanTraceStore
    from modex_agent.trace.score_injector import L2ScoreInjector
    from modex_agent.trace.session_state import TraceSessionState
__all__ = [
    "TRACE_HOOK_NAMES",
    "TracingCapability",
    "TracingCapabilityConfig",
    "TraceSupply",
    "require_tracing_supply",
]

#: The seven span-hook registration names, in construction (and
#: execution) order: root first (it seeds the trace/root span IDs every
#: other hook parents to), tool before handoff (the batch span the
#: handoff parents to must exist by the time the handoff hook reads it).
TRACE_HOOK_NAMES: tuple[str, ...] = (
    "trace_root",
    "trace_chat",
    "trace_tool",
    "trace_handoff",
    "trace_approval",
    "trace_agent_start",
    "trace_iteration",
)

#: The tier → vouched-subset mapping (build_trace_hooks' tier selection,
#: expressed through the binding-vouching mechanism).
_TIER_VOUCHED: dict[TraceSpanMode, tuple[str, ...]] = {
    TraceSpanMode.MINIMAL: ("trace_root",),
    TraceSpanMode.STANDARD: TRACE_HOOK_NAMES[:5],
    TraceSpanMode.FULL: TRACE_HOOK_NAMES,
}


class TracingCapabilityConfig(BaseModel):
    """The tracing capability's config — the agent-declarable subset of
    :class:`~modex_agent.ioc.configs.observability.ObservabilityConfig`.

    Model/provider never live here: they derive from the agent's own
    assembly inputs at ``assemble`` time. The OTLP endpoint/headers
    carry over from the global config via
    :meth:`from_observability` (the BIZ fallback projection) because
    the score injector's construction needs them; declaring them
    per-agent is possible but unusual.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_spans: TraceSpanMode = TraceSpanMode.STANDARD
    trace_backend: TraceBackend = TraceBackend.FILE
    otel_endpoint: str | None = None
    otel_headers: dict[str, str] | None = None
    otel_service_name: str = "modex_agent"
    eval_score_injection: bool = False
    eval_ingestion_url: str | None = None
    retain_reasoning_content: bool = True
    prompt_capture: str = "summary"
    capture_tools: bool = False
    environment: str = "default"
    version: str | None = None
    tags: list[str] = []

    @classmethod
    def from_observability(cls, config: ObservabilityConfig) -> TracingCapabilityConfig:
        """Project the global ``ObservabilityConfig`` onto the capability
        config (the BIZ fallback's mapping — the tracing-shape knobs only)."""
        return cls(
            trace_spans=config.trace_spans,
            trace_backend=config.trace_backend,
            otel_endpoint=config.otel_endpoint,
            otel_headers=config.otel_headers,
            otel_service_name=config.otel_service_name,
            eval_score_injection=config.eval_score_injection,
            eval_ingestion_url=config.eval_ingestion_url,
            retain_reasoning_content=config.retain_reasoning_content,
            prompt_capture=config.prompt_capture.value,
            capture_tools=config.capture_tools,
            environment=config.environment,
            version=config.version,
            tags=list(config.tags),
        )


class TraceSupply(CapabilitySupply):
    """The tracing capability's pool-level supply (SPEC §7.1).

    Regular class (rule 11/12): carries the self-built-store close flag
    mutated by :meth:`stop`. Carries the ONE
    :class:`~modex_agent.trace.otel_store.OtelSpanTraceStore` the pool's
    span hooks, the memory trace hook, and the per-turn runtime services
    share; the :class:`~modex_agent.trace.score_injector.L2ScoreInjector`
    (``None`` unless eval score injection is configured); and the first
    entry's validated config snapshot. ``owns_store`` records whether
    ``stop()`` may close the store — an ADOPTED store (the caller-carried
    ``pool_data.trace_store``) stays with its owner.
    """

    def __init__(
        self,
        *,
        store: OtelSpanTraceStore,
        score_injector: L2ScoreInjector | None = None,
        config: TracingCapabilityConfig | None = None,
        owns_store: bool = False,
    ) -> None:
        self.store = store
        self.score_injector = score_injector
        self.config = config
        self._owns_store = owns_store
        self._stopped = False

    @property
    def owns_store(self) -> bool:
        return self._owns_store

    async def start(self) -> None:
        # The OTEL_HTTP daemon sender thread starts inside the store's
        # constructor (build_trace_stores / OtelSpanTraceStore.__init__);
        # there is no additional worker to start here.
        return None

    async def stop(self) -> None:
        """Close the store's export machinery (graceful flush ≤ 2 s) —
        only when this supply BUILT it. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        if self._owns_store:
            self.store.close()


def require_tracing_supply(pool_runtime: PoolRuntimeDeps | None) -> TraceSupply:
    """Loud supply read shared by the tracing HOOK factories (SPEC §7.1).

    The pool's ``capability_supply['tracing']`` must be the concrete
    :class:`TraceSupply` — :meth:`TracingCapability.supply` builds it iff
    the capability is effective with a non-OFF backend somewhere in the
    pool. Missing or wrong-typed supply raises with the repair path.
    """
    supply = pool_runtime.capability_supply.get("tracing") if pool_runtime is not None else None
    if supply is None:
        raise ValueError(
            "tracing components require the pool's 'tracing' capability supply "
            "(capability_supply['tracing']); it is built iff the tracing "
            "capability is effective in the pool with a non-off backend — "
            "declare capabilities: {tracing: {…}} on the referencing agent"
        )
    if not isinstance(supply, TraceSupply):
        raise ValueError(
            "capability_supply['tracing'] must be TraceSupply, got "
            f"{type(supply).__name__}; only TracingCapability.supply builds "
            "the tracing supply"
        )
    return supply


class TracingCapability(Capability):
    """The OTel span-hook family as an opt-in capability bundle.

    Five-phase shape: ``applies`` defaults False (the shipped tree's
    default-on tracing rides the BIZ fallback, not an auto-apply
    predicate); ``contribute`` declares all 7 hook names
    unconditionally; ``bind`` vouches the ``trace_spans`` tier's subset
    (``trace_backend=off`` vouches nothing — tracing dark); ``supply``
    builds the pool's :class:`TraceSupply` (store + score injector +
    config snapshot); ``assemble`` is the single construction authority
    for the per-agent hook instances (:func:`build_trace_hooks` — one
    shared ``TraceSessionState``, ordered root-first).
    """

    name = "tracing"
    config_model: ClassVar[type[BaseModel]] = TracingCapabilityConfig

    def contribute(self, tree: TreePositionView, config: BaseModel) -> CapabilityContribution:
        del tree, config  # all 7 names unconditionally — the tier filter is bind's business
        return CapabilityContribution(hooks=TRACE_HOOK_NAMES)

    def bind(
        self, tree: TreePositionView, config: BaseModel, final: FinalRosterView
    ) -> CapabilityBinding:
        del tree
        cfg = config if isinstance(config, TracingCapabilityConfig) else TracingCapabilityConfig()
        if cfg.trace_backend == TraceBackend.OFF:
            return CapabilityBinding(hooks=())
        # Veto semantics preserved: a hook minus-removed from merged_hooks
        # (BEFORE bind sees it) is simply not vouched — the compiler's
        # post-bind gate and the vouch set agree without an anchor error.
        return CapabilityBinding(
            hooks=tuple(name for name in _TIER_VOUCHED[cfg.trace_spans] if name in final.hooks)
        )

    def supply(self, view: PoolSupplyView) -> TraceSupply | None:
        """Build the pool's tracing supply — the retired BIZ faces
        converged (``build_pool_data``'s store block + the retired
        ``DefaultAgentFactory`` score-injector construction).

        Store resolution order (path parity with the retired block):
        the caller-carried ``pool_data.trace_store`` (ADOPTED — lifecycle
        stays with its owner), else ``build_trace_stores`` at
        ``runtime_dir/"trace"`` (workspace pool-data runtime dir when
        materialized, else ``<data>/runtime_state/<pool>/trace``) — the
        supply then owns the store's lifecycle. Config arbitration is
        FIRST-entry-wins (OQ1); the score injector builds iff that entry
        enables ``eval_score_injection`` with the OTEL_HTTP backend AND
        an endpoint (``eval_ingestion_url`` wins, else derived from
        ``otel_endpoint`` — the retired construction's precedence).
        """
        from modex_agent.trace.otel_store import build_trace_stores

        if not view.entries:
            return None
        config = TracingCapabilityConfig.model_validate(view.entries[0].config)
        if config.trace_backend == TraceBackend.OFF:
            return None

        carried = view.trace_store
        if carried is not None:
            store, owns_store = carried, False
        elif view.runtime_dir is not None:
            store, owns_store = (
                build_trace_stores(self._to_observability(config), view.runtime_dir / "trace"),
                True,
            )
        elif view.data_dir is not None:
            base = view.data_dir / "runtime_state" / view.pool_name / "trace"
            store, owns_store = build_trace_stores(self._to_observability(config), base), True
        else:
            raise ValueError(
                f"capability 'tracing' on pool {view.pool_name!r} cannot build "
                "its supply: the pool assembly context carries neither a "
                "carried trace_store, a pool runtime_dir, nor a workspace "
                "data_dir"
            )
        if store is None:  # build_trace_stores is None only for OFF (handled above)
            return None

        return TraceSupply(
            store=store,
            score_injector=self._build_score_injector(config),
            config=config,
            owns_store=owns_store,
        )

    async def assemble(self, binding: CapabilityBinding, ctx: AgentContext) -> CapabilityWiring:
        """Build the per-agent span-hook instances — the family's single
        construction authority.

        :func:`build_trace_hooks` receives the supply's store + score
        injector, the config snapshot reconstructed from the binding
        vouch set (the tier), and the model facts derived from the
        chain's ``llm_defaults`` (the model name is otherwise
        unreachable from the agent layer). Returns empty wiring for an
        empty vouch set (OFF / fully vetoed) — nothing registers.
        """
        from modex_agent.plugins.defaults.hooks import TRACE_PRIORITY
        from modex_agent.trace.factory import build_trace_hooks

        supply = require_tracing_supply(ctx.pool_runtime)
        vouched = binding.hooks
        if not vouched:
            return CapabilityWiring()
        config = supply.config if supply.config is not None else TracingCapabilityConfig()
        observability = self._to_observability(config)
        if len(vouched) == len(TRACE_HOOK_NAMES):
            observability = observability.model_copy(update={"trace_spans": TraceSpanMode.FULL})
        elif len(vouched) > 1:
            observability = observability.model_copy(update={"trace_spans": TraceSpanMode.STANDARD})
        else:
            observability = observability.model_copy(update={"trace_spans": TraceSpanMode.MINIMAL})

        llm = ctx.llm_defaults
        model = llm.model if llm is not None else None
        provider_name = model.split("/")[0] if model is not None and "/" in model else None
        request_params: dict[str, object] | None = None
        if llm is not None and (llm.temperature is not None or llm.max_output_tokens is not None):
            request_params = {
                "temperature": llm.temperature,
                "max_tokens": llm.max_output_tokens,
            }

        specs = build_trace_hooks(
            observability,
            model=model,
            provider_name=provider_name,
            request_params=request_params,
            score_injector=supply.score_injector,
            store=supply.store,
        )
        hooks: tuple[Hook, ...] = tuple(spec.hook for spec in specs)  # type: ignore[attr-defined]
        # The span hooks' ``Hook.name`` is the CLASS name (``RootSpanHook``),
        # not the registration name — the ordered tuple preserves execution
        # order (build_trace_hooks emits exactly the tier's subset, in
        # TRACE_HOOK_NAMES order); the name→instance mapping is the
        # factories' lookup face.
        return CapabilityWiring(
            artifacts={
                "hooks": hooks,
                "by_name": dict(zip(_TIER_VOUCHED[observability.trace_spans], hooks, strict=True)),
                "priority": TRACE_PRIORITY,
            }
        )

    @staticmethod
    def _to_observability(config: TracingCapabilityConfig) -> ObservabilityConfig:
        """Rebuild the ``ObservabilityConfig`` face the trace family's
        builders consume (build_trace_stores / build_trace_hooks /
        build_prompt_capture read the global-config shape)."""
        from modex_agent.ioc.configs.observability import PromptCaptureMode

        return ObservabilityConfig(
            trace_backend=config.trace_backend,
            trace_spans=config.trace_spans,
            otel_endpoint=config.otel_endpoint,
            otel_headers=config.otel_headers,
            otel_service_name=config.otel_service_name,
            eval_score_injection=config.eval_score_injection,
            eval_ingestion_url=config.eval_ingestion_url,
            retain_reasoning_content=config.retain_reasoning_content,
            prompt_capture=PromptCaptureMode(config.prompt_capture),
            capture_tools=config.capture_tools,
            environment=config.environment,
            version=config.version,
            tags=list(config.tags),
        )

    @staticmethod
    def _build_score_injector(
        config: TracingCapabilityConfig,
    ) -> L2ScoreInjector | None:
        """The retired ``DefaultAgentFactory`` score-injector construction:
        gated on ``eval_score_injection`` + OTEL_HTTP + a configured
        endpoint; ``eval_ingestion_url`` wins over the derived URL."""
        if not (
            config.eval_score_injection
            and config.trace_backend == TraceBackend.OTEL_HTTP
            and config.otel_endpoint
        ):
            return None
        try:
            from urllib.parse import urlparse

            from modex_agent.trace.score_injector import L2ScoreInjector

            parsed = urlparse(config.otel_endpoint)
            ingestion_url = config.eval_ingestion_url or (
                f"{parsed.scheme}://{parsed.netloc}/api/public/ingestion"
            )
            return L2ScoreInjector(ingestion_url=ingestion_url, headers=config.otel_headers or {})
        except Exception:
            import logging

            logging.getLogger(__name__).warning(
                "L2ScoreInjector creation failed; score injection disabled.",
                exc_info=True,
            )
            return None


def tracing_hook_session(hooks: tuple[Hook, ...]) -> TraceSessionState:
    """The shared ``TraceSessionState`` across one assemble()'s hook tuple.

    Diagnostic/read helper (tests, observability tooling): every hook
    from one :meth:`TracingCapability.assemble` call shares ONE session
    object so child spans resolve parent span IDs written by sibling
    hooks.
    """
    sessions = {getattr(hook, "_session", None) for hook in hooks}
    (session,) = sessions
    return session  # type: ignore[no-any-return]
