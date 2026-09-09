"""Single-project editor entry over the framework ACP backend seam.

The runtime binds the editor's cwd; handles use pool request admission;
emitters reuse the bot transcript recorder. The CLI loads this package only
for ``modexbot acp`` so the optional wire SDK never enters resident startup.
"""
