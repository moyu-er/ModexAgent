"""Unit tests for per-provider-session env snapshot writing.

Covers the shared module-level ``write_env_snapshot_for_session`` function
added to ``agent.py`` (Task 2). This function is the single convergence
point for writing ``env-snapshots/<provider_session_id>.json`` files:

- The **main session** snapshot is written by ``OpenCodeServerBackend.
  execute_streaming`` right after ``create_session_v1`` / resume resolves
  the provider session id, BEFORE ``prompt_async_v1`` runs (so the agent
  can call ``modexctl`` mid-turn and the snapshot already exists).
- The **child session** snapshot is written by ``ExternalAgent.
  _handle_emission`` on child discovery, using the child spec built from
  the parent spec + child identity overrides.

The snapshot file contains only ``MODEX_*`` keys + ``PATH`` — the same
filtered subset the legacy ``env-snapshot.json`` carries. modexctl reads
it when ``OPENCODE_SESSION_ID`` (injected by the shell.env plugin) matches
the provider session id in the filename.
"""

from __future__ import annotations

import json
from pathlib import Path

from modex_agent.agents.external.agent import write_env_snapshot_for_session
from modex_agent.agents.external.env_builder import ExternalEnvBuilder
from modex_agent.agents.external.paths import ExternalPaths
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind

_MODEX_ENV_KEYS = (
    "MODEX_WORKSPACE_ROOT",
    "MODEX_INBOX_ROOT",
    "MODEX_WORKDIR",
    "MODEX_SESSION_ID",
    "MODEX_AGENT_NAME",
    "MODEX_PROVIDER_SESSION_ID",
    "MODEX_AGENT_POOL_MAP",
    "MODEX_TARGETS",
    "MODEX_COMM_KIND",
    "MODEX_CONTROL_ORIGIN",
)


def _spec(
    tmp_path: Path,
    *,
    session_id: str = "conv.main",
    agent_name: str = "main",
    provider_session_id: str = "prov-1",
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
    parent_session_id: str | None = None,
) -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=tmp_path / "ws",
        inbox_root=tmp_path / "inbox",
        workdir=tmp_path / "wd",
        session_id=session_id,
        agent_name=agent_name,
        provider_session_id=provider_session_id,
        agent_pool_map={"main": "pool_main"},
        targets=[("helper", "a helper")],
        modexctl_bin_dir=tmp_path / "bin",
        comm_kind=comm_kind,
        parent_session_id=parent_session_id,
    )


def _build_env(tmp_path: Path, **kw: object) -> dict[str, str]:
    spec = _spec(tmp_path, **kw)  # type: ignore[arg-type]
    return ExternalEnvBuilder.build(spec, base_env={"PATH": "/usr/bin"})


