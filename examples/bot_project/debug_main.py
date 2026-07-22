"""debug entry point — run this file directly.

Runs the bot in the current process (no detached subprocess).
Writes the PID file so ``modexbot stop`` / ``modexbot status`` can
discover the process through the standard PID-file layer.

Defaults: config from ``config/``, port from ``bot_config.yml``, .env auto-loaded.
"""

if __name__ == "__main__":
    import os
    from functools import partial
    from pathlib import Path

    from modexbot.main import create_webui_service, run_with_supervisor

    _here = Path(__file__).resolve().parent
    _config = _here / "config"

    from bot.config.webui_config import load_webui_port

    _port = load_webui_port(_config)

    # Write PID file so the CLI can discover this process.
    _pid_dir = _here / ".modex"
    _pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file = _pid_dir / "bot.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    try:
        run_with_supervisor(partial(create_webui_service, _config, port=_port))
    finally:
        pid_file.unlink(missing_ok=True)
