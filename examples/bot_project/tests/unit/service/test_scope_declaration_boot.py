"""Ticket 07 — scope-declaration boot wiring tests.

The boot sequence (``boot_scope_declaration``): load → validate the FULL
declaration (phase 1 incl. the V10 graph cross-check; phase 2 over the
compiled effective configs) → compile. Covers the boot half of the ACs:

- (f) all four shipped declarations pass V1-V11 with the real graphs;
- boot failure is fatal and carries ALL issues;
- (g) the ACI ``edit ← aci`` replacement records are logged at boot;
- (a) the declaration-path module imports no per-agent component
  construction symbols (import-level architecture guard);
- AC (b)'s compile-level half: the review root derives ``task`` with
  targets explore+general — same rule as today's legacy behavior.
"""

from __future__ import annotations

import ast
import logging
import re
import sys
from functools import lru_cache
from pathlib import Path

import pytest
from bot.service.pool import declaration as declaration_module
from bot.service.pool.declaration import (
    ScopeBootError,
    boot_scope_declaration,
    declared_pool_build,
)
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER

from modex_agent.ioc.configs.observability import ObservabilityConfig, TraceBackend
from modex_agent.plugins.capability import CapabilityError
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry

sys.path.insert(0, str(Path(__file__).parents[3]))

BOT_BASE = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "scope_boot"

FORBIDDEN_IMPORT_SYMBOLS = (
    # tool construction (per-agent components)
    "TaskDispatchTool",
    "SendToPeerTool",
    "SendToAgentTool",
    "CommandTool",
    "ProcessTool",
    "TerminalTool",
    "SubprocessTool",
    "TodoWriteTool",
    "TodoReadTool",
    # legacy communication registration (the road this ticket replaces)
    "register_communication_tools",
    "_build_communication",
    # hook construction
    "UserNoticeCleanupHook",
    "TodoReorientationHook",
    "SubagentAutoSendHook",
    "NativeEnvInjectionHook",
    "CurrentTimeInjectionHook",
    "KnowledgeHook",
    "LoopDetectionHook",
    "CheckpointHook",
    # MCP loading — FW-side since ticket 10 (Stage 4 reads the chain's
    # shared-connection handle)
    "MCPClientManager",
    "acquire_mcp_tools",
    "load_per_agent_mcp",
    "_load_agent_mcp_tools",
)

# Tool-construction + MCP symbols banned across the WHOLE create_pool
# subpackage (both boot roads flow through it — after ticket 10 nothing
# there constructs tools or loads MCP directly).
_POOL_WIDE_FORBIDDEN = (
    "CommandTool",
    "ProcessTool",
    "TerminalTool",
    "SubprocessTool",
    "TodoWriteTool",
    "TodoReadTool",
    "MCPClientManager",
    "acquire_mcp_tools",
    "load_per_agent_mcp",
    "_load_agent_mcp_tools",
)

# Communication-TOOL construction + the legacy registration call: banned
# across pool/ — the declaration road resolves its communication tools at
# Stage 4 from the compiled spec's derived entries. communication.py now
# hosts only UserNoticeCleanupHook (HOOK-slot roster reference); the
# template-scan builder and the registration call are deleted with the
# roster road.
_COMMUNICATION_SYMBOLS = (
    "TaskDispatchTool",
    "SendToPeerTool",
    "SendToAgentTool",
    "register_communication_tools",
)

_POOL_DIR = BOT_BASE / "bot" / "service" / "pool"


