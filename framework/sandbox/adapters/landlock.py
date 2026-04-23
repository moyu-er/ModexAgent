import os
import sys
import shutil
import tempfile
import subprocess
import time
from pathlib import Path
from typing import Optional, List

from .base import SandboxAdapter
from ..types import SandboxResult
from ..config import SandboxConfig
from ..exceptions import SandboxError, SandboxUnavailableError
from ..validation import validate_code


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

    def __init__(self, config: Optional[SandboxConfig] = None):
        self.config = config or SandboxConfig()

    def _create_ruleset(self, allowed_dirs: List[str]):
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
        config: Optional[SandboxConfig] = None,
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
                env=self._get_safe_env(cfg),
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
        cwd: Optional[str] = None,
        config: Optional[SandboxConfig] = None,
    ) -> SandboxResult:
        if not self.is_available:
            return SandboxResult(
                success=False,
                error="Landlock is not available on this system (requires Linux 5.13+)",
            )

        cfg = config or self.config
        start_time = time.time()

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
                env=self._get_safe_env(cfg),
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

    def _get_safe_env(self, config: SandboxConfig) -> dict:
        env = os.environ.copy()
        safe_vars = [
            "PATH",
            "HOME",
            "USER",
            "LANG",
            "LC_ALL",
            "PYTHONPATH",
            "PYTHONHOME",
        ]
        filtered_env = {k: env[k] for k in safe_vars if k in env}
        filtered_env["SANDBOX_ARTIFACTS_DIR"] = self._get_artifacts_dir(config)
        return filtered_env

    async def cleanup(self, sandbox_id: Optional[str] = None) -> None:
        pass
