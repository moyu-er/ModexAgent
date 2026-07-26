# 09 — Import isolation for bot/control

**What to build:** Remove server-side re-exports from
`bot/control/__init__.py` so the CLI's import graph does not pull in the
full `modex_agent` framework. The initial implementation re-exported
`BotControlFacade` and history helpers from the package `__init__.py`,
which dragged in `modex_agent` on every CLI invocation.

The CLI should import only what it needs: `ModexCtlContext`, HTTP
client code, and presentation helpers. Server-side imports
(`BotControlFacade`, `send.py`, `history.py`, route adapters) are
confined to the bot process.

A regression test verifies that importing the CLI entry point
(`bot.cli.modexctl`) does not load `modex_agent`.

**Blocked by:** 08 (console script cutover — the CLI must be the
installed command before import isolation matters).

**Status:** done (commit e414b304)

- [x] `bot/control/__init__.py` does not re-export `BotControlFacade`,
      `history` module, or other server-side components.
- [x] CLI entry point (`bot.cli.modexctl`) imports without loading
      `modex_agent`.
- [x] Regression test verifies import isolation.
- [x] Bot process still imports `BotControlFacade` from
      `bot.control.facade` directly (not via `__init__.py`).
- [x] All existing bot-side tests pass.
