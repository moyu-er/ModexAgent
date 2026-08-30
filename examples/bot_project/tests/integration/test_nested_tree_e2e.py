"""Ticket 12 — nested declaration tree activation, end to end.

One config (``fixtures/scope/nested-tree-e2e.yml``): a three-level tree
(``root → mid → leaf``), a bidirectional peer link to another pool's root,
and a graph referencing the leaf. Three scenarios over that one config:

1. **Dispatch chain** — the root dispatches ``mid`` via ``task``; ``mid``
   (a SUBAGENT — non-main dispatcher, ticket 12's relaxation) dispatches
   ``leaf`` via its own ``task``; results flow back up both levels; the
   session tree records the full chain with invocation-prefixed branches
   and strict two-segment session ids (SPEC §13 Errata-1).
2. **Peer link** — the main-pool root sends ``send_to_peer`` to the peer
   pool's root; the peer replies; the reply lands back on the sender's
   prefix (session group, ADR-0019).
3. **Graph-referenced lazy leaf** — the never-dispatched leaf cold-starts
   from its template (ticket 08's inbox-driven path), its graph node turn
   receives the ``deliver`` tool (configurator gate relaxed to the
   session-binding graph signal, SPEC §4 axis 3), and the delivered
   result flows back through ``__end__``.

Only the LLM is scripted; the pools, pollers, session trees, template
registries, and materialization deps are the real production objects
built by ``create_pool`` on the declaration road.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bot.graph.agent_node import BotAgentNode
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.pool.declaration import (
    boot_scope_declaration,
    declared_pool_build,
)
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER
from bot.workspace.pool_data import build_pool_data
from bot.workspace.wiring.stack import declared_assembly_deps
from pydantic import BaseModel

from modex_agent.core.constants import FinishReason
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import InMemorySessionRegistry
from modex_agent.core.types import InputMessage, LLMResponse, ToolCall
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.multi_agent.tools import CommunicationTarget
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths
from modex_graph import (
    DefaultGraphState,
    Graph,
    GraphContext,
    GraphEngine,
    GraphNode,
    GraphPayload,
    GraphRuntime,
    NodeTrigger,
    SchedulerKind,
)
from modex_graph.persistence import (
    InMemoryNodeStateStore,
    NullDeliverStoreFactory,
    NullGraphInstanceStore,
)
from modex_graph.persistence.persistence_coordinator import (
    GraphPersistenceCoordinator,
)
from modex_graph.scheduler.bootstrap import BootstrapMode

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).parent / "fixtures" / "scope"
_MAIN_POOL = "main-pool"
_PEER_POOL = "peer-pool"
_ROOT = "root"
_MID = "mid"
_LEAF = "leaf"
_PEER_ROOT = "peerroot"
_LEAF_RESULT = "LEAF-FINAL-RESULT"
_MID_RESULT = "MID-FINAL-RESULT"
_PEER_REPLY = "PEER-ROOT-REPLY"
_LEAF_GRAPH_RESULT = "LEAF-GRAPH-DELIVERED"


# ── Scripted LLM (the only fake) ─────────────────────────────────────────


class _NestedScriptedProvider(CallbackStreamProvider):
    """Routes responses by the caller's TOOL SURFACE.

    Each tree position has a distinct toolset: the main-pool root carries
    ``task``+``send_to_peer``, ``mid`` carries ``task``+``send_to_agent``,
    the leaf only read tools + ``send_to_agent``, the peer root carries
    ``send_to_peer`` without ``task``, and a graph-node turn additionally
    carries ``deliver``.
    """

    def __init__(self) -> None:
        self.calls: dict[str, int] = {"root": 0, "mid": 0, "leaf": 0, "peer": 0}
        self.graph_delivers = 0
        self.root_sends_peer = False

    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        names = _tool_names(tools)
        if "deliver" in names:
            # A graph node turn (the lazy leaf referenced by the graph).
            self.graph_delivers += 1
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        tool_name="deliver",
                        arguments={
                            "target": GraphNode.END,
                            "content": _LEAF_GRAPH_RESULT,
                        },
                        call_id=f"g{self.graph_delivers}",
                    )
                ],
            )
        if "send_to_peer" in names and "task" in names:
            # The main-pool root.
            self.calls["root"] += 1
            if self.calls["root"] == 1:
                if self.root_sends_peer:
                    return _tool_call(
                        "send_to_peer", "target_peer", _PEER_ROOT, "hello from root"
                    )
                return _tool_call("task", "target_agent", _MID, "investigate the tree")
            return LLMResponse(content="root done", finish_reason=FinishReason.STOP)
        if "task" in names:
            # The mid-level agent — dispatches its own declared child.
            self.calls["mid"] += 1
            if self.calls["mid"] == 1:
                return _tool_call("task", "target_agent", _LEAF, "leaf subtask")
            return LLMResponse(content=_MID_RESULT, finish_reason=FinishReason.STOP)
        if "send_to_peer" in names:
            # The peer pool's root — replies via send_to_peer.
            self.calls["peer"] += 1
            return _tool_call("send_to_peer", "target_peer", _ROOT, _PEER_REPLY)
        # The leaf (session turn): its deliverable IS the final reply text.
        self.calls["leaf"] += 1
        return LLMResponse(content=_LEAF_RESULT, finish_reason=FinishReason.STOP)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta: Any = None,
        on_reasoning_delta: Any = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await self.chat(
            messages,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            **kwargs,
        )

    def get_default_model(self) -> str:
        return "mock-model"

    def total_calls(self) -> int:
        return sum(self.calls.values()) + self.graph_delivers


class _ScriptedProviderConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


def _tool_names(tools: list[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        if isinstance(name, str):
            names.add(name)
    return names


def _tool_call(tool: str, target_key: str, target: str, content: str) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                tool_name=tool,
                arguments={target_key: target, "content": content},
                call_id=f"{tool}-{target}",
            )
        ],
    )


# ── Harness: real create_pool on the declaration road ───────────────────


class _PoolBuild:
    """One pool booted from the fixture declaration via create_pool."""

    def __init__(self, instance, pool_data, broker) -> None:
        self.instance = instance
        self.pool_data = pool_data
        self.broker = broker


class _NestedTreeEnv:
    """Both pools of the fixture config + Phase-2 peer wiring."""

    def __init__(self, provider: _NestedScriptedProvider) -> None:
        self.provider = provider
        self.graph_ctx_holder: dict[str, GraphContext[DefaultGraphState] | None] = {}
        self.cell = SimpleNamespace()
        self.builds: dict[str, _PoolBuild] = {}
        self._resources: SimpleNamespace | None = None


async def _build_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    root_sends_peer: bool = False,
) -> _NestedTreeEnv:
    """Boot BOTH fixture pools through the real declaration road.

    Mirrors the production wiring shape (resources.py): per-pool
    ``create_pool(declared=...)`` + Phase-2 peer-target injection into the
    roots' per-agent stores, one resolver cell shared by both pools.
    """
    fake_bin = tmp_path / "fake_bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "modexctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(fake_bin))

    provider = _NestedScriptedProvider()
    provider.root_sends_peer = root_sends_peer
    env = _NestedTreeEnv(provider)

    # The tree derivation (task/send_to_agent/send_to_peer) is
    # capability-contributed since the subagents migration — the boot
    # registry resolves it at compile (registry-less compiles of
    # child-carrying trees fail V6).
    registry = await _load_registry(provider)
    boot = boot_scope_declaration(
        declaration_path=_FIXTURES / "nested-tree-e2e.yml",
        project_dir=tmp_path,
        data_dir=tmp_path / ".modex",
        graphs_dirs=(_FIXTURES / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
        registry=registry,
    )

    for pool_name, _root_name in ((_MAIN_POOL, _ROOT), (_PEER_POOL, _PEER_ROOT)):
        declared = declared_pool_build(boot, pool_name)
        deps: PoolAssemblyDeps = declared_assembly_deps(
            declared.root, max_context_tokens=32000
        )
        pool_data = await build_pool_data(
            _ws_ctx(tmp_path), pool_name, declared.pool.root_agent, provider, deps, ""
        )
        broker = InMemoryMessageBroker()
        await broker.start()
        instance = await create_pool(
            pool_name=pool_name,
            declared=declared,
            assembly_deps=deps,
            project_dir=tmp_path,
            workspace_registry=object(),
            workspace_resources=object(),
            data_dir=tmp_path / ".modex",
            broker=broker,
            output_adapter=AsyncMock(spec=OutputAdapter),
            safety=RuntimeSafetyPolicy(),
            retention=SessionRetentionPolicy(),
            im_ui=AsyncMock(),
            shared_hooks=[],
            shared_hook_runner=HookRunner(),
            shared_interceptor_chain=InterceptorChain(),
            workspace_resolver=env.cell,  # type: ignore[arg-type]
            bot_model_config=None,
            model_choice_registry=ModelChoiceRegistry(),
            component_registry=registry,
            session_registry=InMemorySessionRegistry(),
            pool_data=pool_data,
        )
        env.builds[pool_name] = _PoolBuild(instance, pool_data, broker)

    # Phase-2 peer wiring (mirrors resources.py): the roots' per-agent
    # stores gain NORMAL peer targets carrying the peer pool's tree ref.
    main_b = env.builds[_MAIN_POOL]
    peer_b = env.builds[_PEER_POOL]
    main_b.instance.target_store.add(
        CommunicationTarget(
            name=_PEER_ROOT,
            kind=AgentCommKind.NORMAL,
            pool_name=_PEER_POOL,
            tree_ref=peer_b.instance.tree_manager,
            description="peer pool root",
        )
    )
    peer_b.instance.target_store.add(
        CommunicationTarget(
            name=_ROOT,
            kind=AgentCommKind.NORMAL,
            pool_name=_MAIN_POOL,
            tree_ref=main_b.instance.tree_manager,
            description="main pool root",
        )
    )

    # The shared resolver-cell payload: the pools handle (BotAgentNode),
    # the per-pool data snapshots, and the graph-orchestrator view.
    env._resources = SimpleNamespace(  # noqa: SLF001
        ctx=_ws_ctx(tmp_path),
        pools={
            _MAIN_POOL: SimpleNamespace(
                pool=main_b.instance.pool,
                session_binding_store=main_b.instance.session_binding_store,
                tree_manager=main_b.instance.tree_manager,
            ),
            _PEER_POOL: SimpleNamespace(
                pool=peer_b.instance.pool,
                session_binding_store=peer_b.instance.session_binding_store,
                tree_manager=peer_b.instance.tree_manager,
            ),
        },
        pool_data={_MAIN_POOL: main_b.pool_data, _PEER_POOL: peer_b.pool_data},
        graph_orchestrator=SimpleNamespace(
            get_graph_context=lambda gid: env.graph_ctx_holder.get("ctx")
        ),
    )
    env.cell.resolve_workspace = lambda: env._resources  # type: ignore[method-assign]
    return env


async def _teardown_env(env: _NestedTreeEnv) -> None:
    for build in env.builds.values():
        await build.instance.pool.shutdown_all()
        await build.broker.stop()
        await build.pool_data.context_manager.memory_system.close()


async def _load_registry(provider: _NestedScriptedProvider) -> ComponentRegistry:
    """The production factory set with the scripted provider PRE-REGISTERED
    as ``bot_default`` (a direct registration occupies the name first —
    the plugin's later same-name registration is skipped with a warning,
    O2 source rules)."""
    registry = ComponentRegistry()
    registry.register(
        ComponentSlot.LLM_PROVIDER,
        _BOT_DEFAULT_LLM_PROVIDER,
        SimpleFactory(provider, _ScriptedProviderConfig),
    )
    bot_base = Path(__file__).resolve().parents[2]
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(bot_base / "plugins",),
        ),
    )
    return registry


def _ws_ctx(root: Path) -> WorkspaceContext:
    return WorkspaceContext(
        target=root, paths=WorkspacePaths(root=root / ".modex"), is_home=False
    )


async def _wait_for(predicate, what: str, timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"condition not reached within {timeout}s: {what}")


async def _wait_stable(env: _NestedTreeEnv, quiet: float = 1.5) -> None:
    """Wait until the provider stops being called for a quiet window —
    the dispatch/backflow chain has settled."""
    deadline = asyncio.get_running_loop().time() + 30.0
    while asyncio.get_running_loop().time() < deadline:
        before = env.provider.total_calls()
        await asyncio.sleep(quiet)
        if env.provider.total_calls() == before:
            return
    raise AssertionError("chain did not settle within 30s")


async def _submit_user_message(env: _NestedTreeEnv, pool: str, agent: str) -> str:
    build = env.builds[pool]
    session = SessionIdFactory().create(agent, external_id="conv-e2e")
    registry = build.instance.pool.session_registry
    assert registry is not None
    await registry.register(session)
    await build.instance.pool.submit_input(
        session.session_id,
        InputMessage(content=f"user message for {agent}", session=session),
    )
    return session.session_id


def _tool_of(instance, name: str):
    if instance.pipeline is None:
        return None
    tm = instance.pipeline._turn_runner.tool_manager  # noqa: SLF001
    return tm.get_tool(name) if tm is not None else None


# ── Scenario 1: three-level dispatch chain + session tree ───────────────


async def test_three_level_dispatch_and_backflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC (a)/(b)/(c): root dispatches mid; mid (a SUBAGENT — non-main
    dispatcher) dispatches leaf; results flow back up both levels; the
    session tree records the full chain with invocation branches and
    strictly two-segment session ids."""
    env = await _build_env(tmp_path, monkeypatch)
    try:
        build = env.builds[_MAIN_POOL]
        root_sid = await _submit_user_message(env, _MAIN_POOL, _ROOT)

        # mid's SECOND turn runs only after the leaf's notification came
        # back — that is the leaf→mid backflow half of the chain.
        await _wait_for(
            lambda: env.provider.calls["mid"] >= 2, what="mid backflow turn"
        )
        await _wait_stable(env)

        # AC (b): the root's task tool lists ONLY its direct child (mid —
        # never the grandchild leaf); the mid's lists ONLY the leaf; the
        # leaf has NO task tool at all (not empty-enabled).
        root_task = build.instance.tool_manager.get_tool("task")
        assert root_task is not None
        assert [t.name for t in root_task.list_targets()] == [_MID]

        mid_instance = build.instance.pool.get(_MID)
        leaf_instance = build.instance.pool.get(_LEAF)
        assert mid_instance is not None, "mid never materialized"
        assert leaf_instance is not None, "leaf never materialized"
        mid_task = _tool_of(mid_instance, "task")
        assert mid_task is not None, "mid-level agent has no task tool"
        assert [t.name for t in mid_task.list_targets()] == [_LEAF]
        assert _tool_of(leaf_instance, "task") is None

        # AC (c): session tree records the full chain with invocation
        # branches; every session id is strictly two segments and the
        # invocation id is the PREFIX (SPEC §13 Errata-1).
        node_store = build.instance.tree_manager._node_store  # noqa: SLF001
        root_node = await node_store.get(root_sid)
        assert root_node is not None
        assert root_node.parent_session_id is None

        tree_id = await build.instance.tree_manager.tree_id_for_session(root_sid)
        assert tree_id is not None
        records = {
            r.agent_name: r for r in await node_store.get_tree_node_records(tree_id)
        }
        assert set(records) == {_ROOT, _MID, _LEAF}

        mid_node = records[_MID]
        leaf_node = records[_LEAF]
        mid_sid = mid_node.session_id
        leaf_sid = leaf_node.session_id
        assert mid_node.parent_session_id == root_sid  # mid hangs off root
        assert leaf_node.parent_session_id == mid_sid  # leaf hangs off mid

        # Two segments exactly — no third invocation segment (Errata-1);
        # the invocation id is the PREFIX (the branch identity).
        assert mid_sid.count(".") == 1
        assert leaf_sid.count(".") == 1
        assert mid_sid.split(".")[1] == _MID
        assert leaf_sid.split(".")[1] == _LEAF
        assert mid_sid.split(".")[0] != root_sid.split(".")[0]
        assert leaf_sid.split(".")[0] != mid_sid.split(".")[0]
    finally:
        await _teardown_env(env)


