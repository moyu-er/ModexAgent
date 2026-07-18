"""Shared context interface for input-pipeline stages."""

from __future__ import annotations

from abc import ABC, abstractmethod


class InputContext(ABC):
    """Interface implemented by the business layer.

    The framework defines only the interface; concrete dependencies
    (pool store, transcript store, command processor, ...) are provided
    by the business-layer context.
    """

    @property
    @abstractmethod
    def default_pool(self) -> str | None:
        ...
