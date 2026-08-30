from __future__ import annotations

import importlib.metadata
import re
import tarfile
from pathlib import Path

import pytest
from bot.eval.harbor import source_package
from bot.eval.harbor.agent import DEPENDENCY_CLOSURE
from bot.eval.harbor.source_package import (
    ENTRY_SOURCE_PATH,
    build_source_archive,
)

_BOT_PROJECT_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BOT_PROJECT_DIR.parents[1]
_VENDOR_BLOB_MEMBER = "examples/bot_project/bot/memory/vendor/cl100k_base.tiktoken"


def _source_fixture(root: Path) -> None:
    (root / "src" / "modex_agent").mkdir(parents=True)
    (root / "src" / "modex_graph").mkdir(parents=True)
    (root / "src" / "modex_agent" / "alpha.py").write_text("ALPHA = 1\n", encoding="utf-8")
    (root / "src" / "modex_graph" / "zeta.py").write_text("ZETA = 2\n", encoding="utf-8")
    (root / "src" / "modex_agent" / "__pycache__").mkdir()
    (root / "src" / "modex_agent" / "__pycache__" / "alpha.pyc").write_bytes(b"cache")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")


def test_build_source_archive_contains_required_manifest_and_entry_placeholder(
    tmp_path: Path,
) -> None:
    _source_fixture(tmp_path)

    archive = build_source_archive(tmp_path, tmp_path / "modex-src.tar.gz")
    rebuilt = build_source_archive(tmp_path, tmp_path / "modex-src-rebuilt.tar.gz")

    assert "src/modex_agent/alpha.py" in archive.members
    assert "src/modex_graph/zeta.py" in archive.members
    assert "pyproject.toml" in archive.members
    assert ENTRY_SOURCE_PATH in archive.members
    assert archive.members == (
        ENTRY_SOURCE_PATH,
        "pyproject.toml",
        "src/modex_agent/alpha.py",
        "src/modex_graph/zeta.py",
    )
    assert archive.sha256 == rebuilt.sha256
    assert not any("__pycache__" in member for member in archive.members)
    assert archive.members == tuple(sorted(archive.members))
    with tarfile.open(archive.path, mode="r:gz") as packaged:
        entry = packaged.extractfile(ENTRY_SOURCE_PATH)
        assert entry is not None
        assert entry.read() == b""
        assert {member.mtime for member in packaged.getmembers()} == {0}


def test_build_source_archive_with_same_content_has_reproducible_sha(tmp_path: Path) -> None:
    _source_fixture(tmp_path)

    first = build_source_archive(tmp_path, tmp_path / "first.tar.gz")
    second = build_source_archive(tmp_path, tmp_path / "second.tar.gz")

    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()


def test_pool_source_archive_contains_runtime_surface_without_excluded_trees(
    tmp_path: Path,
) -> None:
    archive = source_package.build_source_archive(
        _REPO_ROOT,
        tmp_path / "pool-src.tar.gz",
        manifest=source_package.SourceManifest.POOL,
    )

    required = {
        "examples/bot_project/agents/orchestrator.md",
        "examples/bot_project/bot/config/__init__.py",
        "examples/bot_project/bot/memory/token_estimator.py",
        "examples/bot_project/bot/memory/vendor/cl100k_base.tiktoken",
        "examples/bot_project/bot/eval/harbor/mode_runner.py",
        "examples/bot_project/bot/eval/harbor/pool_budget.py",
        "examples/bot_project/bot/eval/harbor/pool_mode.py",
        "examples/bot_project/bot/eval/harbor/pool_mode_artifacts.py",
        "examples/bot_project/bot/eval/harbor/pool_mode_types.py",
        "examples/bot_project/bot/eval/probes/budget.py",
        "examples/bot_project/bot/input_pipeline/__init__.py",
        "examples/bot_project/bot/scope.py",
        "examples/bot_project/bot/service/pool/factory.py",
        "examples/bot_project/bot/utils/__init__.py",
        "examples/bot_project/bot/webui/transcript_store.py",
        "examples/bot_project/bot/workspace/__init__.py",
        "examples/bot_project/config/bot_config.yml",
        "examples/bot_project/config/scopes/bot.yml",
        "examples/bot_project/plugins/bot_hooks.py",
        "examples/bot_project/plugins/bot_strategies.py",
        "examples/bot_project/bot/service/pool/declaration.py",
        "examples/bot_project/pyproject.toml",
        ENTRY_SOURCE_PATH,
    }
    assert required <= set(archive.members)
    assert "examples/bot_project/bot/webui/server.py" not in archive.members
    assert not any(
        member.startswith("examples/bot_project/bot/webui/routes/")
        for member in archive.members
    )
    # Secrets invariant: the bot's credential config and the host-side model
    # resolver must never enter the container tar.
    assert "examples/bot_project/config/model.yml" not in archive.members
    assert "examples/bot_project/bot/eval/harbor/model_source.py" not in archive.members
    assert not any(member.endswith("/model.yml") for member in archive.members)


