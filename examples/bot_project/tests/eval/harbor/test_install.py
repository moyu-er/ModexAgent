from __future__ import annotations

import functools
import json
import os
import sys
import types
from pathlib import Path

import pytest
from bot.eval.harbor.agent import (
    APT_PROBE_COMMAND,
    DEFAULT_PIP_INDEX,
    MANAGED_PYTHON_REQUEST,
    MODECTL_BIN_DIR,
    MODEX_PIP_INDEX,
    POOL_MODE_ENV_VARS,
    PYTHON_INSTALL_ROOT,
    PYTHON_PROBE_COMMAND,
    PYTHON_VERSION_GATE_COMMAND,
    SOURCE_TAR_CONTAINER_PATH,
    UV_BIN,
    UV_PROBE_COMMAND,
    UV_PYTHON_INSTALL_DIR_ENV,
    UV_PYTHON_INSTALL_MIRROR_ENV,
    VENV_ROOT,
    CommandExecution,
    CommandResult,
    HarborTaskResult,
    InstallPlan,
    InstallProbeResult,
    InstallSettings,
    InstallSkipReason,
    InstallTier,
    TimeoutBudget,
    build_install_plan,
    execute_install_plan,
    probe_install_runtime,
    select_install_tier,
)
from bot.eval.harbor.source_package import SourceArchive, SourceManifest


class _RecordingInstalledBase:
    """Stand-in for harbor's BaseInstalledAgent recording container exec calls."""

    def __init__(self, logs_dir: Path, extra_env: dict[str, str] | None = None) -> None:
        self.logs_dir = logs_dir
        self._extra_env = dict(extra_env) if extra_env else {}
        self.agent_commands: list[str | None] = []
        self.agent_envs: list[dict[str, str] | None] = []
        self.agent_timeouts: list[int | None] = []

    def _get_env(self, key: str) -> str | None:
        if key in self._extra_env:
            return self._extra_env[key]
        return os.environ.get(key)

    async def exec_as_agent(
        self,
        environment: object,
        command: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ) -> None:
        _ = environment
        self.agent_commands.append(command)
        self.agent_envs.append(env)
        self.agent_timeouts.append(timeout_sec)


def _passthrough_prompt_template(fn):
    @functools.wraps(fn)
    async def wrapper(self, instruction, *args, **kwargs):
        return await fn(self, instruction, *args, **kwargs)

    return wrapper


def _install_harbor_stubs() -> None:
    """Register minimal harbor modules so the bridge imports without the package."""
    if "harbor.agents.installed.base" in sys.modules:
        return
    installed_base = types.ModuleType("harbor.agents.installed.base")
    installed_base.__dict__.update(
        BaseInstalledAgent=_RecordingInstalledBase,
        with_prompt_template=_passthrough_prompt_template,
    )
    environments_base = types.ModuleType("harbor.environments.base")
    environments_base.__dict__.update(BaseEnvironment=object)
    agent_context = types.ModuleType("harbor.models.agent.context")
    agent_context.__dict__.update(AgentContext=object)
    sys.modules.update(
        {
            "harbor": types.ModuleType("harbor"),
            "harbor.agents": types.ModuleType("harbor.agents"),
            "harbor.agents.installed": types.ModuleType("harbor.agents.installed"),
            "harbor.agents.installed.base": installed_base,
            "harbor.environments": types.ModuleType("harbor.environments"),
            "harbor.environments.base": environments_base,
            "harbor.models": types.ModuleType("harbor.models"),
            "harbor.models.agent": types.ModuleType("harbor.models.agent"),
            "harbor.models.agent.context": agent_context,
        }
    )


_install_harbor_stubs()

from bot.eval.harbor.installed_agent import (  # noqa: E402
    POOL_INSTALL_SECONDS,
    ModexHarborAgent,
)


def _modern_python_probe() -> InstallProbeResult:
    """uv overlay live, system python3 >= 3.12 (waterfall step ②)."""
    return InstallProbeResult(
        python_available=True,
        apt_available=False,
        python_modern=True,
        uv_available=True,
    )


