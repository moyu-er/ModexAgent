"""Architecture guard: no execution_strategy branching in assembly or pipeline (ADR-0025).

Asserts that pool_builder.create_pool and AgentPipeline.__init__ source code
contain no `if is_external` or strategy-specific `if execution_strategy ==`
assembly branches. The execution-strategy refactor (ADR-0025) replaced
scattered if-else strategy branching with an ExecutionStrategy ABC + registry;
this guard prevents regression.

Allowed residual `execution_strategy ==` references in the framework
(all are runtime per-target behaviour or runtime dispatch, NOT assembly
branching — per ADR-0025 D5):
- `peer_normal.py` — runtime per-target routing (which reply mechanism a
  *target* agent uses). This is runtime routing, not assembly branching.
- `factory.py` — `_get_builder` runtime agent-construction dispatch (selects
  ExternalAgentBuilder vs ReActAgentBuilder). This is runtime
  construction, not assembly branching.
- `subagent_validator.py` — runtime subagent registration validation.
- `pool_config/specs.py` — Pydantic `@model_validator` cross-field validation
  (provider_kind set iff execution_strategy == EXTERNAL).
- `communication/strategies/subagent_dispatch.py` — `SubagentDispatchStrategy.build_result`
  selects ack field shape (output_path/trace_dir omitted for external targets)
  based on `req.target.execution_strategy`. Same per-target runtime category
  as `peer_normal.py`; added when ADR-0027 (external coding subagent) introduced
  the external-result shape.

Any other file containing `execution_strategy ==` is a regression.

Prior art: test_pipeline_god_object_gone.py (same AST/source-scan pattern).
"""
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FRAMEWORK_SRC = _REPO_ROOT / "src" / "modex_agent"
_POOL_DIR = _REPO_ROOT / "examples" / "bot_project" / "bot" / "service" / "pool"
_PIPELINE = _FRAMEWORK_SRC / "pipeline" / "pipeline.py"

# Files allowed to contain `execution_strategy ==` (runtime dispatch/routing/
# validation/docstring, NOT assembly branching — per ADR-0025 D5).
# - peer_normal.py — runtime per-target routing (which reply mechanism).
# - factory.py — _get_builder runtime agent-construction dispatch.
# - subagent_validator.py — runtime subagent registration validation.
# - execution_strategy.py — docstring text (the phrase "if execution_strategy =="
#   appears in the module docstring describing what the ABC replaces).
# - pool_config/specs.py — Pydantic @model_validator cross-field validation
#   (provider_kind set iff execution_strategy == EXTERNAL). Same
#   validation category as subagent_validator.py; not assembly branching.
# - communication/strategies/subagent_dispatch.py — build_result picks ack
#   field shape (output_path/trace_dir omitted for external targets) based on
#   req.target.execution_strategy. Same per-target runtime category as
#   peer_normal.py; added with ADR-0027 (external coding subagent).
# - template.py — materialize's early dispatch of EXTERNAL subagents to
#   ExecutionStrategy.assemble_sub (ADR-0025 D5 runtime dispatch category;
#   the react path below it is the default). Restored with the direct
#   subagent construction path (SPEC Errata-5).
_ALLOWED_EXECUTION_STRATEGY_FILES = {
    _FRAMEWORK_SRC / "multi_agent" / "template.py",
    _FRAMEWORK_SRC / "multi_agent" / "communication" / "strategies" / "peer_normal.py",
    _FRAMEWORK_SRC / "multi_agent" / "factory.py",
    _FRAMEWORK_SRC / "multi_agent" / "subagent_validator.py",
    _FRAMEWORK_SRC / "multi_agent" / "execution_strategy.py",
    _FRAMEWORK_SRC / "multi_agent" / "pool_config" / "specs.py",
    _FRAMEWORK_SRC / "multi_agent" / "communication" / "strategies" / "subagent_dispatch.py",
    # scope/spec.py: the AgentSpec model validator enforcing the
    # provider_kind ↔ external pairing — a runtime validation site (D5
    # category), not an assembly branch (ticket 02; allowlist gap found
    # during ticket 09's gate run).
    _FRAMEWORK_SRC / "scope" / "spec.py",
}

