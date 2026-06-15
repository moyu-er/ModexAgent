"""Custom Hatch build hook — includes bot/ and web/dist/ in the wheel."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Include ``bot/`` package and ``bot/web/dist/`` static assets."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)

        # Include bot/ Python package in the wheel
        bot_dir = root / "bot"
        if bot_dir.is_dir():
            build_data.setdefault("force_include", {})
            for py_file in bot_dir.rglob("*.py"):
                rel = str(py_file.relative_to(root))
                build_data["force_include"][rel] = rel

        # Include built frontend assets
        dist_dir = root / "bot" / "web" / "dist"
        if dist_dir.is_dir():
            build_data.setdefault("force_include", {})
            for asset in dist_dir.rglob("*"):
                if asset.is_file():
                    rel = str(asset.relative_to(root))
                    build_data["force_include"][rel] = rel

    def finalize(self, version: str, build_data: dict[str, Any], artifact_path: str) -> None:
        pass
