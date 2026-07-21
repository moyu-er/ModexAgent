"""Sync the project version from ``_version.py`` into all ``package.json`` files.

The single source of truth for the version is ``src/modex_agent/_version.py``.
Python packages derive from it via ``[tool.hatch.version]``; the Windows
installer reads it via ``build.bat``. This script extends that chain to the
two npm ``package.json`` files (``webui/`` and ``packaging/electron/``) so
that ``ModexBot@<version> pack`` and the WebUI build metadata stay in lockstep.

Run manually or automatically from ``build.bat`` (Step 0).

Usage::

    python sync_versions.py          # sync + report
    python sync_versions.py --check  # exit 1 if out of sync (CI gate)
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


def _read_version() -> str:
    """Read ``__version__`` from ``src/modex_agent/_version.py``."""
    here = Path(__file__).resolve().parent
    version_file = here.parent.parent.parent / "src" / "modex_agent" / "_version.py"
    tree = ast.parse(version_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "__version__":
                if isinstance(node.value, ast.Constant):
                    return str(node.value.value)
    raise RuntimeError(f"__version__ not found in {version_file}")


def _sync_package_json(path: Path, version: str) -> bool:
    """Update the ``version`` field in a ``package.json`` file.

    Returns ``True`` if the file was modified.
    """
    if not path.exists():
        return False
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if data.get("version") == version:
        return False
    data["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def sync(check_only: bool = False) -> int:
    version = _read_version()
    here = Path(__file__).resolve().parent
    repo_root = here.parent.parent.parent

    targets = [
        repo_root / "examples" / "bot_project" / "webui" / "package.json",
        here / "electron" / "package.json",
    ]

    changed = False
    for target in targets:
        rel = target.relative_to(repo_root) if repo_root in target.parents else target
        if not target.exists():
            print(f"  [sync_versions] SKIP (not found): {rel}")
            continue
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
        current = data.get("version", "<missing>")
        if current == version:
            print(f"  [sync_versions] OK: {rel} → {version}")
            continue
        if check_only:
            print(f"  [sync_versions] OUT OF SYNC: {rel} (has {current}, expected {version})")
            changed = True
            continue
        _sync_package_json(target, version)
        print(f"  [sync_versions] UPDATED: {rel} ({current} → {version})")
        changed = True

    if check_only and changed:
        print("\n  [sync_versions] Run `python packaging/sync_versions.py` to fix.")
        return 1

    if not changed:
        print(f"\n  [sync_versions] All package.json files already at {version}.")
    else:
        print(f"\n  [sync_versions] Synced all package.json files to {version}.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync _version.py → package.json files.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if any package.json is out of sync (no writes).",
    )
    args = parser.parse_args()
    sys.exit(sync(check_only=args.check))


if __name__ == "__main__":
    main()
