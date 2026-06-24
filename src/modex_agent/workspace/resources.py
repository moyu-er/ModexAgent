"""Framework type contract for workspace resources.

Declares the attribute the framework pipeline and ``AgentCommunicationService``
read off a workspace's resources (``pool_data``), so those call sites are
type-checked instead of going through ``object`` + ``# type: ignore``.

The concrete resource bundle lives in business code
(``bot.workspace.bundle.handle.PoolWorkspaceResources``); the framework must
not import it. This ABC declares only what the framework reads, and the
business bundle satisfies it by inheriting it.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Mapping

from modex_agent.pipeline.snapshot import PoolDataSnapshot


class WorkspaceResources(ABC):
    """Framework view of a workspace's per-pool data snapshots.

    The framework never imports the concrete resource bundle; it reads
    ``pool_data`` through this contract. Keys are pool names; values are the
    per-pool :class:`PoolDataSnapshot` (turn/command/trace stores, memory dir,
    experience dir, ...).
    """

    pool_data: Mapping[str, PoolDataSnapshot]
