# 14 — Install script hardening

**What to build:** Harden the install scripts to handle stale `bot/`
packages and ensure the bot package is included in the wheel:

1. **Add `bot` to wheel packages**: The `bot` package lives in
   `examples/bot_project/` and was not listed in the root
   `pyproject.toml`'s `packages` field. A previous install could leave a
   stale `site-packages/bot/` that shadowed the editable source. Add
   `bot` to the wheel packages list.

2. **Explicit uninstall before reinstall**: `install.bat` and
   `install.sh` run an explicit `uv pip uninstall` before
   `--reinstall` to clear stale packages from previous installs.

3. **Post-install stale cleanup**: `postinstall.py` adds a cleanup step
   that removes any stale `bot/` directory from `site-packages` before
   the editable install takes effect.

**Blocked by:** 08 (console script cutover — the packaging changes build
on the CLI migration).

**Status:** done (commit e414b304)

- [x] `bot` is listed in root `pyproject.toml` wheel `packages`.
- [x] `install.bat` runs explicit uninstall before `--reinstall`.
- [x] `install.sh` runs explicit uninstall before `--reinstall`.
- [x] `postinstall.py` includes stale `bot/` cleanup in
      `site-packages`.
- [x] A stale `site-packages/bot/` from a previous install does not
      shadow the editable source after running the install script.
- [x] `uv pip install -e .` produces a working `modexctl` command that
      imports from the editable source, not from a stale copy.
