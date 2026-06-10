from __future__ import annotations

import fnmatch
import mimetypes
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..guard import CommandPatternGuard
    from ..env_builder import EnvironmentBuilder
    from ..workspace_policy import WorkspacePolicy

from ..config import SandboxConfig
from ..types import SandboxArtifact, SandboxResult


class SandboxAdapter(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def is_available(self) -> bool:
        pass

    @abstractmethod
    async def execute(
        self,
        code: str,
        language: str = "python",
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        pass

    @abstractmethod
    async def execute_command(
        self,
        command: str,
        cwd: str | None = None,
        config: SandboxConfig | None = None,
    ) -> SandboxResult:
        pass

    @abstractmethod
    async def cleanup(self, sandbox_id: str | None = None) -> None:
        pass

    def _get_command_guard(self) -> CommandPatternGuard | None:
        """Optional hook: return a CommandPatternGuard for pre-execution checks.

        Default returns None. Local adapters override to provide a guard.
        Cloud/container adapters return None (isolation is their job).
        """
        return None

    def _get_env_builder(self) -> EnvironmentBuilder | None:
        """Optional hook: return an EnvironmentBuilder for env sanitization.

        Default returns None. Local adapters override.
        """
        return None

    def _get_workspace_policy(self) -> WorkspacePolicy | None:
        """Optional hook: return a WorkspacePolicy for path boundary enforcement.

        Default returns None. Local adapters override.
        """
        return None

    def _get_artifacts_dir(self, config: SandboxConfig | None) -> str:
        if config is None:
            config = SandboxConfig()
        return os.path.join(config.workspace_dir, "artifacts")

    def _ensure_artifacts_dir(self, config: SandboxConfig | None) -> str:
        artifacts_dir = self._get_artifacts_dir(config)
        os.makedirs(artifacts_dir, exist_ok=True)
        return artifacts_dir

    def _get_mime_type(self, filename: str) -> str:
        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type is None:
            return "application/octet-stream"
        return mime_type

    def _collect_artifacts(
        self,
        config: SandboxConfig | None,
        patterns: list[str] | None = None,
    ) -> list[SandboxArtifact]:
        if patterns is None:
            patterns = ["*"]

        artifacts_dir = self._get_artifacts_dir(config)
        if not os.path.exists(artifacts_dir):
            return []

        artifacts = []
        for root, _, files in os.walk(artifacts_dir):
            for filename in files:
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, artifacts_dir)

                if any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns):
                    try:
                        size = os.path.getsize(filepath)
                        mime_type, _ = mimetypes.guess_type(filepath)
                        if mime_type is None:
                            mime_type = "application/octet-stream"
                        artifacts.append(SandboxArtifact(
                            path=rel_path,
                            size=size,
                            mime_type=mime_type,
                        ))
                    except OSError:
                        continue

        return artifacts

    def get_artifacts(
        self,
        patterns: list[str],
        config: SandboxConfig | None = None,
    ) -> dict[str, bytes]:
        artifacts_dir = self._get_artifacts_dir(config)
        if not os.path.exists(artifacts_dir):
            return {}

        result = {}
        max_size = config.artifact_max_size if config else 10485760

        for root, _, files in os.walk(artifacts_dir):
            for filename in files:
                filepath = os.path.join(root, filename)
                rel_path = os.path.relpath(filepath, artifacts_dir)

                if any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns):
                    try:
                        size = os.path.getsize(filepath)
                        if size > max_size:
                            continue
                        with open(filepath, "rb") as f:
                            result[rel_path] = f.read()
                    except OSError:
                        continue

        return result
