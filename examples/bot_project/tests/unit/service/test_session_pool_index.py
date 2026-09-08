"""SessionPoolIndex unit tests — attribution authority + backend consistency.

Covers the W3-6 contract: the session tree is the attribution authority,
unknown sessions resolve to None, the index never depends on the prefix
routing store, and ``pool_of`` behaves identically across the InMemory,
LocalFile, and SQLite store backends.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import bot.service.session_pool_index as session_pool_index_module
import pytest
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.session_pool_index import SessionPoolIndex
from bot.workspace.handle import WorkspaceResolverCell

from modex_agent.adapters.output import OutputAdapter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.scope import RecordScope
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.session_tree.models import (
    NodeVersionStatus,
    SessionTreeRecord,
    SessionTreeStatus,
    TreeNodeRecord,
)
from modex_agent.multi_agent.session_tree.store_node import (
    InMemoryTreeNodeStore,
    LocalFileTreeNodeStore,
    SqliteTreeNodeStore,
    TreeNodeStore,
)
from modex_agent.multi_agent.session_tree.store_tree import (
    InMemorySessionTreeStore,
    LocalFileSessionTreeStore,
    SessionTreeStore,
    SqliteSessionTreeStore,
)
from modex_agent.persistence import ConnectionManager, DatabaseKind

from ...declaration_driver import build_declared

_POOL_DECLARATION = """\
pool:
  name: indexed-pool
  agents:
    main:
      description: indexed test main
      toolset: none
"""

_NOW = 1_700_000_000_000


def _tree_record(tree_id: str, root_session: str, pool_name: str) -> SessionTreeRecord:
    return SessionTreeRecord(
        tree_id=tree_id,
        root_node_session_id=root_session,
        pool_name=pool_name,
        workspace_root=".",
        status=SessionTreeStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _node_record(tree_id: str, session_id: str) -> TreeNodeRecord:
    return TreeNodeRecord(
        tree_id=tree_id,
        session_id=session_id,
        parent_session_id=None,
        agent_name="main",
        version=1,
        parent_version=None,
        status=NodeVersionStatus.RUNNING,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _RecordingIndex(SessionPoolIndex):
    """Test double exposing the registered store handles for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.registrations: list[tuple[str, SessionTreeStore, TreeNodeStore]] = []

    def register(
        self,
        pool_name: str,
        tree_store: SessionTreeStore,
        node_store: TreeNodeStore,
    ) -> None:
        self.registrations.append((pool_name, tree_store, node_store))
        super().register(pool_name, tree_store, node_store)


async def test_two_registered_pools_cross_attribution(tmp_path: Path) -> None:
    index = SessionPoolIndex()

    alpha_tree = InMemorySessionTreeStore()
    alpha_nodes = InMemoryTreeNodeStore()
    index.register("alpha", alpha_tree, alpha_nodes)

    beta_tree = LocalFileSessionTreeStore(tmp_path / "beta" / "trees")
    beta_nodes = LocalFileTreeNodeStore(tmp_path / "beta" / "nodes")
    index.register("beta", beta_tree, beta_nodes)

    await alpha_tree.create(_tree_record("tree-a", "conv1.alpha-main", "alpha"))
    await alpha_nodes.create(_node_record("tree-a", "conv1.alpha-main"))
    await beta_tree.create(_tree_record("tree-b", "conv2.beta-main", "beta"))
    await beta_nodes.create(_node_record("tree-b", "conv2.beta-main"))

    assert await index.pool_of("conv1.alpha-main") == "alpha"
    assert await index.pool_of("conv2.beta-main") == "beta"


async def test_unknown_session_returns_none() -> None:
    index = SessionPoolIndex()
    tree_store: SessionTreeStore = InMemorySessionTreeStore()
    node_store: TreeNodeStore = InMemoryTreeNodeStore()
    index.register("alpha", tree_store, node_store)
    await tree_store.create(_tree_record("tree-a", "conv1.alpha-main", "alpha"))
    await node_store.create(_node_record("tree-a", "conv1.alpha-main"))

    assert await index.pool_of("missing.session") is None


async def test_empty_index_returns_none() -> None:
    assert await SessionPoolIndex().pool_of("any.session") is None


async def test_tree_record_is_authority_not_registration_key() -> None:
    index = SessionPoolIndex()
    tree_store: SessionTreeStore = InMemorySessionTreeStore()
    node_store: TreeNodeStore = InMemoryTreeNodeStore()
    index.register("registered-name", tree_store, node_store)
    await tree_store.create(_tree_record("tree-a", "conv1.main", "authoritative-pool"))
    await node_store.create(_node_record("tree-a", "conv1.main"))

    assert await index.pool_of("conv1.main") == "authoritative-pool"


