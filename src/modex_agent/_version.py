"""Single source of truth for the ModexAgent package version.

Read by:
- ``src/modex_agent/__init__.py`` (runtime import)
- root ``pyproject.toml`` via ``[tool.hatch.version]`` (framework wheel)
- ``examples/bot_project/pyproject.toml`` via ``[tool.hatch.version]`` (bot wheel)
- ``examples/bot_project/packaging/build.bat`` (Windows installer version)

To change the version, edit ONLY this file. All other locations derive from it.
"""

__version__ = "1.0.0"
