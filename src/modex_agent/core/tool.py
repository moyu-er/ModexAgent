"""Dynamic schema ABC for tools.

Rule 7: Use ABC instead of Protocol for interfaces and extension points.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from modex_agent.core.capabilities import ModelCapabilities


class DynamicSchemaProvider(ABC):
    """ABC for tools that generate dynamic, context-aware schemas.

    Override get_dynamic_schema() to return a schema with a description
    tailored to the tool's current state (e.g. available communication targets).
    Default implementation in Tool returns ``self.get_schema()``.
    """

    @abstractmethod
    def get_dynamic_schema(self) -> dict[str, Any]:
        """Return the tool schema for LLM consumption.

        The default ``Tool`` implementation delegates to ``get_schema()``,
        which uses ``self.description``.  Subclasses that need dynamic
        descriptions should override ``description`` (the property) rather
        than this method.
        """
        ...

    def get_dynamic_schema_for(
        self, caps: ModelCapabilities | None = None
    ) -> dict[str, Any]:
        """Return the tool schema, optionally adapted to model capabilities.

        Default implementation ignores ``caps`` and delegates to
        :meth:`get_dynamic_schema`, so existing subclasses keep working
        unchanged. Subclasses that produce capability-aware schemas (e.g.
        hiding image parameters when the active model is text-only) override
        this method instead of ``get_dynamic_schema``.
        """
        return self.get_dynamic_schema()


__all__ = ["DynamicSchemaProvider"]