def _old_python_probe() -> InstallProbeResult:
    """uv overlay live, system python3 < 3.12, apt present (steps ③ → ④)."""
    return InstallProbeResult(
        python_available=True,
        apt_available=True,
        python_modern=False,
        uv_available=True,
    )


def _no_python_probe(*, apt: bool) -> InstallProbeResult:
    return InstallProbeResult(
        python_available=False,
        apt_available=apt,
        python_modern=False,
        uv_available=True,
    )


def _legacy_python_probe() -> InstallProbeResult:
    """uv overlay dead — the ⑤ pip safety net (uv_available defaults False)."""
    return InstallProbeResult(python_available=True, apt_available=False)


def _flat_argv(plan: InstallPlan) -> list[str]:
    return [arg for stage in plan.stages for command in stage for arg in command.argv]


@pytest.mark.parametrize(
    ("uv_available", "python_available", "apt_available", "expected"),
    [
        (True, True, True, InstallTier.UV),
        (True, True, False, InstallTier.UV),
        (True, False, False, InstallTier.UV),
        (True, False, True, InstallTier.UV),
        (False, True, True, InstallTier.PYTHON),
        (False, True, False, InstallTier.PYTHON),
        (False, False, True, InstallTier.APT),
        (False, False, False, InstallTier.SKIPPED),
    ],
)
def test_select_install_tier_when_probe_results_vary(
    uv_available: bool,
    python_available: bool,
    apt_available: bool,
    expected: InstallTier,
) -> None:
    probe = InstallProbeResult(
        python_available=python_available,
        apt_available=apt_available,
        uv_available=uv_available,
    )

    tier = select_install_tier(probe)

    assert tier is expected


@pytest.mark.asyncio
async def test_probe_install_runtime_without_uv_keeps_python_then_apt_order() -> None:
    seen: list[tuple[str, ...]] = []

    async def fake_probe(command: tuple[str, ...]) -> bool:
        seen.append(command)
        return command == APT_PROBE_COMMAND

    result = await probe_install_runtime(fake_probe)

    assert result == InstallProbeResult(
        python_available=False,
        apt_available=True,
        python_modern=None,
        uv_available=False,
    )
    assert seen == [UV_PROBE_COMMAND, PYTHON_PROBE_COMMAND, APT_PROBE_COMMAND]


@pytest.mark.asyncio
async def test_probe_install_runtime_with_modern_python_skips_version_and_apt() -> None:
    seen: list[tuple[str, ...]] = []

    async def fake_probe(command: tuple[str, ...]) -> bool:
        seen.append(command)
        return True

    result = await probe_install_runtime(fake_probe)

    assert result == InstallProbeResult(
        python_available=True,
        apt_available=False,
        python_modern=True,
        uv_available=True,
    )
    assert seen == [UV_PROBE_COMMAND, PYTHON_PROBE_COMMAND, PYTHON_VERSION_GATE_COMMAND]


@pytest.mark.asyncio
async def test_probe_install_runtime_with_old_python_probes_apt_for_fallback() -> None:
    seen: list[tuple[str, ...]] = []

    async def fake_probe(command: tuple[str, ...]) -> bool:
        seen.append(command)
        return command != PYTHON_VERSION_GATE_COMMAND

    result = await probe_install_runtime(fake_probe)

    assert result == InstallProbeResult(
        python_available=True,
        apt_available=True,
        python_modern=False,
        uv_available=True,
    )
    assert seen == [
        UV_PROBE_COMMAND,
        PYTHON_PROBE_COMMAND,
        PYTHON_VERSION_GATE_COMMAND,
        APT_PROBE_COMMAND,
    ]


@pytest.mark.asyncio
async def test_probe_install_runtime_without_python_skips_gate_and_probes_apt() -> None:
    seen: list[tuple[str, ...]] = []

    async def fake_probe(command: tuple[str, ...]) -> bool:
        seen.append(command)
        return command in (UV_PROBE_COMMAND, APT_PROBE_COMMAND)

    result = await probe_install_runtime(fake_probe)

    assert result == InstallProbeResult(
        python_available=False,
        apt_available=True,
        python_modern=False,
        uv_available=True,
    )
    assert seen == [UV_PROBE_COMMAND, PYTHON_PROBE_COMMAND, APT_PROBE_COMMAND]


