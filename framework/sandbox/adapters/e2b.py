import asyncio
import fnmatch
import logging
import mimetypes
import os
import time
from pathlib import Path

from ..config import SandboxConfig
from ..exceptions import SandboxUnavailableError
from ..types import SandboxArtifact, SandboxResult
from ..validation import validate_code
from .base import SandboxAdapter

logger = logging.getLogger(__name__)

E2B_AVAILABLE = False
try:
    from e2b_code_interpreter import Sandbox

    E2B_AVAILABLE = True
except ImportError:
    pass


def _check_e2b_available(api_key: str | None = None) -> bool:
    if not E2B_AVAILABLE:
        return False
    key = api_key or os.environ.get("E2B_API_KEY")
    return key is not None


class E2BSandbox(SandboxAdapter):
    """E2B Cloud Sandbox adapter with lazy artifact loading.

    This adapter executes code in E2B's cloud environment and supports
    lazy loading of artifacts. Artifacts are not automatically downloaded
    after execution; instead, they are fetched on-demand when get_artifacts()
    is called, as long as the sandbox is still alive.

    The sandbox has a default timeout of 5 minutes. After timeout, the
    sandbox is destroyed and artifacts can no longer be fetched.

    Usage:
        sandbox = get_sandbox(SandboxType.E2B)
        result = await sandbox.execute(code)

        # Lazy loading - fetch from remote sandbox
        artifacts = sandbox.get_artifacts(["*.txt"])

        # Or explicitly download to local directory
        paths = sandbox.download_artifacts(["*"], "/local/path")

        # Clean up when done
        await sandbox.cleanup()

    Note on Artifact Download Behavior:
    -----------------------------------
    Unlike local sandbox adapters (e.g., DockerSandbox, LocalSandbox), the E2B
    adapter has a fundamentally different artifact handling model due to its
    cloud-based nature:

    1. LAZY LOADING (Default):
       - execute() only returns artifact METADATA (path, size, mime_type)
       - The actual content remains in the remote cloud sandbox
       - Content is fetched on-demand via get_artifacts() while sandbox is alive
       - This avoids unnecessary data transfer and memory usage

    2. EXPLICIT DOWNLOAD:
       - download_artifacts() fetches content from remote and saves to local disk
       - Requires sandbox to be alive (not timed out)
       - Raises SandboxUnavailableError if sandbox has been cleaned up or timed out

    3. AUTO-DOWNLOAD (Optional):
       - Can be enabled via SandboxConfig(auto_download_artifacts=True)
       - Automatically downloads artifacts after execute() completes
       - Supports pattern filtering via auto_download_patterns

    4. SANDBOX LIFECYCLE:
       - Sandbox remains alive after execute() for lazy loading
       - Default timeout: 5 minutes (DEFAULT_SANDBOX_TIMEOUT_SECONDS)
       - Must call cleanup() to release resources
       - After cleanup or timeout, artifacts can no longer be fetched

    This design differs from local adapters where:
    - Local adapters: Artifacts are always available on local filesystem
    - E2B adapter: Artifacts are transient and tied to sandbox lifecycle
    """

    ARTIFACTS_DIR = "/home/user/artifacts"
    DEFAULT_SANDBOX_TIMEOUT_SECONDS = 300  # 5 minutes

    @property
    def name(self) -> str:
        return "e2b"

    @property
    def is_available(self) -> bool:
        return _check_e2b_available(self.api_key)

    def __init__(self, config: SandboxConfig | None = None, api_key: str | None = None) -> None:
        self.config = config or SandboxConfig()
        self.api_key = api_key or os.environ.get("E2B_API_KEY")

        # Sandbox lifecycle management
        self._active_sandbox: Sandbox | None = None
        self._sandbox_created_at: float | None = None
        self._artifacts_metadata: list[SandboxArtifact] = []

    def _set_api_key_env(self):
        old_key = os.environ.get("E2B_API_KEY")
        os.environ["E2B_API_KEY"] = self.api_key
        return old_key

    def _restore_api_key_env(self, old_key):
        if old_key is not None:
            os.environ["E2B_API_KEY"] = old_key
        else:
            os.environ.pop("E2B_API_KEY", None)

    def _is_sandbox_alive(self) -> bool:
        """Check if the sandbox is still alive and not timed out."""
        if self._active_sandbox is None:
            return False
        if self._sandbox_created_at is None:
            return False

        elapsed = time.time() - self._sandbox_created_at
        return elapsed < self.DEFAULT_SANDBOX_TIMEOUT_SECONDS

    def _collect_artifacts_from_e2b(
        self,
        sandbox,
        patterns: list[str] | None = None,
    ) -> list[SandboxArtifact]:
        """Collect artifact metadata from E2B remote sandbox."""
        if patterns is None:
            patterns = ["*"]

        artifacts = []

        def list_directory(dir_path: str, prefix: str = ""):
            """Recursively list files in directory."""
            try:
                files_result = sandbox.files.list(dir_path)

                for file_info in files_result:
                    name = file_info.name
                    full_rel_path = f"{prefix}{name}" if prefix else name

                    # Check if it's a directory using type attribute
                    file_type = getattr(file_info, "type", None)
                    is_dir = False
                    if file_type is not None:
                        if hasattr(file_type, "name"):
                            is_dir = file_type.name == "DIR"
                        else:
                            is_dir = str(file_type) == "FileType.DIR"

                    if is_dir:
                        # Recurse into subdirectory
                        subdir_path = os.path.join(dir_path, name)
                        subdir_prefix = full_rel_path + "/"
                        list_directory(subdir_path, subdir_prefix)
                    else:
                        # It's a file
                        if any(fnmatch.fnmatch(full_rel_path, pattern) for pattern in patterns):
                            try:
                                size = getattr(file_info, "size", 0)
                                mime_type, _ = mimetypes.guess_type(full_rel_path)
                                if mime_type is None:
                                    mime_type = "application/octet-stream"

                                artifacts.append(
                                    SandboxArtifact(
                                        path=full_rel_path,
                                        size=size,
                                        mime_type=mime_type,
                                    )
                                )
                            except Exception:
                                continue
            except Exception:
                pass

        list_directory(self.ARTIFACTS_DIR)
        return artifacts

    def _get_artifact_content_from_e2b(
        self,
        sandbox,
        artifact_path: str,
        max_size: int = 10485760,
    ) -> bytes | None:
        """Read a single artifact's content from E2B remote sandbox.

        Args:
            sandbox: The E2B sandbox instance
            artifact_path: Relative path to the artifact
            max_size: Maximum file size to read

        Returns:
            File content as bytes, or None if failed
        """
        try:
            full_path = os.path.join(self.ARTIFACTS_DIR, artifact_path)

            # Check size first
            file_info = sandbox.files.list(os.path.dirname(full_path))
            for info in file_info:
                if info.name == os.path.basename(artifact_path):
                    size = getattr(info, "size", 0)
                    if size > max_size:
                        logger.warning(
                            f"Artifact {artifact_path} exceeds max size ({size} > {max_size})"
                        )
                        return None
                    break

            # Read file content
            content = sandbox.files.read(full_path)

            # E2B returns string, convert to bytes
            if isinstance(content, str):
                content = content.encode("utf-8")

            return content
        except Exception as e:
            logger.warning(f"Failed to read artifact {artifact_path}: {e}")
            return None

    def _auto_download_artifacts(
        self,
        sandbox,
        config: SandboxConfig,
    ) -> None:
        """Automatically download artifacts if auto_download_artifacts is enabled."""
        if not config.auto_download_artifacts:
            return

        patterns = config.auto_download_patterns or ["*"]
        artifacts_dir = self._get_artifacts_dir(config)

        try:
            os.makedirs(artifacts_dir, exist_ok=True)

            for artifact in self._artifacts_metadata:
                if any(fnmatch.fnmatch(artifact.path, pattern) for pattern in patterns):
                    content = self._get_artifact_content_from_e2b(
                        sandbox, artifact.path, config.artifact_max_size
                    )
                    if content is not None:
                        filepath = os.path.join(artifacts_dir, artifact.path)
                        parent_dir = os.path.dirname(filepath)
                        if parent_dir:
                            os.makedirs(parent_dir, exist_ok=True)
                        with open(filepath, "wb") as f:
                            f.write(content)

        except Exception as e:
            logger.warning(f"Auto-download artifacts failed: {e}")

    async def execute(
        self,
        code: str,
        language: str = "python",
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        """Execute code in E2B cloud sandbox.

        IMPORTANT: This method behaves differently from local sandbox adapters
        regarding artifact handling.

        For E2B (Cloud Sandbox):
        - Creates a new cloud sandbox instance
        - Returns artifact METADATA only (not content) in result.artifacts
        - The actual content remains in the remote sandbox
        - Sandbox stays alive for lazy loading (until timeout or cleanup)
        - Must call get_artifacts() or download_artifacts() to fetch content
        - Must call cleanup() to release resources

        For Local Adapters (DockerSandbox, LocalSandbox):
        - Artifacts are automatically available on local filesystem
        - result.artifacts contains metadata with local paths
        - No additional steps needed to access artifact content
        - No explicit cleanup required for artifact access

        After execution, the sandbox remains alive for lazy loading of artifacts.
        Call cleanup() when done to release resources.
        """
        if not self.is_available:
            return SandboxResult(
                success=False,
                error="E2B is not configured. Set E2B_API_KEY environment variable.",
            )

        if language != "python":
            return SandboxResult(
                success=False,
                error=f"E2BSandbox only supports Python, got {language}",
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

        # Clean up any existing sandbox first
        await self.cleanup()

        old_key = self._set_api_key_env()
        try:
            # Create new sandbox
            self._active_sandbox = await asyncio.to_thread(Sandbox.create)
            self._sandbox_created_at = time.time()
            sandbox = self._active_sandbox

            # Create artifacts directory
            try:
                await asyncio.to_thread(sandbox.files.make_dir, self.ARTIFACTS_DIR)
            except Exception:
                pass  # Directory may already exist

            result = await asyncio.to_thread(sandbox.run_code, code, language="python")

            execution_time_ms = (time.time() - start_time) * 1000

            stdout_parts = []
            stderr_parts = []

            if result.logs.stdout:
                stdout_parts.extend(result.logs.stdout)
            if result.logs.stderr:
                stderr_parts.extend(result.logs.stderr)

            stdout = "\n".join(stdout_parts)
            stderr = "\n".join(stderr_parts)
            error = str(result.error) if result.error else None

            # Collect artifact metadata only (not content)
            self._artifacts_metadata = self._collect_artifacts_from_e2b(sandbox, patterns=["*"])

            # Auto-download if enabled
            self._auto_download_artifacts(sandbox, cfg)

            return SandboxResult(
                success=result.error is None,
                stdout=stdout,
                stderr=stderr,
                exit_code=0 if result.error is None else 1,
                artifacts=self._artifacts_metadata,
                execution_time_ms=execution_time_ms,
                error=error,
            )

        except Exception as e:
            await self.cleanup()
            return SandboxResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000,
            )
        finally:
            self._restore_api_key_env(old_key)

    async def execute_command(
        self,
        command: str,
        cwd: str | None = None,
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        """Execute a command in E2B cloud sandbox."""
        if not self.is_available:
            return SandboxResult(
                success=False,
                error="E2B is not configured. Set E2B_API_KEY environment variable.",
            )

        cfg = config or self.config
        start_time = time.time()

        # Clean up any existing sandbox first
        await self.cleanup()

        old_key = self._set_api_key_env()
        try:
            # Create new sandbox
            self._active_sandbox = await asyncio.to_thread(Sandbox.create)
            self._sandbox_created_at = time.time()
            sandbox = self._active_sandbox

            # Create artifacts directory
            try:
                await asyncio.to_thread(sandbox.files.make_dir, self.ARTIFACTS_DIR)
            except Exception:
                pass  # Directory may already exist

            result = await asyncio.to_thread(sandbox.commands.run, command)

            execution_time_ms = (time.time() - start_time) * 1000

            stdout = result.stdout if result.stdout else ""
            stderr = result.stderr if result.stderr else ""

            # Collect artifact metadata only (not content)
            self._artifacts_metadata = self._collect_artifacts_from_e2b(sandbox, patterns=["*"])

            # Auto-download if enabled
            self._auto_download_artifacts(sandbox, cfg)

            return SandboxResult(
                success=result.exit_code == 0,
                stdout=stdout,
                stderr=stderr,
                exit_code=result.exit_code,
                artifacts=self._artifacts_metadata,
                execution_time_ms=execution_time_ms,
                error=None if result.exit_code == 0 else stderr,
            )

        except Exception as e:
            await self.cleanup()
            return SandboxResult(
                success=False,
                error=str(e),
                execution_time_ms=(time.time() - start_time) * 1000,
            )
        finally:
            self._restore_api_key_env(old_key)

    async def cleanup(self, sandbox_id: str | None = None) -> None:
        """Clean up the E2B sandbox and release resources."""
        if self._active_sandbox is not None:
            try:
                await asyncio.to_thread(self._active_sandbox.kill)
            except Exception:
                pass
            finally:
                self._active_sandbox = None
                self._sandbox_created_at = None
                self._artifacts_metadata = []

    def get_artifacts(
        self,
        patterns: list[str],
        config: SandboxConfig | None = None,
    ) -> dict[str, bytes]:
        """Get artifacts from E2B sandbox with lazy loading.

        IMPORTANT: This method behaves differently from local sandbox adapters.

        For E2B (Cloud Sandbox):
        - Fetches content from the REMOTE cloud sandbox on-demand
        - Requires the sandbox to be alive (not timed out or cleaned up)
        - Returns empty dict if sandbox is unavailable
        - Each call fetches fresh data from the cloud

        For Local Adapters (DockerSandbox, LocalSandbox):
        - Reads content from LOCAL filesystem
        - Artifacts are always available (persisted on disk)
        - No lifecycle constraints

        If the sandbox is still alive, fetches content from the remote sandbox.
        If the sandbox has timed out or been cleaned up, returns an empty dict.

        Args:
            patterns: List of glob patterns to match artifact names
            config: Optional sandbox configuration

        Returns:
            Dict mapping artifact paths to their content as bytes
        """
        cfg = config or self.config
        max_size = cfg.artifact_max_size
        result = {}

        # Check if sandbox is alive for lazy loading
        if not self._is_sandbox_alive():
            logger.warning("Sandbox is not alive or has timed out. Cannot lazy load artifacts.")
            return result

        # Lazy load from remote sandbox
        sandbox = self._active_sandbox
        for artifact in self._artifacts_metadata:
            if any(fnmatch.fnmatch(artifact.path, pattern) for pattern in patterns):
                if artifact.size <= max_size:
                    content = self._get_artifact_content_from_e2b(sandbox, artifact.path, max_size)
                    if content is not None:
                        result[artifact.path] = content

        return result

    def download_artifacts(
        self,
        patterns: list[str],
        local_dir: str,
        config: SandboxConfig | None = None,
    ) -> dict[str, str]:
        """Download artifacts from E2B sandbox to a local directory.

        IMPORTANT: This method behaves differently from local sandbox adapters.

        For E2B (Cloud Sandbox):
        - Fetches content from the REMOTE cloud sandbox and saves to local disk
        - Requires the sandbox to be alive (not timed out or cleaned up)
        - Raises SandboxUnavailableError if sandbox is unavailable
        - This is the ONLY way to persist E2B artifacts locally

        For Local Adapters (DockerSandbox, LocalSandbox):
        - Simply copies files from one local path to another
        - Source files are always available on local filesystem
        - No lifecycle constraints

        This method fetches artifacts from the remote sandbox (if alive) and
        saves them to the specified local directory.

        Args:
            patterns: List of glob patterns to match artifact names
            local_dir: Local directory path to save artifacts
            config: Optional sandbox configuration

        Returns:
            Dict mapping artifact paths to local file paths

        Raises:
            SandboxUnavailableError: If the sandbox has timed out or been cleaned up
        """
        if not self._is_sandbox_alive():
            raise SandboxUnavailableError(
                "Sandbox is not alive or has timed out. Cannot download artifacts."
            )

        cfg = config or self.config
        max_size = cfg.artifact_max_size
        result = {}

        os.makedirs(local_dir, exist_ok=True)
        sandbox = self._active_sandbox

        for artifact in self._artifacts_metadata:
            if any(fnmatch.fnmatch(artifact.path, pattern) for pattern in patterns):
                if artifact.size <= max_size:
                    content = self._get_artifact_content_from_e2b(sandbox, artifact.path, max_size)
                    if content is not None:
                        # Handle subdirectories in artifact path
                        local_path = Path(local_dir) / artifact.path
                        local_path.parent.mkdir(parents=True, exist_ok=True)

                        with open(local_path, "wb") as f:
                            f.write(content)

                        result[artifact.path] = str(local_path)

        return result
