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
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.skills import SkillsConfig
from modex_agent.multi_agent.execution_strategy import strategy_name_of
from modex_agent.multi_agent.tools import SEND_TO_AGENT_TOOL_NAME
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.scope.spec import AgentSpec
from modex_agent.tools.presets import ContextMode, ToolPreset
from modex_agent.workspace.scope_path import resolve_scope_path

if TYPE_CHECKING:
    from modex_agent.core.provider import LLMProvider
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.skills import SkillManager
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.multi_agent.descriptor import AgentInstance
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.plugins.assembly.context import AssemblyContext
    from modex_agent.plugins.assembly.spec import AssemblySpec


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


logger = logging.getLogger(__name__)


@dataclass
class AgentTemplate:
    """Preset definition for a dynamically creatable subagent type.

    Communication tools (``send_to_agent``) are auto-injected by the
    framework in ``_build_tool_manager`` — they must not appear in template
    config.

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
    skills: SkillsConfig | None = None
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
        """Build a subagent AgentInstance from this template (ADR-0015 D3, Design B).

        subagent-only construction; ``parent_session`` gates the FORK
        context feature. Normals are registered by business wiring via
        factory defaults, never via materialize.

        A materialize call with ``parent_session=None`` is a subagent with no
        parent context (e.g. a cold-started template): it still gets a built
        tool_manager, skill_manager, and session-scoped memory; only the
        parent-dependent feature above is skipped. The
        ``subagent_auto_send`` hook is roster-dispatched for every non-root
        agent regardless (its factory derives the parent from the declared
        tree).

        ``EXTERNAL`` subagents dispatch early to
        :meth:`_materialize_external`, skipping react-specific assembly
        (memory, tool_manager, skill_manager, hooks) — the external strategy
        owns that assembly. React/pipeline/single-turn subagents take the
        existing path below.
        """
        if strategy_name_of(self.spec.execution_strategy) == ExecutionStrategyKind.EXTERNAL.value:
            return await self._materialize_external(parent_session, invocation_id, deps)

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
        subagent_workspace_root: Path
        if deps.project_dir is not None:
            subagent_workspace_root = deps.project_dir
        elif runtime_dir is not None and len(runtime_dir.parents) >= 3:
            subagent_workspace_root = runtime_dir.parents[2]
        elif runtime_dir is not None:
            subagent_workspace_root = runtime_dir
        else:
            subagent_workspace_root = Path(".")

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
                    root_provider=deps.root_provider,
                    mcp_registry=deps.mcp_registry,
                    emitter_factory=deps.emitter_factory,
                    pool_assembly_ctx=deps.pool_assembly_ctx,
                    capability_supply=deps.capability_supply,
                ),
            )

        if assembly_spec is None or component_ctx is None:
            raise RuntimeError(
                "Native subagent materialization requires a compiled_spec "
                "(the scope-declaration assembly input) and a "
                "component_registry in AgentMaterializeDeps"
            )

        # ── Build session-scoped memory + preset tools (subagent-only, Design B) ──
        # materialize is always subagent construction: session-scoped memory +
        # preset tools from the template. Normals are registered by business
        # wiring via factory defaults, never via materialize.
        from modex_agent.core.scope import MemoryAgentRole
        from modex_agent.ioc.factories.descriptors import build_session_only_memory

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
        skill_manager = self._build_skill_manager(deps, name)
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
                skill_manager=skill_manager,
                root_provider=deps.root_provider,
                safety=deps.safety,
                project_dir=deps.project_dir,
                on_subagent_created=deps.on_subagent_created,
                extra_hooks=(),
                execution_strategy=ExecutionStrategyKind(self.spec.execution_strategy),
            ),
            ctx=component_ctx,
            parent_session=str(parent_session) if parent_session is not None else None,
            invocation_id=invocation_id,
        )
        instance = result.instance

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

        pool_data = resolve_scope_path(deps.workspace_manager, deps.scope_path)
        runtime_dir: Path | None = pool_data.runtime_dir if pool_data is not None else None
        subagent_workspace_root: Path
        if deps.project_dir is not None:
            subagent_workspace_root = deps.project_dir
        elif runtime_dir is not None and len(runtime_dir.parents) >= 3:
            subagent_workspace_root = runtime_dir.parents[2]
        elif runtime_dir is not None:
            subagent_workspace_root = runtime_dir
        else:
            subagent_workspace_root = Path(".")
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

        Registers the ``SendToAgentTool`` wired against a subagent-scoped
        communication service (baked default — every subagent can
        delegate/reply). Preset/supplement tools and per-agent MCP tools
        resolve downstream in ``assemble_native_agent`` (TOOL-slot
        factories + the FW MCP loader, both reading the context chain —
        ticket 10 converged the subagent MCP path onto that single point).
        """
        from modex_agent.core.tool_manager import (
            InMemoryToolManager,
            ToolManagerConfig,
        )

        tm = InMemoryToolManager(config=ToolManagerConfig())

        # Baked default: every subagent gets send_to_agent for CONSULTATION
        # (asking its parent a question / for a decision). The single target
        # (the parent) is resolved dynamically at execution time, since this
        # instance is reused across different invokers. Wired from deps so the
        # subagent's SendToAgentTool shares the pool's broker/bus/registry.
        # The scope-declaration road supersedes this baked default: its
        # compiled spec carries the derived ``send_to_agent`` entry, resolved
        # through the TOOL-slot factory against the pool's ``subagents``
        # capability supply.
        if SEND_TO_AGENT_TOOL_NAME not in assembly_spec.tools:
            self._register_send_to_agent(tm, deps, name)

        return tm

    @staticmethod
    def _register_send_to_agent(
        tm: InMemoryToolManager,
        deps: AgentMaterializeDeps,
        name: str,
    ) -> None:
        """Register a subagent-scoped ``SendToAgentTool`` against deps.

        Builds a minimal :class:`AgentCommunicationService` + a subagent-mode
        :class:`CommunicationTargetStore` (``for_subagent=True``). The store
        does NOT bake a static target list: the tool instance is reused across
        different invokers, so the parent is resolved dynamically at execution
        time from ``current_agent_context`` (see ``resolve_parent_name``). The
        subagent's ``send_to_agent`` is for consultation only — the deliverable
        is the subagent's final reply text (forwarded by
        ``SubagentAutoSendHook``). Failures are logged and swallowed — a
        subagent must still materialize without a comm tool.
        """
        if deps.pool is None:
            return
        try:
            from modex_agent.multi_agent.address import AgentAddress
            from modex_agent.multi_agent.communication import AgentCommunicationService
            from modex_agent.multi_agent.tools import (
                CommunicationTargetStore,
                SendToAgentTool,
            )

            store = CommunicationTargetStore(for_subagent=True)
            service = AgentCommunicationService(
                source=AgentAddress(name=name),
                registry=deps.pool,
                tree=deps.tree,
                pool=deps.pool,
                pool_name=_pool_name(deps),
                project_dir=deps.project_dir,
                session_registry=deps.session_registry,
                scope_path=deps.scope_path,
                workspace_manager=deps.workspace_manager,
            )
            tm.register(
                SendToAgentTool(
                    store=store,
                    source=AgentAddress(name=name),
                    service=service,
                )
            )
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "Failed to register SendToAgentTool for subagent %s",
                name,
            )

    def _build_skill_manager(
        self,
        deps: AgentMaterializeDeps,
        name: str,
    ) -> SkillManager | None:
        """Build a SkillManager so the skill-injection pipeline stage is always present.

        Baked default (ADR: skill injection default-on): every subagent gets
        a SkillManager over its skill root, even when empty — the
        skill-injection pipeline stage must always be present. Roots come
        from ``self.skills.roots`` when set; otherwise the convention root
        ``skills/<pool_name>/<agent_name>/``. Non-existent roots are still
        included (an empty/non-existent root simply yields no skills but
        keeps the pipeline stage wired). Returns ``None`` only when no
        project_dir is set.
        """
        if deps.project_dir is None:
            return None
        explicit_roots: list[str] = []
        if self.skills is not None and self.skills.roots:
            explicit_roots = list(self.skills.roots)
        if not explicit_roots:
            # Convention root: skills/<pool_name>/<agent_name>/
            pool_name = _pool_name(deps)
            explicit_roots = [f"skills/{pool_name}/{name}"]
            logger.debug(
                "_build_skill_manager: agent %r has no explicit skill roots; "
                "using convention root skills/%s/%s/ (scope path wired=%s).",
                name,
                pool_name,
                name,
                deps.scope_path is not None,
            )
        skill_roots = [deps.project_dir / r for r in explicit_roots]
        from modex_agent.core.skills import (
            DefaultSkillBuilder,
            FileSkillSource,
            SkillManager,
        )

        skill_source = FileSkillSource(
            directories=skill_roots,
            cache=True,
            layout="directory",
            skill_filename="SKILL.md",
        )
        builder = DefaultSkillBuilder(base_path=deps.project_dir)
        return SkillManager(source=skill_source, builder=builder)


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