def test_build_install_plan_modern_python_uses_uv_system_venv_only() -> None:
    plan = build_install_plan(_modern_python_probe(), InstallSettings())

    assert plan.tier is InstallTier.UV
    assert len(plan.stages) == 1
    commands = plan.stages[0]
    assert any(
        command.argv == (UV_BIN, "venv", VENV_ROOT, "--python", "python3") for command in commands
    )
    assert any(command.argv[:4] == (UV_BIN, "pip", "install", "--python") for command in commands)
    # No apt bootstrap and no managed-python download when python3 >= 3.12.
    assert not any(command.argv[0] == "apt-get" for command in commands)
    assert not any(command.argv[:3] == (UV_BIN, "python", "install") for command in commands)
    assert plan.exhaustion_skip is None


def test_build_install_plan_old_python_adds_managed_python_then_apt_fallback() -> None:
    plan = build_install_plan(_old_python_probe(), InstallSettings())

    assert plan.tier is InstallTier.UV
    assert len(plan.stages) == 2
    managed_stage, apt_stage = plan.stages
    assert any(
        command.argv
        == (
            UV_BIN,
            "python",
            "install",
            MANAGED_PYTHON_REQUEST,
            "--install-dir",
            PYTHON_INSTALL_ROOT,
        )
        for command in managed_stage
    )
    assert any(
        command.argv == (UV_BIN, "venv", VENV_ROOT, "--python", MANAGED_PYTHON_REQUEST)
        for command in managed_stage
    )
    assert apt_stage[0].argv == ("apt-get", "update")
    assert any(
        command.argv[:3] == ("apt-get", "install", "-y") and "python3-venv" in command.argv
        for command in apt_stage
    )
    # The apt branch re-probes the version and terminates as python_too_old on failure.
    gate = next(command for command in apt_stage if command.version_gate)
    assert gate.argv == PYTHON_VERSION_GATE_COMMAND
    assert plan.environment[UV_PYTHON_INSTALL_DIR_ENV] == PYTHON_INSTALL_ROOT


def test_build_install_plan_missing_python_with_apt_keeps_managed_then_apt_waterfall() -> None:
    plan = build_install_plan(_no_python_probe(apt=True), InstallSettings())

    assert len(plan.stages) == 2
    apt_stage = plan.stages[1]
    assert any(
        command.argv[:3] == ("apt-get", "install", "-y")
        and "python3" in command.argv
        and "python3-venv" in command.argv
        for command in apt_stage
    )
    assert any(command.version_gate for command in apt_stage)


def test_build_install_plan_missing_python_without_apt_has_only_managed_stage() -> None:
    plan = build_install_plan(_no_python_probe(apt=False), InstallSettings())

    assert len(plan.stages) == 1
    assert any(command.argv[:3] == (UV_BIN, "python", "install") for command in plan.stages[0])
    assert plan.exhaustion_skip is InstallSkipReason.NO_PYTHON_RUNTIME


def test_build_install_plan_without_any_runtime_marks_no_test() -> None:
    plan = build_install_plan(
        InstallProbeResult(python_available=False, apt_available=False),
        InstallSettings(),
    )

    assert plan.tier is InstallTier.SKIPPED
    assert plan.stages == ()
    assert plan.exhaustion_skip is InstallSkipReason.NO_PYTHON_RUNTIME
    assert plan.task_result is HarborTaskResult.NO_TEST
    assert plan.include_in_aggregate is False


def test_build_install_plan_without_uv_keeps_pip_safety_net() -> None:
    plan = build_install_plan(_legacy_python_probe(), InstallSettings())

    assert plan.tier is InstallTier.PYTHON
    commands = plan.stages[0]
    assert any(command.argv[:3] == ("python3", "-m", "venv") for command in commands)
    assert any(command.argv[1:4] == ("-m", "pip", "install") for command in commands)
    assert not any(command.argv[0] == UV_BIN for command in commands)
    assert plan.exhaustion_skip is None


