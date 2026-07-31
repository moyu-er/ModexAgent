"""Unit tests for `ExternalPaths` and `ProviderKind`."""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.agents.external import ExternalPaths, ProviderKind


class TestProviderKind:
    """Discriminator used by provider backends and the path accessor."""

    def test_day_one_values(self) -> None:
        assert ProviderKind.PI.value == "pi"
        assert ProviderKind.OPENCODE.value == "opencode"

    def test_extensible_via_subclass_or_extra_value(self) -> None:
        # The ticket explicitly notes this is extensible; the day-one
        # set is closed but does not preclude future entries.
        assert set(ProviderKind) >= {ProviderKind.PI, ProviderKind.OPENCODE}

    def test_str_compare(self) -> None:
        assert ProviderKind.PI == "pi"


class TestExternalPaths:
    """Workdir-relative path accessor."""

    def test_workdir_resolved_to_canonical_root(self, tmp_path: Path) -> None:
        # Even when handed a relative-style trailing-slash dir, the
        # canonical root is resolved once and shared by every accessor.
        workdir = tmp_path / "ext"
        workdir.mkdir()
        paths = ExternalPaths(workdir)
        assert paths.workdir == workdir.resolve()

    def test_modex_root_lives_under_workdir(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        assert paths.modex_root == (tmp_path.resolve() / ".modex")

    def test_external_root_lives_under_modex(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        assert paths.external_root == paths.modex_root / "external"

    @pytest.mark.parametrize(
        "name,expected_suffix",
        [
            ("outbox", "outbox.jsonl"),
            ("inbox_snapshot", "inbox-snapshot.jsonl"),
            ("result", "result.json"),
            ("env_snapshot", "env-snapshot.json"),
        ],
    )
    def test_named_paths_anchor_under_external_root(
        self, tmp_path: Path, name: str, expected_suffix: str
    ) -> None:
        paths = ExternalPaths(tmp_path)
        attr = getattr(paths, name)
        assert attr == paths.external_root / expected_suffix

    def test_agents_md_anchors_at_workdir_root(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        assert paths.agents_md == paths.workdir / "AGENTS.md"

    def test_provider_session_pi_suffix(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        # Spec: <workdir>/.modex/external/<kind>-session.jsonl
        assert paths.provider_session(ProviderKind.PI) == paths.external_root / "pi-session.jsonl"

    def test_provider_session_opencode_suffix(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        assert (
            paths.provider_session(ProviderKind.OPENCODE)
            == paths.external_root / "opencode-session.jsonl"
        )

    def test_session_map_is_under_external_root(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        assert paths.session_map() == paths.external_root / "session-map.json"

    def test_derived_paths_never_escape_workdir(self, tmp_path: Path) -> None:
        # ExternalPaths is not asked to defend against absolute workdirs,
        # only to keep its derivations inside them. All derivation roots
        # chain through ``workdir``.
        paths = ExternalPaths(tmp_path)
        workdir = paths.workdir
        for p in [
            paths.modex_root,
            paths.external_root,
            paths.outbox,
            paths.inbox_snapshot,
            paths.result,
            paths.env_snapshot,
            paths.agents_md,
            paths.session_map(),
        ]:
            assert workdir in p.parents or p == workdir

    def test_provider_session_paths_never_escape_workdir(self, tmp_path: Path) -> None:
        paths = ExternalPaths(tmp_path)
        for kind in ProviderKind:
            p = paths.provider_session(kind)
            assert paths.workdir in p.parents

    def test_resolve_is_idempotent(self, tmp_path: Path) -> None:
        # Calling .resolve() externally before handing to ExternalPaths
        # yields the same root as passing an unresolved Path.
        unresolved = tmp_path / "sub" / ".."
        a = ExternalPaths(unresolved)
        b = ExternalPaths(unresolved.resolve())
        assert a.workdir == b.workdir
