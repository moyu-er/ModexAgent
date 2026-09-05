"""debug entry point — run this file directly.

Runs the bot in the current process (no detached subprocess).
Writes the PID file so ``modexbot stop`` / ``modexbot status`` can
discover the process through the standard PID-file layer.

Defaults: config from ``config/``, port from ``bot_config.yml``, .env auto-loaded.

Select the repository-root ``.venv`` interpreter in the IDE. This entry
uses the same in-process startup and environment check as the CLI; it
does not borrow command executables from another Python environment.
"""

if __name__ == "__main__":
    from modexbot.cli import _DEFAULT_PORT, _PKG_ROOT, _run_bot

    _run_bot(str(_PKG_ROOT / "config"), _DEFAULT_PORT, False)
