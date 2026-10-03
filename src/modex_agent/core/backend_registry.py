"""BackendRegistry — name → factory for one service-level backend family.

The generic registry container every service-level backend family
resolves through (message broker, control channel, persistence bundles,
LLM protocol engines, external transports). Backends resolve once per
boot (or per provider construction) from a configured name, never
through scope compilation; the plugin loader lands third-party
factories into a family's registry, and each family module seeds its
bundled defaults.

Sits in ``core`` (level 0) because the lowest family owner — the
protocol-engine registry under ``providers`` (level 1) — must import it
downward; the plugin-level families import it from above.

Registries are process-singletons created on first access by their
family's accessor — one instance per backend family.
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["BackendRegistry"]


class BackendRegistry[G]:
    """name → factory for one service-level backend family.

    Backends resolve once per boot/per pool from config, never through
    scope compilation. Re-registering the same ``(name, factory)`` is
    idempotent; the same name with a different factory is a packaging
    error (loud).

    ``family`` labels the backend family in error messages (e.g.
    ``"message broker"``) so a boot failure names the family the
    configured name failed to resolve in.
    """

    def __init__(self, *, family: str) -> None:
        self._family = family
        self._factories: dict[str, Callable[[], G]] = {}

    def register(self, name: str, factory: Callable[[], G]) -> None:
        existing = self._factories.get(name)
        if existing is not None and existing is not factory:
            raise ValueError(
                f"{self._family} backend {name!r} registered twice with "
                "different factories (packaging/config error)"
            )
        self._factories[name] = factory

    def resolve(self, name: str) -> G:
        """Construct the backend registered under *name* (the
        config-resolution face — one call, one fresh backend instance)."""
        try:
            factory = self._factories[name]
        except KeyError:
            raise ValueError(
                f"{self._family} backend {name!r} is not registered — "
                f"registered backends: {list(self._factories)}; no plugin "
                "contributed it to the backend registry"
            ) from None
        return factory()

    def names(self) -> tuple[str, ...]:
        """Registered backend names, in registration order."""
        return tuple(self._factories)
