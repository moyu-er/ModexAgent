"""Unit tests for `ExternalEnvBuilder.build()`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from modex_agent.agents.external_coding import ExternalEnvBuilder, ExternalEnvSpec


def _spec(
    tmp_path: Path,
    *,
    session_id: str = "abc.pi",
    agent_name: str = "pi",
    provider_session_id: str = "provider-1",
    agent_pool_map: dict[str, str] | None = None,
    targets: list[tuple[str, str]] | None = None,
) -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=tmp_path / "ws",
        inbox_root=tmp_path / "inbox",
        workdir=tmp_path / "wd",
        session_id=session_id,
        agent_name=agent_name,
        provider_session_id=provider_session_id,
        agent_pool_map=agent_pool_map
        if agent_pool_map is not None
        else {"default": "pool_default"},
        targets=targets if targets is not None else [("default", "main pool")],
        modexctl_bin_dir=tmp_path / "bin",
    )


class TestExternalEnvBuilder:
    """The 9 ``MODEX_*`` vars + PATH-prepend contract."""

    def test_produces_all_eight_str_modex_vars(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        out = ExternalEnvBuilder.build(spec, base_env={})
        # Eight vars total per ADR-0022 D6 (PATH is the 9th, modified separately).
        assert set(out) >= {
            "MODEX_WORKSPACE_ROOT",
            "MODEX_INBOX_ROOT",
            "MODEX_WORKDIR",
            "MODEX_SESSION_ID",
            "MODEX_AGENT_NAME",
            "MODEX_PROVIDER_SESSION_ID",
            "MODEX_AGENT_POOL_MAP",
            "MODEX_TARGETS",
        }

    def test_path_values_serialised_as_strings(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        out = ExternalEnvBuilder.build(spec, base_env={})
        # All Path sources must be stringified.
        assert out["MODEX_WORKSPACE_ROOT"] == str((tmp_path / "ws").resolve())
        assert out["MODEX_INBOX_ROOT"] == str((tmp_path / "inbox").resolve())
        assert out["MODEX_WORKDIR"] == str((tmp_path / "wd").resolve())

    def test_string_fields_passthrough(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        out = ExternalEnvBuilder.build(spec, base_env={})
        assert out["MODEX_SESSION_ID"] == "abc.pi"
        assert out["MODEX_AGENT_NAME"] == "pi"
        assert out["MODEX_PROVIDER_SESSION_ID"] == "provider-1"

    def test_agent_pool_map_serialised_as_name_equals_pool_pairs(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, agent_pool_map={"a": "pa", "b": "pb", "c": "pc"})
        out = ExternalEnvBuilder.build(spec, base_env={})
        pairs = out["MODEX_AGENT_POOL_MAP"].split(";")
        assert sorted(pairs) == sorted(["a=pa", "b=pb", "c=pc"])

    def test_targets_serialised_as_name_equals_description_pairs(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path, targets=[("alpha", "first"), ("beta", "second")])
        out = ExternalEnvBuilder.build(spec, base_env={})
        pairs = out["MODEX_TARGETS"].split(";")
        assert pairs == ["alpha=first", "beta=second"]

    def test_targets_order_is_preserved(self, tmp_path: Path) -> None:
        # Deliberately out of insertion order — preserves caller's order
        # so the LLM sees the same catalogue the CommunicationTargetStore
        # produced.
        spec = _spec(tmp_path, targets=[("z", "z"), ("a", "a"), ("m", "m")])
        out = ExternalEnvBuilder.build(spec, base_env={})
        assert out["MODEX_TARGETS"] == "z=z;a=a;m=m"

    def test_path_prepends_modexbot_then_inherits_base_path(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        base = {"PATH": "/usr/bin:/bin"}
        out = ExternalEnvBuilder.build(spec, base_env=base)
        # Path-aware: Windows uses ";" as well via os.pathsep; we rely on
        # os.pathsep but the structural assertion is "first entry, then
        # base trailing tail preserved".
        bin_dir = str((tmp_path / "bin").resolve())
        assert out["PATH"].startswith(bin_dir)
        assert out["PATH"].endswith(os.pathsep + "/usr/bin:/bin")

    def test_path_handles_empty_base_path(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        out = ExternalEnvBuilder.build(spec, base_env={})
        # No leading/trailing os.pathsep duplication when base PATH missing.
        assert out["PATH"] == str((tmp_path / "bin").resolve())

    def test_does_not_mutate_base_env(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        base = {"PATH": "/usr/bin:/bin", "EXISTING": "keep"}
        before = dict(base)
        ExternalEnvBuilder.build(spec, base_env=base)
        assert base == before

    def test_merges_base_env_so_unrelated_keys_survive(self, tmp_path: Path) -> None:
        spec = _spec(tmp_path)
        base = {"PATH": "/usr/bin", "FOO": "bar", "BAZ": "qux"}
        out = ExternalEnvBuilder.build(spec, base_env=base)
        assert out["FOO"] == "bar"
        assert out["BAZ"] == "qux"

    def test_round_trip_via_spec_serialisation(self, tmp_path: Path) -> None:
        # Spec itself is round-trippable through JSON (env dict rebuild
        # is tested above; this only proves the spec serialises).
        spec = _spec(tmp_path)
        restored = ExternalEnvSpec.model_validate_json(spec.model_dump_json())
        assert restored == spec

    @pytest.mark.parametrize(
        "agent_pool_map",
        [
            {},
            {"only": "pool_x"},
            {"x": "px", "y": "py", "z": "pz"},
        ],
    )
    def test_pool_map_serialisation_is_stable(
        self, tmp_path: Path, agent_pool_map: dict[str, str]
    ) -> None:
        spec = _spec(tmp_path, agent_pool_map=agent_pool_map)
        out = ExternalEnvBuilder.build(spec, base_env={})
        if not agent_pool_map:
            assert out["MODEX_AGENT_POOL_MAP"] == ""
        else:
            pairs = out["MODEX_AGENT_POOL_MAP"].split(";")
            assert dict(p.split("=", 1) for p in pairs) == agent_pool_map