# ── Scenario 2: peer link to the other pool's root ──────────────────────


async def test_peer_link_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root messages the peer pool's root via ``send_to_peer``; the
    peer replies; the reply lands back on the sender's session prefix
    (session group, ADR-0019) and drives the sender's next turn."""
    env = await _build_env(tmp_path, monkeypatch, root_sends_peer=True)
    try:
        build = env.builds[_MAIN_POOL]
        peer_build = env.builds[_PEER_POOL]
        root_sid = await _submit_user_message(env, _MAIN_POOL, _ROOT)

        # Root turn 1 sends the peer message; the peer's root replies; the
        # reply returns to the root's session and drives its next turn.
        await _wait_for(
            lambda: env.provider.calls["peer"] >= 1, what="peer root turn"
        )
        await _wait_for(
            lambda: env.provider.calls["root"] >= 3, what="root consumed peer reply"
        )
        await _wait_stable(env)

        # The peer session is a ROOT node in the PEER pool's tree, keyed
        # by the sender's prefix (session group): {conv}.peerroot.
        peer_tree = await peer_build.instance.tree_manager.tree_id_for_session(
            f"{root_sid.split('.')[0]}.{_PEER_ROOT}"
        )
        assert peer_tree is not None, "peer session never reached the peer tree"

        # The main-pool root's task tool still lists only mid — peer
        # targets never leak into the dispatch surface.
        root_task = build.instance.tool_manager.get_tool("task")
        assert root_task is not None
        assert [t.name for t in root_task.list_targets()] == [_MID]
    finally:
        await _teardown_env(env)


