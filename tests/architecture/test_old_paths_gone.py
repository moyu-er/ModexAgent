"""Old-path and compatibility-re-export absence guards (plan §15 A1, §20.1).

The migration deletes old module paths with no shims ("Compatibility": no
re-export shim, alias, fallback, old module, or dual-read migration path).
This file is the append-only infrastructure for that rule:

- FORBIDDEN_MODULE_PATHS: relocated/deleted paths verified gone TODAY; work
  packages B1-E2 append the paths they delete.
- FORBIDDEN_COMPAT_REEXPORTS: (shim module, origin module) pairs meaning the
  shim module must not import names from the origin module. Empty today by
  design: the one known live shim is modex_agent.multi_agent.comm_kind
  re-exporting AgentCommKind from modex_agent.core.agent (ADR-0006
  candidate (1) deprecation window) — still present, so NOT assertable
  today. D2/E2 append ("modex_agent.multi_agent.comm_kind",
  "modex_agent.core.agent") after deleting the shim.

Never seed an entry that is not already true — the suite stays green with
every entry verifiable today.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "modex_agent"

# Paths relative to src/modex_agent (posix). All verified gone today.
FORBIDDEN_MODULE_PATHS: tuple[str, ...] = (
    # Graph engine relocated to the standalone modex_graph package
    # (ADR-0033 D13 Stage 4). Import references are guarded separately in
    # test_no_modex_agent_core_graph_imports.py.
    "core/graph",
    # Relocated to agents/react/message_builder.py (ADR-0006 candidate (6),
    # 2026-06-27) — utils must be a pure leaf.
    "utils/message_builder.py",
    # memory.core.{scope,message} re-export shims deleted (ADR-0006
    # candidate (2)); import references guarded in test_memory_shims_gone.py.
    "memory/core/scope.py",
    "memory/core/message.py",
    # A2 (plan §15): atomic-write helpers consolidated into
    # utils/file_io.py; safe_atomic_replace moved core→memory→utils until
    # the cycle died, then converged here. core/utils.py deleted.
    "core/utils.py",
    # A2 (plan §15): frontmatter parsing moved to utils/frontmatter.py
    # (pure leaf, stdlib+pyyaml only); core/frontmatter.py deleted.
    "core/frontmatter.py",
    # A2 (plan §15): ExperienceUsageTracker deleted — deprecated sidecar
    # replaced by PerFileExperienceMetaStore (core/experience/meta.py).
    "core/experience/usage.py",
    # B1 (plan §15): Session Artifacts moved to
    # persistence/session_artifacts/ (plan §12); the old core/persistence
    # homes are deleted with no shims.
    "core/cleanup.py",
    "core/session_cleanup.py",
    "core/session_scope_discovery.py",
    "persistence/session_cleanup.py",
    # B2 (plan §15): Runtime Context moved to runtime/context.py (+ the
    # hook to runtime/hooks.py, BusyInputMode to pipeline/busy_input.py);
    # the RuntimeContextStore hierarchy folded into RuntimeContextManager.
    "core/runtime_context.py",
    "core/agent_runtime_config.py",
    "hook/builtin/runtime_context.py",
    # B3 (plan §15): EventAssembler folded into core/stream_events.py —
    # provider-neutral stream folding no longer lives under the HTTP
    # adapter; deleted with no shim.
    "providers/http/assembler.py",
    # B4 (plan §15): Output/Emitter moved to adapters/ — OutputAdapter
    # family to adapters/output.py, StreamingAwareEmitter to
    # adapters/emitter.py, ContentFilter family to adapters/filters.py;
    # pipeline/filters.py deleted with no shim.
    "pipeline/filters.py",
    # C1 (plan §15): media contracts (Attachment, MediaStore, StoredFile,
    # StoredMediaKind, MediaRefCollisionError) promoted to core/media.py;
    # media/models.py deleted with no shim.
    "media/models.py",
    # C2 (plan §15): InMemoryToolManager moved to tools/manager.py;
    # DynamicSchemaProvider folded into Tool (core/tool_manager.py);
    # ToolRegistry compatibility shell deleted (deletion test passed —
    # get_definitions had zero callers, MCPToolRegistry zero users);
    # the modex_agent.registry re-export package deleted with it.
    "core/tool.py",
    "tools/registry.py",
    "registry/__init__.py",
    # D1 (plan §15): the Experience vertical slice moved into the
    # ``experience`` capability package
    # (plugins/defaults/capabilities/experience/); every old path is
    # deleted with no shims — the package is the sole implementation.
    "core/experience",
    "agents/experience",
    "hook/builtin/experience_review.py",
    "memory/tools/experience.py",
    "memory/prompts/experience",
    "plugins/defaults/capabilities/experience.py",
    "multi_agent/pool_config/experience.py",
)

# (shim module, origin module) — the shim must not import anything from the
# origin. Appended by the work package that deletes each shim; empty today.
FORBIDDEN_COMPAT_REEXPORTS: set[tuple[str, str]] = set()

# Symbols proven dead and deleted by work packages (plan §15 A2). Grepped in
# src/ so re-introduction fails loudly. Lives here rather than in
# test_dead_code_gone.py because that file's contract is scoped to the
# ADR-0007 candidate-④ control-plane removals.
FORBIDDEN_SYMBOLS: tuple[str, ...] = (
    # A2: deprecated sidecar tracker replaced by PerFileExperienceMetaStore.
    "ExperienceUsageTracker",
    # A2: dormant LLM factory config — zero production consumers; live LLM
    # wiring is ioc/configs/llm.py LLMConfig + providers/http. Its private
    # enum and DefaultValues members died with it.
    "LLMProviderConfig",
    "LLMProviderKind",
    # C2: deleted ToolManager configuration shells. ToolManagerConfig was
    # empty (zero field readers); ToolRegistry/MCPToolRegistry were
    # compatibility subclasses of InMemoryToolManager with zero live users.
    "ToolManagerConfig",
    "ToolRegistry",
    "MCPToolRegistry",
    # C2: DynamicSchemaProvider folded into Tool — Tool is its only
    # implementer; the separate ABC is deleted.
    "DynamicSchemaProvider",
)


def _existing_forbidden_paths(root: Path) -> list[str]:
    return [rel for rel in FORBIDDEN_MODULE_PATHS if (root / rel).exists()]


def _module_file(module: str) -> Path | None:
    rel = Path(*module.split("."))
    for candidate in (rel.with_suffix(".py"), rel / "__init__.py"):
        file = PACKAGE_ROOT / candidate
        if file.is_file():
            return file
    return None


def _reexports_from(shim_path: Path, origin: str) -> bool:
    """Any import of `origin` in the shim — a deleted shim leaves zero
    references, so TYPE_CHECKING imports count too."""
    tree = ast.parse(shim_path.read_text(encoding="utf-8"))
    parent, _, last = origin.rpartition(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == origin or node.module.startswith(origin + "."):
                return True
            if node.module == parent and any(
                alias.name == last for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == origin or alias.name.startswith(origin + "."):
                    return True
    return False


def test_old_module_paths_stay_gone() -> None:
    resurrected = _existing_forbidden_paths(PACKAGE_ROOT)
    assert not resurrected, (
        "Deleted module paths re-introduced under src/modex_agent "
        "(plan §20.1 'No old module path exists'; provenance in "
        "FORBIDDEN_MODULE_PATHS): "
        f"{resurrected}"
    )


def test_no_compat_reexport_shims() -> None:
    offenders: list[str] = []
    for shim, origin in sorted(FORBIDDEN_COMPAT_REEXPORTS):
        shim_file = _module_file(shim)
        if shim_file is not None and _reexports_from(shim_file, origin):
            offenders.append(f"{shim} re-exports from {origin}")
    assert not offenders, (
        "Compatibility re-export shims still present (plan §20.1 'No "
        f"compatibility re-export or fallback remains'): {offenders}"
    )


def test_forbidden_path_list_is_nonempty() -> None:
    """Sanity: the guard must actually watch something."""
    assert FORBIDDEN_MODULE_PATHS


def test_forbidden_symbols_absent_from_src() -> None:
    _pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in FORBIDDEN_SYMBOLS) + r")\b")
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        hits = set(_pattern.findall(path.read_text(encoding="utf-8")))
        if hits:
            offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {sorted(hits)}")
    assert not offenders, (
        "Symbols deleted as proven dead re-introduced under src/modex_agent "
        f"(provenance in FORBIDDEN_SYMBOLS): {offenders}"
    )
