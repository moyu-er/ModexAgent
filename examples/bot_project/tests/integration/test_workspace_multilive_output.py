"""Multi-workspace end-to-end output delivery (regression for the silent
switched/new workspace bug).

Regression being locked down
----------------------------
Before the fix, ``BrokerBridgeService.start()`` (the loop that forwards agent
output published on a workspace's broker to the output adapter) was called ONLY
for the HOME workspace, in ``BotService.start()``. Lazily-materialized
workspaces — every workspace you ``/cd`` into or create after startup — had
their pools' output bridges built but NEVER started, so a turn ran but its
output never left that workspace's broker: the agent looked silent.

This test materializes the HOME workspace PLUS several non-home workspaces
through the REAL ``_build_resources`` path, drives one message per workspace
through the real workspace dispatcher, runs each turn against a scripted
(mocked) LLM provider, and asserts that EVERY workspace's reply reaches the
output adapter. The LLM is mocked; everything else (registry, factory,
create_pool, broker, bridge, InboxPoller, dispatcher, pool_router) is real.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from pathlib import Path
from typing import Any

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.core import BotService

from modex_agent.adapters.emitter import StreamingAwareEmitter
from modex_agent.adapters.output import OutputAdapter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.types import InputMessage, LLMResponse, OutputMessage
from modex_agent.ioc.configs.app import AppConfig

pytestmark = pytest.mark.integration

# Number of NON-home workspaces to materialize and drive (home is +1).
NUM_EXTRA_WORKSPACES = 3


# ---------------------------------------------------------------------------
# Fakes — scripted LLM provider + recording output adapter
# ---------------------------------------------------------------------------


def _last_user_content(messages: list[Any]) -> str:
    """Best-effort extract the last real user message content from a chat history.

    Skips ``<system-reminder>`` injected by RuntimeProvider so the echo reflects
    the user's actual input, not framework-injected metadata.
    """
    last = ""
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
        if role == "user" and content:
            text = str(content)
            if text.startswith("<system-reminder>"):
                continue
            last = text
    return last


class _ScriptedProvider(CallbackStreamProvider):
    """Echoes the last user message back as the assistant reply.

    Subclasses ``CallbackStreamProvider`` so it rides the callback→event bridge and
    memory summarizer (ArchiveSummarizer). One shared instance serves every
    pool/workspace/turn. ``calls`` counts ``chat`` invocations so the test can
    assert a turn actually ran per workspace.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_default_model(self) -> str:
        return "dummy-mini"

    async def chat_stream(
        self,
        messages: list[Any],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: object,
    ) -> LLMResponse:
        self.calls += 1
        content = _last_user_content(messages)
        return LLMResponse(content=f"echo:{content}" if content else "echo:ok")


class _RecordingOutputAdapter(OutputAdapter):
    """Non-streaming adapter that records every ``(session_id, content)`` send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "recording"

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.PSEUDO

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: OutputMessage, session_id: str) -> None:
        self.sent.append((session_id, message.content or ""))

    async def send_delta(
        self,
        delta: str,
        session_id: str,
        metadata: dict[str, object] | None = None,
    ) -> None: ...
    async def flush_deltas(self, session_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Minimal on-disk config (real AppConfig.from_yaml path)
# ---------------------------------------------------------------------------


def _write_minimal_config(project_dir: Path) -> Path:
    """Write a one-pool (``main``) workspace-enabled config tree under tmp."""
    config_dir = project_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "agents").mkdir(parents=True, exist_ok=True)

    # BIZ components (execution strategies: react/external) are directory-
    # discovered from <project>/plugins by BotService — a bootable project
    # carries the real plugin set, so the synthetic one must too.
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "plugins",
        project_dir / "plugins",
    )

    (project_dir / "agents" / "main.md").write_text(
        "You are a helpful assistant. Reply briefly.\n", encoding="utf-8"
    )

    (config_dir / "bot_config.yml").write_text(
        """
safety:
  llm: {request_timeout: 45.0, stream_idle_timeout: 90.0, max_retries: 1, retry_backoff: [2.0, 8.0]}
  turn: {hook_timeout: 10.0, tool_timeout: 30.0}
paths:
  data_dir_name: ".modex"
""",
        encoding="utf-8",
    )

    # Ticket 14: the workspace layer of the scope declaration selects the
    # multi-live stack (N15 — workspace.enabled is dead). Ticket 11: the
    # declared pool boots the declaration road (every declared pool does).
    (config_dir / "scopes").mkdir(parents=True, exist_ok=True)
    (config_dir / "scopes" / "bot.yml").write_text(
        """
