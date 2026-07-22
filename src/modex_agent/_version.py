"""Single source of truth for the entire project version.

Read by:
- ``src/modex_agent/__init__.py`` (runtime import)
- root ``pyproject.toml`` via ``[tool.hatch.version]`` (framework wheel)
- ``examples/bot_project/pyproject.toml`` via ``[tool.hatch.version]`` (bot wheel)
- ``examples/bot_project/modexbot/cli.py`` (``modexbot --version``)
- ``examples/bot_project/packaging/build.bat`` (Windows installer version)
- ``examples/bot_project/packaging/sync_versions.py`` → ``webui/package.json``
  (npm package version, synced at build time so the WebUI build metadata
  matches the Python version)

To change the version, edit ONLY this file. All other locations derive
from it — Python packages via hatch, npm packages via ``sync_versions.py``,
the installer via ``build.bat``, and the CLI via direct import.
"""

__version__ = "1.0.0"
