"""debug entry point — run this file directly.

Runs the bot in the current process (no detached subprocess).
Writes the PID file so ``modexbot stop`` / ``modexbot status`` can
discover the process through the standard PID-file layer.

Defaults: config from ``config/``, port from ``bot_config.yml``, .env auto-loaded.

If ``MODEXBOT_BIN_DIR`` is not set, auto-discovers the venv ``Scripts/``
directory that contains ``modexctl`` — works in PyCharm and bare ``python
debug_main.py`` without requiring the venv to be active.
"""

if __name__ == "__main__":
    import os
    import shutil
    import sys
    from functools import partial
    from pathlib import Path

    _here = Path(__file__).resolve().parent
    _config = _here / "config"

    if not os.environ.get("MODEXBOT_BIN_DIR"):
        for candidate in (
            Path(sys.executable).parent / "Scripts",
            Path(sys.executable).parent / "bin",
            _here / ".venv" / "Scripts",
            _here / ".venv" / "bin",
        ):
            for name in ("modexctl", "modexctl.exe", "modexctl.bat"):
                if (candidate / name).is_file():
                    os.environ["MODEXBOT_BIN_DIR"] = str(candidate)
                    break
            if os.environ.get("MODEXBOT_BIN_DIR"):
                break
        if not os.environ.get("MODEXBOT_BIN_DIR"):
            which = shutil.which("modexctl")
            if which:
                os.environ["MODEXBOT_BIN_DIR"] = str(Path(which).parent)

    from modexbot.main import create_webui_service, run_with_supervisor

    from bot.config.webui_config import load_webui_port

    _port = load_webui_port(_config)

    _pid_dir = _here / ".modex"
    _pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file = _pid_dir / "bot.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    try:
        run_with_supervisor(partial(create_webui_service, _config, port=_port))
    finally:
        pid_file.unlink(missing_ok=True)