def test_pool_source_archive_with_same_content_has_reproducible_sha(tmp_path: Path) -> None:
    first = source_package.build_source_archive(
        _REPO_ROOT,
        tmp_path / "pool-first.tar.gz",
        manifest=source_package.SourceManifest.POOL,
    )
    second = source_package.build_source_archive(
        _REPO_ROOT,
        tmp_path / "pool-second.tar.gz",
        manifest=source_package.SourceManifest.POOL,
    )

    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()


def test_pool_ast_import_closure_finds_function_body_imports() -> None:
    closure = source_package.collect_pool_import_closure(_REPO_ROOT)

    assert "examples/bot_project/bot/memory/token_estimator.py" in closure.module_files
    assert "examples/bot_project/bot/webui/transcript_store.py" in closure.module_files
    assert "examples/bot_project/bot/webui/server.py" not in closure.module_files
    assert not any(
        member.startswith("examples/bot_project/bot/webui/routes/")
        for member in closure.module_files
    )


def test_pool_archive_contains_ast_import_fixpoint(tmp_path: Path) -> None:
    closure = source_package.collect_pool_import_closure(_REPO_ROOT)
    archive = source_package.build_source_archive(
        _REPO_ROOT,
        tmp_path / "pool-closure.tar.gz",
        manifest=source_package.SourceManifest.POOL,
    )

    assert set(closure.module_files) <= set(archive.members)
    assert "examples/bot_project/bot/webui/transcript_store.py" in archive.members


def test_pool_ast_import_dependencies_are_in_install_closure() -> None:
    closure = source_package.collect_pool_import_closure(_REPO_ROOT)
    distributions = importlib.metadata.packages_distributions()
    required_dependencies = {
        _dependency_name(distribution)
        for module in closure.third_party_modules
        for distribution in distributions.get(module, ())
    }
    install_dependencies = {_dependency_name(spec) for spec in DEPENDENCY_CLOSURE}

    assert required_dependencies <= install_dependencies


def test_bare_source_archive_excludes_tiktoken_vendor_blob(tmp_path: Path) -> None:
    archive = build_source_archive(_REPO_ROOT, tmp_path / "bare-src.tar.gz")

    assert _VENDOR_BLOB_MEMBER not in archive.members


def test_pool_source_archive_carries_default_skills_tree(tmp_path: Path) -> None:
    archive = source_package.build_source_archive(
        _REPO_ROOT,
        tmp_path / "pool-skills.tar.gz",
        manifest=source_package.SourceManifest.POOL,
    )
    bare = build_source_archive(_REPO_ROOT, tmp_path / "bare-skills.tar.gz")

    skills_members = {
        member
        for member in archive.members
        if member.startswith("examples/bot_project/skills/")
    }
    # Presence-derived golden: pin the skills tree that exists on disk.
    # The skills/coder tree is deleted; only the default pool's skills ship.
    skills_default_root = _BOT_PROJECT_DIR / "skills" / "default"
    expected_members = {
        "examples/bot_project/skills/default/"
        + path.relative_to(skills_default_root).as_posix()
        for path in skills_default_root.rglob("*")
        if path.is_file()
    }
    assert expected_members
    assert expected_members <= skills_members
    assert "examples/bot_project/skills/default/default/weather/SKILL.md" in (
        skills_members
    )
    assert not any(
        member.startswith("examples/bot_project/skills/coder/")
        for member in skills_members
    )
    assert not any(
        member.startswith("examples/bot_project/skills/")
        for member in bare.members
    )


def test_pool_manifest_missing_data_file_fails_loudly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        source_package,
        "_POOL_DATA_FILES",
        ("examples/bot_project/bot/memory/vendor/__missing__.tiktoken",),
    )

    with pytest.raises(FileNotFoundError, match=r"__missing__\.tiktoken.*DOWNLOAD_URL"):
        source_package.build_source_archive(
            _REPO_ROOT,
            tmp_path / "pool-missing.tar.gz",
            manifest=source_package.SourceManifest.POOL,
        )


def _dependency_name(specification: str) -> str:
    name = re.split(r"[<>=!~\[]", specification, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()
