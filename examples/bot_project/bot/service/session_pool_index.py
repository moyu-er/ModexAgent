"""Registry-based per-workspace index: "which pool owns this session_id?".

Contract (pool-attribution convergence, W3-6):

- **The session tree is the attribution authority.** ``pool_of`` resolves
  session_id → tree node → tree record and returns ``tree.pool_name``. The
  registration key only selects which stores are searched; it is never the
  answer itself.
- **Read-only attribution surface.** The index must NOT be used for the
  reverse of routing decisions — no pool → session enumeration, no routing
  mutation, no prefix inference. Prefix-based routing (``PoolSessionStore``)
  is orthogonal to and unaffected by this index.
- **Per-workspace instances; no caching.** One index lives on each
  workspace's ``PoolWorkspaceResources``; every pool registers its locally
  constructed tree/node stores at ``create_pool`` time, and the index is
  released with the bundle on workspace eviction. Per-request caching
  belongs to consumers, not here.
"""

from __future__ import annotations

from modex_agent.multi_agent.session_tree.store_node import TreeNodeStore
from modex_agent.multi_agent.session_tree.store_tree import SessionTreeStore

__all__ = ["SessionPoolIndex"]


class SessionPoolIndex:
    """Answers session→pool attribution from the session trees of one workspace.

    Pools (or tests) register their session-tree store pair under a pool
    name. Queries walk the registered node stores in registration order; the
    first store holding the session resolves its tree record, whose
    ``pool_name`` is the authoritative answer.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[SessionTreeStore, TreeNodeStore]] = {}

    def register(
        self,
        pool_name: str,
        tree_store: SessionTreeStore,
        node_store: TreeNodeStore,
    ) -> None:
        """Register one pool's session-tree stores under ``pool_name``.

        Re-registering the same pool name replaces the previous entry, so a
        pool rebuild swaps in its fresh store handles.
        """
        self._entries[pool_name] = (tree_store, node_store)

    async def pool_of(self, session_id: str) -> str | None:
        """Return the pool owning ``session_id``, or ``None`` when unknown.

        Unknown to every registered node store → ``None``; node found but its
        tree record missing (data anomaly) → ``None``. The returned value is
        ``tree.pool_name`` — the session tree is the authority.
        """
        for tree_store, node_store in self._entries.values():
            node = await node_store.get(session_id)
            if node is None:
                continue
            tree = await tree_store.get(node.tree_id)
            if tree is None:
                return None
            return tree.pool_name
        return None
