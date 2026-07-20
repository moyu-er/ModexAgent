"""Entry point for ``python -m modexctl``.

Mirrors :file:`examples/bot_project/modexbot/__main__.py` so both CLI
packages support ``python -m <pkg>`` invocation uniformly. The wheel
console-script entry (``modexctl = "modexctl.main:main"`` in
:file:`pyproject.toml`) is unaffected.
"""

from __future__ import annotations

from modexctl.main import main

if __name__ == "__main__":
    main()