@lru_cache(maxsize=1)
def _component_registry() -> ComponentRegistry:
    """DefaultPlugin registry — the shipped declaration's
    ``capabilities:`` blocks resolve against it at compile."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _boot(
    declaration_path: Path,
    tmp_path: Path,
    graphs_dirs=(),
):
    return boot_scope_declaration(
        declaration_path=declaration_path,
        project_dir=BOT_BASE,
        data_dir=tmp_path / ".modex",
        graphs_dirs=graphs_dirs or (BOT_BASE / "config" / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
        registry=_component_registry(),
    )


# ── Boot: happy path (AC f) ────────────────────────────────────────────


def test_boot_validates_all_four_declarations(tmp_path: Path) -> None:
    """All 4 shipped declarations pass V1-V11 (incl. V10 against the
    shipped review_cycle graph) and compile to 9 agents."""
    boot = _boot(BOT_BASE / "config" / "scopes" / "bot.yml", tmp_path)
    pools = {agent.provenance.pool for agent in boot.compilation.agents}
    assert pools == {"default", "coder", "review", "opencode"}
    assert len(boot.compilation.agents) == 9
    # V10 actually consumed the graph references: review_cycle's
    # (review, reviewer) and (coder, orchestrator) resolved against the tree.
    assert boot.spec.workspace is not None
    assert {pool.name for pool in boot.spec.workspace.pools} == pools


def test_boot_v10_missing_graph_agent_fails_loud(tmp_path: Path) -> None:
    """A graph referencing an agent the declaration does not carry aborts
    boot with a V10 issue (one startup cycle earlier than the runtime
    RuntimeError BotAgentNode used to raise)."""
    with pytest.raises(ScopeBootError) as excinfo:
        _boot(
            BOT_BASE / "config" / "scopes" / "bot.yml",
            tmp_path,
            graphs_dirs=(FIXTURES / "graphs_ghost",),
        )
    assert "V10" in str(excinfo.value)
    assert "does-not-exist" in str(excinfo.value)


def test_boot_v3_and_v11_issues_all_carried(tmp_path: Path) -> None:
    """Multiple declaration-shape violations surface TOGETHER in one fatal
    boot error (no first-issue-only truncation)."""
    with pytest.raises(ScopeBootError) as excinfo:
        _boot(FIXTURES / "v3_v11_pool.yml", tmp_path)
    message = str(excinfo.value)
    assert "V3" in message
    assert "V11" in message
    assert excinfo.value.issues  # the structured list carries every issue


def test_boot_v6_missing_task_fails_phase2(tmp_path: Path) -> None:
    """A child-carrying root whose wholesale tools list drops ``task``
    aborts boot — since the subagents migration, at COMPILE time: the
    capability's bind anchor (the V6 dual check's richer-error layer)
    fires one boot cycle before the phase-2 validator. The registry-less
    phase-2 V6 layer keeps its own boot pin in
    ``test_boot_v6_mid_level_missing_task_fails_phase2`` below."""
    with pytest.raises(CapabilityError) as excinfo:
        _boot(
            FIXTURES / "v6_missing_task.yml",
            tmp_path,
            graphs_dirs=(tmp_path / "no-graphs",),
        )
    message = str(excinfo.value)
    assert "'subagents'" in message
    assert "V6 dual check" in message
    assert "'task'" in message


def test_boot_v9_non_root_approval_fails_phase2(tmp_path: Path) -> None:
    """Ticket 09 V9 regression (boot wiring): a non-root agent declaring
    approval aborts STARTUP — the phase-2 validation runs inside
    ``boot_scope_declaration``, so the positional approval gate is enforced
    one boot cycle before any agent could run. (The pure-function halves
    live in tests/unit/scope/test_validator.py; this pins the boot
    integration.)"""
    with pytest.raises(ScopeBootError) as excinfo:
        _boot(
            FIXTURES / "v9_non_root_approval.yml",
            tmp_path,
            graphs_dirs=(tmp_path / "no-graphs",),
        )
    assert "V9" in str(excinfo.value)
    assert "phase-2" in str(excinfo.value)
    assert "sub" in str(excinfo.value)


def test_boot_v5_cross_workspace_peer_fails_startup(tmp_path: Path) -> None:
    """Ticket 13 V5 (boot wiring): a peer link to a pool the workspace
    does not host — the v1 cross-workspace shape — aborts STARTUP with
    the V5 rule and the same-workspace guidance (N5: no cross-workspace
    peer in v1)."""
    with pytest.raises(ScopeBootError) as excinfo:
        _boot(
            FIXTURES / "v5_cross_workspace_peer.yml",
            tmp_path,
            graphs_dirs=(tmp_path / "no-graphs",),
        )
    assert "V5" in str(excinfo.value)
    assert "same-workspace only" in str(excinfo.value)
    assert "ghost" in str(excinfo.value)


def test_boot_v5_pool_as_root_peer_fails_startup(tmp_path: Path) -> None:
    """Ticket 13 V5 (boot wiring): a pool-as-root declaration (no
    workspace layer) carrying peers aborts STARTUP with the v1 rule
    spelled out — the same-workspace premise is undefined without a
    workspace layer."""
    with pytest.raises(ScopeBootError) as excinfo:
        _boot(
            FIXTURES / "v5_pool_as_root_peer.yml",
            tmp_path,
            graphs_dirs=(tmp_path / "no-graphs",),
        )
    assert "V5" in str(excinfo.value)
    assert "cannot declare peers" in str(excinfo.value)


# ── Boot: ACI accounting (AC g) ────────────────────────────────────────


def test_aci_replacement_logged_at_boot(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The ``edit ← aci`` replacement records (ticket 06's provenance)
    surface in the boot log for every agent whose roster opts into the
    aci supplement — the default root and office-expert."""
    with caplog.at_level(logging.INFO, logger="bot.service.pool.declaration"):
        _boot(BOT_BASE / "config" / "scopes" / "bot.yml", tmp_path)
    records = [r.getMessage() for r in caplog.records if "replaced by" in r.getMessage()]
    assert any(
        "pool 'default' agent 'default'" in m and "'edit' replaced by 'aci_edit'" in m
        for m in records
    )
    assert any(
        "pool 'default' agent 'office-expert'" in m and "'edit' replaced by 'aci_edit'" in m
        for m in records
    )


