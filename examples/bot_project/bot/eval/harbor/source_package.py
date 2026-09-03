from __future__ import annotations

import ast
import gzip
import hashlib
import io
import sys
import tarfile
from enum import StrEnum
from pathlib import Path
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict

ENTRY_SOURCE_PATH: Final = "examples/bot_project/bot/eval/harbor/entry.py"
_SOURCE_ROOTS: Final = ("src/modex_agent", "src/modex_graph")
_POOL_SOURCE_ROOTS: Final = (
    "examples/bot_project/agents",
    "examples/bot_project/bot/config",
    "examples/bot_project/bot/input_pipeline",
    "examples/bot_project/bot/service",
    "examples/bot_project/bot/tools",
    "examples/bot_project/bot/utils",
    "examples/bot_project/bot/workspace",
    "examples/bot_project/plugins",
    # Runtime data, not code: the SkillsSupply catalogs read each agent's
    # skills/<pool>/<agent>/ assignment tree directly from disk.
    # The skills/coder tree is deleted — the coder pool ships no skills.
    "examples/bot_project/skills/default",
)
_POOL_SOURCE_FILES: Final = (
    "examples/bot_project/bot/__init__.py",
    "examples/bot_project/bot/eval/harbor/mode_runner.py",
    "examples/bot_project/bot/eval/harbor/pool_budget.py",
    "examples/bot_project/bot/eval/harbor/pool_mode.py",
    "examples/bot_project/bot/eval/harbor/pool_mode_artifacts.py",
    "examples/bot_project/bot/eval/harbor/pool_mode_assembly.py",
    "examples/bot_project/bot/eval/harbor/pool_mode_types.py",
    "examples/bot_project/bot/eval/harbor/eval_overlay.py",
    "examples/bot_project/bot/eval/probes/budget.py",
    "examples/bot_project/bot/scope.py",
    "examples/bot_project/config/bot_config.yml",
    # The scope declaration is the single assembly source (ADR-0042) — the
    # legacy config/pools/coder/ tree is deleted. One file, not a directory:
    # dynamic workspace declarations (config/scopes/workspaces/) are runtime
    # state and must not enter the container tar.
    "examples/bot_project/config/scopes/bot.yml",
    # Eval-arm overlays + standalone harness declarations are load-bearing
    # assembly inputs since the benchmark-arm declarative switch (c9d60556):
    # pool_mode_assembly.load_eval_arm reads scopes/eval/eval.yml and
    # entry.py reads scopes/eval/agents/react-harness.yml — both must ship.
    # Agents/*.yml also cover the prompt file references they declare.
    "examples/bot_project/config/scopes/eval/eval.yml",
    "examples/bot_project/config/scopes/eval/agents/react-harness.yml",
    "examples/bot_project/agents/benchmark.md",
    "examples/bot_project/agents/react-harness.md",
    "examples/bot_project/pyproject.toml",
)
# Non-.py runtime data shipped in the pool tar; never seeds the AST import closure.
_POOL_DATA_FILES: Final = (
    "examples/bot_project/bot/memory/vendor/cl100k_base.tiktoken",
)
_BOT_SOURCE_ROOT: Final = "examples/bot_project/bot"
_PROJECT_MODULES: Final = frozenset({"bot", "modex_agent", "modex_graph", "plugins"})
_IGNORED_PARTS: Final = frozenset({"__pycache__", ".mypy_cache"})
_IGNORED_SUFFIXES: Final = frozenset({".pyc", ".pyo"})
_FIXED_MTIME: Final = 0


class SourceManifest(StrEnum):
    BARE = "bare"
    POOL = "pool"


class SourceArchive(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: Path
    sha256: str
    members: tuple[str, ...]


class PoolImportClosure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    module_files: tuple[str, ...]
    third_party_modules: tuple[str, ...]


def build_source_archive(
    repo_root: Path,
    destination: Path,
    *,
    manifest: SourceManifest = SourceManifest.BARE,
) -> SourceArchive:
    members = _collect_members(repo_root, manifest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        destination.open("wb") as raw_archive,
        gzip.GzipFile(
            filename="",
            mode="wb",
            compresslevel=9,
            fileobj=raw_archive,
            mtime=_FIXED_MTIME,
        ) as compressed,
        tarfile.open(
            mode="w",
            fileobj=compressed,
            format=tarfile.GNU_FORMAT,
        ) as archive,
    ):
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = _FIXED_MTIME
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(content))
    return SourceArchive(
        path=destination,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
        members=tuple(name for name, _content in members),
    )


