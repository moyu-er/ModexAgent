"""``modexctl`` CLI package — cross-pool messaging for external coding agents.

Re-exports :func:`main` so the console script ``modexctl`` (registered as
``bot.cli.modexctl:main`` in :file:`pyproject.toml`) resolves without callers
needing to reach into the :mod:`bot.cli.modexctl.main` submodule.
"""

from bot.cli.modexctl.main import main

__all__ = ["main"]
