"""AssemblyManifest — split-brain comparison artifact for migration tickets.

Test-only introspection helper (scope-assembly tickets 05+). The manifest
observes ACTUAL assembly products (tool manager contents, hook runners,
descriptor memory config, prompt hash, provider classes, communication
targets) — never the echo of input configuration.

Split-brain discipline (plan §Verification strategy):
- The FIRST commit of a migration ticket freezes the OLD road's products as
  golden JSON fixtures; the migration commit then re-runs the SAME driver
  and compares against the frozen goldens.
- Any intentional difference must be listed in the test's explicit
  allowlist (reason per entry). Bare golden refreshes are forbidden: the
  tests assert manifest == golden modulo the allowlist, and any unlisted
  difference turns red.

The model is frozen Pydantic with ``extra="forbid"`` so a golden fixture
can never silently absorb new fields.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.plugins.abc import ComponentSlot

# ── Models ──────────────────────────────────────────────────────────────


class ToolEntry(BaseModel):
    """One tool in an assembled tool manager."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    tool_class: str
    # Driver-known provenance: "roster:<registration_source>" for registry-
    # resolved tools, "builders" for legacy direct construction, "glue" for
    # tools no roster entry maps to (there are no shipped glue tools —
    # kb/send_file_to_user are registered TOOL-slot factories, resolved
    # only when a declaration references them), "communication" for
    # task / send_to_peer.
    source: str
    # Key observable parameters (bash timeout, todo store class, ...).
    params: dict[str, str | int | float | bool | None] = {}


class HookEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    hook_class: str
    runner: str  # "react" | "memory"


class CommTargetEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_name: str
    target_kind: str


class TerminalManagerSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manager_class: str
    visibility: str | None
    shell_family: str | None


class TodoStoreSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    store_class: str
    dir_relative: str  # relative to the pool's data_dir