def _collect_members(
    repo_root: Path,
    manifest: SourceManifest,
) -> list[tuple[str, bytes]]:
    source_roots: tuple[str, ...]
    source_files: tuple[str, ...]
    match manifest:
        case SourceManifest.BARE:
            source_roots = _SOURCE_ROOTS
            source_files = ()
        case SourceManifest.POOL:
            for name in _POOL_DATA_FILES:
                if not (repo_root / name).is_file():
                    raise FileNotFoundError(
                        f"Pool manifest data file is missing: {repo_root / name}. "
                        "It is a gitignored runtime blob — restore it on fresh "
                        "clones by re-downloading the canonical file (see "
                        "bot/memory/vendor_loader.py DOWNLOAD_URL)."
                    )
            source_roots = (*_SOURCE_ROOTS, *_POOL_SOURCE_ROOTS)
            closure = collect_pool_import_closure(repo_root)
            source_files = (
                *_POOL_SOURCE_FILES,
                *_POOL_DATA_FILES,
                *closure.module_files,
            )
        case unreachable:
            assert_never(unreachable)
    source_paths: set[Path] = set()
    for source_root in source_roots:
        source_paths.update(
            path
            for path in (repo_root / source_root).rglob("*")
            if path.is_file() and _is_source_content(path, repo_root)
        )
    source_paths.update(repo_root / name for name in source_files)
    items = [(path.relative_to(repo_root).as_posix(), path.read_bytes()) for path in source_paths]
    items.append(("pyproject.toml", (repo_root / "pyproject.toml").read_bytes()))
    entry_path = repo_root / ENTRY_SOURCE_PATH
    items.append((ENTRY_SOURCE_PATH, entry_path.read_bytes() if entry_path.is_file() else b""))
    return sorted(items, key=lambda item: item[0])


def collect_pool_import_closure(repo_root: Path) -> PoolImportClosure:
    bot_root = repo_root / _BOT_SOURCE_ROOT
    pending = _pool_bot_seed_paths(repo_root)
    module_files: set[str] = set()
    third_party_modules: set[str] = set()
    while pending:
        path = pending.pop()
        relative = path.relative_to(repo_root).as_posix()
        if relative in module_files:
            continue
        module_files.add(relative)
        module_name = _module_name(path, bot_root)
        package_name = module_name if path.name == "__init__.py" else module_name.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported_modules = _imported_modules(node, package_name)
            for imported_module in imported_modules:
                top_level = imported_module.partition(".")[0]
                if top_level == "bot":
                    for imported_path in _bot_module_paths(imported_module, bot_root):
                        if imported_path.relative_to(repo_root).as_posix() not in module_files:
                            pending.append(imported_path)
                elif top_level not in _PROJECT_MODULES and top_level not in sys.stdlib_module_names:
                    third_party_modules.add(top_level)
    return PoolImportClosure(
        module_files=tuple(sorted(module_files)),
        third_party_modules=tuple(sorted(third_party_modules)),
    )


def _pool_bot_seed_paths(repo_root: Path) -> list[Path]:
    bot_root_prefix = f"{_BOT_SOURCE_ROOT}/"
    return [
        repo_root / name
        for name in _POOL_SOURCE_FILES
        if name.startswith(bot_root_prefix) and name.endswith(".py")
    ]


def _module_name(path: Path, bot_root: Path) -> str:
    relative = path.relative_to(bot_root)
    parts = relative.parts[:-1] if path.name == "__init__.py" else (*relative.parts[:-1], path.stem)
    return ".".join(("bot", *parts))


def _imported_modules(node: ast.AST, package_name: str) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return ()
    if node.level:
        package_parts = package_name.split(".")
        base_parts = package_parts[: len(package_parts) - node.level + 1]
        if node.module:
            base_parts.extend(node.module.split("."))
        base = ".".join(base_parts)
    else:
        base = node.module or ""
    aliases = tuple(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return (base, *aliases) if base else aliases


def _bot_module_paths(module_name: str, bot_root: Path) -> tuple[Path, ...]:
    relative_parts = module_name.split(".")[1:]
    module_path = bot_root.joinpath(*relative_parts)
    target = module_path.with_suffix(".py")
    if not target.is_file():
        target = module_path / "__init__.py"
    if not target.is_file():
        return ()
    package_inits: list[Path] = []
    package_dir = target.parent
    while package_dir.is_relative_to(bot_root):
        package_init = package_dir / "__init__.py"
        if package_init.is_file():
            package_inits.append(package_init)
        if package_dir == bot_root:
            break
        package_dir = package_dir.parent
    package_inits.reverse()
    return tuple(dict.fromkeys((*package_inits, target)))


def _is_source_content(path: Path, repo_root: Path) -> bool:
    relative = path.relative_to(repo_root)
    return not any(part in _IGNORED_PARTS for part in relative.parts) and (
        path.suffix not in _IGNORED_SUFFIXES
    )


__all__ = [
    "ENTRY_SOURCE_PATH",
    "PoolImportClosure",
    "SourceArchive",
    "SourceManifest",
    "build_source_archive",
    "collect_pool_import_closure",
]
