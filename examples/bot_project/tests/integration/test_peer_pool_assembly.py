"""Integration tests for cross-pool peer target assembly (ADR-0019 T6)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.core import BotService

from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.emitter import StreamingAwareEmitter
from modex_agent.core.provider import LLMProvider
from modex_agent.core.types import LLMResponse, OutputMessage
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.pipeline.adapters import OutputAdapter

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fakes — scripted LLM provider + recording output adapter
# ---------------------------------------------------------------------------


class _ScriptedProvider(LLMProvider):  # type: ignore[misc]
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_default_model(self) -> str:
        return "dummy-mini"

    async def chat(
        self,
        messages: list[Any],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: object,
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="ok")


class _RecordingOutputAdapter(OutputAdapter):  # type: ignore[misc]
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "recording"

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.NONE

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.sent.append((session_id, message.content or ""))

    async def send_delta(self, delta: str, session_id: str, metadata: object = None) -> None: ...
    async def flush_deltas(self, session_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _write_minimal_config(project_dir: Path) -> None:
    config_dir = project_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "agents").mkdir(parents=True, exist_ok=True)

    (config_dir / "bot_config.yml").write_text(
        """
safety:
  llm: {request_timeout: 45.0, stream_idle_timeout: 90.0, max_retries: 1, retry_backoff: [2.0, 8.0]}
  turn: {agent_run_timeout: 60.0, hook_timeout: 10.0, tool_timeout: 30.0}
paths:
  data_dir_name: ".modex"
workspace:
  enabled: true
""",
        encoding="utf-8",
    )

    (config_dir / "model.yml").write_text(
        """
default_provider: dummy
default_model: dummy-mini
max_context_tokens: 32000
providers:
  - key: dummy
    name: dummy
    url: http://localhost
    api_key: dummy
    models:
      - name: dummy-mini
        model: openai/dummy-mini
        capabilities: [text]
        temperature: 0.7
        max_output_tokens: 1000
""",
        encoding="utf-8",
    )

    mcp_dir = config_dir / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "registry.json").write_text('{"sharedRegistry": false}', encoding="utf-8")


def _write_pool(
    project_dir: Path,
    pool_name: str,
    peers: list[str],
    main_agent_name: str | None = None,
) -> None:
    pool_dir = project_dir / "config" / "pools" / pool_name
    pool_dir.mkdir(parents=True, exist_ok=True)
    (pool_dir / "templates").mkdir(exist_ok=True)

    name = main_agent_name if main_agent_name is not None else pool_name
    data: dict[str, Any] = {
        "max_steps": 5,
        "use_terminal": False,
        "tool_preset": "minimal",
        "main_agent_name": name,
    }
    if peers:
        data["peers"] = peers
    (pool_dir / "pool.yml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )

    (pool_dir / "templates" / "helper.yml").write_text(
        yaml.safe_dump(
            {"agent_name": f"{pool_name}-helper", "max_steps": 5}, sort_keys=False
        ),
        encoding="utf-8",
    )

    (project_dir / "agents" / f"{name}.md").write_text("prompt", encoding="utf-8")


async def _build_and_initialize_service(
    project_dir: Path, app_config: AppConfig
) -> BotService:
    input_adapter = WebSocketInputAdapter()
    output_adapter = _RecordingOutputAdapter()

    def emitter_factory(session_id: str) -> StreamingAwareEmitter:
        return StreamingAwareEmitter(output_adapter, session_id)

    service = BotService(
        config_dir=project_dir / "config",
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        emitter_factory=emitter_factory,
        app_config=app_config,
    )
    await service.initialize()
    return service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_targets_wired_after_assembly(tmp_path: Path) -> None:
    """Two peered pools see each other's main agent as a NORMAL peer target."""
    _write_minimal_config(tmp_path)
    _write_pool(tmp_path, "alpha", ["beta"])
    _write_pool(tmp_path, "beta", ["alpha"])

    app_config = AppConfig.from_yaml(tmp_path / "config" / "bot_config.yml")
    provider = _ScriptedProvider()

    import bot.service.core as core_mod
    import bot.service.pool_builder as pool_builder_mod

    original_llm_provider = pool_builder_mod._build_llm_provider
    original_default_provider = core_mod.BotService._build_default_provider
    original_project_dir = core_mod.BotService._project_dir

    pool_builder_mod._build_llm_provider = lambda *a, **k: provider
    core_mod.BotService._build_default_provider = lambda self: provider  # type: ignore[method-assign]
    core_mod.BotService._project_dir = property(lambda self: tmp_path)  # type: ignore[assignment, method-assign]

    service: BotService | None = None
    try:
        service = await _build_and_initialize_service(tmp_path, app_config)
        resources = service._home_resources

        pool_alpha = resources.pools["alpha"]
        pool_beta = resources.pools["beta"]

        # Pool alpha's store has a peer target for beta's main agent.
        target_beta = pool_alpha.target_store.get("beta")
        assert target_beta is not None
        assert target_beta.name == "beta"
        assert target_beta.kind == AgentCommKind.NORMAL
        assert target_beta.pool_name == "beta"
        assert target_beta.bus_ref is pool_beta.agent_bus

        # Pool beta's store has a reciprocal peer target for alpha's main agent.
        target_alpha = pool_beta.target_store.get("alpha")
        assert target_alpha is not None
        assert target_alpha.name == "alpha"
        assert target_alpha.kind == AgentCommKind.NORMAL
        assert target_alpha.pool_name == "alpha"
        assert target_alpha.bus_ref is pool_alpha.agent_bus

        # Subagent targets (Phase 1) precede peer targets (Phase 2) in list order.
        alpha_names = [t.name for t in pool_alpha.target_store.list()]
        assert alpha_names.index("alpha-helper") < alpha_names.index("beta")
        beta_names = [t.name for t in pool_beta.target_store.list()]
        assert beta_names.index("beta-helper") < beta_names.index("alpha")
    finally:
        pool_builder_mod._build_llm_provider = original_llm_provider
        core_mod.BotService._build_default_provider = original_default_provider  # type: ignore[method-assign]
        core_mod.BotService._project_dir = original_project_dir  # type: ignore[method-assign]
        if service is not None and service.workspace_stack is not None:
            with __import__("contextlib").suppress(BaseException):
                await service.workspace_stack.registry.evict_all()


