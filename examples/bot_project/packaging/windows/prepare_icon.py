"""Convert the WebUI logo into a multi-size Windows .ico for the installer.

Source of truth: ``examples/bot_project/webui/public/logo.jpg``
Output:          ``examples/bot_project/packaging/logo.ico``

The same .ico feeds three consumers:
  - Inno Setup ``SetupIconFile``  -> installer exe icon
  - Inno Setup shortcut ``IconFilename`` -> desktop / start-menu icons
  - Tauri ``bundle.icon`` -> embedded ModexBot.exe icon + window title-bar icon

Usage::

    python prepare_icon.py
    python prepare_icon.py --source <jpg> --output <ico>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("  [prepare_icon] ERROR: Pillow is not installed", file=sys.stderr)
    print("    Run:  uv pip install Pillow", file=sys.stderr)
    sys.exit(1)

_SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_SOURCE = _SCRIPT_DIR.parents[1] / "webui" / "public" / "logo.jpg"
_DEFAULT_OUTPUT = _SCRIPT_DIR / "logo.ico"

_ICON_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def convert(source: Path, output: Path) -> None:
    if not source.exists():
        print(f"  [prepare_icon] ERROR: source not found: {source}", file=sys.stderr)
        sys.exit(1)

    print(f"  [prepare_icon] Source: {source}")
    print(f"  [prepare_icon] Output: {output}")

    img: Image.Image = Image.open(source)
    print(f"  [prepare_icon] Source size: {img.size}, mode: {img.mode}")

    if img.mode != "RGBA":
        img = img.convert("RGBA")

    max_size = _ICON_SIZES[-1]
    if img.size[0] < max_size[0] or img.size[1] < max_size[1]:
        img = img.resize(max_size, Image.Resampling.LANCZOS)
        print(f"  [prepare_icon] Upscaled to {img.size} (LANCZOS)")

    output.parent.mkdir(parents=True, exist_ok=True)
    img.save(output, format="ICO", sizes=_ICON_SIZES)

    size_kb = output.stat().st_size / 1024
    print(f"  [prepare_icon] Done. ICO: {size_kb:.1f} KB, sizes: {_ICON_SIZES}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate logo.ico from logo.jpg.")
    parser.add_argument("--source", type=Path, default=_DEFAULT_SOURCE,
                        help="Source image (default: webui/public/logo.jpg)")
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT,
                        help="Output .ico path (default: packaging/logo.ico)")
    args = parser.parse_args()
    convert(args.source, args.output)


if __name__ == "__main__":
    main()
