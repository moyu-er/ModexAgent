"""Dynamic schema ABC for tools.

Rule 7: Use ABC instead of Protocol for interfaces and extension points.
"""

from abc import ABC, abstractmethod
from typing import Any


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


__all__ = ["DynamicSchemaProvider"]