@pytest.mark.asyncio
async def test_duplicate_peer_main_agent_name_raises(tmp_path: Path) -> None:
    """When two peers share a main agent name, Phase 2 add() raises ValueError."""
    _write_minimal_config(tmp_path)
    _write_pool(tmp_path, "alpha", ["beta", "gamma"])
    _write_pool(tmp_path, "beta", ["alpha"])
    # gamma's main agent is also named "beta", creating a duplicate in alpha's store.
    _write_pool(tmp_path, "gamma", ["alpha"], main_agent_name="beta")

    app_config = AppConfig.from_yaml(tmp_path / "config" / "bot_config.yml")
    provider = _ScriptedProvider()

    import bot.service.core as core_mod
    import bot.service.pool_builder as pool_builder_mod

    original_llm_provider = pool_builder_mod._build_llm_provider
    original_default_provider = core_mod.BotService._build_default_provider
    original_project_dir = core_mod.BotService._project_dir

    pool_builder_mod._build_llm_provider = lambda *a, **k: provider
    core_mod.BotService._build_default_provider = lambda self: provider  # type: ignore[method-assign]
    core_mod.BotService._project_dir = property(lambda self: tmp_path)  # type: ignore[assignment, method-assign]

    service: BotService | None = None
    try:
        service = await _build_and_initialize_service(tmp_path, app_config)
        # initialize() should have raised; reaching here is a failure.
        raise AssertionError("Expected ValueError during peer target assembly")
    except ValueError as exc:
        assert "Duplicate communication target name" in str(exc)
    finally:
        pool_builder_mod._build_llm_provider = original_llm_provider
        core_mod.BotService._build_default_provider = original_default_provider  # type: ignore[method-assign]
        core_mod.BotService._project_dir = original_project_dir  # type: ignore[method-assign]
        if service is not None and service.workspace_stack is not None:
            with __import__("contextlib").suppress(BaseException):
                await service.workspace_stack.registry.evict_all()


__all__ = ["test_peer_targets_wired_after_assembly", "test_duplicate_peer_main_agent_name_raises"]
