from __future__ import annotations

import shlex
import time
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict, Field

MODEX_PIP_INDEX: Final = "MODEX_PIP_INDEX"
DEFAULT_PIP_INDEX: Final = "https://pypi.org/simple"
SOURCE_TAR_CONTAINER_PATH: Final = "/tmp/modex-src.tar.gz"
INSTALL_ROOT: Final = "/opt/modex"
VENV_ROOT: Final = f"{INSTALL_ROOT}/venv"
# uv lands at this absolute path via the docker-compose.uv.yml overlay (the
# modex-uv-bin volume is seeded at /uv-bin and mounted at /opt/modex-uv in the
# main service); commands reference it absolutely instead of trusting PATH.
UV_BIN: Final = "/opt/modex-uv/uv"
# Managed-python target of the waterfall's uv step. uv nests each build under
# <install-dir>/cpython-…, so later commands rediscover it through
# UV_PYTHON_INSTALL_DIR instead of a hard-coded interpreter path.
PYTHON_INSTALL_ROOT: Final = f"{INSTALL_ROOT}/python"
MANAGED_PYTHON_REQUEST: Final = "3.12"
UV_PYTHON_INSTALL_MIRROR_ENV: Final = "UV_PYTHON_INSTALL_MIRROR"
UV_PYTHON_INSTALL_DIR_ENV: Final = "UV_PYTHON_INSTALL_DIR"
# Pool-mode env names that host_runtime and installed_agent conditionally
# forward to container trials; must mirror the pool-mode env contract readers.
POOL_MODE_ENV_VARS: Final[tuple[str, ...]] = (
    "MODEX_AGENT_MODE",
    "MODEX_POOL_NAME",
    "MODEX_BUDGET_USD",
    "MODEX_APPROVAL",
    "MODEX_BOT_PROJECT_DIR",
    "MODEX_TEMPERATURE",
    "MODEX_REASONING_EFFORT",
    "MODEX_MAX_CONTEXT_TOKENS",
    "MODEX_MAX_OUTPUT_TOKENS",
    "MODEX_TASK_NAME",
    "MODEX_TASK_WORKSPACE",
    "MODEX_EVAL_ROSTER",
    "OTEL_FORMAT",
)

PYTHON_PROBE_COMMAND: Final = (
    "sh",
    "-lc",
    "command -v python3 >/dev/null 2>&1",
)
APT_PROBE_COMMAND: Final = (
    "sh",
    "-lc",
    "command -v apt-get >/dev/null 2>&1",
)
UV_PROBE_COMMAND: Final = ("sh", "-lc", f"test -x {UV_BIN}")
# Exit-code form of the version probe: the ProbeCommand callback only sees the
# exit code, so >=3.12 exits 0. On failure the found version is written to
# stderr so the plan executor can record exactly which version was too old.
PYTHON_VERSION_GATE_COMMAND: Final = (
    "sh",
    "-lc",
    'python3 -c "import sys; sys.stderr.write(sys.version.split()[0]); '
    'sys.exit(0 if sys.version_info >= (3, 12) else 1)"',
)

DEPENDENCY_CLOSURE: Final = (
    "pydantic>=2.0.0,<3",
    "anyio>=4.0.0",
    "filelock>=3.12.0",
    "httpx>=0.24.0",
    "certifi>=2024.0.0",
    "aiohttp>=3.9.0,<4.0.0",
    "pyyaml>=6.0.0",
    "mcp>=1.0.0,<2",
    "pathvalidate>=3.0.0",
    "typing-extensions>=4.8.0",
    "ddgs>=9.0.0",
    "markdownify>=0.13.0",
    "typer>=0.9.0",
    "rich>=14.0.0,<15.0.0",
    "tiktoken>=0.13.0",
    "path>=17.1.1",
    "aiosqlite>=0.20.0,<1",
    "python-dotenv>=1.2.2",
    "litellm>=1.82.6",
    "openai>=2.20.0,<3",
    "pexpect>=4.0",
    "libtmux>=0.15.0",
    "opentelemetry-sdk>=1.28",
    "opentelemetry-exporter-otlp-proto-http>=1.28",
)

