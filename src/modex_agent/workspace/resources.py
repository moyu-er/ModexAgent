"""Framework type contracts for workspace resources.

Declares the attribute the framework pipeline and ``AgentCommunicationService``
read off a workspace's resources (``pool_data``), plus the
:class:`WorkspaceManager` ABC the framework uses to resolve the active
workspace's resources. The concrete resource bundle / resolver lives in
business code (``bot.workspace.bundle.handle``); the framework must not import
it. These ABCs declare only what the framework reads.

Business-decoupled: this package imports only the standard library, ``typing``,
``abc``, and other ``modex_agent.*`` modules at or below tier 2 — never
``pipeline``/``multi_agent``/``ioc`` (tier 3+) at runtime.
``PoolDataSnapshot`` is referenced under TYPE_CHECKING only (annotation).
Guarded by ``tests/architecture/test_dependency_tree.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.pipeline.snapshot import PoolDataSnapshot


class WorkspaceResources(ABC):
    """Framework view of a workspace's per-pool data snapshots.

    The framework never imports the concrete resource bundle; it reads
    ``pool_data`` through this contract. Keys are pool names; values are the
    per-pool :class:`PoolDataSnapshot` (turn/command/trace stores, memory dir,
    experience dir, ...).
    """

    pool_data: Mapping[str, PoolDataSnapshot]


class WorkspaceManager(ABC):
    """Abstract interface for workspace resolution used by the framework.

    The concrete implementation (e.g. ``WorkspaceResolverCell`` in the bot
    layer) is provided by business code. This ABC keeps the framework decoupled
    from business types while giving constructors a precise type annotation.

    Relocated from ``multi_agent/communication.py`` per ADR-0006: a workspace
    concept must not be owned by ``multi_agent`` (the last upward edge).
    """

    @abstractmethod
    def resolve_workspace(self) -> WorkspaceResources:
        """Return the currently active workspace's resources.

        The returned :class:`WorkspaceResources` exposes ``pool_data`` (pool
        name → :class:`PoolDataSnapshot`), whose values carry ``memory_dir``,
        ``runtime_dir``, and ``pruned_manager``.
        """
        ...
