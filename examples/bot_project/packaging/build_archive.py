"""Export the git-tracked source tree + pre-built frontend into staging/app/.

Only files tracked by git are included — .gitignore defines the exact
boundary between source (shipped) and runtime data (excluded).  The
frontend dist/ (gitignored) is built separately and copied in after
extraction.

Usage::

    python build_archive.py --staging-dir staging
    # → staging/app/   (full repo source, ready for installer)
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# --- Helpers ----------------------------------------------------------------


def _find_repo_root() -> Path:
    """Return the git repository root containing this script."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def _build_frontend(repo_root: Path, force: bool = False) -> None:
    """Run ``npm install && npm run build`` in webui/ if dist/ is stale."""
    webui_dir = repo_root / "examples" / "bot_project" / "webui"
    dist_dir = repo_root / "examples" / "bot_project" / "bot" / "web" / "dist"

    # Skip if dist exists and is newer than all source files
    if dist_dir.exists() and not force:
        index_html = dist_dir / "index.html"
        if index_html.exists():
            src_mtime = max(
                f.stat().st_mtime for f in webui_dir.rglob("*.tsx")
            ) if any(webui_dir.rglob("*.tsx")) else 0
            if index_html.stat().st_mtime > src_mtime:
                print("  [build_archive] Frontend dist is up-to-date, skipping build")
                return

    if not shutil.which("node"):
        print("  [build_archive] WARNING: Node.js not found — skipping frontend build")
        print("    The installer will not include a pre-built WebUI.")
        print("    Users can build it later with: modexbot install -f")
        return

    print("  [build_archive] Building frontend (npm install + npm run build)...")
    npm = shutil.which("npm") or "npm"

    subprocess.run([npm, "install"], cwd=str(webui_dir), check=True)
    subprocess.run([npm, "run", "build"], cwd=str(webui_dir), check=True)
    print(f"  [build_archive] Frontend built → {dist_dir}")


# Paths under the repo root that are excluded from the installer source archive.
# They are not needed at runtime and only bloat the install footprint:
#   - tests/         : unit/integration/architecture tests (build-machine only)
#   - assets/        : README screenshots + demo GIFs (docs, not runtime)
#   - docs/          : ADRs, design docs (reference material)
#   - .github/       : CI workflow definitions
#   - rules/         : lint/type-safety rule docs
#   - scripts/       : repo maintenance scripts
# Kept explicit (not globbed) so each exclusion is auditable.
_ARCHIVE_EXCLUDES = {
    "tests",
    "assets",
    "docs",
    ".github",
    "rules",
    "scripts",
}


def _prune_excluded(app_dir: Path) -> None:
    """Delete excluded top-level directories from the extracted archive."""
    print("  [build_archive] Pruning non-runtime directories from archive...")
    removed_bytes = 0
    for name in _ARCHIVE_EXCLUDES:
        p = app_dir / name
        if not p.exists():
            continue
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        removed_bytes += size
        shutil.rmtree(p)
        print(f"    Removed: {name}/ ({size / 1e6:.1f} MB)")
    print(f"    Pruned: {removed_bytes / 1e6:.1f} MB total")


# --- Public API -------------------------------------------------------------


def build_archive(staging_dir: Path, force_frontend: bool = False) -> Path:
    """Export git-tracked source + pre-built frontend into staging/app/.

    Returns the path to ``staging/app/``.
    """
    repo_root = _find_repo_root()
    app_dir = staging_dir / "app"

    # 1. Build frontend (if needed) — dist/ is gitignored so git archive
    #    won't include it; we copy it in after extraction.
    _build_frontend(repo_root, force=force_frontend)

    # 2. git archive — exports only tracked files (respects .gitignore)
    print("  [build_archive] Exporting git-tracked source...")
    if app_dir.exists():
        shutil.rmtree(app_dir)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        subprocess.run(
            ["git", "archive", "HEAD", "--format=zip", "-o", str(tmp_path)],
            cwd=str(repo_root),
            check=True,
        )
        with zipfile.ZipFile(tmp_path) as zf:
            zf.extractall(app_dir)
    finally:
        tmp_path.unlink(missing_ok=True)

    print(f"  [build_archive] Source exported → {app_dir}")

    # 3. Prune non-runtime directories (tests/assets/docs/.github/...) that
    #    git archive included but the installer doesn't need.
    _prune_excluded(app_dir)

    # 4. Copy pre-built frontend dist/ (gitignored, not in archive)
    src_dist = repo_root / "examples" / "bot_project" / "bot" / "web" / "dist"
    dst_dist = app_dir / "examples" / "bot_project" / "bot" / "web" / "dist"
    if src_dist.exists():
        if dst_dist.exists():
            shutil.rmtree(dst_dist)
        shutil.copytree(src_dist, dst_dist)
        print(f"  [build_archive] Frontend dist copied → {dst_dist}")
    else:
        print("  [build_archive] WARNING: no pre-built frontend dist found")

    return app_dir


# --- CLI --------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export git source + frontend into installer staging.",
    )
    parser.add_argument(
        "--staging-dir",
        type=Path,
        default=Path("staging"),
        help="Staging directory root (default: staging)",
    )
    parser.add_argument(
        "--force-frontend",
        action="store_true",
        help="Force rebuild frontend even if dist/ is up-to-date",
    )
    args = parser.parse_args()
    build_archive(args.staging_dir, force_frontend=args.force_frontend)


if __name__ == "__main__":
    main()
