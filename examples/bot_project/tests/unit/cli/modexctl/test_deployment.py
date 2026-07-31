"""Seam 4 deployment verification — T08 console script cutover (ADR-0036).

Static deployment invariants guarding the modexctl cutover and the
control-plane env wiring in a single place:

1. Root ``pyproject.toml`` no longer registers a ``modexctl`` console script.
2. ``examples/bot_project/pyproject.toml`` registers ``modexctl`` pointing at
   ``bot.cli.modexctl:main`` (the new bot-owned CLI from T03).
3. ``postinstall.py`` ``create_cli_shims()`` emits a ``modexctl.bat`` that
   invokes ``python -m bot.cli.modexctl`` (not the legacy ``-c`` shim).
4. ``postinstall.py`` ``verify_imports()`` checks ``import bot.cli.modexctl``
   and no longer checks ``import modexctl``.
5. ``src/modexctl/`` is fully deleted (T10) — the legacy package no longer
   ships in the wheel.
6. ``ExternalEnvSpec`` has a ``control_origin`` field and
   ``ExternalEnvBuilder.build_modex_vars()`` emits ``MODEX_CONTROL_ORIGIN``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from modex_agent.agents.external.env_builder import ExternalEnvBuilder
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind

# ---------------------------------------------------------------------------
# Path resolution — repo root is 7 levels up from this test file
# (repo / examples / bot_project / tests / unit / cli / modexctl / test_file)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[6]
_ROOT_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_BOT_PYPROJECT = _REPO_ROOT / "examples" / "bot_project" / "pyproject.toml"
_POSTINSTALL = _REPO_ROOT / "examples" / "bot_project" / "packaging" / "windows" / "postinstall.py"
_MODEXCTL_INIT = _REPO_ROOT / "src" / "modexctl" / "__init__.py"


def _load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# 1. Root pyproject.toml — no modexctl console script
# ---------------------------------------------------------------------------


class TestRootPyprojectNoModexctl:
    def test_root_pyproject_has_no_modexctl_script(self) -> None:
        data = _load_toml(_ROOT_PYPROJECT)
        scripts = data.get("project", {}).get("scripts", {})
        assert "modexctl" not in scripts, (
            "Root pyproject.toml must not register a 'modexctl' console script "
            "(ADR-0036: the new bot-owned CLI is the sole 'modexctl' command)."
        )


# ---------------------------------------------------------------------------
# 2. Bot pyproject.toml — modexctl points at bot.cli.modexctl:main
# ---------------------------------------------------------------------------


class TestBotPyprojectModexctlScript:
    def test_bot_pyproject_registers_modexctl_to_new_cli(self) -> None:
        data = _load_toml(_BOT_PYPROJECT)
        scripts = data.get("project", {}).get("scripts", {})
        assert scripts.get("modexctl") == "bot.cli.modexctl:main", (
            "examples/bot_project/pyproject.toml must register "
            "'modexctl = \"bot.cli.modexctl:main\"' (T03 new CLI)."
        )


# ---------------------------------------------------------------------------
# 3. postinstall.py create_cli_shims — modexctl.bat uses -m bot.cli.modexctl
# ---------------------------------------------------------------------------


class TestPostinstallCliShim:
    def test_modexctl_bat_uses_module_invocation(self) -> None:
        source = _POSTINSTALL.read_text(encoding="utf-8")
        assert "-m bot.cli.modexctl" in source, (
            "postinstall.py create_cli_shims() must invoke "
            "'python -m bot.cli.modexctl' for modexctl.bat."
        )

    def test_modexctl_bat_no_longer_uses_legacy_c_shim(self) -> None:
        source = _POSTINSTALL.read_text(encoding="utf-8")
        assert "from modexctl.main import main" not in source, (
            "postinstall.py must no longer use the legacy "
            "'from modexctl.main import main' -c shim."
        )


# ---------------------------------------------------------------------------
# 4. postinstall.py verify_imports — checks bot.cli.modexctl, not modexctl
# ---------------------------------------------------------------------------


class TestPostinstallVerifyImports:
    def test_verify_imports_checks_bot_cli_modexctl(self) -> None:
        source = _POSTINSTALL.read_text(encoding="utf-8")
        assert "import bot.cli.modexctl" in source, (
            "postinstall.py verify_imports() must check 'import bot.cli.modexctl'."
        )

    def test_verify_imports_no_longer_checks_legacy_modexctl(self) -> None:
        source = _POSTINSTALL.read_text(encoding="utf-8")
        # The semicolon pins the match to the verify_imports check string,
        # not the docstring of create_pth_files which mentions ``import modexctl``.
        assert "import modexctl;" not in source, (
            "postinstall.py verify_imports() must no longer check "
            "'import modexctl' (legacy package)."
        )


# ---------------------------------------------------------------------------
# 5. src/modexctl/ — legacy package fully removed (T10)
# ---------------------------------------------------------------------------


class TestLegacyModexctlRemoved:
    def test_modexctl_directory_does_not_exist(self) -> None:
        assert not _MODEXCTL_INIT.parent.exists(), (
            "src/modexctl/ must not exist — the legacy package was fully "
            "deleted in T10 (ADR-0036). The bot-owned CLI in "
            "examples/bot_project/bot/cli/modexctl/ is the sole modexctl."
        )

    def test_modexctl_init_does_not_exist(self) -> None:
        assert not _MODEXCTL_INIT.exists(), (
            "src/modexctl/__init__.py must not exist — the legacy package "
            "was fully deleted in T10 (ADR-0036)."
        )


# ---------------------------------------------------------------------------
# 6. ExternalEnvSpec.control_origin + build_modex_vars emits MODEX_CONTROL_ORIGIN
# ---------------------------------------------------------------------------


def _make_spec(control_origin: str = "") -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=Path("/tmp/ws"),
        inbox_root=Path("/tmp/ws/.modex/inbox"),
        workdir=Path("/tmp/ws"),
        session_id="abc.coder",
        agent_name="coder",
        provider_session_id="prov-1",
        agent_pool_map={"analyst": "pool_analyst"},
        targets=[("analyst", "Reviews code")],
        modexctl_bin_dir=Path("/tmp/bin"),
        comm_kind=AgentCommKind.NORMAL,
        control_origin=control_origin,
    )


class TestControlOriginEnvWiring:
    def test_external_env_spec_has_control_origin_field(self) -> None:
        assert "control_origin" in ExternalEnvSpec.model_fields, (
            "ExternalEnvSpec must declare a 'control_origin' field (ADR-0036 D6)."
        )

    def test_build_modex_vars_emits_control_origin(self) -> None:
        spec = _make_spec(control_origin="http://127.0.0.1:21800")
        modex_vars = ExternalEnvBuilder.build_modex_vars(spec)
        assert modex_vars["MODEX_CONTROL_ORIGIN"] == "http://127.0.0.1:21800"

    def test_build_modex_vars_emits_empty_control_origin_by_default(self) -> None:
        spec = _make_spec(control_origin="")
        modex_vars = ExternalEnvBuilder.build_modex_vars(spec)
        assert modex_vars["MODEX_CONTROL_ORIGIN"] == ""