async def test_missing_tree_record_returns_none() -> None:
    index = SessionPoolIndex()
    tree_store: SessionTreeStore = InMemorySessionTreeStore()
    node_store: TreeNodeStore = InMemoryTreeNodeStore()
    index.register("alpha", tree_store, node_store)
    await node_store.create(_node_record("tree-a", "conv1.alpha-main"))

    assert await index.pool_of("conv1.alpha-main") is None


def test_index_has_no_pool_routing_dependency() -> None:
    # Attribution comes from the session tree only — the index must never
    # depend on (or consult) the prefix-routing store or the pool router.
    assert "PoolSessionStore" not in vars(session_pool_index_module)
    assert "PoolRouter" not in vars(session_pool_index_module)
    init_params = set(inspect.signature(SessionPoolIndex.__init__).parameters)
    register_params = set(inspect.signature(SessionPoolIndex.register).parameters)
    assert init_params == {"self"}
    assert register_params == {"self", "pool_name", "tree_store", "node_store"}


@pytest.fixture(params=["inmemory", "localfile", "sqlite"])
async def single_pool_index(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> AsyncIterator[SessionPoolIndex]:
    index = SessionPoolIndex()
    manager: ConnectionManager | None = None
    if request.param == "inmemory":
        tree_store: SessionTreeStore = InMemorySessionTreeStore()
        node_store: TreeNodeStore = InMemoryTreeNodeStore()
    elif request.param == "localfile":
        tree_store = LocalFileSessionTreeStore(tmp_path / "trees")
        node_store = LocalFileTreeNodeStore(tmp_path / "nodes")
    else:
        manager = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
        await manager.open()
        scope = RecordScope(workspace_id="index-test")
        tree_store = SqliteSessionTreeStore(manager, scope)
        node_store = SqliteTreeNodeStore(manager, scope)

    index.register("alpha", tree_store, node_store)
    await tree_store.create(_tree_record("tree-a", "conv1.alpha-main", "alpha"))
    await node_store.create(_node_record("tree-a", "conv1.alpha-main"))
    try:
        yield index
    finally:
        if manager is not None:
            await manager.close()


async def test_pool_of_consistent_across_backends(
    single_pool_index: SessionPoolIndex,
) -> None:
    assert await single_pool_index.pool_of("conv1.alpha-main") == "alpha"
    assert await single_pool_index.pool_of("missing.session") is None


async def test_create_pool_registers_tree_stores_in_index(tmp_path: Path) -> None:
    pool_name = "indexed-pool"
    output_adapter = MagicMock(spec=OutputAdapter)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()

    index = _RecordingIndex()
    pool_instance = None
    try:
        with patch.dict("os.environ", {"MODEXBOT_BIN_DIR": str(bin_dir)}):
            pool_instance = await create_pool(
                pool_name=pool_name,
                declared=build_declared(
                    _POOL_DECLARATION,
                    project_dir=tmp_path,
                    data_dir=tmp_path / ".modex",
                    pool_name=pool_name,
                ),
                assembly_deps=PoolAssemblyDeps(),
                project_dir=tmp_path,
                workspace_registry=object(),
                workspace_resources=object(),
                data_dir=tmp_path / ".modex",
                broker=broker,
                output_adapter=output_adapter,
                safety=RuntimeSafetyPolicy(),
                retention=SessionRetentionPolicy(),
                im_ui=MagicMock(),
                shared_hooks=[],
                shared_hook_runner=HookRunner(),
                shared_interceptor_chain=InterceptorChain(),
                workspace_resolver=WorkspaceResolverCell(),
                bot_model_config=None,
                model_choice_registry=ModelChoiceRegistry(),
                session_pool_index=index,
            )

        assert [entry[0] for entry in index.registrations] == [pool_name]
        _, tree_store, node_store = index.registrations[0]

        # Write through the captured handles and resolve through the index:
        # proves create_pool registered the very stores the pool runs on.
        await tree_store.create(_tree_record("tree-i", "conv1.indexed-pool-main", pool_name))
        await node_store.create(_node_record("tree-i", "conv1.indexed-pool-main"))
        assert await index.pool_of("conv1.indexed-pool-main") == pool_name
    finally:
        if pool_instance is not None:
            await pool_instance.pool.shutdown_all()
        await broker.stop()