# ── AC (b) compile-level half: the derivation rule ─────────────────────


def test_review_root_task_derivation_matches_legacy_behavior(tmp_path: Path) -> None:
    """The review root has two declared children (explore + general), so it
    derives ``task`` with BOTH as targets — the same rule today's legacy
    registration applies (communication.py registers task when templates
    exist); leaves derive no task."""
    boot = _boot(BOT_BASE / "config" / "scopes" / "bot.yml", tmp_path)
    review_root = next(
        agent
        for agent in boot.compilation.agents
        if agent.provenance.pool == "review" and agent.spec.agent_type.value == "native_main"
    )
    task_entry = next(entry for entry in review_root.provenance.tools if entry.tool == "task")
    assert task_entry.targets == ["explore", "general"]
    assert "task" in review_root.effective.tools

    for leaf_name in ("explore", "general"):
        leaf = next(
            agent
            for agent in boot.compilation.agents
            if agent.provenance.pool == "review" and agent.provenance.agent == leaf_name
        )
        assert "task" not in leaf.effective.tools, (
            "leaves with no declared children get NO task tool (SPEC §3.2)"
        )
        assert "send_to_agent" in leaf.effective.tools


def test_pivot_selection_is_default_and_opencode(tmp_path: Path) -> None:
    """Ticket 11: the dual-boot pivot set is gone — every pool the
    declaration hosts boots the declaration road (the constant
    ``DECLARATION_BOOT_POOLS`` died with the full switch)."""
    from bot.workspace.wiring.resources import _declaration_road_pools

    boot = _boot(BOT_BASE / "config" / "scopes" / "bot.yml", tmp_path)
    assert _declaration_road_pools(boot) == ["default", "coder", "review", "opencode"]


def test_declared_pool_build_partitions_default(tmp_path: Path) -> None:
    """The pivot pool's build products: one root + one lazy subagent whose
    template carries the compiled spec."""
    boot = _boot(BOT_BASE / "config" / "scopes" / "bot.yml", tmp_path)
    build = declared_pool_build(boot, "default")
    assert build.root.provenance.agent == "default"
    assert [s.provenance.agent for s in build.subagents] == ["office-expert"]
    template = build.template_registry.get_template("default", "office-expert")
    assert template is not None
    assert template.compiled_spec is build.subagents[0].spec

    with pytest.raises(ValueError, match="declares no pool"):
        declared_pool_build(boot, "no-such-pool")


# ── AC (a)/(f): import-level architecture guard ────────────────────────


def _imported_symbols(source_path: Path) -> set[str]:
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
            if node.module:
                imported.add(node.module.split(".")[-1])
    return imported


def test_declaration_module_imports_no_component_construction_symbols() -> None:
    """The declaration-path modules construct ZERO per-agent components:
    no tool, hook, MCP-loading, or communication-registration construction
    symbol may be imported (deterministic AST check)."""
    declaration_sources = [
        Path(declaration_module.__file__),
        Path(declaration_module.__file__).with_name("declaration_graphs.py"),
    ]
    for source_path in declaration_sources:
        violations = _imported_symbols(source_path) & set(FORBIDDEN_IMPORT_SYMBOLS)
        assert not violations, (
            f"declaration-path module {source_path.name} imports per-agent "
            f"component construction symbols: {sorted(violations)}"
        )


