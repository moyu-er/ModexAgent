"""Bot project plugins — the deployment's plugin package (W6 packaging contract).

Discovered by ``ComponentRegistryLoader`` through the project plugin dir
(``AppAssemblyRoots.plugins_dir`` = ``bot_plugins/``). Each plugin file is
imported under a QUALIFIED synthetic name — nothing here needs to be on
``sys.path`` and no top-level ``plugins`` package exists. The plugin
classes stay here; the underlying hook/strategy/stage CLASSES stay in
their respective ``bot/service/`` or ``bot/input_pipeline/`` directories
(rule 9, FW/BIZ separation).
"""