workspace:
  name: multilive-test
  pools:
    main:
      agents:
        main:
          description: test main agent
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

    main_pool_dir = config_dir / "pools" / "main"
    main_pool_dir.mkdir(parents=True, exist_ok=True)
    (main_pool_dir / "pool.yml").write_text(
        """
max_steps: 5
use_terminal: false
tool_preset: read_only
""",
        encoding="utf-8",
    )

    # Disable the shared MCP registry and define no servers, so materialize
    # spawns zero MCP subprocesses (the pool's mcp selection is empty too).
    mcp_dir = config_dir / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "registry.json").write_text('{"sharedRegistry": false}', encoding="utf-8")

    return config_dir


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_materialized_workspace_delivers_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home + N non-home workspaces: each turn's output reaches the adapter."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))
    config_dir = _write_minimal_config(tmp_path)
    app_config = AppConfig.from_yaml(config_dir / "bot_config.yml")

    input_adapter = WebSocketInputAdapter()
    output_adapter = _RecordingOutputAdapter()

    def emitter_factory(session_id: str, pool: str) -> StreamingAwareEmitter:
        assert pool == "main"
        return StreamingAwareEmitter(output_adapter, session_id)

    service = BotService(
        config_dir=config_dir,
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        emitter_factory=emitter_factory,
        app_config=app_config,
    )
    # Base BotService does not set _transcript_store (WebUIService-only), but
    # _build_resources reads it — provide None so materialize works for home too.
    service._transcript_store = None

    provider = _ScriptedProvider()

    # Only the service default provider (memory summarizer / background) is
    # mocked: the pool-level LLM_PROVIDER slot resolves through the registry
    # (bot_default → BotModelProvider over the dummy model.yml).
    import bot.service.core as core_mod

    original_default_provider = core_mod.BotService._build_default_provider
    core_mod.BotService._build_default_provider = lambda self: provider  # type: ignore[assignment]

    # _project_dir is hard-coded to the bot project source tree; repoint it at
    # the temp dir so home / agents / MCP / pool-session-store are all isolated
    # under tmp (no real MCP subprocesses, no writing into the repo).
    original_project_dir = core_mod.BotService._project_dir
    core_mod.BotService._project_dir = property(lambda self: tmp_path)  # type: ignore[assignment]

    # Pool turns resolve their LLM through the registry (bot_default →
    # BotModelProvider over the dummy model.yml URL), which the service-level
    # provider patch above never reaches — echo at the provider class instead.
    from bot.service.model_provider import BotModelProvider

    original_chat_stream = BotModelProvider.chat_stream

    async def _echo_chat_stream(
        self: BotModelProvider,
        messages: list[Any],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Any = None,
        on_reasoning_delta: Any = None,
        **kwargs: object,
    ) -> Any:
        del model, temperature, max_output_tokens, tools, on_reasoning_delta, kwargs
        provider.calls += 1
        content = _last_user_content(messages)
        text = f"echo:{content}" if content else "echo:ok"
        if on_content_delta is not None:
            await on_content_delta(text)
        return LLMResponse(content=text)

    BotModelProvider.chat_stream = _echo_chat_stream  # type: ignore[method-assign]

    try:
        await service.initialize()
        registry_db = tmp_path / app_config.paths.data_dir_name / "_registry" / "state.db"
        assert registry_db.exists()
        assert service.workspace_stack.store is service._registry_persistence.store

        # HOME is materialized by initialize(); its bridges must be running too.
        home_resources = service._home_resources
        for pi in home_resources.pools.values():
            assert pi.broker_bridge._tasks, "home pool bridge must be running"

        registry = service.workspace_stack.registry

        # Pre-create N non-home workspace target dirs.
        ws_targets = [tmp_path / f"ws{i}" for i in range(NUM_EXTRA_WORKSPACES)]
        for ws in ws_targets:
            ws.mkdir()

        # Start the dispatcher (what production BotService.start() runs) so the
        # real resolve -> materialize -> route -> submit_input -> poller -> turn
        # chain executes per message. Bridges start inside _build_resources.
        await input_adapter.start()
        router_task = asyncio.create_task(service.workspace_stack.dispatcher.run())

        session_factory = SessionIdFactory()
        marker = [f"ws{i}-hi" for i in range(NUM_EXTRA_WORKSPACES)]

        # Push one message per non-home workspace, each carrying its workspace.
        for i, ws in enumerate(ws_targets):
            session = session_factory.create(agent_name="main", external_id=f"ws{i}")
            msg = InputMessage(
                content=marker[i],
                session=session,
                workspace=ws,
                channel="test",
            )
            input_adapter.put_input_message(msg)

        # Wait until every workspace's echoed reply lands in the adapter.
        async def _wait_for_all(timeout: float = 30.0) -> None:
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                contents = [c for _, c in output_adapter.sent]
                if all(f"echo:{m}" in contents for m in marker):
                    return
                await asyncio.sleep(0.1)
            raise AssertionError(
                f"Timed out waiting for replies. Got sends: {output_adapter.sent!r} "
                f"(provider.calls={provider.calls})"
            )

        await _wait_for_all()

        # 1. Every non-home workspace was materialized (cached in the registry).
        resolved_targets = {Path(ws).resolve() for ws in ws_targets}
        cached_targets = {
            Path(t).resolve()
            for t in registry._resources  # type: ignore[attr-defined]
        }
        assert resolved_targets.issubset(cached_targets), (
            f"Not all workspaces materialized: missing={resolved_targets - cached_targets}"
        )

        # 2. EVERY materialized workspace's pool bridge is running — the fix.
        for resources in registry.iter_materialized_resources():
            for pi in resources.pools.values():
                assert pi.broker_bridge._tasks, (
                    f"workspace {resources.target} pool bridge not running (the silent-switch bug)"
                )

        # 3. Each workspace's distinct reply was delivered end-to-end.
        contents = [c for _, c in output_adapter.sent]
        for m in marker:
            assert f"echo:{m}" in contents, f"reply {m!r} not delivered; got {contents!r}"

    finally:
        if "router_task" in locals():
            router_task.cancel()
            with contextlib.suppress(BaseException):
                await router_task
        # Full service stop, not bare evict_all(): the service-level registry
        # persistence (aiosqlite) is closed only in stop(), and its non-daemon
        # worker thread otherwise hangs interpreter exit.
        with contextlib.suppress(BaseException):
            await service.stop()
        core_mod.BotService._build_default_provider = original_default_provider  # type: ignore[assignment]
        core_mod.BotService._project_dir = original_project_dir  # type: ignore[assignment]
        BotModelProvider.chat_stream = original_chat_stream  # type: ignore[method-assign]
