from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GRAPH_PROJECT = REPO_ROOT / "src" / "modex_graph"

PROMPT_RESOURCES = (
    "modex_agent/plugins/defaults/capabilities/experience/prompts/review_system.md",
    "modex_agent/plugins/defaults/capabilities/experience/prompts/review_user.md",
)
MIGRATION_RESOURCES = (
    "modex_agent/persistence/migrations/registry/001_initial.sql",
    "modex_agent/persistence/migrations/workspace/001_initial.sql",
)
EXPECTED_RESOURCES = PROMPT_RESOURCES + MIGRATION_RESOURCES


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"command failed: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


@pytest.mark.integration
def test_built_wheels_install_with_packaged_resources(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    _run(
        ["uv", "build", "--wheel", "--out-dir", str(artifact_dir)],
        cwd=REPO_ROOT,
    )
    _run(
        ["uv", "build", "--wheel", "--out-dir", str(artifact_dir)],
        cwd=GRAPH_PROJECT,
    )

    wheels = sorted(artifact_dir.glob("*.whl"))
    assert len(wheels) == 2, f"expected the ModexAgent and modex-graph wheels, got {wheels}"
    agent_wheel = next(wheel for wheel in wheels if wheel.name.lower().startswith("modexagent-"))
    graph_wheel = next(wheel for wheel in wheels if wheel.name.lower().startswith("modex_graph-"))

    members_by_wheel: dict[Path, list[str]] = {}
    for wheel_path in wheels:
        with zipfile.ZipFile(wheel_path) as wheel:
            members = wheel.namelist()
        members_by_wheel[wheel_path] = members
        assert len(members) == len(set(members)), f"duplicate archive members in {wheel_path.name}"
        assert not [
            member
            for member in members
            if "__pycache__" in Path(member).parts or member.endswith((".pyc", ".pyo"))
        ], f"bytecode cache files found in {wheel_path.name}"

    for resource_path in EXPECTED_RESOURCES:
        owners = [
            wheel_path.name
            for wheel_path, members in members_by_wheel.items()
            for member in members
            if member == resource_path
        ]
        assert owners == [agent_wheel.name], f"unexpected owners for {resource_path}: {owners}"
    assert not any(
        member.startswith("modex_agent/") for member in members_by_wheel[graph_wheel]
    )
    assert "modex_graph/__init__.py" in members_by_wheel[graph_wheel]

    target = tmp_path / "site-packages"
    target.mkdir()
    _run(
        [
            "uv",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(target),
            str(graph_wheel),
            str(agent_wheel),
        ],
        cwd=tmp_path,
    )

    probe = """
import json
import sys
from importlib import metadata, resources
from pathlib import Path

target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target))

import modex_agent
import modex_graph

prompt_package = "modex_agent.plugins.defaults.capabilities.experience.prompts"
migration_package = "modex_agent.persistence.migrations"
review_system = (resources.files(prompt_package) / "review_system.md").read_text(encoding="utf-8")
review_user = (resources.files(prompt_package) / "review_user.md").read_text(encoding="utf-8")
registry_sql = (
    resources.files(migration_package) / "registry" / "001_initial.sql"
).read_text(encoding="utf-8")
workspace_sql = (
    resources.files(migration_package) / "workspace" / "001_initial.sql"
).read_text(encoding="utf-8")
installed_distributions = {
    distribution.metadata["Name"].lower()
    for distribution in metadata.distributions(path=[str(target)])
}

print(json.dumps({
    "agent_from_target": Path(modex_agent.__file__).resolve().is_relative_to(target),
    "graph_from_target": Path(modex_graph.__file__).resolve().is_relative_to(target),
    "graph_public_surface": bool(modex_graph.__all__),
    "agent_distribution_installed": "modexagent" in installed_distributions,
    "graph_distribution_installed": "modex-graph" in installed_distributions,
    "review_system": "experience review agent" in review_system.lower(),
    "review_user_snapshot": "{conversation_snapshot}" in review_user,
    "review_user_existing": "{existing_experiences}" in review_user,
    "registry_migration": bool(registry_sql.strip()),
    "workspace_migration": bool(workspace_sql.strip()),
}, sort_keys=True))
"""
    probe_env = os.environ.copy()
    probe_env["PYTHONDONTWRITEBYTECODE"] = "1"
    probe_result = _run(
        [sys.executable, "-I", "-B", "-c", probe, str(target)],
        cwd=tmp_path,
        env=probe_env,
    )
    probe_checks = json.loads(probe_result.stdout)
    assert all(probe_checks.values()), probe_checks

    polluted = [
        path.relative_to(target).as_posix()
        for path in target.rglob("*")
        if path.name == "__pycache__" or path.suffix in {".pyc", ".pyo"}
    ]
    assert not polluted, f"installed target contains bytecode cache files: {polluted}"
