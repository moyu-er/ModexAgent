# 08 — Console script cutover, packaging, and legacy deprecation

**What to build:** Complete the migration by removing the legacy `modexctl` console script registration, updating packaging scripts to generate the correct shim, and marking the legacy source as deprecated.

Remove `modexctl = "modexctl.main:main"` from root `pyproject.toml` `[project.scripts]`. The new CLI (registered in ticket 03) becomes the sole `modexctl` command. Update `postinstall.py` `create_cli_shims()` so `modexctl.bat` points to `python.exe -m bot.cli.modexctl`. Update `verify_imports()` to check `import bot.cli.modexctl` instead of `import modexctl`. Add a `# DEPRECATED` module docstring to `src/modexctl/__init__.py`.

The legacy `src/modexctl/` source remains in the repository and in the root wheel's `packages` list so that `modexbot` CLI imports and legacy tests continue to resolve. No source files are deleted. No runtime fallback is introduced.

**Blocked by:** 03 (CLI skeleton with agents), 04 (native history), 05 (external history), 06 (send basic), 07 (send invocation-id).

**Status:** done (commit bee1f612)

- [x] Root `pyproject.toml` no longer has `modexctl` in `[project.scripts]`.
- [x] `examples/bot_project/pyproject.toml` has `modexctl = "bot.cli.modexctl:main"` (from ticket 03).
- [x] `postinstall.py` `create_cli_shims()` generates `modexctl.bat` pointing to `python.exe -m bot.cli.modexctl`.
- [x] `postinstall.py` `verify_imports()` checks `import bot.cli.modexctl`.
- [x] `postinstall.py` no longer checks `import modexctl` in `verify_imports()`.
- [x] `src/modexctl/__init__.py` has a `# DEPRECATED` docstring noting supersession by ADR-0035.
- [x] Legacy tests under `tests/unit/cli/modexctl/` still pass (they import from `modexctl.main` directly, not via console script).
- [x] `modexbot` CLI (`src/modex_agent/cli/modexbot/`) still imports from `modexctl.main` without error.
- [x] After `uv pip install -e .`, `modexctl --help` shows the new CLI (bot.cli.modexctl), not the legacy one.
- [x] No changes to `build.bat`, `build_archive.py`, `prepare_python.py`, `prepare_bundled_bin.py`, or `modexbot.iss`.
- [x] Deployment verification (Seam 4): `postinstall.py` shim content is correct; `pyproject.toml` console script is correct; `ExternalEnvSpec.control_origin` field exists and `build_modex_vars()` emits `MODEX_CONTROL_ORIGIN`.
