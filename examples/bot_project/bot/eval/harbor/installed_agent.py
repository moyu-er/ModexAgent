"""Harbor installed-agent bridge that executes the protected T26/T27 seams."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Final, override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment

if TYPE_CHECKING:
    import harbor.models.agent.context as harbor_agent_context

from bot.eval.harbor.agent import (
    MODECTL_BIN_DIR,
    MODEX_PIP_INDEX,
    POOL_MODE_ENV_VARS,
    SOURCE_TAR_CONTAINER_PATH,
    UV_PYTHON_INSTALL_MIRROR_ENV,
    CommandExecution,
    CommandResult,
    HarborTaskResult,
    InstallExecutionResult,
    InstallSettings,
    TimeoutBudget,
    build_install_plan,
    execute_install_plan,
    probe_install_runtime,
)
from bot.eval.harbor.source_package import SourceManifest, build_source_archive

_REPO_ROOT: Final = Path(__file__).resolve().parents[5]
_ENTRY_COMMAND: Final = (
    "/opt/modex/venv/bin/python /opt/modex/examples/bot_project/bot/eval/harbor/entry.py"
)
_POOL_AGENT_MODE: Final = "pool"
# Pool manifest ships the full bot/ tree, so its pip closure (aiohttp, rich)
# needs a wider install budget than the bare tier's 600s default.
POOL_INSTALL_SECONDS: Final = 900
# Trial containers resolve apt sources to archive/security.ubuntu.com and
# deb.debian.org — all routed through the VPN tunnel, where concurrent
# apt downloads (8 trials racing) both crawl and get truncated mid-flight,
# leaving `dpkg --configure -a` broken for the verifier (observed: 4 tasks
# poisoned in one window, tb21-all-v8). Rewriting to the aliyun mirror puts
# apt traffic on the DIRECT route, bypassing the tunnel entirely. Plain
# HTTP (not HTTPS): task images ship stale ca-certificates that fail the
# aliyun TLS chain, and apt verifies repository metadata via GPG signatures,
# so HTTP is safe here. Handles both source formats: the legacy one-line
# .list and Ubuntu 24.04's deb822 .sources (URIs: lines). Idempotent,
# best-effort: a failed rewrite is no worse than the previous status quo.
_APT_MIRROR_SETUP: Final = (
    "sed -i.bak -E 's|http://(archive\\|security)\\.ubuntu\\.com/ubuntu/?|"
    "http://mirrors.aliyun.com/ubuntu/|g; "
    "s|http://deb\\.debian\\.org/debian-security/?|http://mirrors.aliyun.com/debian-security/|g; "
    "s|http://deb\\.debian\\.org/debian/?|http://mirrors.aliyun.com/debian/|g; "
    "s|http://security\\.debian\\.org/debian-security/?|http://mirrors.aliyun.com/debian-security/|g' "
    "/etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources 2>/dev/null "
    "|| true"
)


class ModexHarborInstallError(RuntimeError):
    pass


class ModexHarborAgent(BaseInstalledAgent):
    """Harbor adapter whose policy remains in T26 and whose turn remains in T27."""

    _install_result: InstallExecutionResult | None = None

    @staticmethod
    @override
    def name() -> str:
        return "modex-harbor"

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # Apt mirror rewrite FIRST — before any probe or install command can
        # trigger an apt transaction over the VPN tunnel. Best-effort: never
        # blocks the install waterfall.
        mirror_result = await environment.exec(
            command=f"sh -c {_APT_MIRROR_SETUP!r}", user="root"
        )
        if mirror_result.return_code != 0:
            self.logs_dir.joinpath("apt-mirror-setup.log").write_text(
                f"exit={mirror_result.return_code}\n{mirror_result.stderr or ''}",
                encoding="utf-8",
            )

        async def probe(argv: tuple[str, ...]) -> bool:
            result = await environment.exec(command=shlex.join(argv), user="root")
            return result.return_code == 0

        probe_result = await probe_install_runtime(probe)
        pool_mode = self._get_env("MODEX_AGENT_MODE") == _POOL_AGENT_MODE
        settings = InstallSettings(
            pip_index=self._get_env(MODEX_PIP_INDEX) or InstallSettings().pip_index,
            python_install_mirror=self._get_env(UV_PYTHON_INSTALL_MIRROR_ENV) or "",
            timeouts=(
                TimeoutBudget(install_seconds=POOL_INSTALL_SECONDS)
                if pool_mode
                else TimeoutBudget()
            ),
        )
        plan = build_install_plan(probe_result, settings)
        if plan.include_in_aggregate:
            archive = build_source_archive(
                _REPO_ROOT,
                self.logs_dir / "setup" / "modex-src.tar.gz",
                manifest=SourceManifest.POOL if pool_mode else SourceManifest.BARE,
            )
            await environment.upload_file(archive.path, SOURCE_TAR_CONTAINER_PATH)

        async def execute(execution: CommandExecution) -> CommandResult:
            result = await environment.exec(
                command=shlex.join(execution.command.argv),
                user="root" if execution.command.run_as_root else None,
                env=execution.environment,
                timeout_sec=execution.timeout_seconds,
            )
            return CommandResult(
                exit_code=result.return_code,
                stderr=result.stderr or "",
            )

        self._install_result = await execute_install_plan(plan, execute)
        (self.logs_dir / "install-result.json").write_text(
            self._install_result.model_dump_json(indent=2),
            encoding="utf-8",
        )
        if self._install_result.task_result is HarborTaskResult.INSTALL_FAILED:
            raise ModexHarborInstallError(self._install_result.guidance or "install failed")

    @with_prompt_template
    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: harbor_agent_context.AgentContext,
    ) -> None:
        _ = context
        if (
            self._install_result is not None
            and self._install_result.task_result is HarborTaskResult.NO_TEST
        ):
            (self.logs_dir / "install-result.json").write_text(
                self._install_result.model_dump_json(indent=2),
                encoding="utf-8",
            )
            return
        instruction_path = "/tmp/modex-task/instruction.txt"
        command = (
            "mkdir -p /tmp/modex-task /logs/agent && "
            f"printf %s {shlex.quote(instruction)} > {instruction_path} && "
            f"{_ENTRY_COMMAND}"
        )
        entry_environment = {
            "MODEX_TASK_INPUT_DIR": "/tmp/modex-task",
            "MODEX_TASK_INSTRUCTION_PATH": instruction_path,
            "MODEX_AGENT_OUTPUT_DIR": "/logs/agent",
            # Points the modexctl resolver at the dedicated bin dir (install
            # stage populates it). Without this, the resolver falls back to
            # the venv's bin dir, whose python3/python symlinks + closure
            # packages (e.g. cryptography) leak onto the agent shell's PATH
            # and corrupt the task image's environment.
            "MODEXBOT_BIN_DIR": MODECTL_BIN_DIR,
        }
        for name in POOL_MODE_ENV_VARS:
            if value := self._get_env(name):
                entry_environment[name] = value
        await self.exec_as_agent(
            environment,
            command=command,
            env=entry_environment,
        )


__all__ = ["ModexHarborAgent", "ModexHarborInstallError", "POOL_INSTALL_SECONDS"]