def test_build_install_plan_without_uv_and_python_bootstraps_apt_first() -> None:
    plan = build_install_plan(
        InstallProbeResult(python_available=False, apt_available=True),
        InstallSettings(),
    )

    assert plan.tier is InstallTier.APT
    commands = plan.stages[0]
    assert commands[0].argv == ("apt-get", "update")
    assert commands[1].argv == ("apt-get", "install", "-y", "python3-venv")
    assert any(command.argv[:3] == ("python3", "-m", "venv") for command in commands)


def test_build_install_plan_forwards_uv_python_mirror_when_configured() -> None:
    settings = InstallSettings(python_install_mirror="https://mirrors.example/pbs")
    plan = build_install_plan(_old_python_probe(), settings)

    assert plan.environment[UV_PYTHON_INSTALL_MIRROR_ENV] == settings.python_install_mirror


def test_build_install_plan_without_mirror_keeps_uv_default_download_source() -> None:
    plan = build_install_plan(_old_python_probe(), InstallSettings())

    assert UV_PYTHON_INSTALL_MIRROR_ENV not in plan.environment


def test_install_settings_from_environment_reads_index_and_uv_mirror() -> None:
    settings = InstallSettings.from_environment(
        {
            MODEX_PIP_INDEX: "https://mirror.example/simple",
            UV_PYTHON_INSTALL_MIRROR_ENV: "https://mirrors.example/pbs",
        }
    )

    assert settings.pip_index == "https://mirror.example/simple"
    assert settings.python_install_mirror == "https://mirrors.example/pbs"
    assert InstallSettings.from_environment({}).python_install_mirror == ""


@pytest.mark.parametrize(
    ("environment", "expected_index"),
    [
        ({}, DEFAULT_PIP_INDEX),
        ({MODEX_PIP_INDEX: ""}, DEFAULT_PIP_INDEX),
        ({MODEX_PIP_INDEX: "https://mirror.example/simple"}, "https://mirror.example/simple"),
    ],
)
def test_install_settings_use_default_or_overridden_pip_index(
    environment: dict[str, str],
    expected_index: str,
) -> None:
    settings = InstallSettings.from_environment(environment)
    plan = build_install_plan(_legacy_python_probe(), settings)

    pip_command = next(
        command for stage in plan.stages for command in stage if "pip" in command.argv
    )
    index_position = pip_command.argv.index("--index-url") + 1
    assert settings.pip_index == expected_index
    assert plan.environment == {MODEX_PIP_INDEX: expected_index}
    assert pip_command.argv[index_position] == expected_index


@pytest.mark.parametrize(
    "probe",
    [
        _modern_python_probe(),
        _legacy_python_probe(),
    ],
    ids=["uv-system", "legacy-pip"],
)
def test_build_install_plan_writes_framework_and_bot_project_pth_paths(
    probe: InstallProbeResult,
) -> None:
    plan = build_install_plan(probe, InstallSettings())

    pth_command = plan.stages[0][-1].argv

    assert pth_command[:2] == (f"{VENV_ROOT}/bin/python", "-c")
    assert "'/opt/modex/src\\n/opt/modex/examples/bot_project\\n'" in pth_command[2]


def test_build_install_plan_pool_tier_materializes_modexctl_after_pip() -> None:
    plan = build_install_plan(
        _modern_python_probe(),
        InstallSettings(timeouts=TimeoutBudget(install_seconds=POOL_INSTALL_SECONDS)),
    )

    commands = plan.stages[0]
    command_index, command = next(
        (index, command)
        for index, command in enumerate(commands)
        if f"{MODECTL_BIN_DIR}/modexctl" in " ".join(command.argv)
    )
    pip_index = next(
        index for index, command in enumerate(commands) if command.argv[:2] == (UV_BIN, "pip")
    )

    assert command_index > pip_index
    assert command.argv[:2] == ("sh", "-lc")
    assert "from bot.cli.modexctl import main" in command.argv[2]
    assert f"chmod +x {MODECTL_BIN_DIR}/modexctl" in command.argv[2]
    # The shebang still points into the venv; only the binary lives in the
    # dedicated dir so the venv bin never lands on the agent shell's PATH.
    assert f"'#!{VENV_ROOT}/bin/python'" in command.argv[2]


