"""debug entry point — run this file directly.

Runs the bot in the current process
with all defaults: config from ``config/``, port 21800, .env auto-loaded.
"""

if __name__ == "__main__":
    from functools import partial
    from pathlib import Path

    from modexbot.main import create_webui_service, run_with_supervisor

    _here = Path(__file__).resolve().parent
    _config = _here / "config"
    _port = 21800
    run_with_supervisor(partial(create_webui_service, _config, port=_port))
