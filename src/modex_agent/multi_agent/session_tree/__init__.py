"""Session-tree persistence and models.

Public API:
    - Stores (ABCs + concrete impls): ``SessionTreeStore``, ``TreeNodeStore``,
      ``MessageTrackStore`` and their ``LocalFile``/``Sqlite``/``InMemory``
      variants.
    - Models: ``SessionTreeRecord``, ``TreeNodeRecord``, ``MessageTrack``.
    - Enums: ``SessionTreeStatus``, ``NodeVersionStatus``, ``MessageTrackStatus``.
    - Manager: ``SessionTreeManager`` — runtime coordinator for the
      session-tree lifecycle (deliver, quiesce, recover, eviction cleanup).
"""

from __future__ import annotations

from modex_agent.multi_agent.inbox.types import SessionWork
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.multi_agent.session_tree.models import (
    MessageTrack,
    MessageTrackStatus,
    NodeVersionStatus,
    SessionTreeMetadata,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.session_binding import (
    InMemorySessionBindingStore,
    SessionBinding,
    SessionBindingStore,
)
from modex_agent.multi_agent.session_tree.store_node import (
    InMemoryTreeNodeStore,
    LocalFileTreeNodeStore,
    SqliteTreeNodeStore,
    TreeNodeStore,
)
from modex_agent.multi_agent.session_tree.store_track import (
    InMemoryMessageTrackStore,
    LocalFileMessageTrackStore,
    MessageTrackStore,
    SqliteMessageTrackStore,
)
from modex_agent.multi_agent.session_tree.store_tree import (
    InMemorySessionTreeStore,
    LocalFileSessionTreeStore,
    SessionTreeStore,
    SqliteSessionTreeStore,
)

__all__ = [
    "InMemoryMessageTrackStore",
    "InMemorySessionBindingStore",
    "InMemorySessionTreeStore",
    "InMemoryTreeNodeStore",
    "LocalFileMessageTrackStore",
    "LocalFileSessionTreeStore",
    "LocalFileTreeNodeStore",
    "MessageTrack",
    "MessageTrackStatus",
    "MessageTrackStore",
    "NodeVersionStatus",
    "SessionBinding",
    "SessionBindingStore",
    "SessionTreeManager",
    "SessionTreeMetadata",
    "SessionTreeRecord",
    "SessionTreeStatus",
    "SessionTreeStore",
    "SessionWork",
    "SqliteMessageTrackStore",
    "SqliteSessionTreeStore",
    "SqliteTreeNodeStore",
    "TreeNodeRecord",
    "TreeNodeStore",
]
