import os
import sys
import shutil
import tempfile
import subprocess
import time
import logging
from pathlib import Path
from typing import Optional

from .base import SandboxAdapter
from ..types import SandboxResult
from ..config import SandboxConfig
from ..exceptions import SandboxError, SandboxTimeoutError
from ..validation import validate_code
from framework.security import SecurityChecker, SecurityConfig
from ..platform import get_shell_command_args
from ..isolation import IsolationManager, IsolationConfig, FilesystemIsolationConfig, NetworkIsolationConfig

logger = logging.getLogger(__name__)


_IS_UNIX = hasattr(os, "fork")


def _apply_resource_limits(memory_limit_mb: Optional[int]):
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

    def __init__(self, config: Optional[SandboxConfig] = None):
        self.config = config or SandboxConfig()
        self._security_checker: Optional[SecurityChecker] = None
        self._isolation_manager: Optional[IsolationManager] = None
    
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
    
    def _get_security_checker(self) -> SecurityChecker:
        """Get or create the security checker."""
        if self._security_checker is None:
            security_config = self.config.security or SecurityConfig()
            self._security_checker = SecurityChecker(security_config)
        return self._security_checker

    async def execute(
        self,
        code: str,
        language: str = "python",
        config: Optional[SandboxConfig] = None,
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
                preexec_fn = lambda: _apply_resource_limits(cfg.memory_limit_mb)

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmpdir,
                env=self._get_safe_env(cfg),
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
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass

    async def execute_command(
        self,
        command: str,
        cwd: Optional[str] = None,
        config: Optional[SandboxConfig] = None,
    ) -> SandboxResult:
        cfg = config or self.config
        start_time = time.time()

        # Security check with approval
        security_checker = self._get_security_checker()
        try:
            security_result = await security_checker.check_and_approve(command)
        except SandboxError as e:
            return SandboxResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000,
            )

        os.makedirs(cfg.workspace_dir, exist_ok=True)
        self._ensure_artifacts_dir(cfg)

        work_dir = cwd or cfg.workspace_dir

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
                preexec_fn = lambda: _apply_resource_limits(cfg.memory_limit_mb)

            proc = subprocess.Popen(
                isolated_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=work_dir,
                env=self._get_safe_env(cfg),
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

    def _get_safe_env(self, config: SandboxConfig) -> dict:
        env = os.environ.copy()
        safe_vars = [
            # Cross-platform
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "LC_ALL",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONIOENCODING",
            # Windows-specific -- critical for subprocess operation
            "SYSTEMROOT",
            "COMSPEC",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "PATHEXT",
            "TEMP",
            "TMP",
            "PROGRAMFILES",
            "HOMEDRIVE",
            "HOMEPATH",
        ]
        filtered_env = {k: env[k] for k in safe_vars if k in env}
        filtered_env["SANDBOX_ARTIFACTS_DIR"] = self._get_artifacts_dir(config)
        return filtered_env

    async def cleanup(self, sandbox_id: Optional[str] = None) -> None:
        pass
