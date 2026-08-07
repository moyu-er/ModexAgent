"""``modexctl`` command closures — split from the former ``main.py``.

Each module exposes a ``build_<name>_command(ctx)`` factory that returns the
Typer command closure capturing :class:`~bot.cli.modexctl.context.ModexCtlContext`.
The closure bodies are moved verbatim from ``main.py``; only the wrapping
factory and indentation are new.
"""