def test_build_install_plan_bare_tier_does_not_materialize_modexctl() -> None:
    plan = build_install_plan(_modern_python_probe(), InstallSettings())

    assert not any(
        f"{MODECTL_BIN_DIR}/modexctl" in " ".join(command.argv)
        for stage in plan.stages
        for command in stage
    )


@pytest.mark.asyncio
async def test_execute_install_plan_when_runtime_is_skipped_does_not_execute() -> None:
    async def must_not_execute(_execution: CommandExecution) -> CommandResult:
        pytest.fail("skipped install plan must not execute install commands")

    plan = build_install_plan(
        InstallProbeResult(python_available=False, apt_available=False),
        InstallSettings(),
    )

    result = await execute_install_plan(plan, must_not_execute)

    assert result.task_result is HarborTaskResult.NO_TEST
    assert result.install_skipped is InstallSkipReason.NO_PYTHON_RUNTIME
    assert result.include_in_aggregate is False


@pytest.mark.asyncio
async def test_execute_install_plan_applies_configured_install_timeout() -> None:
    executions: list[CommandExecution] = []

    async def fake_execute(execution: CommandExecution) -> CommandResult:
        executions.append(execution)
        return CommandResult(exit_code=0)

    plan = build_install_plan(
        _legacy_python_probe(),
        InstallSettings(timeouts=TimeoutBudget(install_seconds=17, agent_seconds=29)),
    )

    result = await execute_install_plan(plan, fake_execute)

    assert result.task_result is HarborTaskResult.READY
    assert TimeoutBudget().install_seconds == 600
    assert TimeoutBudget().agent_seconds == 1800
    assert plan.timeouts.agent_seconds == 29
    assert executions
    assert {execution.timeout_seconds for execution in executions} == {17}


@pytest.mark.asyncio
async def test_execute_install_plan_when_command_fails_names_exact_command() -> None:
    async def fail_first(_execution: CommandExecution) -> CommandResult:
        return CommandResult(exit_code=1, stderr="offline")

    plan = build_install_plan(_legacy_python_probe(), InstallSettings())

    result = await execute_install_plan(plan, fail_first)

    assert result.task_result is HarborTaskResult.INSTALL_FAILED
    assert result.failed_command == plan.stages[0][0]
    assert "mkdir -p /opt/modex" in (result.guidance or "")


@pytest.mark.asyncio
async def test_execute_install_plan_uv_system_stage_ready_when_all_succeed() -> None:
    executions: list[CommandExecution] = []

    async def fake_execute(execution: CommandExecution) -> CommandResult:
        executions.append(execution)
        return CommandResult(exit_code=0)

    plan = build_install_plan(_modern_python_probe(), InstallSettings())

    result = await execute_install_plan(plan, fake_execute)

    assert result.task_result is HarborTaskResult.READY
    assert result.include_in_aggregate is True
    assert len(executions) == len(plan.stages[0])


@pytest.mark.asyncio
async def test_execute_install_plan_mirror_dead_and_apt_python_too_old_is_no_test() -> None:
    """python too old everywhere + mirror dead → NO_TEST python_too_old with version."""

    async def fake_execute(execution: CommandExecution) -> CommandResult:
        if execution.command.argv[:3] == (UV_BIN, "python", "install"):
            return CommandResult(exit_code=1, stderr="mirror unreachable")
        if execution.command.version_gate:
            return CommandResult(exit_code=1, stderr="3.10.12")
        return CommandResult(exit_code=0)

    plan = build_install_plan(_old_python_probe(), InstallSettings())

    result = await execute_install_plan(plan, fake_execute)

    assert result.task_result is HarborTaskResult.NO_TEST
    assert result.install_skipped is InstallSkipReason.PYTHON_TOO_OLD
    assert result.include_in_aggregate is False
    assert "3.10.12" in (result.guidance or "")


