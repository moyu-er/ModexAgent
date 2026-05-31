---
name: botctl
description: Restart or stop this bot process via scripts/botctl.py. Use when the user asks to restart, stop, or kill the bot, or when you need to restart the bot service after a config change or deployment.
---

The bot control script lives at `scripts/botctl.py` (relative to the bot project root examples/bot_project):

```bash
python scripts/botctl.py restart   # stop old + start new (default)
python scripts/botctl.py stop      # kill the bot process tree
python scripts/botctl.py --help    # full usage
```

- `restart` is the default subcommand — `python scripts/botctl.py` alone does a restart.
- The script is cross-platform (Windows / Linux / macOS).
- It auto-detects the uv project root and falls back to native python if uv is absent.
- When in doubt, run it with `--help` to see all options.
