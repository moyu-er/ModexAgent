# T06: Persistence design — SessionTreeStore ABC + implementations

> Type: `wayfinder:grilling` (HITL)
> Status: **Abandoned** — fully determined by T01
> Blocked by: T01, T03

## Question

What is the `SessionTreeStore` ABC interface, and what are the three implementations?

## Resolution

**Abandoned.** T01 fully determines the persistence design: three stores (SessionTreeStore / TreeNodeStore / MessageTrackStore), each ABC + InMemory / File / Sqlite. The ABC interfaces are defined in T01's resolution. No independent decision remains.

### Context

- Per-workspace, like InboxMQ / SessionStore
- Tree must survive process restart (crash recovery — T07)
- Tree must be cleaned up when sessions are evicted (session GC — ADR-0018)
- If T01 decides modex_graph reuse: `NodeStateStore` + `DeliverStore` may be reused, and `SessionTreeStore` is a thin wrapper or not needed at all
- If T01 decides separate: `SessionTreeStore` is a new ABC with its own schema

### Proposed ABC (if separate — T01 = Option B/C)

```python
class SessionTreeStore(ABC):
    """Per-workspace persistence for SessionTree state."""

    @abstractmethod
    async def get_tree(self, tree_id: str) -> SessionTreeRecord | None: ...

    @abstractmethod
    async def save_tree(self, tree_id: str, record: SessionTreeRecord) -> None: ...

    @abstractmethod
    async def create_version(self, tree_id: str, version: TreeVersionRecord) -> None: ...

    @abstractmethod
    async def get_active_version(self, tree_id: str) -> TreeVersionRecord | None: ...

    @abstractmethod
    async def add_node(self, tree_id: str, version: int, node: TreeNodeRecord) -> None: ...

    @abstractmethod
    async def update_node_status(
        self, tree_id: str, version: int, session_id: str, status: TreeNodeStatus
    ) -> None: ...

    @abstractmethod
    async def get_pending_nodes(self, tree_id: str, version: int) -> list[TreeNodeRecord]: ...

    @abstractmethod
    async def cancel_tree(self, tree_id: str) -> None: ...
```

### Implementations

| Impl | Storage | Use case |
|---|---|---|
| `InMemorySessionTreeStore` | Process-local dict | Tests + single-process |
| `FileSessionTreeStore` | `<workspace_data>/session_trees/<tree_id>.json` | File-based persistence |
| `SqliteSessionTreeStore` | `state.db` table `session_trees` | SQLite persistence |

### Cleanup integration

- `AgentPool._evict_dynamic_session(session_id)`:
  - If `session_id` is a tree root → `tree_store.cancel_tree(session_id)` (entire tree)
  - If `session_id` is a tree child node → `tree_store.update_node_status(tree_id, version, session_id, CANCELLED)`
- Session GC (ADR-0018) should also trigger tree cleanup

### Open questions for grilling

- If modex_graph reuse (T01 = A): do we use `NodeStateStore` directly, or wrap it? `NodeStateStore` is per-graph-instance, not per-session-tree. How do we map tree_id → graph_instance_id?
- Schema for Sqlite: what tables? `session_trees` (tree-level), `session_tree_nodes` (node-level), `session_tree_versions` (version-level)?
- How does File impl handle concurrent access? (Single-process asyncio — no file locking needed, same as LocalFileInboxMQ)
- How does tree store interact with `InboxMQ`? Are they separate stores, or does tree store reference inbox entries?
