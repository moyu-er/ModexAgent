"""AgentTemplate — preset definition + subagent construction path.

``AgentTemplate.materialize`` folds the old
``AgentCommunicationService._create_dynamic_subagent`` god-method into a deep
module on the template (ADR-0015 D3, Design B). It is the subagent-only
construction path: normals are registered by business wiring via factory
defaults, never via materialize. ``comm_kind`` is always ``SUBAGENT``;
``parent_session`` gates parent-dependent features (APPEND prompt, FORK
context, SubagentAutoSendHook).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.skills import SkillsConfig
from modex_agent.multi_agent.pool_config.specs import SubagentSpec
from modex_agent.tools.presets import (
    ContextMode,
    SystemPromptMode,
    ToolPreset,
)

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.skills import SkillManager
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.hook.abc import Hook
    from modex_agent.multi_agent.descriptor import AgentInstance
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps


def _pool_name(deps: AgentMaterializeDeps) -> str:
    """Read the authoritative pool name from the workspace path resolver.

    ``AgentPool`` carries no pool-name attribute; the resolver is the single
    source of truth (set at pool-wiring time). Falls back to ``"main"`` when
    no resolver is wired (non-workspace tests).
    """
    resolver = deps.workspace_path_resolver
    if resolver is not None and resolver.pool_name:
        return resolver.pool_name
    logger.debug(
        "_pool_name: no workspace_path_resolver wired (or empty pool_name); "
        "convention skill root defaulting to pool='main'."
    )
    return "main"


logger = logging.getLogger(__name__)


@dataclass
class AgentTemplate:
    """Preset definition for a dynamically creatable subagent type.

    Communication tools (``send_to_agent``) are auto-injected by the
    framework in ``_build_tool_manager`` — they must not appear in
    template config.

    ``tool_preset`` controls base tool registration; ``tool_supplements``
    layer additive tools on top. ``context_mode`` controls memory
    inheritance. ``mcp`` lists registry server names resolved via
    ``bot.config.mcp_registry``.
    """

    spec: SubagentSpec
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None

    async def materialize(
        self,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance:
        """Build a subagent AgentInstance from this template (ADR-0015 D3, Design B).

        subagent-only construction; ``parent_session`` gates parent-dependent
        features (APPEND prompt, FORK context, SubagentAutoSendHook).
        Normals are registered by business wiring via factory defaults, never
        via materialize.

        A materialize call with ``parent_session=None`` is a subagent with no
        parent context (e.g. a cold-started template): it still gets a built
        tool_manager, skill_manager, and session-scoped memory; only the three
        parent-dependent features above are skipped.

        ``EXTERNAL_CODING`` subagents dispatch early to
        :meth:`_materialize_external`, skipping react-specific assembly
        (memory, tool_manager, skill_manager, hooks) — the external builder
        owns that assembly. React/pipeline/single-turn subagents take the
        existing path below.
        """
        if self.spec.execution_strategy == ExecutionStrategyKind.EXTERNAL_CODING:
            return await self._materialize_external(parent_session, invocation_id, deps)

        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.comm_kind import AgentCommKind
        from modex_agent.multi_agent.descriptor import AgentDescriptor, AgentLLMConfig

        name = self.spec.agent_name
        comm_kind = AgentCommKind.SUBAGENT
        parent_name = str(parent_session).split(".")[-1] if parent_session else ""

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
        if self.spec.tool_preset == ToolPreset.READ_ONLY:
            guard = (
                "\n\n---\n\n"
                "## Read-Only Mode\n\n"
                "You are in read-only mode for project files. Your task is to read, "
                "search, and analyze — NOT to modify project source code.\n\n"
                "- Your `write` and `edit` tools are restricted to the output directory "
                "(for writing OUTPUT.md) — they will NOT work on project paths.\n"
                "- Your `bash` tool is for reading/searching only. Do NOT use it to "
                "modify, delete, or create files.\n"
                "- Do NOT use shell redirection (> / >>) or heredocs to write files.\n"
                "- **You CAN and MUST use `write` to save OUTPUT.md** — "
                "the path shown above is in your allowed write directory."
            )
            system_prompt = system_prompt + guard

        # ── Workspace path resolution (needed by both branches) ──
        resolver = deps.workspace_path_resolver
        runtime_dir: Path | None = resolver.runtime_dir() if resolver else None

        # ── Build session-scoped memory + preset tools (subagent-only, Design B) ──
        # materialize is always subagent construction: session-scoped memory +
        # preset tools from the template. Normals are registered by business
        # wiring via factory defaults, never via materialize.
        from modex_agent.core.scope import MemoryAgentRole
        from modex_agent.ioc.factories.descriptors import build_session_only_memory

        memory_workspace = (resolver.memory_dir() if resolver else None) or (
            deps.project_dir / "data" / "memory" / _pool_name(deps)
            if deps.project_dir
            else Path(".")
        )
        output_base_dir: Path | None = (runtime_dir / "output") if runtime_dir is not None else None
        pruned_manager = resolver.pruned_manager() if resolver else None

        # ── Per-invocation providers (APPEND parent prompt + FORK context) ──
        # These move the invocation-specific parts of the system prompt OUT of
        # the baked string and into per-turn pipeline providers, so a reused
        # instance (one slot per agent_type in the pool) rebuilds them per
        # invocation. The parent *value* for each turn arrives via runtime_info
        # (threaded from the dispatch envelope); the lookup below only resolves
        # the parent's prompt from the in-memory pool. None when there is no
        # parent or the mode is off → providers are skipped.
        parent_prompt_lookup = None
        fork_context_spec = None
        if parent_session is not None:
            if self.spec.system_prompt_mode == SystemPromptMode.APPEND:
                pool_ref = deps.pool

                async def _parent_prompt_of(parent_sid: str, _pool=pool_ref) -> str | None:
                    # In-memory instance lookup only — never a session store.
                    inst = _pool.get(str(parent_sid).split(".")[-1])
                    if inst is None or not inst.descriptor.system_prompt_template:
                        return None
                    return inst.descriptor.system_prompt_template

                parent_prompt_lookup = _parent_prompt_of

            if self.spec.context_mode == ContextMode.FORK and deps.context_fork_builder is not None:
                from modex_agent.memory.prompt_pipeline.providers import ForkContextSpec

                fork_context_spec = ForkContextSpec(
                    builder=deps.context_fork_builder,
                    agent_type=name,
                    fork_max_messages=self.spec.fork_max_messages,
                    template_memory=self.memory,
                )

        subagent_ctx = build_session_only_memory(
            cfg=self.memory,
            workspace=memory_workspace,
            agent_id=name,
            agent_role=MemoryAgentRole.SUBAGENT,
            system_prompt=system_prompt,
            pruned_manager=pruned_manager,
            output_base_dir=output_base_dir,
            parent_prompt_lookup=parent_prompt_lookup,
            fork_context_spec=fork_context_spec,
            roles=list(self.spec.roles),
        )

        tool_manager = await self._build_tool_manager(deps, name, runtime_dir)
        skill_manager = self._build_skill_manager(deps, name)
        context_manager_for_create = subagent_ctx

        # ── Hooks ──
        # ``SubagentAutoSendHook`` and ``MaxIterationNotifyHook`` both descend
        # from the ``Hook`` ABC and are passed via ``hooks=`` to create_agent,
        # then re-added to ``pipeline.hook_runner`` below (the ``hooks=`` list
        # itself is not dispatched by the turn loop). ``InboxFlushHook`` is
        # NOT here: AgentFactory auto-injects it onto ``hook_runner`` for every
        # agent with ``inbox_strategy != "none"`` + a consumer, so fold-in is
        # wired once for both main and subagent at the factory.
        hooks: list[Hook] = []
        if parent_session is not None and deps.agent_bus is not None:
            from modex_agent.hook.builtin import SubagentAutoSendHook

            hooks.append(
                SubagentAutoSendHook(
                    agent_bus=deps.agent_bus,
                    self_name=name,
                    parent_name=parent_name,
                    runtime_dir=runtime_dir,
                    trace_enabled=deps.trace_enabled,
                )
            )
        if deps.notification_service is not None:
            from modex_agent.hook.notification import MaxIterationNotifyHook

            hooks.append(
                MaxIterationNotifyHook(
                    notification_service=deps.notification_service,
                )
            )

        # ── Descriptor ──
        descriptor = AgentDescriptor(
            address=AgentAddress(name=name),
            llm_config=AgentLLMConfig(
                model=deps.llm_model or "",
                temperature=deps.llm_temperature,
                max_output_tokens=deps.llm_max_output_tokens,
                reasoning_effort=deps.llm_reasoning_effort,
            ),
            system_prompt_template=system_prompt,
            max_iterations=self.spec.max_steps,
            execution_strategy=self.spec.execution_strategy,
            provider_kind=self.spec.provider_kind,
            context_strategy="persistent",
            safety_policy=deps.safety,
            comm_kind=comm_kind,
            memory_config=self.memory,
            roles=list(self.spec.roles),
        )

        # ── Create instance ──
        instance = await deps.agent_factory.create_agent(
            descriptor,
            broker=deps.broker,
            tool_manager=tool_manager,
            skill_manager=skill_manager,
            context_manager=context_manager_for_create,
            hooks=hooks,
        )

        # ── Wire hooks to hook_runner (factory's hooks= param only lands on
        # pipeline.hooks, a plain list the turn loop does NOT dispatch). The
        # turn loop dispatches via pipeline.hook_runner — so add each hook as
        # a HookSpec there. Fall back to pipeline.hooks when no runner exists
        # (mirrors the factory's own TraceCollectorHook wiring). ADR-0015 D5.
        if hooks and instance.pipeline is not None:
            pipeline_hook_runner = instance.pipeline.hook_runner
            if pipeline_hook_runner is not None:
                from modex_agent.hook import HookErrorPolicy, HookSpec

                for hook in hooks:
                    pipeline_hook_runner.add(HookSpec(hook=hook, on_error=HookErrorPolicy.LOG))
            else:
                from modex_agent.hook import HookErrorPolicy, HookSpec

                instance.pipeline.hooks.extend(
                    HookSpec(hook=hook, on_error=HookErrorPolicy.LOG) for hook in hooks
                )

        # ── Register resident (new two-arg signature: descriptor + instance) ──
        await deps.pool.register_resident(descriptor, instance)

        # ── Record parent-child relationship (subagent only) ──
        if parent_session is not None and deps.on_subagent_created is not None:
            session_id = f"{invocation_id or ''}.{name}"
            await deps.on_subagent_created(session_id, str(parent_session))

        return instance

    async def _materialize_external(
        self,
        parent_session: SessionInfo | str | None,
        invocation_id: str | None,
        deps: AgentMaterializeDeps,
    ) -> AgentInstance:
        """External-coding subagent dispatch (T5).

        Delegates the full subagent assembly (provider backend, parser,
        session store, env builder, harness, pipeline) to
        :attr:`AgentMaterializeDeps.subagent_external_coding_builder`. The
        dispatch ends with the same ``pool.register_resident`` +
        ``on_subagent_created`` calls the react path makes, so parent-child
        wiring is uniform across execution strategies.

        Raises ``ValueError`` if no builder is wired — react-only pools do
        not inject one, and an ``EXTERNAL_CODING`` subagent without a builder
        is a configuration error the framework cannot recover from.
        """
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.comm_kind import AgentCommKind
        from modex_agent.multi_agent.descriptor import AgentDescriptor

        if deps.subagent_external_coding_builder is None:
            raise ValueError(
                f"Subagent {self.spec.agent_name!r} requires external_coding "
                "execution_strategy but no subagent_external_coding_builder is "
                "wired in AgentMaterializeDeps"
            )

        name = self.spec.agent_name
        descriptor = AgentDescriptor(
            address=AgentAddress(name=name),
            execution_strategy=self.spec.execution_strategy,
            provider_kind=self.spec.provider_kind,
            comm_kind=AgentCommKind.SUBAGENT,
            max_iterations=self.spec.max_steps,
            system_prompt_template="",
            roles=list(self.spec.roles),
        )

        instance = await deps.subagent_external_coding_builder.build(
            spec=self.spec,
            descriptor=descriptor,
            parent_session=parent_session,
            invocation_id=invocation_id,
            deps=deps,
        )

        # External subagents bypass pool_builder's ``_create_with_emitter``
        # wrapper, so the framework must inject the emitter factory here.
        # ``ExternalTurnRunner`` inherits the no-op ``set_pool_context``
        # default, so only emitter wiring is needed.
        if (
            instance.pipeline is not None
            and deps.emitter_factory is not None
        ):
            instance.pipeline._turn_runner.set_emitter_factory(
                deps.emitter_factory
            )

        await deps.pool.register_resident(descriptor, instance)

        if parent_session is not None and deps.on_subagent_created is not None:
            session_id = f"{invocation_id or ''}.{name}"
            await deps.on_subagent_created(session_id, str(parent_session))

        return instance

    async def _build_tool_manager(
        self,
        deps: AgentMaterializeDeps,
        name: str,
        runtime_dir: Path | None,
    ) -> InMemoryToolManager:
        """Build the agent tool manager from this template's tool policy.

        Registers, in order: preset tools (with a scoped write dir so
        READ_ONLY agents can still write OUTPUT.md), additive supplement
        tools (e.g. ast_grep), per-agent MCP tools resolved from the
        registry by this template's ``mcp`` selection, and finally a
        ``SendToAgentTool`` wired against a subagent-scoped communication
        service (baked default — every subagent can delegate/reply).
        """
        from modex_agent.core.tool_manager import InMemoryToolManager, ToolManagerConfig
        from modex_agent.tools.presets import get_preset_tools, get_supplement_tools
        from modex_agent.tools.terminal import SubprocessTool

        tm = InMemoryToolManager(config=ToolManagerConfig())

        def _make_bash() -> SubprocessTool:
            return SubprocessTool(timeout=300)

        # READ_ONLY agents (scout, oracle) get a scoped write dir so they can
        # still write OUTPUT.md via the restricted write tool.
        scoped_write_dir: Path | None = None
        if runtime_dir is not None:
            scoped_write_dir = runtime_dir / "output"

        for tool in get_preset_tools(
            self.spec.tool_preset,
            subprocess_tool_factory=_make_bash,
            scoped_write_dir=scoped_write_dir,
            root_provider=deps.root_provider,
        ):
            tm.register(tool)

        # Additive supplement tools (e.g. AST_GREP, TODO) layered on top of the preset.
        for tool in get_supplement_tools(
            self.spec.tool_supplements,
            root_provider=deps.root_provider,
            todo_store=deps.todo_store,
        ):
            tm.register(tool)

        # MCP tools resolved from the registry by this template's mcp selection.
        if deps.project_dir is not None and self.spec.mcp:
            try:
                from modex_agent.tools.mcp_loader import load_per_agent_mcp

                await load_per_agent_mcp(
                    tm, list(self.spec.mcp), deps.project_dir, name, registry=deps.mcp_registry
                )
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "Failed to load MCP tools for agent %s (selection=%s)",
                    name,
                    list(self.spec.mcp),
                )

        # Baked default: every subagent gets send_to_agent for CONSULTATION
        # (asking its parent a question / for a decision). The single target
        # (the parent) is resolved dynamically at execution time, since this
        # instance is reused across different invokers. Wired from deps so the
        # subagent's SendToAgentTool shares the pool's broker/bus/registry.
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
        goes to OUTPUT.md (enforced elsewhere). Failures are logged and
        swallowed — a subagent must still materialize without a comm tool.
        """
        if deps.pool is None or deps.broker is None or deps.agent_bus is None:
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
                broker=deps.broker,
                registry=deps.pool,
                agent_bus=deps.agent_bus,
                pool=deps.pool,
                pool_name=_pool_name(deps),
                project_dir=deps.project_dir,
                session_registry=deps.session_registry,
                workspace_path_resolver=deps.workspace_path_resolver,
            )
            tm.register(
                SendToAgentTool(
                    store=store,
                    source=AgentAddress(name=name),
                    broker=deps.broker,
                    registry=deps.pool,
                    agent_bus=deps.agent_bus,
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
                "using convention root skills/%s/%s/ (resolver wired=%s).",
                name,
                pool_name,
                name,
                deps.workspace_path_resolver is not None,
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