# ── Scenario 3: graph referencing the never-dispatched lazy leaf ────────


def _compile(g: Graph[DefaultGraphState]) -> Any:
    return g.compile(
        max_iterations=15,
        cycle_detection="off",
        scheduler=SchedulerKind.PARALLEL,
        default_trigger=NodeTrigger.ON_ALL_PREDS,
    )


def _make_coordinator(compiled: Any) -> GraphPersistenceCoordinator:
    coord = GraphPersistenceCoordinator(
        graph_instance_id=0,
        instance_store=NullGraphInstanceStore(),
        node_state_store=InMemoryNodeStateStore(0),
        default_deliver_store_factory=NullDeliverStoreFactory(),
    )
    if compiled is not None:
        for node in compiled.nodes.values():
            coord.register_node(node.node_id)
    return coord


async def _run_graph(compiled: Any, env: _NestedTreeEnv, user_input: str) -> Any:
    ctx = GraphContext(
        state=DefaultGraphState(),
        runtime=GraphRuntime(),
        coordinator=_make_coordinator(compiled),
        user_input=GraphPayload(content=user_input),
        scheduler_kind=SchedulerKind.PARALLEL,
        graph_instance_id=0,
    )
    env.graph_ctx_holder["ctx"] = ctx
    engine = GraphEngine(compiled)
    return await engine.run_async(ctx, mode=BootstrapMode.FRESH)