# Patterns that indicate strategy-specific assembly branching (forbidden).
# `if is_external` — any usage (the old pipeline branch pattern).
# `^if\s+.*execution_strategy\s*==` (MULTILINE) — a block-level if statement
#   branching on execution_strategy. The ternary form
#   `x = "a" if execution_strategy == Y else "b"` (strategy-name selection) is
#   ALLOWED — it is a 1-line dispatch, not an assembly path branch.
_FORBIDDEN_PATTERNS = [
    re.compile(r"\bis_external\b"),
    re.compile(r"^\s*if\s+.*execution_strategy\s*==", re.MULTILINE),
]


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _files_with_execution_strategy_compare(root: Path) -> set[Path]:
    """Return all .py files under root containing `execution_strategy ==`."""
    pattern = re.compile(r"execution_strategy\s*==")
    found: set[Path] = set()
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            found.add(path.resolve())
    return found


def test_pool_builder_create_pool_has_no_strategy_branching() -> None:
    """pool/ subpackage must not contain `if is_external` or
    `if execution_strategy ==` assembly branches.

    The function may use `execution_strategy ==` for a 1-line strategy-name
    selection (ternary), but not for branching the assembly path. This test
    checks ALL .py files in the pool/ subpackage for the forbidden patterns;
    the ternary at factory.py line ~186 (`strategy_name = "external" if ... else "react"`)
    uses `if` inline but does NOT match `if\\s+.*execution_strategy\\s*==` (no
    leading `if` keyword on the same statement as the comparison in a branch).
    """
    for py_file in _POOL_DIR.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        source = _source(py_file)
        for pattern in _FORBIDDEN_PATTERNS:
            matches = pattern.findall(source)
            assert not matches, (
                f"{py_file.name} contains forbidden strategy-branching pattern "
                f"{pattern.pattern!r} ({len(matches)} matches). ADR-0025 requires "
                f"zero strategy-specific branching in create_pool."
            )


def test_pipeline_init_has_no_strategy_branching() -> None:
    """AgentPipeline.__init__ must not contain `if is_external` or
    `if execution_strategy ==` branches. The pipeline accepts a TurnRunner
    (ABC) parameter; strategy selection happens in the factory, not the pipeline.
    """
    source = _source(_PIPELINE)
    for pattern in _FORBIDDEN_PATTERNS:
        matches = pattern.findall(source)
        assert not matches, (
            f"pipeline.py contains forbidden strategy-branching pattern "
            f"{pattern.pattern!r} ({len(matches)} matches). ADR-0025 D4 requires "
            f"the pipeline to be strategy-agnostic."
        )


def test_execution_strategy_compare_only_in_allowed_files() -> None:
    """`execution_strategy ==` in src/modex_agent/ must appear only in the
    explicitly-allowed files (per ADR-0025 D5: runtime per-target routing,
    runtime agent-construction dispatch, runtime validation, or docstring
    text — NOT assembly branching). Any other file is a regression.
    """
    offenders = _files_with_execution_strategy_compare(_FRAMEWORK_SRC)
    unexpected = offenders - {p.resolve() for p in _ALLOWED_EXECUTION_STRATEGY_FILES}
    assert not unexpected, (
        f"`execution_strategy ==` found in unexpected framework files "
        f"(ADR-0025 D5 allows only the runtime per-target / runtime dispatch / "
        f"runtime validation / docstring sites listed in "
        f"_ALLOWED_EXECUTION_STRATEGY_FILES):\n"
        + "\n".join(f"  {p.relative_to(_REPO_ROOT)}" for p in sorted(unexpected))
    )


def test_pipeline_init_param_count() -> None:
    """AgentPipeline.__init__ must have at most 14 parameters (ADR-0025 D4).

    The slimmed constructor accepts: agent, turn_runner, input_adapter,
    output_adapter, registry, safety, router, command_processor, deduplicator,
    busy_input_mode, control_channel, dream_engine, dream_interval (13 params).
    Allow 14 as a ceiling to accommodate minor additions without breaking the
    guard; anything higher indicates the god-object has regressed.
    """
    import ast

    source = _source(_PIPELINE)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "AgentPipeline":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
                    arg_count = len(item.args.args) + len(item.args.kwonlyargs)
                    assert arg_count <= 14, (
                        f"AgentPipeline.__init__ has {arg_count} params "
                        f"(ADR-0025 D4 target is 13, ceiling 14). "
                        f"The god-object has regressed."
                    )
                    return
    pytest.fail("AgentPipeline.__init__ not found in pipeline.py")
