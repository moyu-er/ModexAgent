from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bot.adapters import channels
from bot.adapters.web_socket import WebSocketInputAdapter, WebSocketOutputAdapter
from bot.graph.agent_node import BotAgentNode
from bot.graph.agent_node_factory import BotAgentNodeFactory
from bot.service.core import BotService
from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.web_ui_service import WebUIService
from bot.webui.emitter import WebBotEmitter
from bot.workspace.handle import PoolWorkspaceResources, WorkspaceResolverCell

from modex_agent.core.emitter import StreamingAwareEmitter
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
from modex_agent.pipeline.adapters import OutputAdapter
from modex_graph.spec import NodeSpec

from ..declaration_driver import build_declared

_POOL_DECLARATION = """\
pool:
  name: graph-pool
  agents:
    main:
      description: graph test root
      toolset: none
"""

_POOL_NAME = "graph-pool"
_SESSION_ID = "same-prefix.main"


class _UnifiedFactoryCapturedError(RuntimeError):
    pass


async def test_graph_node_resolves_pool_assembled_emitter_with_node_pool(
    tmp_path: Path,
) -> None:
    resolver = WorkspaceResolverCell()
    input_adapter = WebSocketInputAdapter()
    web_output = WebSocketOutputAdapter(input_adapter)
    output_adapter = MagicMock(spec=OutputAdapter)

    def emitter_factory(session_id: str, pool: str) -> WebBotEmitter:
        return WebBotEmitter(web_output, session_id, pool=pool)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()
    pool_instance = None
    try:
        with patch.dict("os.environ", {"MODEXBOT_BIN_DIR": str(bin_dir)}):
            pool_instance = await create_pool(
                pool_name=_POOL_NAME,
                declared=build_declared(
                    _POOL_DECLARATION,
                    project_dir=tmp_path,
                    data_dir=tmp_path / ".modex",
                    pool_name=_POOL_NAME,
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
                workspace_resolver=resolver,
                emitter_factory=emitter_factory,
                bot_model_config=None,
                model_choice_registry=ModelChoiceRegistry(),
            )

        resources = MagicMock(spec=PoolWorkspaceResources)
        resources.pools = {_POOL_NAME: pool_instance}
        resolver.set(resources)
        node = BotAgentNodeFactory(resolver).create(
            NodeSpec(
                name="graph-agent",
                node_type="agent",
                config={"agent": "main", "pool": _POOL_NAME},
            )
        )
        assert isinstance(node, BotAgentNode)
        assert node._resolve_pool() is pool_instance

        materialize_deps = pool_instance.pool.materialize_deps
        assert materialize_deps is not None
        assert materialize_deps.emitter_factory is not None
        emitter = materialize_deps.emitter_factory(_SESSION_ID)
        assert isinstance(emitter, WebBotEmitter)
        assert emitter._pool == _POOL_NAME
    finally:
        if pool_instance is not None:
            await pool_instance.pool.shutdown_all()
        await broker.stop()


def test_unified_factory_forwards_same_pool_to_qq_and_telegram_leaves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[tuple[str, str]] = []

    def build_qq_leaf(_ctx):
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)

        def emitter_factory(session_id: str, pool: str) -> StreamingAwareEmitter:
            received.append(("qq", pool))
            return StreamingAwareEmitter(output_adapter, session_id)

        return input_adapter, output_adapter, emitter_factory

    def build_telegram_leaf(_ctx):
        input_adapter = WebSocketInputAdapter()
        output_adapter = WebSocketOutputAdapter(input_adapter)

        def emitter_factory(session_id: str, pool: str) -> StreamingAwareEmitter:
            received.append(("telegram", pool))
            return StreamingAwareEmitter(output_adapter, session_id)

        return input_adapter, output_adapter, emitter_factory

    def capture_unified_factory(
        _service,
        _config_dir,
        _input_adapter,
        _output_adapter,
        emitter_factory,
        **_kwargs,
    ) -> None:
        emitter_factory(_SESSION_ID, _POOL_NAME)
        raise _UnifiedFactoryCapturedError

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "bot_config.yml").write_text(
        "multi_agent: {}\npaths: {data_dir_name: .modex}\nworkspace: {enabled: false}\n",
        encoding="utf-8",
    )
    fallback_input = WebSocketInputAdapter()
    fallback_output = WebSocketOutputAdapter(fallback_input)
    from bot.adapters import register_websocket

    monkeypatch.setattr(register_websocket, "get_ws_output", lambda: fallback_output)
    monkeypatch.setattr(
        WebUIService,
        "_import_adapter_registration_modules",
        lambda _service, _channels: None,
    )
    monkeypatch.setattr(
        channels,
        "ADAPTERS",
        [
            SimpleNamespace(name="qq", enabled=True, build=build_qq_leaf),
            SimpleNamespace(
                name="telegram", enabled=True, build=build_telegram_leaf
            ),
        ],
    )
    monkeypatch.setattr(BotService, "__init__", capture_unified_factory)

    with pytest.raises(_UnifiedFactoryCapturedError):
        WebUIService(config_dir)

    assert received == [("qq", _POOL_NAME), ("telegram", _POOL_NAME)]
