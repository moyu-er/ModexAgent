import contextlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..config import SandboxConfig
from ..env_builder import EnvironmentBuilder
from ..exceptions import CommandRejectedError
from ..guard import CommandPatternGuard
from ..isolation import (
    FilesystemIsolationConfig,
    IsolationConfig,
    IsolationManager,
    NetworkIsolationConfig,
)
from ..platform import get_shell_command_args
from ..types import SandboxResult
from ..validation import validate_code
from ..workspace_policy import WorkspacePolicy
from .base import SandboxAdapter

logger = logging.getLogger(__name__)


_IS_UNIX = hasattr(os, "fork")


def _apply_resource_limits(memory_limit_mb: int | None) -> None:
    if sys.platform == "win32":
        return
    if memory_limit_mb is None or not _IS_UNIX:
        return
    import resource

    limit_bytes = memory_limit_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))


class SubprocessSandbox(SandboxAdapter):
    @property
    def name(self) -> str:
        return "subprocess"

    @property
    def is_available(self) -> bool:
        return True

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig()
        self._command_guard: CommandPatternGuard | None = None
        self._env_builder: EnvironmentBuilder | None = None
        self._workspace_policy: WorkspacePolicy | None = None
        self._isolation_manager: IsolationManager | None = None

    def _get_isolation_manager(self) -> IsolationManager:
        """Get or create the OS-level isolation manager."""
        if self._isolation_manager is None:
            # Build isolation config from sandbox config
            isolation_config = IsolationConfig(
                filesystem=FilesystemIsolationConfig(
                    allow_read=self.config.allowed_dirs or [self.config.workspace_dir],
                    allow_write=[self.config.workspace_dir],
                ),
                network=NetworkIsolationConfig(
                    allow_all=True,  # Allow network by default
                ),
            )
            self._isolation_manager = IsolationManager(isolation_config)
        return self._isolation_manager

    def _get_command_guard(self) -> CommandPatternGuard:
        """Lazily create CommandPatternGuard from config."""
        if self._command_guard is None:
            cfg = self.config
            guard_config = cfg.command_guard if cfg and cfg.command_guard else None
            self._command_guard = CommandPatternGuard(guard_config)
        return self._command_guard

    def _get_env_builder(self) -> EnvironmentBuilder:
        """Lazily create EnvironmentBuilder from config."""
        if self._env_builder is None:
            from ..env_builder import EnvBuilderConfig, EnvPolicy

            cfg = self.config
            policy = cfg.env_policy if cfg else EnvPolicy.STANDARD
            self._env_builder = EnvironmentBuilder(EnvBuilderConfig(policy=policy))
        return self._env_builder

    def _get_workspace_policy(self) -> WorkspacePolicy | None:
        """Lazily create WorkspacePolicy from config. Returns None if not configured."""
        if self._workspace_policy is None:
            cfg = self.config
            if cfg and cfg.workspace:
                self._workspace_policy = WorkspacePolicy(cfg.workspace)
        return self._workspace_policy

    async def execute(
        self,
        code: str,
        language: str = "python",
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        if language != "python":
            return SandboxResult(
                success=False,
                error=f"SubprocessSandbox only supports Python, got {language}",
            )

        cfg = config or self.config
        start_time = time.time()

        if cfg.enable_validation:
            validation_result = validate_code(code)
            if not validation_result.is_valid:
                error_msg = f"[Validation] {validation_result.error_message}"
                if validation_result.line_number:
                    error_msg += f" (line {validation_result.line_number})"
                return SandboxResult(
                    success=False,
                    error=error_msg,
                    exit_code=None,
                    execution_time_ms=(time.time() - start_time) * 1000,
                )

        os.makedirs(cfg.workspace_dir, exist_ok=True)
        self._ensure_artifacts_dir(cfg)

        tmpdir = None
        try:
            tmpdir = tempfile.mkdtemp(dir=cfg.workspace_dir)
            code_file = Path(tmpdir) / "main.py"
            code_file.write_text(code, encoding="utf-8")

            cmd = [sys.executable, str(code_file)]

            preexec_fn = None
            if cfg.memory_limit_mb is not None and _IS_UNIX:
                def preexec_fn():
                    return _apply_resource_limits(cfg.memory_limit_mb)

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmpdir,
                env=self._get_env_builder().build(overrides={}),
                preexec_fn=preexec_fn,
            )

            try:
                stdout_bytes, stderr_bytes = proc.communicate(
                    timeout=cfg.max_execution_time_seconds
                )
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return SandboxResult(
                    success=False,
                    error=f"Execution timeout after {cfg.max_execution_time_seconds}s",
                    execution_time_ms=(time.time() - start_time) * 1000,
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            execution_time_ms = (time.time() - start_time) * 1000

            artifacts = self._collect_artifacts(cfg)

            return SandboxResult(
                success=proc.returncode == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=proc.returncode,
                artifacts=artifacts,
                execution_time_ms=execution_time_ms,
                error=None if proc.returncode == 0 else stderr,
            )

        except Exception as e:
            return SandboxResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000,
            )
        finally:
            if tmpdir and os.path.exists(tmpdir):
                with contextlib.suppress(Exception):
                    shutil.rmtree(tmpdir)

    async def execute_command(
        self,
        command: str,
        cwd: str | None = None,
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        cfg = config or self.config
        start_time = time.time()

        # Command guard check
        guard = self._get_command_guard()
        result = guard.check(command)
        if not result.allowed:
            return SandboxResult(success=False, error=f"Command blocked: {result.reason}")

        os.makedirs(cfg.workspace_dir, exist_ok=True)
        self._ensure_artifacts_dir(cfg)

        work_dir = cwd or cfg.workspace_dir

        # Workspace path check
        workspace = self._get_workspace_policy()
        if workspace and cwd:
            try:
                workspace.require_within(cwd)
            except CommandRejectedError as e:
                return SandboxResult(success=False, error=str(e))

        try:
            # Get OS-level isolation manager
            isolation = self._get_isolation_manager()

            # Build the command with isolation if available
            if isolation.is_available():
                shell_cmd = get_shell_command_args(command)
                isolated_cmd = isolation.wrap_command(shell_cmd)
                logger.info(f"Using OS-level isolation: {isolation.get_provider_name()}")
            else:
                isolated_cmd = get_shell_command_args(command)

            preexec_fn = None
            if cfg.memory_limit_mb is not None and _IS_UNIX:
                def preexec_fn():
                    return _apply_resource_limits(cfg.memory_limit_mb)

            proc = subprocess.Popen(
                isolated_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=work_dir,
                env=self._get_env_builder().build(overrides={}),
                preexec_fn=preexec_fn,
                shell=isinstance(isolated_cmd, str),
            )

            try:
                stdout_bytes, stderr_bytes = proc.communicate(
                    timeout=cfg.max_execution_time_seconds
                )
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return SandboxResult(
                    success=False,
                    error=f"Command timeout after {cfg.max_execution_time_seconds}s",
                    execution_time_ms=(time.time() - start_time) * 1000,
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            execution_time_ms = (time.time() - start_time) * 1000

            artifacts = self._collect_artifacts(cfg)

            return SandboxResult(
                success=proc.returncode == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=proc.returncode,
                artifacts=artifacts,
                execution_time_ms=execution_time_ms,
                error=None if proc.returncode == 0 else stderr,
            )

        except Exception as e:
            return SandboxResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000,
            )

    async def cleanup(self, sandbox_id: str | None = None) -> None:
        pass