async def test_graph_references_never_dispatched_lazy_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The graph references the leaf — a never-dispatched lazy subagent.
    It cold-starts from its template (ticket 08 path), its graph node turn
    receives the deliver tool through the SAME materialization wiring the
    main agent gets (ticket 12's shared trio), and the delivered result
    flows back through ``__end__``."""
    env = await _build_env(tmp_path, monkeypatch)
    try:
        build = env.builds[_MAIN_POOL]
        assert build.instance.pool.get(_LEAF) is None, (
            "leaf must start never-dispatched (lazy)"
        )

        leaf_node = BotAgentNode(_LEAF, _MAIN_POOL, env.cell)  # type: ignore[arg-type]
        g: Graph[DefaultGraphState] = Graph("nested_leaf_flow")
        g.add_node("leaf", leaf_node)
        g.add_edge(GraphNode.START, "leaf")
        g.add_edge("leaf", GraphNode.END)
        compiled = _compile(g)

        result = await _run_graph(compiled, env, "summarize via the leaf")

        # The leaf cold-started from its template and ran its graph turn.
        leaf_instance = build.instance.pool.get(_LEAF)
        assert leaf_instance is not None, "lazy leaf never materialized"
        assert leaf_instance.descriptor.comm_kind == AgentCommKind.SUBAGENT
        assert env.provider.graph_delivers >= 1

        # The delivered content flowed back through END into the result.
        assert result is not None and result.result is not None
        assert any(
            _LEAF_GRAPH_RESULT in (p.content or "") for p in result.result
        ), f"leaf deliver did not flow back through END: {result.result}"

        # Mode neutrality: the materialized instance carries no graph
        # state — the deliver tool lives in per-turn artifacts only.
        tm = leaf_instance.pipeline._turn_runner.tool_manager  # noqa: SLF001
        assert tm is not None and tm.get_tool("deliver") is None
        assert leaf_node._session is not None  # noqa: SLF001
        assert build.instance.session_binding_store is not None
        assert (
            build.instance.session_binding_store.get(
                leaf_node._session.session_id  # noqa: SLF001
            )
            is None
        ), "graph binding must be unbound after the node turn"
    finally:
        await _teardown_env(env)
