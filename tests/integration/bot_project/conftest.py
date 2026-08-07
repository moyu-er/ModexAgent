"""Local conftest: ensure the source tree's bot package is used for tests."""

from __future__ import annotations

import sys
from pathlib import Path

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

# If a stale installed `bot` package is already loaded, evict it so the source tree wins.
for _mod_name in list(sys.modules):
    if _mod_name == "bot" or _mod_name.startswith("bot."):
        del sys.modules[_mod_name]

pytest_plugins = ["tests.integration.bot_project._external_fixtures"]
