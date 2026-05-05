"""Compaction pipeline: unified message policy + boundary + summary + archive.

NOTE: MemoryCompactionPipeline has been removed.  The compaction workflow is
unified under MemoryCompressionCoordinator.  Policy and boundary abstractions
remain available for custom coordinator configurations.
"""

from framework.memory.compaction.boundary import (
    BoundaryPolicy,
    ToolChainBoundaryPolicy,
    UserTurnToolChainBoundaryPolicy,
)
from framework.memory.compaction.policy import (
    ConservativeCompactionPolicy,
    KeepAllCompactionPolicy,
    MessageCompactionDecision,
    MessageCompactionPolicy,
    SemanticToolCompactionPolicy,
)

__all__ = [
    "BoundaryPolicy",
    "ConservativeCompactionPolicy",
    "KeepAllCompactionPolicy",
    "MessageCompactionDecision",
    "MessageCompactionPolicy",
    "SemanticToolCompactionPolicy",
    "ToolChainBoundaryPolicy",
    "UserTurnToolChainBoundaryPolicy",
]
