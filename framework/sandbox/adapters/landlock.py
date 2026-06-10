import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..config import SandboxConfig
from ..env_builder import EnvBuilderConfig, EnvPolicy, EnvironmentBuilder
from ..exceptions import SandboxUnavailableError
from ..guard import CommandPatternGuard, CommandPatternGuardConfig
from ..types import SandboxResult
from ..validation import validate_code
from ..workspace_policy import WorkspacePolicy, WorkspacePolicyConfig
from .base import SandboxAdapter

LANDLOCK_AVAILABLE = False
try:
    import landlock

    LANDLOCK_AVAILABLE = True
except ImportError:
    pass


def _check_landlock_available() -> bool:
    if not LANDLOCK_AVAILABLE:
        return False
    if sys.platform != "linux":
        return False
    try:
        landlock.create_ruleset(accesses={"read", "write", "exec"})
        return True
    except Exception:
        return False


class LandlockSandbox(SandboxAdapter):
    @property
    def name(self) -> str:
        return "landlock"

    @property
    def is_available(self) -> bool:
        return _check_landlock_available()

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()
        self._command_guard: CommandPatternGuard | None = None
        self._env_builder: EnvironmentBuilder | None = None
        self._workspace_policy: WorkspacePolicy | None = None

    def _create_ruleset(self, allowed_dirs: list[str]):
        if not LANDLOCK_AVAILABLE:
            raise SandboxUnavailableError("landlock Python package not installed")

        ruleset = landlock.create_ruleset(
            accesses={"read", "write", "exec", "read_file", "write_file", "exec_dir"}
        )

        for dir_path in allowed_dirs:
            path = Path(dir_path).resolve()
            if path.exists():
                ruleset.add_path_allowed(str(path), landlock.Accesses.ALL)

        return ruleset

    async def execute(
        self,
        code: str,
        language: str = "python",
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        if not self.is_available:
            return SandboxResult(
                success=False,
                error="Landlock is not available on this system (requires Linux 5.13+)",
            )

        if language != "python":
            return SandboxResult(
                success=False,
                error=f"LandlockSandbox only supports Python, got {language}",
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
                    execution_time_ms=(time.time() - start_time) * 1000,
                )

        try:
            ruleset = self._create_ruleset(cfg.allowed_dirs)
        except Exception as e:
            return SandboxResult(success=False, error=f"Failed to create ruleset: {e}")

        os.makedirs(cfg.workspace_dir, exist_ok=True)
        self._ensure_artifacts_dir(cfg)

        tmpdir = None
        try:
            tmpdir = tempfile.mkdtemp(dir=cfg.workspace_dir)
            code_file = Path(tmpdir) / "main.py"
            code_file.write_text(code, encoding="utf-8")

            cmd = [sys.executable, str(code_file)]

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmpdir,
                env=self._get_env_builder().build(overrides={"SANDBOX_ARTIFACTS_DIR": self._get_artifacts_dir(cfg)}),
                preexec_fn=ruleset.restrict_self,
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
        cwd: str | None = None,
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        if not self.is_available:
            return SandboxResult(
                success=False,
                error="Landlock is not available on this system (requires Linux 5.13+)",
            )

        cfg = config or self.config
        start_time = time.time()

        guard = self._get_command_guard()
        result = guard.check(command)
        if not result.allowed:
            return SandboxResult(success=False, error=f"Command blocked: {result.reason}")

        try:
            ruleset = self._create_ruleset(cfg.allowed_dirs)
        except Exception as e:
            return SandboxResult(success=False, error=f"Failed to create ruleset: {e}")

        os.makedirs(cfg.workspace_dir, exist_ok=True)
        self._ensure_artifacts_dir(cfg)

        work_dir = cwd or cfg.workspace_dir

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=work_dir,
                env=self._get_env_builder().build(overrides={"SANDBOX_ARTIFACTS_DIR": self._get_artifacts_dir(cfg)}),
                preexec_fn=ruleset.restrict_self,
                shell=True,
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

    def _get_command_guard(self) -> CommandPatternGuard:
        if self._command_guard is None:
            cfg = self.config
            guard_config = cfg.command_guard if cfg and cfg.command_guard else None
            self._command_guard = CommandPatternGuard(guard_config)
        return self._command_guard

    def _get_env_builder(self) -> EnvironmentBuilder:
        if self._env_builder is None:
            cfg = self.config
            policy = cfg.env_policy if cfg else EnvPolicy.STANDARD
            self._env_builder = EnvironmentBuilder(EnvBuilderConfig(policy=policy))
        return self._env_builder

    def _get_workspace_policy(self) -> WorkspacePolicy | None:
        if self._workspace_policy is None:
            cfg = self.config
            if cfg and cfg.workspace:
                self._workspace_policy = WorkspacePolicy(cfg.workspace)
        return self._workspace_policy

    async def cleanup(self, sandbox_id: str | None = None) -> None:
        pass