_PTH_SCRIPT: Final = (
    "from pathlib import Path; import site; "
    "Path(site.getsitepackages()[0], 'modex-src.pth').write_text("
    "'/opt/modex/src\\n/opt/modex/examples/bot_project\\n', encoding='utf-8')"
)
_MODEXCTL_SCRIPT: Final = (
    "printf '%s\\n' "
    f"'#!{VENV_ROOT}/bin/python' "
    "'import sys' 'from bot.cli.modexctl import main' "
    f"'sys.exit(main())' > {VENV_ROOT}/bin/modexctl && "
    f"chmod +x {VENV_ROOT}/bin/modexctl"
)


class InstallTier(StrEnum):
    PYTHON = "python"
    APT = "apt"
    UV = "uv"
    SKIPPED = "skipped"


class InstallSkipReason(StrEnum):
    NO_PYTHON_RUNTIME = "no_python_runtime"
    PYTHON_TOO_OLD = "python_too_old"


class HarborTaskResult(StrEnum):
    READY = "READY"
    NO_TEST = "NO_TEST"
    INSTALL_FAILED = "INSTALL_FAILED"


class TimeoutBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    install_seconds: int = Field(default=600, gt=0)
    agent_seconds: int = Field(default=1800, gt=0)


class InstallSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pip_index: str = Field(default=DEFAULT_PIP_INDEX, min_length=1)
    source_tar_path: str = Field(default=SOURCE_TAR_CONTAINER_PATH, min_length=1)
    python_install_mirror: str = ""
    timeouts: TimeoutBudget = Field(default_factory=TimeoutBudget)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> InstallSettings:
        return cls(
            pip_index=environment.get(MODEX_PIP_INDEX) or DEFAULT_PIP_INDEX,
            python_install_mirror=environment.get(UV_PYTHON_INSTALL_MIRROR_ENV) or "",
        )


class InstallProbeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    python_available: bool
    apt_available: bool
    # True ⇔ probed python3 >= 3.12 (PEP 695 framework floor); False covers
    # both "present but older" and "absent"; None = version not probed (no uv).
    python_modern: bool | None = None
    uv_available: bool = False


class InstallCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: tuple[str, ...] = Field(min_length=1)
    run_as_root: bool = True
    # A failing version-gate command means "python still < 3.12" and terminates
    # the waterfall as NO_TEST (the found version arrives via stderr) instead
    # of falling through to the next stage.
    version_gate: bool = False


type InstallStage = tuple[InstallCommand, ...]

type ProbeCommand = Callable[[tuple[str, ...]], Awaitable[bool]]
type ExecuteCommand = Callable[[CommandExecution], Awaitable[CommandResult]]


class InstallPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: InstallTier
    # Waterfall stages: a failed stage falls through to the next one; the
    # legacy safety-net tiers carry exactly one stage.
    stages: tuple[InstallStage, ...]
    environment: dict[str, str]
    timeouts: TimeoutBudget
    # Outcome recorded when every stage failed: a skip reason turns exhaustion
    # into NO_TEST; None keeps the historical INSTALL_FAILED semantics.
    exhaustion_skip: InstallSkipReason | None = None
    task_result: HarborTaskResult
    include_in_aggregate: bool


class CommandExecution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    command: InstallCommand
    environment: dict[str, str]
    timeout_seconds: int = Field(gt=0)


class CommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stderr: str = ""


class InstallExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_result: HarborTaskResult
    include_in_aggregate: bool
    install_skipped: InstallSkipReason | None = None
    failed_command: InstallCommand | None = None
    guidance: str | None = None
    duration_seconds: float = 0.0


