"""AgentTemplate — preset definition + subagent construction path.

``AgentTemplate.materialize`` folds the old
``AgentCommunicationService._create_dynamic_subagent`` god-method into a deep
module on the template (ADR-0015 D3, Design B). It is the subagent-only
construction path: normals are registered by business wiring via factory
defaults, never via materialize. ``comm_kind`` is always ``SUBAGENT``;
``parent_session`` gates the FORK context feature.

Construction is direct (not via ``AssemblyPipeline``): the pipeline is a
per-pool main-agent orchestrator (stages 1-3, SPEC Errata-5), while subagent
construction needs per-invocation data (``parent_session``,
``invocation_id``, materialize deps). Since ticket 10 the per-invocation
data rides the per-agent ``AgentContext`` chain carrier — the same
mechanism the native core uses — alongside the per-pool materialize deps.

``EXTERNAL`` subagents dispatch to
:meth:`ExecutionStrategy.assemble_sub` via the strategy registry — the
strategy owns the external subagent shape (ADR-0027 convergence).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.agent import ExecutionStrategyKind
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.multi_agent.execution_strategy import strategy_name_of
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.scope.spec import AgentSpec
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.presets import ContextMode, ToolPreset
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider
from modex_agent.workspace.scope_path import resolve_scope_path

if TYPE_CHECKING:
    from modex_agent.commands.skill import SkillResolver
    from modex_agent.core.provider import LLMProvider
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.descriptor import AgentInstance
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.plugins.assembly.context import AssemblyContext
    from modex_agent.plugins.assembly.spec import AssemblySpec
    from modex_agent.runtime.approval_decision import ApprovalAuditStore
    from modex_agent.sandbox.delegation import DelegationSnapshot
    from modex_agent.sandbox.settings import SandboxSettings
    from modex_agent.scope.spec import PoolSpec
    from modex_agent.tools.manager import InMemoryToolManager


def _pool_name(deps: AgentMaterializeDeps) -> str:
    """Read the authoritative pool name from the scope path.

    ``AgentPool`` carries no pool-name attribute; the pool's
    :class:`ScopePath` is the single source of truth (set at pool-wiring
    time). Falls back to ``"main"`` when no scope path is wired
    (non-workspace tests).
    """
    scope_path = deps.scope_path
    if scope_path is not None and scope_path.pool_name:
        return scope_path.pool_name
    logger.debug(
        "_pool_name: no scope path wired (or empty pool_name); "
        "convention skill root defaulting to pool='main'."
    )
    return "main"


def _subagent_workspace_root(deps: AgentMaterializeDeps) -> Path:
    """Resolve the subagent's workspace root from the threaded authorities.

    ``scope_path.workspace_root`` (the canonical addressing carrier) is
    the primary source; the live ``root_provider`` is the alternative for
    callers that thread one without a scope path. ``project_dir`` is
    deliberately NOT consulted here — it is business-asset lookup only
    (agents/<name>.md prompt templates, data/memory paths), never
    workspace identity: production assemblies pass the static service
    project dir alongside a live workspace handle, and the workspace
    handle must win. All sources absent is a wiring error — raised, not
    silently defaulted.

    Shared by the native and external materialization roads (rule 15:
    one mechanism, both callers).
    """
    if deps.scope_path is not None:
        return deps.scope_path.workspace_root
    if deps.root_provider is not None:
        return deps.root_provider.current()
    raise ValueError(
        "AgentTemplate.materialize requires scope_path or root_provider "
        "on AgentMaterializeDeps to resolve the subagent workspace root"
    )


def _declared_depth(pool_spec: PoolSpec, agent_name: str) -> int:
    """Delegation depth from the declared tree (root = 0, spawn +1).

    Walks the ``parent`` chain up to the root; the chain length IS the
    generation. An unknown name or a broken chain stops at 0 — the
    runtime budget check (task dispatch) reads the snapshot's depth, so
    a hand-built context without a declared tree simply reports depth 0.
    """
    depth = 0
    seen: set[str] = set()
    current: str | None = agent_name
    while current is not None and current not in seen:
        seen.add(current)
        parent = next(
            (agent.parent for agent in pool_spec.agents if agent.name == current),
            None,
        )
        if parent is None:
            return depth
        depth += 1
        current = parent
    return depth


def _pool_sandbox_settings(deps: AgentMaterializeDeps) -> SandboxSettings | None:
    """The pool root's declared sandbox settings, including dormant policy.

    Reads the same ``interceptor_configs["sandbox_guard"]`` declaration
    the interceptor factory consumes (one declaration, two assemblies —
    the ``_declared_sandbox_settings`` pattern from the bot's pipeline
    wiring). ``None`` only when no section is declared. DEFAULT does not
    activate a substrate, but an explicit READ_ONLY policy is preserved.
    """
    from modex_agent.sandbox.settings import SandboxSettings

    pool_assembly = deps.pool_assembly_ctx
    if pool_assembly is None:
        return None
    raw = (pool_assembly.pool_spec.root_agent.interceptor_configs or {}).get(
        "sandbox_guard"
    )
    if raw is None:
        return None
    section = raw.get("sandbox", {}) if isinstance(raw, dict) else {}
    settings = SandboxSettings.model_validate(section)
    return settings


logger = logging.getLogger(__name__)


class _StaticRootProvider(WorkspaceRootProvider):
    """Frozen workspace-root provider — the delegation snapshot's anchor.

    Both tools and guards use the spawn-time root. Later changes to the
    pool's live provider must not move an already-delegated file boundary.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