class AgentManifest(BaseModel):
    """Per-agent products (materialized) or compiled effective spec (lazy)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    materialized: bool
    tools: list[ToolEntry] = []
    hooks: list[HookEntry] = []
    memory_config: dict[str, Any] | None = None
    system_prompt_provider: str | None = None
    system_prompt_sha256: str | None = None
    llm_provider_class: str | None = None
    # Non-materialized agents: the compiled effective spec's tool names
    # (the ScopeCompiler's output).
    effective_spec_tools: list[str] | None = None


class AssemblyManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pool_name: str
    execution_strategy: str
    terminal_manager: TerminalManagerSummary | None
    # None when no trio tools are present; True iff every present trio tool
    # (bash, process, terminal) shares ONE ProcessRegistry identity.
    trio_registry_shared: bool | None
    todo_store: TodoStoreSummary | None
    interceptors: list[str]
    commands: list[str] | None
    comm_targets: list[CommTargetEntry]
    # The pool's memory-system cleanup hooks (the notification face —
    # UserNoticeCleanupHook / TodoReorientationHook register on the memory
    # runner, NOT on the react HookRunner). Empty when the driver builds no
    # pool_data (unobserved), so pre-ticket-09 goldens stay valid.
    memory_hooks: list[HookEntry] = []
    agents: list[AgentManifest]


# ── Tool introspection ──────────────────────────────────────────────────


def assert_bash_wave_parity(
    new_tools: dict[str, ToolEntry],
    golden_names,
    allowed_extra: frozenset[str] = frozenset({"bash_input"}),
) -> None:
    """Allow the persistent-bash wave's divergence from the frozen goldens.

    The goldens predate the wave (the bash slot froze as the stateless
    SubprocessTool). On POSIX hosts the no-terminal-manager pools now
    resolve the pool's PersistentBashTool and register the bash_input
    companion post-roster — presence-derived: the divergence applies iff
    the companion actually registered, so terminal pools (CommandTool)
    and Windows hosts (SubprocessTool fallback) stay byte-identical to
    the goldens and their exact comparisons continue to run.
    *allowed_extra* widens the tolerated extra set for goldens that also
    predate other additions (e.g. the derived communication tools).
    """
    extra = set(new_tools) - set(golden_names)
    assert extra <= allowed_extra, f"unexpected extra tools: {sorted(extra)}"
    if "bash_input" in new_tools:
        assert new_tools["bash"].tool_class == "PersistentBashTool"
        assert new_tools["bash_input"].tool_class == "BashInputTool"


def _tool_params(tool: Any) -> dict[str, str | int | float | bool | None]:
    """Key observable parameters (class-specific extraction)."""
    params: dict[str, str | int | float | bool | None] = {}
    if type(tool).__name__ == "SubprocessTool":
        params["timeout"] = tool.timeout
    store = getattr(tool, "_store", None)  # todo tools
    if store is not None:
        params["store_class"] = type(store).__name__
    return params


def dump_tool_roster(
    tool_manager: Any,
    *,
    source_of: dict[str, str] | None = None,
) -> list[ToolEntry]:
    """Introspect a tool manager's actual contents into ToolEntry list.

    ``source_of`` maps tool name → provenance label; unmapped tools default
    to "glue" (BIZ-registered). Registration order is preserved — it is an
    observable product.
    """
    source_of = source_of or {}
    entries: list[ToolEntry] = []
    for name in tool_manager.list_tools():
        tool = tool_manager.get_tool(name)
        entries.append(
            ToolEntry(
                name=name,
                tool_class=type(tool).__name__,
                source=source_of.get(name, "glue"),
                params=_tool_params(tool),
            )
        )
    return entries


def trio_registry_shared(tool_manager: Any) -> bool | None:
    """Whether all present trio tools share one ProcessRegistry identity.

    ``None`` when no trio tools are registered (the use_terminal=false
    shape). Guards the ProcessRegistry split-brain class of bug
    (InMemoryToolManager.register silently overwrites — identity must be
    observed, not assumed).
    """
    registries: set[int] = set()
    present = False
    for name in ("bash", "process", "terminal"):
        tool = tool_manager.get_tool(name)
        if tool is None:
            continue
        registry = getattr(tool, "_registry", None)
        if registry is None:
            continue
        present = True
        registries.add(id(registry))
    if not present:
        return None
    return len(registries) == 1


# ── Full-pool introspection ─────────────────────────────────────────────


def _terminal_manager_summary(manager: Any) -> TerminalManagerSummary | None:
    if manager is None:
        return None
    visibility = getattr(manager, "visibility", None)
    shell_info = getattr(manager, "shell_info", None)
    family = getattr(shell_info, "family", None)
    return TerminalManagerSummary(
        manager_class=type(manager).__name__,
        visibility=visibility.value if visibility is not None else None,
        shell_family=family.value if family is not None else None,
    )


def _prompt_sha256(instance: Any) -> str | None:
    template = getattr(instance.descriptor, "system_prompt_template", None)
    if not template:
        return None
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def _react_hooks(instance: Any) -> list[HookEntry]:
    pipeline = getattr(instance, "pipeline", None)
    runner = getattr(pipeline, "hook_runner", None)
    if runner is None:
        return []
    entries: list[HookEntry] = []
    for spec in runner.hook_specs:
        hook = spec.hook
        entries.append(HookEntry(name=hook.name, hook_class=type(hook).__name__, runner="react"))
    return entries


def _main_pipeline(pool_instance: Any) -> Any | None:
    agents = getattr(pool_instance.pool, "_agents", {}) or {}
    instance = agents.get(pool_instance.root_agent_name)
    return getattr(instance, "pipeline", None) if instance is not None else None


def _interceptor_names(pool_instance: Any) -> list[str]:
    pipeline = _main_pipeline(pool_instance)
    chain = getattr(pipeline, "interceptor_chain", None) if pipeline else None
    if chain is None:
        return []
    return [type(i).__name__ for i in chain.interceptors]


def _command_names(pool_instance: Any) -> list[str] | None:
    pipeline = _main_pipeline(pool_instance)
    processor = getattr(pipeline, "command_processor", None) if pipeline else None
    if processor is None:
        return None
    handlers = getattr(processor, "_handlers", None)
    if handlers is None:
        return None
    return sorted(getattr(h, "name", type(h).__name__) for h in handlers)


def dump_memory_hooks(pool_data: Any) -> list[HookEntry]:
    """Introspect the pool's memory-system cleanup hooks.

    The memory lifecycle hooks (``UserNoticeCleanupHook``,
    ``TodoReorientationHook``) register on the memory system's
    ``MemoryHookRunner`` — a dispatch system separate from the react
    ``HookRunner`` — so the notification face needs its own introspection.
    Returns ``[]`` when no memory system is reachable (no pool_data).
    """
    memory_system = getattr(getattr(pool_data, "context_manager", None), "memory_system", None)
    runner = getattr(memory_system, "_hook_runner", None)  # noqa: SLF001
    hooks = getattr(runner, "_hooks", None) if runner is not None else None  # noqa: SLF001
    if not hooks:
        return []
    return [
        HookEntry(name=type(hook).__name__, hook_class=type(hook).__name__, runner="memory")
        for hook in hooks
    ]


def roster_source_map(
    registry: Any | None,
    spec_tools: list[str],
) -> dict[str, str]:
    """Map each roster tool's LLM-facing name → ``roster:<registration_source>``.

    The spec carries registry names (e.g. ``aci_edit``); the registered
    tool's LLM-facing name may differ (AciEditTool's name is ``edit`` —
    the documented same-name replacement, the ``aci`` capability's O3
    declaration). Instance-holding factories (SimpleFactory /
    PrototypeFactory) are probed for the tool's instance name so the
    rename stays attributed to the roster.
    """
    from modex_agent.plugins.abc import PrototypeFactory, SimpleFactory

    source_of: dict[str, str] = {}
    for name in spec_tools:
        source = "roster:direct"
        if registry is not None:
            reg_source = registry.registration_source(ComponentSlot.TOOL, name)
            if reg_source is not None:
                source = f"roster:{reg_source.value}"
        llm_name = name
        if registry is not None:
            factory = registry.resolve(ComponentSlot.TOOL, name)
            if isinstance(factory, (SimpleFactory, PrototypeFactory)):
                instance_name = getattr(factory.probe(), "name", None)
                if isinstance(instance_name, str):
                    llm_name = instance_name
        source_of[llm_name] = source
    return source_of


def dump_assembly_manifest(
    pool_instance: Any,
    *,
    data_dir: Any,
    source_of: dict[str, str],
    lazy_agents: list[AgentManifest] | None = None,
    memory_hooks: list[HookEntry] | None = None,
) -> AssemblyManifest:
    """Introspect a created PoolInstance into a frozen AssemblyManifest.

    ``source_of`` (see :func:`roster_source_map`) maps each roster tool's
    LLM-facing name → ``roster:<registration_source>`` (O2 audit surface);
    unmapped tools in the manager are BIZ glue.

    ``memory_hooks`` (see :func:`dump_memory_hooks`) records the memory
    runner's cleanup hooks (the notification face); ``None`` leaves the
    face empty — for drivers that build no pool_data it is unobserved.
    """
    # Todo store: the pool's ``todo`` capability supply (the same mapping
    # Stage 3 aggregated; the retired ``materialize_deps.todo_store`` typed
    # carrier died with the supply convergence) — supplied infra observed,
    # not assumed.
    deps = getattr(pool_instance.pool, "materialize_deps", None)
    store_obj = None
    supply = deps.capability_supply.get("todo") if deps is not None else None
    if supply is not None:
        store_obj = getattr(supply, "store", None)
    todo_summary: TodoStoreSummary | None = None
    if store_obj is not None:
        base_dir = getattr(store_obj, "_base_dir", None)
        if base_dir is not None:
            try:
                relative = base_dir.relative_to(data_dir)
            except ValueError:
                relative = base_dir
            # as_posix(): the manifest is a serialized artifact compared
            # against goldens generated on Linux — a native-separator path
            # would diverge on Windows for the same tree.
            todo_summary = TodoStoreSummary(
                store_class=type(store_obj).__name__,
                dir_relative=relative.as_posix(),
            )

    agents_registry = getattr(pool_instance.pool, "_agents", {}) or {}
    agents: list[AgentManifest] = []
    for agent_name, instance in agents_registry.items():
        agents.append(
            AgentManifest(
                agent_name=agent_name,
                materialized=True,
                tools=dump_tool_roster(pool_instance.tool_manager, source_of=source_of),
                hooks=_react_hooks(instance),
                memory_config=instance.descriptor.memory_config.model_dump(mode="json"),
                system_prompt_provider=type(instance.context_manager).__name__,
                system_prompt_sha256=_prompt_sha256(instance),
                llm_provider_class=(
                    type(pool_instance.provider).__name__
                    if pool_instance.provider is not None
                    else None
                ),
            )
        )

    comm_targets = [
        CommTargetEntry(target_name=t.name, target_kind=t.kind.value)
        for t in pool_instance.target_store.list()
    ]

    agents.extend(lazy_agents or [])

    return AssemblyManifest(
        pool_name=pool_instance.name,
        execution_strategy=pool_instance.main_execution_strategy.value,
        terminal_manager=_terminal_manager_summary(pool_instance.terminal_manager),
        trio_registry_shared=trio_registry_shared(pool_instance.tool_manager),
        todo_store=todo_summary,
        interceptors=_interceptor_names(pool_instance),
        commands=_command_names(pool_instance),
        comm_targets=comm_targets,
        memory_hooks=memory_hooks or [],
        agents=agents,
    )
