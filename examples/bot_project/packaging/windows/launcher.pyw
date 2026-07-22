"""ModexBot launcher — starts the bot and opens the WebUI in the browser.

Run with the bundled pythonw.exe (no console window).  Double-click the
desktop icon → bot starts → browser opens.

Uses the bundled Python at <InstallDir>/python/pythonw.exe — no venv, no CLI.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

_INSTALL_DIR: Path = Path(__file__).resolve().parent
_BOT_PROJECT: Path = _INSTALL_DIR / "app" / "examples" / "bot_project"
_BUNDLED_PYTHON: Path = _INSTALL_DIR / "python" / "python.exe"
_LOG_FILE: Path = _BOT_PROJECT / "logs" / "launcher.log"


def _resolve_webui_port() -> int:
    from bot.config.webui_config import load_webui_port

    return load_webui_port(_BOT_PROJECT / "config")


_WEBUI_PORT: int = _resolve_webui_port()
_WEBUI_URL: str = f"http://localhost:{_WEBUI_PORT}/webui/"
_POLL_INTERVAL: float = 1.0
_MAX_WAIT: int = 90

_CREATE_NO_WINDOW: int = 0x08000000


def _is_server_up() -> bool:
    try:
        urllib.request.urlopen(_WEBUI_URL, timeout=2)
        return True
    except Exception:
        return False


def _start_bot() -> None:
    exe = str(_BUNDLED_PYTHON)
    args = ["-m", "modexbot", "start"]

    _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log_stream = _LOG_FILE.open("a", encoding="utf-8", errors="replace")

    kwargs: dict[str, object] = {
        "cwd": str(_BOT_PROJECT),
        "stdout": log_stream,
        "stderr": subprocess.STDOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW

    subprocess.Popen([exe, *args], **kwargs)  # type: ignore[arg-type]
    log_stream.close()


def main() -> None:
    if _is_server_up():
        webbrowser.open(_WEBUI_URL)
        return

    _start_bot()

    for _ in range(_MAX_WAIT):
        if _is_server_up():
            break
        time.sleep(_POLL_INTERVAL)

    webbrowser.open(_WEBUI_URL)


if __name__ == "__main__":
    main()
