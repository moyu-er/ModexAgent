# 03 — Build CLI skeleton with agents and workflow placeholders

**What to build:** A new Typer application in `examples/bot_project` (module `bot.cli.modexctl`) that registers the `agents` command and workflow placeholder commands. This establishes the CLI package structure, environment validation, and console script registration without requiring the bot HTTP control endpoints.

`agents` reads `MODEX_TARGETS` from the environment, parses the `name=description;...` format, and renders the same output as the legacy CLI. No HTTP call is made. Workflow placeholder commands report `workflow not available`, matching the legacy behavior when their environment gate is satisfied.

Per-command environment validation (not a global callback) checks required `MODEX_*` variables for each command. The Typer app uses `rich_markup_mode=None` and `pretty_exceptions_enable=False` to preserve machine-parseable output. The app is registered as `modexctl = "bot.cli.modexctl:main"` in `examples/bot_project/pyproject.toml`. The root `pyproject.toml` script entry is NOT yet removed (that happens in ticket 08).

**Blocked by:** None — can start immediately.

**Status:** done (commit c059e63f)

- [x] `bot.cli.modexctl` package exists with a Typer app named `modexctl`.
- [x] `agents` command reads `MODEX_TARGETS` locally and produces output matching the legacy CLI's format.
- [x] Workflow placeholder commands report `workflow not available` when their environment gate is satisfied.
- [x] Per-command environment validation rejects missing required `MODEX_*` variables with exit code 1.
- [x] Typer app uses `rich_markup_mode=None` and `pretty_exceptions_enable=False`.
- [x] `examples/bot_project/pyproject.toml` registers `modexctl = "bot.cli.modexctl:main"`.
- [x] Both `modexctl` and `modexctl --help` work from the venv after `uv pip install -e .`.
- [x] `agents` output matches legacy CLI output byte-for-byte for the same `MODEX_TARGETS` input (tested via CliRunner).
