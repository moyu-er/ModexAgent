import shutil
import tempfile
import time
from pathlib import Path

from ..config import SandboxConfig
from ..env_builder import EnvBuilderConfig, EnvironmentBuilder
from ..exceptions import SandboxUnavailableError
from ..guard import CommandPatternGuard, CommandPatternGuardConfig
from ..types import SandboxResult
from ..validation import validate_code
from .base import SandboxAdapter

DOCKER_AVAILABLE = False
try:
    import docker

    DOCKER_AVAILABLE = True
except ImportError:
    pass


def _check_docker_available() -> bool:
    if not DOCKER_AVAILABLE:
        return False
    try:
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


class DockerSandbox(SandboxAdapter):
    LANGUAGE_IMAGES = {
        "python": "python:3.11-slim",
        "javascript": "node:18-slim",
    }

    @property
    def name(self) -> str:
        return "docker"

    @property
    def is_available(self) -> bool:
        return _check_docker_available()

    def __init__(self, config: SandboxConfig | None = None):
        self.config = config or SandboxConfig()
        self._client = None
        self._command_guard: CommandPatternGuard | None = None

    def _get_client(self):
        if not DOCKER_AVAILABLE:
            raise SandboxUnavailableError("docker Python package not installed")
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def _get_image(self, language: str) -> str:
        return self.LANGUAGE_IMAGES.get(language, "python:3.11-slim")

    def _get_command_guard(self) -> CommandPatternGuard:
        if self._command_guard is None:
            cfg = self.config
            guard_config = cfg.command_guard if cfg and cfg.command_guard else None
            self._command_guard = CommandPatternGuard(guard_config)
        return self._command_guard

    async def execute(
        self,
        code: str,
        language: str = "python",
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        if not self.is_available:
            return SandboxResult(
                success=False,
                error="Docker is not available on this system",
            )

        if language != "python":
            return SandboxResult(
                success=False,
                error=f"DockerSandbox only supports Python, got {language}",
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

        tmpdir = None
        container = None
        try:
            tmpdir = tempfile.mkdtemp(dir=cfg.workspace_dir)
            code_file = Path(tmpdir) / "main.py"
            code_file.write_text(code, encoding="utf-8")

            artifacts_dir = self._ensure_artifacts_dir(cfg)

            client = self._get_client()
            image = self._get_image(language)

            env_builder = EnvironmentBuilder(EnvBuilderConfig(policy=cfg.env_policy))
            env = env_builder.build(overrides={"SANDBOX_ARTIFACTS_DIR": "/app/artifacts"})

            volumes = {
                tmpdir: {"bind": "/app", "mode": "rw"},
                artifacts_dir: {"bind": "/app/artifacts", "mode": "rw"},
            }

            run_kwargs = dict(
                image=image,
                command="python /app/main.py",
                volumes=volumes,
                working_dir="/app",
                network_mode="none" if not cfg.enable_network else "bridge",
                detach=True,
                stdout=True,
                stderr=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                user="1000:1000",
                pids_limit=64,
                environment=env,
            )

            if cfg.memory_limit_mb is not None:
                run_kwargs["mem_limit"] = f"{cfg.memory_limit_mb}m"

            if cfg.cpu_limit is not None:
                run_kwargs["nano_cpus"] = int(cfg.cpu_limit * 1e9)

            container = client.containers.run(**run_kwargs)

            result = container.wait(timeout=cfg.max_execution_time_seconds)

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8")

            execution_time_ms = (time.time() - start_time) * 1000

            artifacts = self._collect_artifacts(cfg)

            return SandboxResult(
                success=result["StatusCode"] == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=result["StatusCode"],
                artifacts=artifacts,
                execution_time_ms=execution_time_ms,
                error=None if result["StatusCode"] == 0 else stderr,
            )

        except Exception as e:
            return SandboxResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000,
            )
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            if tmpdir and Path(tmpdir).exists():
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
                error="Docker is not available on this system",
            )

        cfg = config or self.config
        start_time = time.time()

        guard = self._get_command_guard()
        result = guard.check(command)
        if not result.allowed:
            return SandboxResult(success=False, error=f"Command blocked: {result.reason}")

        tmpdir = None
        container = None
        try:
            tmpdir = tempfile.mkdtemp(dir=cfg.workspace_dir)
            artifacts_dir = self._ensure_artifacts_dir(cfg)

            client = self._get_client()
            image = self.LANGUAGE_IMAGES["python"]

            env_builder = EnvironmentBuilder(EnvBuilderConfig(policy=cfg.env_policy))
            env = env_builder.build(overrides={"SANDBOX_ARTIFACTS_DIR": "/app/artifacts"})

            work_dir = cwd or "/app"
            volumes = {
                tmpdir: {"bind": "/app", "mode": "rw"},
                artifacts_dir: {"bind": "/app/artifacts", "mode": "rw"},
            }

            run_kwargs = dict(
                image=image,
                command=f"sh -c '{command}'",
                volumes=volumes,
                working_dir=work_dir,
                network_mode="none" if not cfg.enable_network else "bridge",
                detach=True,
                stdout=True,
                stderr=True,
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                user="1000:1000",
                pids_limit=64,
                environment=env,
            )

            if cfg.memory_limit_mb is not None:
                run_kwargs["mem_limit"] = f"{cfg.memory_limit_mb}m"

            if cfg.cpu_limit is not None:
                run_kwargs["nano_cpus"] = int(cfg.cpu_limit * 1e9)

            container = client.containers.run(**run_kwargs)

            result = container.wait(timeout=cfg.max_execution_time_seconds)

            stdout = container.logs(stdout=True, stderr=False).decode("utf-8")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8")

            execution_time_ms = (time.time() - start_time) * 1000

            artifacts = self._collect_artifacts(cfg)

            return SandboxResult(
                success=result["StatusCode"] == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=result["StatusCode"],
                artifacts=artifacts,
                execution_time_ms=execution_time_ms,
                error=None if result["StatusCode"] == 0 else stderr,
            )

        except Exception as e:
            return SandboxResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000,
            )
        finally:
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            if tmpdir and Path(tmpdir).exists():
                try:
                    shutil.rmtree(tmpdir)
                except Exception:
                    pass

    async def cleanup(self, sandbox_id: str | None = None) -> None:
        if sandbox_id:
            try:
                container = self._get_client().containers.get(sandbox_id)
                container.remove(force=True)
            except Exception:
                pass
