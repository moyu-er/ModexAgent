"""Scheduler package — re-exports for backward compatibility.

Public API: ``from modex_graph.scheduler import Scheduler, LinearScheduler,
ParallelScheduler, NodeInstance`` continues to work after the package split.
"""

from __future__ import annotations

from .base import Scheduler
from .instance import NodeInstance
from .linear import LinearScheduler
from .parallel import ParallelScheduler

__all__ = [
    "Scheduler",
    "LinearScheduler",
    "ParallelScheduler",
    "NodeInstance",
]