@pytest.mark.asyncio
async def test_execute_install_plan_mirror_dead_falls_through_to_apt_python() -> None:
    async def fake_execute(execution: CommandExecution) -> CommandResult:
        if execution.command.argv[:3] == (UV_BIN, "python", "install"):
            return CommandResult(exit_code=1, stderr="mirror unreachable")
        return CommandResult(exit_code=0)

    plan = build_install_plan(_old_python_probe(), InstallSettings())

    result = await execute_install_plan(plan, fake_execute)

    assert result.task_result is HarborTaskResult.READY
    assert result.include_in_aggregate is True


async def _fail_everything(_execution: CommandExecution) -> CommandResult:
    return CommandResult(exit_code=1, stderr="offline")


@pytest.mark.asyncio
async def test_execute_install_plan_exhausted_old_python_without_apt_is_no_test_too_old() -> None:
    probe = InstallProbeResult(
        python_available=True,
        apt_available=False,
        python_modern=False,
        uv_available=True,
    )
    plan = build_install_plan(probe, InstallSettings())

    result = await execute_install_plan(plan, _fail_everything)

    assert result.task_result is HarborTaskResult.NO_TEST
    assert result.install_skipped is InstallSkipReason.PYTHON_TOO_OLD
    assert result.include_in_aggregate is False
    assert result.guidance


@pytest.mark.asyncio
async def test_execute_install_plan_exhausted_missing_python_is_no_python_runtime() -> None:
    plan = build_install_plan(_no_python_probe(apt=False), InstallSettings())

    result = await execute_install_plan(plan, _fail_everything)

    assert result.task_result is HarborTaskResult.NO_TEST
    assert result.install_skipped is InstallSkipReason.NO_PYTHON_RUNTIME
    assert result.guidance


@pytest.mark.asyncio
async def test_execute_install_plan_modern_python_mechanics_failure_is_install_failed() -> None:
    plan = build_install_plan(_modern_python_probe(), InstallSettings())

    result = await execute_install_plan(plan, _fail_everything)

    assert result.task_result is HarborTaskResult.INSTALL_FAILED
    assert result.failed_command == plan.stages[0][0]
    assert result.install_skipped is None


_POOL_MODE_HOST_ENV = {
    "MODEX_AGENT_MODE": "pool",
    "MODEX_POOL_NAME": "coder",
    "MODEX_BUDGET_USD": "1.5",
    "MODEX_APPROVAL": "off",
    "MODEX_BOT_PROJECT_DIR": "/opt/modex/examples/bot_project",
    "MODEX_MAX_CONTEXT_TOKENS": "500000",
    "MODEX_TASK_NAME": "regex-log",
    "MODEX_TASK_WORKSPACE": "/app",
}


@pytest.mark.asyncio
async def test_agent_run_forwards_pool_mode_env_present_in_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name, value in _POOL_MODE_HOST_ENV.items():
        monkeypatch.setenv(name, value)
    agent = ModexHarborAgent(logs_dir=tmp_path)

    await agent.run("do the task", environment=object(), context=object())

    env = agent.agent_envs[0]
    assert env is not None
    for name, value in _POOL_MODE_HOST_ENV.items():
        assert env[name] == value
    assert agent.agent_timeouts == [None]


@pytest.mark.asyncio
async def test_agent_run_without_pool_mode_env_keeps_bare_entry_env_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in (*POOL_MODE_ENV_VARS, MODEX_PIP_INDEX, UV_PYTHON_INSTALL_MIRROR_ENV):
        monkeypatch.delenv(name, raising=False)
    agent = ModexHarborAgent(logs_dir=tmp_path)

    await agent.run("do the task", environment=object(), context=object())

    assert agent.agent_envs == [
        {
            "MODEX_TASK_INPUT_DIR": "/tmp/modex-task",
            "MODEX_TASK_INSTRUCTION_PATH": "/tmp/modex-task/instruction.txt",
            "MODEX_AGENT_OUTPUT_DIR": "/logs/agent",
            "MODEXBOT_BIN_DIR": MODECTL_BIN_DIR,
        }
    ]


