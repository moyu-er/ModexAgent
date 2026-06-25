# Rename the framework package to `modex_agent` under a src/ layout

The import package is currently named `framework` (root `pyproject.toml`:
`packages = ["framework"]`). This is a generic name that collides with
example-side and third-party packages, carries no project identity, and is not
fit for publishing to PyPI. The goal of treating the framework as a first-class
installable package (stated refactor objective) requires a real package name.

## Considered Options

1. **Rename to `modex_agent`, adopt src/ layout (chosen).** Move
   `framework/` → `src/modex_agent/`, publish as `modex_agent`. Standard,
   import-time-safe (test/CI cannot accidentally import the repo-root copy),
   and matches the project name `ModexAgent`. PyCharm *Rename Refactor* on the
   package rewrites every `from framework.` reference mechanically.

2. **Rename only, keep repo-root layout.** `framework/` → `modex_agent/` at
   root. Cheaper, but a flat layout lets tests import the source tree by
   accident instead of the installed package — a classic packaging trap.

3. **Keep `framework`, only normalize imports.** Rejected: the generic name
   remains unpublishable and the rename debt is deferred, not removed.

## Consequences

- All `from framework.x` references (276 inside the package, plus tests and
  `examples/bot_project`) become `from modex_agent.x`. PyCharm Rename handles
  this; relative imports survive the rename unchanged and are normalized
  separately (see ADR-0004).
- `pyproject.toml` switches to a src layout:
  `[tool.hatch.build.targets.wheel] packages = ["src/modex_agent"]`.
- `examples/bot_project` should declare `modex_agent` as an editable
  dependency (`modex-agent` distribution, import `modex_agent`) rather than
  relying on repo-root `sys.path` adjacency.
- One-time mechanical cost; no behavioral change.
