"""Architecture guard: eval pool assembly is frozen after ``create_pool``."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HARBOR = REPO_ROOT / "examples" / "bot_project" / "bot" / "eval" / "harbor"
EVAL_ROOT = REPO_ROOT / "examples" / "bot_project" / "bot" / "eval"
SCANNED_MODULES = (
    HARBOR / "pool_mode.py",
    HARBOR / "pool_mode_assembly.py",
)
MUTATING_METHODS = frozenset({"register", "unregister"})
MUTATION_ALLOWLIST: frozenset[str] = frozenset()
ALLOWED_RUNNER_MODULES = frozenset(
    {
        # The plan's per-turn data-injection clause reserves these runner-plane
        # modules; every other eval module remains in the assembly-free zone.
        EVAL_ROOT / "experiment_runner.py",
        EVAL_ROOT / "memory_harness.py",
        EVAL_ROOT / "replay.py",
        EVAL_ROOT / "cli.py",
    }
)


def _walk_scope(scope: ast.AST) -> Iterator[ast.AST]:
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        yield child
        yield from _walk_scope(child)


def _assigned_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.List | ast.Tuple):
        return {name for element in target.elts for name in _assigned_names(element)}
    return set()


def _call_name(value: ast.expr | None) -> str | None:
    if isinstance(value, ast.Await):
        value = value.value
    if not isinstance(value, ast.Call):
        return None
    if isinstance(value.func, ast.Name):
        return value.func.id
    if isinstance(value.func, ast.Attribute):
        return value.func.attr
    return None


def _annotation_mentions_pool_instance(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    return any(
        (isinstance(node, ast.Name) and node.id == "PoolInstance")
        or (isinstance(node, ast.Attribute) and node.attr == "PoolInstance")
        for node in ast.walk(annotation)
    )


def _root_name(expression: ast.expr) -> str | None:
    while isinstance(expression, ast.Attribute | ast.Subscript):
        expression = expression.value
    return expression.id if isinstance(expression, ast.Name) else None


def _contains_tool_manager(expression: ast.expr) -> bool:
    return any(
        isinstance(node, ast.Attribute) and node.attr == "tool_manager"
        for node in ast.walk(expression)
    )


def _scope_names(
    scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
    nodes: tuple[ast.AST, ...],
) -> tuple[set[str], set[str]]:
    pool_names: set[str] = set()
    if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        arguments = (
            *scope.args.posonlyargs,
            *scope.args.args,
            *scope.args.kwonlyargs,
        )
        pool_names.update(
            argument.arg
            for argument in arguments
            if _annotation_mentions_pool_instance(argument.annotation)
        )

    assignments: list[tuple[set[str], ast.expr | None]] = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            assignments.extend((_assigned_names(target), node.value) for target in node.targets)
        elif isinstance(node, ast.AnnAssign):
            names = _assigned_names(node.target)
            assignments.append((names, node.value))
            if _annotation_mentions_pool_instance(node.annotation):
                pool_names.update(names)

    for names, value in assignments:
        if _call_name(value) == "create_pool":
            pool_names.update(names)

    changed = True
    while changed:
        changed = False
        for names, value in assignments:
            if isinstance(value, ast.Name) and value.id in pool_names:
                before = len(pool_names)
                pool_names.update(names)
                changed = changed or len(pool_names) != before

    manager_names = {
        name
        for names, value in assignments
        if value is not None and _contains_tool_manager(value)
        for name in names
    }
    changed = True
    while changed:
        changed = False
        for names, value in assignments:
            if isinstance(value, ast.Name) and value.id in manager_names:
                before = len(manager_names)
                manager_names.update(names)
                changed = changed or len(manager_names) != before
    return pool_names, manager_names


def _scope_offenders(
    path: Path,
    scope: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    nodes = tuple(_walk_scope(scope))
    pool_names, manager_names = _scope_names(scope, nodes)
    offenders: list[str] = []
    for node in nodes:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if node.func.attr in MUTATING_METHODS and (
                _contains_tool_manager(receiver)
                or (isinstance(receiver, ast.Name) and receiver.id in manager_names | pool_names)
            ):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {ast.unparse(node)}"
                )
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if isinstance(target, ast.Attribute) and _root_name(target) in pool_names:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {ast.unparse(node)}"
                    )
    return offenders


def _symbol_offenders(path: Path, symbol: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == symbol:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {ast.unparse(node)}"
                    )
        elif isinstance(node, ast.Call):
            called = node.func
            if (isinstance(called, ast.Name) and called.id == symbol) or (
                isinstance(called, ast.Attribute) and called.attr == symbol
            ):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {ast.unparse(node)}"
                )
    return offenders


def test_eval_pool_has_no_post_assembly_mutation() -> None:
    offenders: list[str] = []
    for path in SCANNED_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(_scope_offenders(path, tree))
        for function in (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        ):
            allowlist_key = f"{path.name}::{function.name}"
            if allowlist_key not in MUTATION_ALLOWLIST:
                offenders.extend(_scope_offenders(path, function))

    assert not offenders, (
        "eval mutates a PoolInstance after assembly:\n  "
        + "\n  ".join(offenders)
        + "\nPlan axiom: post-assembly structure is frozen; express deviations through "
        "a pre-compile overlay or an explicit typed infrastructure kwarg/registry decorator."
    )


def test_eval_agent_assembly_uses_declared_single_agent_seam() -> None:
    react_offenders: list[str] = []
    context_offenders: list[str] = []
    for path in EVAL_ROOT.rglob("*.py"):
        react_offenders.extend(_symbol_offenders(path, "ReActAgent"))
        if path not in ALLOWED_RUNNER_MODULES:
            context_offenders.extend(_symbol_offenders(path, "AgentContext"))

    assert not react_offenders, (
        "eval binds or constructs ReActAgent directly:\n  "
        + "\n  ".join(react_offenders)
        + "\nAssemble through assemble_declared_single_agent instead."
    )
    assert not context_offenders, (
        "eval assembly binds or constructs AgentContext directly:\n  "
        + "\n  ".join(context_offenders)
        + "\nUse the assembled pipeline context builder; only named runner/data-injection "
        "modules may construct per-turn contexts."
    )