@pytest.mark.asyncio
async def test_agent_run_translates_pip_mirror_into_pip_and_uv_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """MODEX_PIP_INDEX no longer leaks into the agent entry env: official
    indexes are the default (host TUN routes container traffic), and the
    run()-level mirror translation was removed with it."""
    monkeypatch.setenv(MODEX_PIP_INDEX, "https://pypi.example-mirror.cn/simple")
    monkeypatch.setenv(UV_PYTHON_INSTALL_MIRROR_ENV, "https://mirror.example/python")
    agent = ModexHarborAgent(logs_dir=tmp_path)

    await agent.run("do the task", environment=object(), context=object())

    env = agent.agent_envs[0]
    assert env is not None
    assert "PIP_INDEX_URL" not in env
    assert "UV_DEFAULT_INDEX" not in env
    assert UV_PYTHON_INSTALL_MIRROR_ENV not in env
    # MODEX_PIP_INDEX itself is also not in POOL_MODE_ENV_VARS, so it is not
    # forwarded either — the agent's shell sees pip's built-in default index.
    assert MODEX_PIP_INDEX not in env


class _FakeExecResult:
    return_code = 0
    stderr = ""


class _FakeEnvironment:
    """Container exec/upload fake recording per-command timeouts and uploads."""

    def __init__(self) -> None:
        self.timeouts: list[int | None] = []
        self.uploads: list[tuple[Path, str]] = []

    async def exec(self, *, command, user=None, env=None, timeout_sec=None):
        self.timeouts.append(timeout_sec)
        return _FakeExecResult()

    async def upload_file(self, source, target):
        self.uploads.append((Path(source), target))


class _ArchiveSpy:
    """Stand-in for build_source_archive capturing the selected manifest."""

    def __init__(self) -> None:
        self.manifests: list[SourceManifest] = []

    def __call__(self, repo_root: Path, destination: Path, *, manifest=None) -> SourceArchive:
        _ = repo_root
        self.manifests.append(manifest or SourceManifest.BARE)
        return SourceArchive(path=destination, sha256="0" * 64, members=())


@pytest.mark.asyncio
async def test_agent_install_pool_mode_selects_pool_manifest_and_relaxed_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MODEX_AGENT_MODE", raising=False)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    agent = ModexHarborAgent(logs_dir=logs_dir, extra_env={"MODEX_AGENT_MODE": "pool"})
    spy = _ArchiveSpy()
    monkeypatch.setattr("bot.eval.harbor.installed_agent.build_source_archive", spy)
    environment = _FakeEnvironment()

    await agent.install(environment)

    assert spy.manifests == [SourceManifest.POOL]
    assert environment.uploads[0][1] == SOURCE_TAR_CONTAINER_PATH
    assert {t for t in environment.timeouts if t is not None} == {POOL_INSTALL_SECONDS}
    assert TimeoutBudget().install_seconds < POOL_INSTALL_SECONDS
    recorded = json.loads((logs_dir / "install-result.json").read_text(encoding="utf-8"))
    assert recorded["task_result"] == "READY"
    assert recorded["duration_seconds"] >= 0.0


@pytest.mark.asyncio
async def test_agent_install_bare_mode_keeps_bare_manifest_and_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MODEX_AGENT_MODE", raising=False)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    agent = ModexHarborAgent(logs_dir=logs_dir)
    spy = _ArchiveSpy()
    monkeypatch.setattr("bot.eval.harbor.installed_agent.build_source_archive", spy)
    environment = _FakeEnvironment()

    await agent.install(environment)

    assert spy.manifests == [SourceManifest.BARE]
    assert environment.uploads[0][1] == SOURCE_TAR_CONTAINER_PATH
    assert {t for t in environment.timeouts if t is not None} == {TimeoutBudget().install_seconds}
    recorded = json.loads((logs_dir / "install-result.json").read_text(encoding="utf-8"))
    assert recorded["task_result"] == "READY"
