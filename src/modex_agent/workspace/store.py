"""Global-tier persistence — registry of known workspaces (under _registry/).

The set of known non-home workspaces lives in the GLOBAL tier (not under any
workspace's data root): ``<home>/<data_dir_name>/_registry/workspaces.json``.
``_registry`` is a reserved name (``framework.workspace.paths.RESERVED_GLOBAL_DIR``);
per-workspace accessors never produce it.
"""
from __future__ import annotations

import json
from pathlib import Path

from modex_agent.workspace.paths import RESERVED_GLOBAL_DIR
from modex_agent.workspace.registry import RegistryStore


class GlobalWorkspaceStore(RegistryStore):
    """File-backed RegistryStore: known non-home workspace targets on disk."""

    def __init__(self, *, home: Path, data_dir_name: str) -> None:
        self._dir: Path = Path(home).resolve() / data_dir_name / RESERVED_GLOBAL_DIR
        self._file: Path = self._dir / "workspaces.json"

    def load_known_targets(self) -> list[Path]:
        if not self._file.exists():
            return []
        data = json.loads(self._file.read_text(encoding="utf-8"))
        return [Path(target) for target in data.get("targets", [])]

    def save_known_targets(self, targets: list[Path]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(
                {"targets": [str(Path(target).resolve()) for target in targets]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