class TestWriteEnvSnapshotForSession:
    """``write_env_snapshot_for_session`` — the shared snapshot writer."""

    def test_writes_file_at_correct_path(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        env = _build_env(tmp_path, provider_session_id="opc-abc-123")

        write_env_snapshot_for_session(paths, env, "opc-abc-123")

        snapshot_path = paths.env_snapshot_for_session("opc-abc-123")
        assert snapshot_path.exists()

    def test_creates_snapshots_dir_if_not_exists(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        env = _build_env(tmp_path)

        # Directory does not exist yet.
        assert not paths.env_snapshots_dir.exists()

        write_env_snapshot_for_session(paths, env, "prov-1")

        assert paths.env_snapshots_dir.exists()

    def test_snapshot_contains_only_modex_and_path(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        env = _build_env(tmp_path)

        write_env_snapshot_for_session(paths, env, "prov-1")

        snapshot = json.loads(paths.env_snapshot_for_session("prov-1").read_text(encoding="utf-8"))
        # All keys are either MODEX_* or PATH.
        for key in snapshot:
            assert key.startswith("MODEX_") or key == "PATH", f"unexpected key {key}"

    def test_snapshot_excludes_opencode_specific_vars(self, tmp_path: Path) -> None:
        """OPENCODE_PERMISSION and OPENCODE_CONFIG_CONTENT are spawn-only
        vars — they must NOT appear in the per-session snapshot (modexctl
        only reads MODEX_* + PATH)."""
        paths = ExternalPaths(tmp_path)
        env = _build_env(tmp_path)

        write_env_snapshot_for_session(paths, env, "prov-1")

        snapshot = json.loads(paths.env_snapshot_for_session("prov-1").read_text(encoding="utf-8"))
        assert "OPENCODE_PERMISSION" not in snapshot
        assert "OPENCODE_CONFIG_CONTENT" not in snapshot

    def test_snapshot_includes_all_modex_vars(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        env = _build_env(tmp_path)

        write_env_snapshot_for_session(paths, env, "prov-1")

        snapshot = json.loads(paths.env_snapshot_for_session("prov-1").read_text(encoding="utf-8"))
        for key in _MODEX_ENV_KEYS:
            assert key in snapshot, f"missing {key}"

    def test_snapshot_includes_path(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        env = _build_env(tmp_path)

        write_env_snapshot_for_session(paths, env, "prov-1")

        snapshot = json.loads(paths.env_snapshot_for_session("prov-1").read_text(encoding="utf-8"))
        assert "PATH" in snapshot
        assert str((tmp_path / "bin").resolve()) in snapshot["PATH"]

    def test_snapshot_values_match_env(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        env = _build_env(tmp_path, session_id="conv.agent1", provider_session_id="prov-xyz")

        write_env_snapshot_for_session(paths, env, "prov-xyz")

        snapshot = json.loads(
            paths.env_snapshot_for_session("prov-xyz").read_text(encoding="utf-8")
        )
        assert snapshot["MODEX_SESSION_ID"] == "conv.agent1"
        assert snapshot["MODEX_PROVIDER_SESSION_ID"] == "prov-xyz"
        assert snapshot["MODEX_COMM_KIND"] == "normal"

    def test_subagent_snapshot_includes_parent_session_id(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        env = _build_env(
            tmp_path,
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id="conv.parent",
            provider_session_id="child-prov-1",
        )

        write_env_snapshot_for_session(paths, env, "child-prov-1")

        snapshot = json.loads(
            paths.env_snapshot_for_session("child-prov-1").read_text(encoding="utf-8")
        )
        assert snapshot["MODEX_COMM_KIND"] == "subagent"
        assert snapshot["MODEX_PARENT_SESSION_ID"] == "conv.parent"

    def test_concurrent_sessions_write_to_different_files(self, tmp_path: Path) -> None:
        """Two provider sessions writing snapshots simultaneously must
        land in separate files — no overwrite."""
        paths = ExternalPaths(tmp_path)
        env_a = _build_env(tmp_path, session_id="conv.a", provider_session_id="prov-a")
        env_b = _build_env(tmp_path, session_id="conv.b", provider_session_id="prov-b")

        write_env_snapshot_for_session(paths, env_a, "prov-a")
        write_env_snapshot_for_session(paths, env_b, "prov-b")

        snap_a = json.loads(paths.env_snapshot_for_session("prov-a").read_text(encoding="utf-8"))
        snap_b = json.loads(paths.env_snapshot_for_session("prov-b").read_text(encoding="utf-8"))
        assert snap_a["MODEX_SESSION_ID"] == "conv.a"
        assert snap_b["MODEX_SESSION_ID"] == "conv.b"

    def test_overwrite_on_same_session_id(self, tmp_path: Path) -> None:
        """Writing the same provider_session_id twice overwrites the file
        with the latest env (e.g. on a resume turn the vars may have
        changed)."""
        paths = ExternalPaths(tmp_path)
        env_v1 = _build_env(tmp_path, session_id="conv.v1", provider_session_id="prov-1")
        env_v2 = _build_env(tmp_path, session_id="conv.v2", provider_session_id="prov-1")

        write_env_snapshot_for_session(paths, env_v1, "prov-1")
        write_env_snapshot_for_session(paths, env_v2, "prov-1")

        snapshot = json.loads(paths.env_snapshot_for_session("prov-1").read_text(encoding="utf-8"))
        assert snapshot["MODEX_SESSION_ID"] == "conv.v2"

    def test_json_is_sorted_and_indented(self, tmp_path: Path) -> None:
        """The snapshot file is human-readable (sorted keys, indent=2)."""
        paths = ExternalPaths(tmp_path)
        env = _build_env(tmp_path)

        write_env_snapshot_for_session(paths, env, "prov-1")

        raw = paths.env_snapshot_for_session("prov-1").read_text(encoding="utf-8")
        # Re-serialise with the same params to compare byte-for-byte.
        expected = json.dumps(
            {k: v for k, v in env.items() if k.startswith("MODEX_") or k == "PATH"},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        assert raw == expected