def test_pool_subpackage_imports_no_tool_construction_symbols() -> None:
    """Ticket 10 final form: NO module in the create_pool subpackage
    (both boot roads flow through it) may import a tool-construction or
    MCP-loading symbol — per-agent tools and MCP tools resolve at Stage 4
    through the registry / the FW loader reading the context chain."""
    for py_file in sorted(_POOL_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        violations = _imported_symbols(py_file) & set(_POOL_WIDE_FORBIDDEN)
        assert not violations, (
            f"{py_file.name} imports tool-construction / MCP-loading "
            f"symbols: {sorted(violations)} — per-agent tools resolve at "
            "Stage 4 through the registry, never BIZ construction"
        )


def test_communication_tool_construction_confined_to_legacy_module() -> None:
    """Communication-tool construction and the legacy registration paths
    live ONLY in communication.py — the legacy-road module that dies with
    ticket 17. Every other pool/ module (including the shared factory /
    pipeline wiring the declaration road runs through) must be clean."""
    for py_file in sorted(_POOL_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts or py_file.name == "communication.py":
            continue
        violations = _imported_symbols(py_file) & set(_COMMUNICATION_SYMBOLS)
        assert not violations, (
            f"{py_file.name} imports communication construction symbols "
            f"{sorted(violations)} — confined to communication.py "
            "(legacy road, dies with ticket 17)"
        )


def test_pool_subpackage_has_no_agent_type_external_branches() -> None:
    """Convergence rule 1 (ticket 10): the create_pool caller carries ZERO
    ``AgentType.external_*`` identity branches — the external shape's
    assembly differences express through the strategy's capability flags
    (``requires_main_agent_tools`` / ``requires_llm_provider``) and the
    strategy component itself. declaration.py's root-type classification
    frozenset is not a branch and does not match this pattern."""
    pattern = re.compile(r"agent_type\s+(?:is\s+not\s+|is\s+)AgentType\.external")
    for py_file in sorted(_POOL_DIR.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        matches = pattern.findall(py_file.read_text(encoding="utf-8"))
        assert not matches, (
            f"{py_file.name} branches on AgentType.external* "
            f"({len(matches)} matches) — the external shape's differences "
            "belong inside the strategy component (capability flags), not "
            "caller-side identity branches"
        )


# ── Ticket 12: nested declaration tree products ────────────────────────

_E2E_FIXTURE = Path(__file__).parents[2] / "integration" / "fixtures" / "scope"


def _boot_nested(tmp_path: Path):
    """Boot the ticket-12 E2E fixture (3-level tree + peer link + graph
    referencing the leaf) — the same single config the integration test
    drives end to end."""
    return boot_scope_declaration(
        declaration_path=_E2E_FIXTURE / "nested-tree-e2e.yml",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
        graphs_dirs=(_E2E_FIXTURE / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
        # The tree derivation (task/send_to_agent/send_to_peer) is
        # capability-contributed since the subagents migration — the boot
        # registry resolves it at compile.
        registry=_component_registry(),
    )


def test_declared_pool_build_nested_tree_direct_children(tmp_path: Path) -> None:
    """The nested tree's build products follow the direct-children rule
    (SPEC §3.2): the root's store face lists ONLY `mid` (never the
    grandchild `leaf`), the mid template carries `leaf` as its children,
    and the leaf template carries none — its compiled spec has no `task`
    entry at all (not empty-enabled)."""
    boot = _boot_nested(tmp_path)
    build = declared_pool_build(boot, "main-pool")

    assert build.root.provenance.agent == "root"
    assert [s.provenance.agent for s in build.subagents] == ["mid", "leaf"]
    assert [c.provenance.agent for c in build.root_children] == ["mid"]

    mid_template = build.template_registry.get_template("main-pool", "mid")
    leaf_template = build.template_registry.get_template("main-pool", "leaf")
    assert mid_template is not None and leaf_template is not None
    assert [c.name for c in mid_template.children] == ["leaf"]
    assert leaf_template.children == ()
    assert "task" in mid_template.compiled_spec.tools
    assert "task" not in leaf_template.compiled_spec.tools
    assert "send_to_agent" in leaf_template.compiled_spec.tools


def test_declared_pool_build_peer_pool_roots(tmp_path: Path) -> None:
    """The peer pool's build products: its root + no subagents, and the
    V10 graph cross-check passed against the leaf reference (the graph
    referencing (main-pool, leaf) resolved during boot)."""
    boot = _boot_nested(tmp_path)
    build = declared_pool_build(boot, "peer-pool")
    assert build.root.provenance.agent == "peerroot"
    assert build.subagents == ()
    assert build.root_children == ()


def test_boot_v6_mid_level_missing_task_fails_phase2(tmp_path: Path) -> None:
    """Ticket 12 V6 negative (AC e): a MID-LEVEL agent declaring a child
    whose wholesale tools list drops ``task`` aborts boot at phase 2 — the
    declared subtree under `mid` would be unreachable even though the
    root itself keeps its own dispatch surface."""
    declaration = tmp_path / "v6_mid_missing_task.yml"
    declaration.write_text(
        """
pool:
  name: orphaned-mid
  agents:
    root:
      agents:
        mid:
          tools:
          - read
          agents:
            leaf: {}
""",
        encoding="utf-8",
    )
    with pytest.raises(ScopeBootError) as excinfo:
        boot_scope_declaration(
            declaration_path=declaration,
            project_dir=tmp_path,
            data_dir=tmp_path / ".modex",
            graphs_dirs=(tmp_path / "no-graphs",),
            default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
            # The tracing fallback is observability-driven; OFF keeps this
            # registry-less boot free of injected capabilities.
            observability=ObservabilityConfig(trace_backend=TraceBackend.OFF),
        )
    assert "V6" in str(excinfo.value)
    assert "phase-2" in str(excinfo.value)
    assert "mid" in str(excinfo.value)
