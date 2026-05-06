"""Memory injection policies for LLM context assembly.

Provides pluggable strategies for converting MemorySystem state into
structured context bundles.
"""

from __future__ import annotations

from framework.memory.injection.filter import (
    InjectionFilterStrategy,
    NoopFilterStrategy,
    ToolMessageFilterStrategy,
)
from framework.memory.injection.full_injection import FullInjectionPolicy, bundle_to_context_state
from framework.memory.injection.policy import MemoryInjectionPolicy
from framework.memory.injection.restricted_injection import RestrictedInjectionPolicy

__all__ = [
    "FullInjectionPolicy",
    "InjectionFilterStrategy",
    "MemoryInjectionPolicy",
    "NoopFilterStrategy",
    "RestrictedInjectionPolicy",
    "ToolMessageFilterStrategy",
    "bundle_to_context_state",
]
