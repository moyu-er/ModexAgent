"""WebUI route modules.

Each module in this package exports a ``register_*_routes(server)`` function
that registers its route handlers on ``server.app.router`` and a set of
module-level async handler functions. Handlers access server state through
``request.app["server"]`` (set by the ``register_*_routes`` call), matching
the ``control_facade`` pattern in :mod:`bot.control.routes`.
"""