async def probe_install_runtime(probe: ProbeCommand) -> InstallProbeResult:
    """Probe the container: uv overlay, python3, python3 version, then apt.

    The apt probe only runs when a fallback stage may need it (uv waterfall
    with a non-modern python, or the legacy path without python3).
    """
    uv_available = await probe(UV_PROBE_COMMAND)
    python_available = await probe(PYTHON_PROBE_COMMAND)
    if not uv_available:
        if python_available:
            return InstallProbeResult(
                python_available=True,
                apt_available=False,
                python_modern=None,
                uv_available=False,
            )
        return InstallProbeResult(
            python_available=False,
            apt_available=await probe(APT_PROBE_COMMAND),
            python_modern=None,
            uv_available=False,
        )
    python_modern = python_available and await probe(PYTHON_VERSION_GATE_COMMAND)
    return InstallProbeResult(
        python_available=python_available,
        apt_available=(not python_modern) and await probe(APT_PROBE_COMMAND),
        python_modern=python_modern,
        uv_available=True,
    )


def select_install_tier(probe: InstallProbeResult) -> InstallTier:
    if probe.uv_available:
        return InstallTier.UV
    if probe.python_available:
        return InstallTier.PYTHON
    if probe.apt_available:
        return InstallTier.APT
    return InstallTier.SKIPPED


def build_install_plan(probe: InstallProbeResult, settings: InstallSettings) -> InstallPlan:
    tier = select_install_tier(probe)
    environment = {MODEX_PIP_INDEX: settings.pip_index}
    if settings.python_install_mirror:
        environment[UV_PYTHON_INSTALL_MIRROR_ENV] = settings.python_install_mirror
    stages: tuple[InstallStage, ...]
    exhaustion_skip: InstallSkipReason | None
    match tier:
        case InstallTier.SKIPPED:
            return InstallPlan(
                tier=tier,
                stages=(),
                environment=environment,
                timeouts=settings.timeouts,
                exhaustion_skip=InstallSkipReason.NO_PYTHON_RUNTIME,
                task_result=HarborTaskResult.NO_TEST,
                include_in_aggregate=False,
            )
        case InstallTier.PYTHON:
            stages = (_legacy_python_stage(settings),)
            exhaustion_skip = None
        case InstallTier.APT:
            stages = (_legacy_apt_stage(settings),)
            exhaustion_skip = None
        case InstallTier.UV:
            stages = _uv_waterfall_stages(probe, settings)
            exhaustion_skip = _uv_exhaustion_skip(probe)
            if probe.python_modern is not True:
                # The managed stage's venv step resolves `--python 3.12`
                # through uv's install-dir discovery.
                environment[UV_PYTHON_INSTALL_DIR_ENV] = PYTHON_INSTALL_ROOT
        case unreachable:
            assert_never(unreachable)
    return InstallPlan(
        tier=tier,
        stages=stages,
        environment=environment,
        timeouts=settings.timeouts,
        exhaustion_skip=exhaustion_skip,
        task_result=HarborTaskResult.READY,
        include_in_aggregate=True,
    )


async def execute_install_plan(
    plan: InstallPlan,
    execute: ExecuteCommand,
) -> InstallExecutionResult:
    """Execute the waterfall: stage failures fall through, gate failures stop."""
    started = time.perf_counter()
    failed_command: InstallCommand | None = None
    failed_stderr = ""
    for stage in plan.stages:
        for command in stage:
            result = await execute(
                CommandExecution(
                    command=command,
                    environment=plan.environment,
                    timeout_seconds=plan.timeouts.install_seconds,
                )
            )
            if result.exit_code == 0:
                continue
            if command.version_gate:
                return InstallExecutionResult(
                    task_result=HarborTaskResult.NO_TEST,
                    include_in_aggregate=False,
                    install_skipped=InstallSkipReason.PYTHON_TOO_OLD,
                    guidance=(
                        "Python too old for the framework (>=3.12 required): "
                        f"found {result.stderr or 'unknown version'} after all install fallbacks."
                    ),
                    duration_seconds=round(time.perf_counter() - started, 3),
                )
            failed_command, failed_stderr = command, result.stderr
            break
        else:
            return InstallExecutionResult(
                task_result=HarborTaskResult.READY,
                include_in_aggregate=True,
                duration_seconds=round(time.perf_counter() - started, 3),
            )
    return _exhausted_install_result(plan, failed_command, failed_stderr, started)


