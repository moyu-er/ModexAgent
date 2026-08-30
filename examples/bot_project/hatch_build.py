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
        if version == "editable":
            # force_include in the PEP 660 wheel embeds a REAL copy of bot/
            # into site-packages that shadows the checkout: `import bot`
            # resolves to the snapshot copy, so path-anchored plugin
            # discovery (factory.py: <bot pkg parent>/plugins) finds
            # <site-packages>/plugins — pools boot with an empty strategy
            # registry. Editable runs off the source tree; only standard
            # wheels need the data-file copy below.
            return

        root = Path(self.root)

        # Include bot/ Python package and all data files (SQL migrations,
        # tiktoken blobs, etc.) in the wheel. force_include defaults to .py
        # only, silently dropping data files the runtime needs at runtime.
        bot_dir = root / "bot"
        if bot_dir.is_dir():
            build_data.setdefault("force_include", {})
            for path in bot_dir.rglob("*"):
                if path.is_file() and path.suffix != ".pyc":
                    rel = str(path.relative_to(root))
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