@dataclass
class AgentTemplate:
    """Preset definition for a dynamically creatable subagent type.

    Communication tools (``send_to_agent``) arrive via the derived roster
    entries the ``subagents`` capability injects at compile time — they
    resolve through the TOOL-slot factories at assembly, never by
    materialize-time side registration.

    ``toolset_profile`` is the node's RESOLVED toolset profile (position
    default + ``toolset`` override) — the read-only guard reads it.
    ``context_mode`` controls memory inheritance. ``mcp`` lists registry
    server names resolved via ``bot.config.mcp_registry``.

    ``compiled_spec`` is the ScopeCompiler's per-agent
    :class:`AssemblySpec` — REQUIRED for materialization (the declaration
    is the assembly input; there is no roster re-derivation road).
    """

    spec: AgentSpec
    toolset_profile: ToolPreset = ToolPreset.READ_WRITE
    memory: MemoryConfig | None = None
    compiled_spec: AssemblySpec | None = None
    children: tuple[AgentSpec, ...] = ()
    """Declared DIRECT children (SPEC §3.2) — non-empty only for mid-level
    agents of a nested declaration tree. The ``subagents`` capability's
    assemble reads the DECLARED pool tree (the chain's pool assembly
    context) for the same children when building the per-agent
    ``CommunicationTargetStore`` the derived ``task`` TOOL-slot factory
    resolves against; grandchildren never appear here (each child
    dispatches its own)."""

    async def materialize(
        self,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance:
        """Validate before building; every strategy shares post-build delegation metadata."""
        from modex_agent.sandbox.delegation import DelegationSnapshot, delegation_sandbox_settings
        from modex_agent.sandbox.settings import SandboxBackend
        from modex_agent.scope.compiler import validate_allowed_dirs

        root = _subagent_workspace_root(deps)
        pool_settings = _pool_sandbox_settings(deps)
        allowed = tuple(self.spec.allowed_dirs or ())
        validate_allowed_dirs(allowed, root, *(pool_settings.writable_roots if pool_settings else ()))
        snapshot = DelegationSnapshot(
            workspace_root=root, allowed_dirs=allowed, depth=self._declared_depth(deps),
            requested_backend=pool_settings.backend if pool_settings else SandboxBackend.DEFAULT,
        )
        settings = delegation_sandbox_settings(snapshot.allowed_dirs, pool=pool_settings)
        if strategy_name_of(self.spec.execution_strategy) == ExecutionStrategyKind.EXTERNAL.value:
            instance = await self._materialize_external(parent_session, invocation_id, deps)
        else:
            instance = await self._materialize_native(parent_session, invocation_id, deps, snapshot, settings)
        await self._wire_delegation_boundary(
            instance, snapshot, settings, approval_audit=deps.approval_audit,
        )
        return instance

    async def _materialize_native(
        self,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
        snapshot: DelegationSnapshot,
        settings: SandboxSettings,
    ) -> AgentInstance:
        """Build a subagent AgentInstance from this template (ADR-0015 D3, Design B).

        subagent-only construction; ``parent_session`` gates the FORK
        context feature. Normals are registered by business wiring via
        factory defaults, never via materialize.

        A materialize call with ``parent_session=None`` is a subagent with no
        parent context (e.g. a cold-started template): it still gets a built
        tool manager, bound skill resolver, and session-scoped memory; only the
        parent-dependent feature above is skipped. The
        ``subagent_auto_send`` hook is roster-dispatched for every non-root
        agent regardless (its factory derives the parent from the declared
        tree).

        The public materialize entry selects this native-only assembly or
        the external strategy, then applies shared delegation metadata.
        """
        name = self.spec.name

        # ── System prompt (from agents/{type}.md) ──
        system_prompt = ""
        if deps.project_dir is not None:
            md_path = deps.project_dir / "agents" / f"{name}.md"
            if md_path.exists():
                system_prompt = md_path.read_text(encoding="utf-8")
        if not system_prompt:
            from modex_agent.ioc.factories.descriptors import DEFAULT_SYSTEM_PROMPT

            system_prompt = DEFAULT_SYSTEM_PROMPT

        # ── Read-only guard (match source exactly) ──
        if self.toolset_profile == ToolPreset.READ_ONLY:
            guard = (
                "\n\n---\n\n"
                "## Read-Only Mode\n\n"
                "You are in read-only mode. Report your final result via your "
                "reply text, not by writing files.\n\n"
                "- Your `write` and `edit` tools are restricted — they will NOT "
                "work on project paths.\n"
                "- Your `bash` tool is for reading/searching only. Do NOT use it to "
                "modify, delete, or create files.\n"
                "- Do NOT use shell redirection (> / >>) or heredocs to write files."
            )
            system_prompt = system_prompt + guard

        # ── Scope-path resolution (needed by both branches) ──
        pool_data = resolve_scope_path(deps.workspace_manager, deps.scope_path)
        runtime_dir: Path | None = pool_data.runtime_dir if pool_data is not None else None
        subagent_workspace_root = snapshot.workspace_root
        root_provider = _StaticRootProvider(subagent_workspace_root)

        assembly_spec: AssemblySpec | None = self.compiled_spec
        component_ctx: AssemblyContext | None = None
        if deps.component_registry is not None:
            from modex_agent.plugins.assembly.context import (
                PoolRuntimeDeps,
                resolution_context,
            )
            from modex_agent.workspace.context import WorkspaceContext
            from modex_agent.workspace.paths import WorkspacePaths

            workspace_ctx = WorkspaceContext(
                target=subagent_workspace_root,
                paths=WorkspacePaths(root=deps.data_dir or subagent_workspace_root / ".modex"),
                is_home=False,
            )
            component_ctx = resolution_context(
                deps.component_registry,
                workspace_ctx,
                PoolRuntimeDeps(
                    session_tree_manager=deps.tree,
                    root_provider=root_provider,
                    mcp_registry=deps.mcp_registry,
                    emitter_factory=deps.emitter_factory,
                    pool_assembly_ctx=deps.pool_assembly_ctx,
                    capability_supply=deps.capability_supply,
                ),
            )
            if deps.workspace_resources is not None:
                component_ctx = dataclass_replace(
                    component_ctx, workspace_resources=deps.workspace_resources
                )

        if assembly_spec is None or component_ctx is None:
            raise RuntimeError(
                "Native subagent materialization requires a compiled_spec "
                "(the scope-declaration assembly input) and a "
                "component_registry in AgentMaterializeDeps"
            )

        # Feed the scoped substrate into the SAME bash factory as main agents.
        # DEFAULT remains host execution without a sandbox probe or interceptor.
        from modex_agent.interceptor.chain import InterceptorChain
        from modex_agent.plugins.assembly.context import agent_context_chain
        from modex_agent.plugins.defaults.interceptors import (
            SandboxGuardConfig,
            SandboxGuardInterceptorFactory,
        )
        from modex_agent.sandbox.settings import SandboxBackend

        guard_chain: InterceptorChain | None = None
        if snapshot.requested_backend is not SandboxBackend.DEFAULT:
            assert component_ctx.pool_runtime is not None
            sandbox_guard = await SandboxGuardInterceptorFactory().create(
                SandboxGuardConfig(sandbox=settings),
                agent_context_chain(component_ctx, spec=assembly_spec),
            )
            guard_chain = InterceptorChain([sandbox_guard])
            component_ctx = dataclass_replace(
                component_ctx,
                pool_runtime=dataclass_replace(component_ctx.pool_runtime, interceptor_chain=guard_chain),
            )

        # ── Build session-scoped memory + preset tools (subagent-only, Design B) ──
        # materialize is always subagent construction: session-scoped memory +
        # preset tools from the template. Normals are registered by business
        # wiring via factory defaults, never via materialize.
        from modex_agent.ioc.factories.descriptors import build_session_only_memory
        from modex_agent.memory.scope import MemoryAgentRole

        memory_workspace = (pool_data.memory_dir if pool_data is not None else None) or (
            deps.project_dir / "data" / "memory" / _pool_name(deps)
            if deps.project_dir
            else Path(".")
        )
        output_base_dir: Path | None = (runtime_dir / "output") if runtime_dir is not None else None
        pruned_manager = pool_data.pruned_manager if pool_data is not None else None

        fork_context_spec = None
        if (
            parent_session is not None
            and self.spec.context_mode == ContextMode.FORK
            and deps.context_fork_builder is not None
        ):
            from modex_agent.memory.prompt_pipeline.providers import ForkContextSpec

            fork_context_spec = ForkContextSpec(
                builder=deps.context_fork_builder,
                agent_type=name,
                fork_max_messages=self.spec.fork_max_messages,
            )

        subagent_ctx = build_session_only_memory(
            cfg=self.memory,
            workspace=memory_workspace,
            agent_id=name,
            agent_role=MemoryAgentRole.SUBAGENT,
            system_prompt=system_prompt,
            pruned_manager=pruned_manager,
            output_base_dir=output_base_dir,
            fork_context_spec=fork_context_spec,
            roles=list(self.spec.roles),
            store_registry=deps.memory_store_registry,
        )

        # Post-cleanup reorientation (``TodoReorientationHook``) is NOT
        # injected here anymore: the ``todo`` capability contributes
        # ``todo_reorientation`` as a roster entry, and the roster→memory-
        # runner dispatch in ``assemble_native_agent``'s ``_dispatch_hooks``
        # registers it on this same memory system — the single path for
        # both mains and subagents (SPEC §8.2 B2).

        tool_manager = await self._build_tool_manager(
            deps,
            name,
            runtime_dir,
            assembly_spec=assembly_spec,
            component_ctx=component_ctx,
        )
        skill_resolver = self._resolve_skill_resolver(deps, assembly_spec)
        context_manager_for_create = subagent_ctx

        # ── Hooks ──
        # ``SubagentAutoSendHook`` is NOT constructed here anymore: the
        # ``subagents`` capability contributes ``subagent_auto_send`` as a
        # roster entry for every non-root agent, and the roster dispatch
        # in ``assemble_native_agent`` resolves it through the HOOK-slot
        # factory (which derives the per-agent fields from the context
        # chain) — the single registration path for native subagents.
        # ``InboxFlushHook`` is NOT here: AgentFactory auto-injects it
        # onto ``hook_runner`` for every agent with
        # ``inbox_strategy != "none"`` + a consumer, so fold-in is wired
        # once for both main and subagent at the factory.
        # ``NativeEnvInjectionHook`` is NOT here either: ``native_env``
        # is a compiler position-default roster entry (SPEC §3.2 hook
        # rows) dispatched by the same roster path — the factory derives
        # the subagent env template (self + declared parent pool map,
        # SUBAGENT comm kind) from the context chain.

        # deps.llm_provider is the deps-assembly resolution of the NAME
        # deps.default_llm_provider: default-named subs reuse the instance,
        # per-agent override names resolve here (once, C1).
        # Ticket 04: component factories resolve against the per-agent
        # full-chain context derived from the legacy AssemblyContext view.
        from modex_agent.plugins.assembly.context import agent_context_chain
        from modex_agent.plugins.assembly.native_core import (
            LlmDefaults,
            NativeAssemblyInputs,
            _resolve_single,
            assemble_native_agent,
        )

        component_chain = agent_context_chain(
            component_ctx,
            spec=assembly_spec,
            parent_session=parent_session,
            invocation_id=invocation_id,
        )
        llm_provider: LLMProvider | None
        if (
            deps.llm_provider is not None
            and assembly_spec.llm_provider == deps.default_llm_provider
        ):
            llm_provider = deps.llm_provider
        else:
            llm_provider = await _resolve_single(
                component_ctx.registry,
                ComponentSlot.LLM_PROVIDER,
                assembly_spec.llm_provider,
                assembly_spec.llm_provider_config,
                component_chain,
            )

        result = await assemble_native_agent(
            assembly_spec,
            component_ctx.registry,
            NativeAssemblyInputs(
                agent_factory=deps.agent_factory,
                broker=deps.broker,
                llm_defaults=LlmDefaults(
                    model=deps.llm_model,
                    temperature=deps.llm_temperature,
                    max_output_tokens=deps.llm_max_output_tokens,
                    reasoning_effort=deps.llm_reasoning_effort,
                    model_info=deps.llm_model_info,
                ),
                pool=deps.pool,
                context_manager=context_manager_for_create,
                memory_system=subagent_ctx.memory_system,
                memory_config=self.memory,
                llm_provider=llm_provider,
                tool_manager=tool_manager,
                skill_resolver=skill_resolver,
                root_provider=root_provider,
                safety=deps.safety,
                project_dir=deps.project_dir,
                on_subagent_created=deps.on_subagent_created,
                extra_hooks=(),
                execution_strategy=ExecutionStrategyKind(self.spec.execution_strategy),
                depth=self._declared_depth(deps),
            ),
            ctx=component_ctx,
            parent_session=str(parent_session) if parent_session is not None else None,
            invocation_id=invocation_id,
        )
        instance = result.instance
        if guard_chain is not None and instance.pipeline is not None:
            builder = instance.pipeline._turn_runner.turn_context_builder
            if builder is not None:
                existing = instance.pipeline.interceptor_chain
                builder._interceptor_chain = InterceptorChain([
                    *(i for i in existing.interceptors if i.name != "sandbox_guard"),
                    *guard_chain.interceptors,
                ]) if existing is not None else guard_chain

        # The bash_input companion is ensured inside assemble_native_agent
        # (right after roster registration) — the single convergence point
        # shared with the Stage-4 main-agent path.

        # Tree-aware continuation hooks — the deliver_retry + length_guard
        # position defaults (SPEC §3.2 hook rows) ride the compiled roster:
        # the ``_dispatch_hooks`` pass above resolved them through the
        # HOOK-slot factories against this same context chain (the tree
        # from ``pool_runtime.session_tree_manager`` — the same per-pool
        # tree the retired code-wired registration read).

        # Graph turn-config trio — converge with the main-agent path
        # (_wire_main_pipeline calls the same function). A subagent
        # referenced by a graph node executes graph node turns; without
        # the configurators it never receives the deliver tool (SPEC
        # §4 axis 3).
        if instance.pipeline is not None:
            from modex_agent.pipeline.turn_context_config import (
                wire_graph_turn_config,
            )

            wire_graph_turn_config(
                instance.pipeline._turn_runner.turn_context_builder,
                graph_context_resolver=deps.graph_context_resolver,
                session_binding_store=(deps.tree.binding_store if deps.tree is not None else None),
            )

        return instance

    async def _wire_delegation_boundary(
        self,
        instance: AgentInstance,
        snapshot: DelegationSnapshot,
        settings: SandboxSettings,
        *,
        approval_audit: ApprovalAuditStore | None,
    ) -> None:
        """Report real capabilities; install checks only where the runner executes them."""
        from dataclasses import replace as _replace

        from modex_agent.runtime.services import AgentRuntimeServices
        from modex_agent.sandbox.delegation import (
            delegation_denial_message,
        )
        from modex_agent.sandbox.security_classifier import guard_only_runtime
        from modex_agent.sandbox.settings import SandboxBackend
        from modex_agent.sandbox.shell_plan import resolved_substrate
        from modex_agent.sandbox.types import EnforcementLevel

        builder = instance.pipeline._turn_runner.turn_context_builder if instance.pipeline else None
        native = strategy_name_of(self.spec.execution_strategy) != ExecutionStrategyKind.EXTERNAL.value
        checks_run = native and builder is not None
        resolved = await resolved_substrate(instance.pipeline.interceptor_chain) if checks_run and instance.pipeline else None
        limits = (
            "Shell/input guards are best effort, not containment of dynamic code; HOST has no kernel isolation.",
            "Only catalogued file targets are checked; custom/MCP tools and secondary tool effects are not contained.",
        ) if checks_run else (
            "Provider-hosted tools bypass framework guards; no provider-neutral permission capability is available. "
            "Declared roots/policy are metadata only, not enforced; provider kernel enforcement is unknown.",
        )
        snapshot = snapshot.model_copy(update={
            "policy": settings.policy,
            "backend": resolved.backend if resolved else (SandboxBackend.HOST if checks_run else None),
            "enforcement": resolved.enforcement if resolved else (EnforcementLevel.NONE if checks_run else None),
            "file_guards": checks_run,
            "limitations": (*limits, *((resolved.degraded_reason,) if resolved and resolved.degraded_reason else ())),
        })
        instance.delegation = snapshot
        if not checks_run:
            logger.warning("Delegation %s: %s", self.spec.name, limits[0])
            return

        guard_only = guard_only_runtime(
            settings=settings,
            root_provider=_StaticRootProvider(snapshot.workspace_root),
            deny_message_builder=lambda reason, tool_name, target: delegation_denial_message(
                tool_name, target, snapshot
            ),
        )
        assert builder is not None
        base = builder.runtime_services
        builder.runtime_services = (
            _replace(
                base,
                approval=guard_only,
                guard_only_approval=guard_only,
                delegation=snapshot,
                approval_audit=approval_audit,
            )
            if base is not None
            else AgentRuntimeServices(
                approval=guard_only,
                guard_only_approval=guard_only,
                delegation=snapshot,
                approval_audit=approval_audit,
            )
        )

    def _declared_depth(self, deps: AgentMaterializeDeps) -> int:
        """This subagent's delegation depth from the declared pool tree."""
        pool_assembly = deps.pool_assembly_ctx
        if pool_assembly is None:
            return 0
        return _declared_depth(pool_assembly.pool_spec, self.spec.name)

    async def _materialize_external(
        self,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance:
        """External-coding subagent dispatch (ADR-0027 convergence).

        Resolves the subagent's OWN execution strategy from the strategy
        registry (a subagent may select a different strategy than its pool's
        main agent) and delegates the full assembly to
        :meth:`ExecutionStrategy.assemble_sub` with the per-invocation
        :class:`AgentContext` chain (ticket 10: the per-invocation data —
        parent session, invocation id, agent identity, per-agent spec —
        rides the SAME chain carrier the native path builds; the former
        per-invocation special-case context type is deleted). The dispatch
        ends with the same emitter injection + ``pool.register_resident`` +
        ``on_subagent_created`` calls the react path makes, so parent-child
        wiring is uniform across execution strategies.

        Raises ``ValueError`` when no strategy registry is wired (react-only
        pools without a registry cannot assemble external subagents — an
        ``EXTERNAL`` subagent without a strategy is a configuration error
        the framework cannot recover from), when no component registry is
        wired (the chain anchors on it), or when no compiled spec is
        available (the chain's per-agent spec reference cannot be derived).
        """
        if deps.strategy_registry is None:
            raise ValueError(
                f"Subagent {self.spec.name!r} requires external "
                "execution_strategy but no strategy_registry is wired in "
                "AgentMaterializeDeps"
            )
        if deps.component_registry is None:
            raise ValueError(
                f"Subagent {self.spec.name!r} requires external "
                "execution_strategy but no component_registry is wired in "
                "AgentMaterializeDeps (the AgentContext chain anchors on it)"
            )

        from modex_agent.plugins.assembly.context import (
            PoolRuntimeDeps,
            agent_context_chain,
            resolution_context,
        )
        from modex_agent.workspace.context import WorkspaceContext
        from modex_agent.workspace.paths import WorkspacePaths

        subagent_workspace_root = _subagent_workspace_root(deps)
        workspace_ctx = WorkspaceContext(
            target=subagent_workspace_root,
            paths=WorkspacePaths(root=deps.data_dir or subagent_workspace_root / ".modex"),
            is_home=False,
        )

        if self.compiled_spec is None:
            raise ValueError(
                f"Subagent {self.spec.name!r} requires external "
                "execution_strategy but no compiled spec is available "
                "(the per-agent spec reference cannot be derived — the "
                "scope declaration is the assembly input)"
            )
        assembly_spec: AssemblySpec = self.compiled_spec
        component_ctx = resolution_context(
            deps.component_registry,
            workspace_ctx,
            PoolRuntimeDeps(
                session_tree_manager=deps.tree,
                root_provider=deps.root_provider,
                mcp_registry=deps.mcp_registry,
                emitter_factory=deps.emitter_factory,
                pool_assembly_ctx=deps.pool_assembly_ctx,
            ),
        )
        if deps.workspace_resources is not None:
            component_ctx = dataclass_replace(
                component_ctx, workspace_resources=deps.workspace_resources
            )
        chain = agent_context_chain(
            component_ctx,
            spec=assembly_spec,
            parent_session=parent_session,
            invocation_id=invocation_id,
        )

        strategy = deps.strategy_registry.resolve(strategy_name_of(self.spec.execution_strategy))
        sub_assembly = await strategy.assemble_sub(chain, deps)
        instance = sub_assembly.instance

        # External subagents bypass the BIZ ``_create_with_emitter`` wrapper
        # (bot/service/pool/agent_factory.py), so the framework injects the
        # emitter factory + pool context here via the shared
        # ``_inject_emitter_and_pool_context`` helper (architecture rule 15).
        _inject_emitter_and_pool_context(instance, deps)

        await deps.pool.register_resident(sub_assembly.descriptor, instance)

        if parent_session is not None and deps.on_subagent_created is not None:
            session_id = f"{invocation_id or ''}.{self.spec.name}"
            await deps.on_subagent_created(session_id, str(parent_session))

        return instance

    async def _build_tool_manager(
        self,
        deps: AgentMaterializeDeps,
        name: str,
        runtime_dir: Path | None,
        *,
        assembly_spec: AssemblySpec,
        component_ctx: AssemblyContext,
    ) -> InMemoryToolManager:
        """Build the agent tool manager from this template's tool policy.

        Every tool — preset/supplement tools, the derived communication
        entries, per-agent MCP tools — resolves downstream in
        ``assemble_native_agent`` (TOOL-slot factories + the FW MCP loader,
        both reading the context chain — ticket 10 converged the subagent
        MCP path onto that single point).
        """
        from modex_agent.tools.manager import InMemoryToolManager

        tm = InMemoryToolManager()

        return tm

    def _resolve_skill_resolver(
        self,
        deps: AgentMaterializeDeps,
        assembly_spec: AssemblySpec,
    ) -> SkillResolver | None:
        """Look up this subagent's bound resolver from the pool's skills supply.

        Construction lives in the ``skills`` capability (plan §11.3.1):
        ``require_skills_supply`` -> ``resolver_for(name)``. A native compiled
        spec without Skills is the explicit per-agent veto, so only that case
        intentionally maps to no resolver. Active Skills wiring requires the
        pool supply and fails loudly when it is missing or malformed.
        """
        from modex_agent.plugins.defaults.capabilities.skills import (
            SKILLS_CAPABILITY_NAME,
            require_skills_supply,
        )

        if not any(
            capability.name == SKILLS_CAPABILITY_NAME
            for capability in assembly_spec.capabilities
        ):
            return None
        supply = require_skills_supply(deps.capability_supply)
        return supply.resolver_for(assembly_spec.agent_name)


def _inject_emitter_and_pool_context(
    instance: AgentInstance,
    deps: AgentMaterializeDeps,
) -> None:
    """Inject emitter factory + pool context into a turn runner post-build.

    Shared convergence point for post-build turn-runner wiring (architecture
    rule 15). The ``_create_with_emitter`` wrapper in
    ``bot/service/pool/agent_factory.py`` calls the same
    ``set_emitter_factory`` / ``set_pool_context`` methods on the turn runner;
    external subagents bypass that wrapper (they go through
    ``ExecutionStrategy.assemble_sub`` → ``assemble_pipeline`` directly), so
    ``_materialize_external`` calls this function instead. Without the pool
    context, ``ExternalTurnRunner._workspace_manager`` stays None and
    external subagent turns fall back to the pool ``project_dir`` workdir
    instead of the ACTIVE workspace root (wrong under multi-live workspaces).
    """
    if instance.pipeline is None:
        return
    turn_runner = instance.pipeline._turn_runner
    if deps.emitter_factory is not None:
        turn_runner.set_emitter_factory(deps.emitter_factory)
    if deps.workspace_manager is not None:
        turn_runner.set_pool_context(
            workspace_manager=deps.workspace_manager,
            pool_name=_pool_name(deps),
        )