def _exhausted_install_result(
    plan: InstallPlan,
    failed_command: InstallCommand | None,
    failed_stderr: str,
    started: float,
) -> InstallExecutionResult:
    """Every stage failed (or the plan has none): the plan's exhaustion outcome."""
    duration_seconds = round(time.perf_counter() - started, 3)
    if plan.exhaustion_skip is None:
        rendered = shlex.join(failed_command.argv) if failed_command is not None else "none"
        return InstallExecutionResult(
            task_result=HarborTaskResult.INSTALL_FAILED,
            include_in_aggregate=False,
            failed_command=failed_command,
            guidance=(
                f"Install command failed: {rendered}. "
                f"Check {MODEX_PIP_INDEX}={plan.environment[MODEX_PIP_INDEX]!r} "
                f"and rerun this exact command. stderr: {failed_stderr}"
            ),
            duration_seconds=duration_seconds,
        )
    guidance = None
    if failed_command is not None:
        guidance = (
            "Every install fallback failed; last failure: "
            f"{shlex.join(failed_command.argv)}: {failed_stderr}"
        )
    return InstallExecutionResult(
        task_result=HarborTaskResult.NO_TEST,
        include_in_aggregate=False,
        install_skipped=plan.exhaustion_skip,
        guidance=guidance,
        duration_seconds=duration_seconds,
    )


def _uv_waterfall_stages(
    probe: InstallProbeResult,
    settings: InstallSettings,
) -> tuple[InstallStage, ...]:
    """Waterfall ② → ③ → ④: system-python venv, managed 3.12, apt fallback."""
    if probe.python_modern is True:
        return (_uv_system_stage(settings),)
    stages: list[InstallStage] = [_uv_managed_stage(settings)]
    if probe.apt_available:
        stages.append(_uv_apt_stage(settings))
    return tuple(stages)


def _uv_exhaustion_skip(probe: InstallProbeResult) -> InstallSkipReason | None:
    if probe.python_modern is True:
        # Python itself is healthy; exhaustion means the install mechanics
        # broke, which keeps the historical INSTALL_FAILED semantics.
        return None
    if probe.python_available:
        return InstallSkipReason.PYTHON_TOO_OLD
    return InstallSkipReason.NO_PYTHON_RUNTIME


def _uv_system_stage(settings: InstallSettings) -> InstallStage:
    """② system python3 >= 3.12: uv venv (no ensurepip needed) + uv pip."""
    return _finish_stage(
        settings,
        provision=(InstallCommand(argv=(UV_BIN, "venv", VENV_ROOT, "--python", "python3")),),
        closure=_uv_pip_closure(settings),
    )


def _uv_managed_stage(settings: InstallSettings) -> InstallStage:
    """③ managed python: download 3.12 via uv, venv from it, uv pip closure."""
    return _finish_stage(
        settings,
        provision=(
            InstallCommand(
                argv=(
                    UV_BIN,
                    "python",
                    "install",
                    MANAGED_PYTHON_REQUEST,
                    "--install-dir",
                    PYTHON_INSTALL_ROOT,
                )
            ),
            InstallCommand(argv=(UV_BIN, "venv", VENV_ROOT, "--python", MANAGED_PYTHON_REQUEST)),
        ),
        closure=_uv_pip_closure(settings),
    )


def _uv_apt_stage(settings: InstallSettings) -> InstallStage:
    """④ apt fallback: install python3 + venv, re-probe the version, uv install."""
    return _finish_stage(
        settings,
        bootstrap=(
            InstallCommand(argv=("apt-get", "update")),
            InstallCommand(argv=("apt-get", "install", "-y", "python3", "python3-venv")),
            InstallCommand(argv=PYTHON_VERSION_GATE_COMMAND, version_gate=True),
        ),
        provision=(InstallCommand(argv=(UV_BIN, "venv", VENV_ROOT, "--python", "python3")),),
        closure=_uv_pip_closure(settings),
    )


def _legacy_python_stage(settings: InstallSettings) -> InstallStage:
    """⑤ safety net: system python3 with stdlib venv/pip (needs python3-venv)."""
    return _finish_stage(
        settings,
        provision=(InstallCommand(argv=("python3", "-m", "venv", VENV_ROOT)),),
        closure=_pip_closure(settings),
    )


def _legacy_apt_stage(settings: InstallSettings) -> InstallStage:
    """⑤ safety net: apt bootstrap (python3-venv pulls python3) then pip chain."""
    return _finish_stage(
        settings,
        bootstrap=(
            InstallCommand(argv=("apt-get", "update")),
            InstallCommand(argv=("apt-get", "install", "-y", "python3-venv")),
        ),
        provision=(InstallCommand(argv=("python3", "-m", "venv", VENV_ROOT)),),
        closure=_pip_closure(settings),
    )


def _finish_stage(
    settings: InstallSettings,
    *,
    bootstrap: tuple[InstallCommand, ...] = (),
    provision: tuple[InstallCommand, ...],
    closure: InstallCommand,
) -> InstallStage:
    """One waterfall stage: bootstrap → source unpack → interpreter → closure."""
    commands = (
        *bootstrap,
        InstallCommand(argv=("mkdir", "-p", INSTALL_ROOT)),
        InstallCommand(argv=("tar", "-xzf", settings.source_tar_path, "-C", INSTALL_ROOT)),
        *provision,
        closure,
        InstallCommand(argv=(f"{VENV_ROOT}/bin/python", "-c", _PTH_SCRIPT)),
    )
    if settings.timeouts.install_seconds <= TimeoutBudget().install_seconds:
        return commands
    return (*commands, InstallCommand(argv=("sh", "-lc", _MODEXCTL_SCRIPT)))


def _uv_pip_closure(settings: InstallSettings) -> InstallCommand:
    return InstallCommand(
        argv=(
            UV_BIN,
            "pip",
            "install",
            "--python",
            f"{VENV_ROOT}/bin/python",
            "--index-url",
            settings.pip_index,
            *DEPENDENCY_CLOSURE,
        )
    )


def _pip_closure(settings: InstallSettings) -> InstallCommand:
    return InstallCommand(
        argv=(
            f"{VENV_ROOT}/bin/python",
            "-m",
            "pip",
            "install",
            "--index-url",
            settings.pip_index,
            *DEPENDENCY_CLOSURE,
        )
    )


__all__ = [
    "APT_PROBE_COMMAND",
    "CommandExecution",
    "CommandResult",
    "DEFAULT_PIP_INDEX",
    "HarborTaskResult",
    "InstallCommand",
    "InstallPlan",
    "InstallProbeResult",
    "InstallSettings",
    "InstallSkipReason",
    "InstallStage",
    "InstallTier",
    "MANAGED_PYTHON_REQUEST",
    "MODEX_PIP_INDEX",
    "POOL_MODE_ENV_VARS",
    "PYTHON_INSTALL_ROOT",
    "PYTHON_PROBE_COMMAND",
    "PYTHON_VERSION_GATE_COMMAND",
    "SOURCE_TAR_CONTAINER_PATH",
    "TimeoutBudget",
    "UV_BIN",
    "UV_PROBE_COMMAND",
    "UV_PYTHON_INSTALL_DIR_ENV",
    "UV_PYTHON_INSTALL_MIRROR_ENV",
    "VENV_ROOT",
    "build_install_plan",
    "execute_install_plan",
    "probe_install_runtime",
    "select_install_tier",
]
