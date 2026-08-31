"""Integration tests for cross-pool peer target assembly (ADR-0019 T6,
ticket 13).

Ticket 11 follow-up (2026-08-22): the fixtures originally wrote the legacy
``config/pools/<name>/pool.yml`` + ``templates/*.yml`` format, which ticket
11 deleted. They now write ONE scope declaration
(``config/scopes/bot.yml``) hosting every pool — the single boot road. The
assertion semantics are unchanged: Phase-2 peer wiring, target ordering,
loud failures, and the round-trip behavior. Where an error surface moved
(legacy format → declaration validation), the assertion targets the new
authority while keeping the loud-failure contract.
"""
from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.service.core import BotService

from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import StreamingAwareEmitter
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import LLMResponse, OutputMessage
from modex_agent.ioc.configs.app import AppConfig
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.pipeline.adapters import OutputAdapter

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fakes — scripted LLM provider + recording output adapter
# ---------------------------------------------------------------------------


class _ScriptedProvider(CallbackStreamProvider):  # type: ignore[misc]
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
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
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


def _write_minimal_config(project_dir: Path, declaration: str) -> None:
    config_dir = project_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "agents").mkdir(parents=True, exist_ok=True)

    # BIZ components (execution strategies: react/external) are directory-
    # discovered from <project>/plugins by BotService — a bootable project
    # carries the real plugin set, so the synthetic one must too.
    shutil.copytree(
        Path(__file__).resolve().parents[2] / "plugins",
        project_dir / "plugins",
        dirs_exist_ok=True,
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

    # Ticket 11: one scope declaration hosts every pool — the single boot
    # road (the legacy config/pools format is deleted).
    scopes_dir = config_dir / "scopes"
    scopes_dir.mkdir(parents=True, exist_ok=True)
    (scopes_dir / "bot.yml").write_text(declaration, encoding="utf-8")

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


def _peered_pools_declaration(
    pools: dict[str, tuple[str, list[str]]],
) -> str:
    """Render ``pool_name -> (root_agent_name, peers)`` as declaration YAML.

    Each pool is a root agent plus one nested subagent (``<pool>-helper``),
    matching the shape the legacy fixtures built with templates.
    """
    import yaml

    body: dict[str, Any] = {}
    for pool_name, (root_name, peers) in pools.items():
        body[pool_name] = {
            "peers": peers,
            "agents": {
                root_name: {
                    "description": f"{root_name} root",
                    "agents": {f"{pool_name}-helper": {}},
                }
            },
        }
    return yaml.safe_dump(
        {"workspace": {"name": "peer-ws", "pools": body}},
        sort_keys=False,
    )


def _write_agents(project_dir: Path, names: list[str]) -> None:
    for name in names:
        (project_dir / "agents" / f"{name}.md").write_text("prompt", encoding="utf-8")


async def _build_and_initialize_service(
    project_dir: Path, app_config: AppConfig
) -> BotService:
    input_adapter = WebSocketInputAdapter()
    output_adapter = _RecordingOutputAdapter()

    def emitter_factory(
        session_id: str, pool: str
    ) -> StreamingAwareEmitter:
        assert pool
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


async def _run_with_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declaration: str,
    agent_names: list[str],
    body,  # callable(service) -> Awaitable[None]
) -> None:
    """Shared harness: patch, boot, run ``body``, restore, stop."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))
    _write_minimal_config(tmp_path, declaration)
    _write_agents(tmp_path, agent_names)

    app_config = AppConfig.from_yaml(tmp_path / "config" / "bot_config.yml")
    provider = _ScriptedProvider()

    import bot.service.core as core_mod

    monkeypatch.setattr(
        core_mod.BotService, "_build_default_provider", lambda self: provider
    )
    monkeypatch.setattr(
        core_mod.BotService, "_project_dir", property(lambda self: tmp_path)
    )

    service: BotService | None = None
    try:
        service = await _build_and_initialize_service(tmp_path, app_config)
        await body(service)
    finally:
        # Full service stop, not bare evict_all(): the service-level
        # registry persistence (aiosqlite) is closed only in stop(), and
        # its non-daemon worker thread otherwise hangs interpreter exit.
        if service is not None:
            with contextlib.suppress(BaseException):
                await service.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_targets_wired_after_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two peered pools see each other's main agent as a NORMAL peer target."""

    async def body(service: BotService) -> None:
        resources = service._home_resources

        pool_alpha = resources.pools["alpha"]
        pool_beta = resources.pools["beta"]

        # Pool alpha's store has a peer target for beta's main agent.
        target_beta = pool_alpha.target_store.get("beta")
        assert target_beta is not None
        assert target_beta.name == "beta"
        assert target_beta.kind == AgentCommKind.NORMAL
        assert target_beta.pool_name == "beta"
        assert target_beta.tree_ref is pool_beta.tree_manager

        # Pool beta's store has a reciprocal peer target for alpha's main agent.
        target_alpha = pool_beta.target_store.get("alpha")
        assert target_alpha is not None
        assert target_alpha.name == "alpha"
        assert target_alpha.kind == AgentCommKind.NORMAL
        assert target_alpha.pool_name == "alpha"
        assert target_alpha.tree_ref is pool_alpha.tree_manager

        # Subagent targets (Phase 1) precede peer targets (Phase 2) in list order.
        alpha_names = [t.name for t in pool_alpha.target_store.list()]
        assert alpha_names.index("alpha-helper") < alpha_names.index("beta")
        beta_names = [t.name for t in pool_beta.target_store.list()]
        assert beta_names.index("beta-helper") < beta_names.index("alpha")

    await _run_with_service(
        tmp_path,
        monkeypatch,
        _peered_pools_declaration(
            {"alpha": ("alpha", ["beta"]), "beta": ("beta", ["alpha"])}
        ),
        ["alpha", "beta"],
        body,
    )


@pytest.mark.asyncio
async def test_duplicate_peer_main_agent_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two peers whose root agents share a name collide in the same target
    store — Phase 2 ``add()`` raises ValueError during boot (the loud
    failure the legacy format produced; the declaration face reaches the
    same store, since cross-pool root names need not be unique per V5)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))
    declaration = _peered_pools_declaration(
        {
            "alpha": ("alpha", ["beta", "gamma"]),
            "beta": ("beta", ["alpha"]),
            # gamma's root is ALSO named "beta" — a duplicate in alpha's store.
            "gamma": ("beta", ["alpha"]),
        }
    )
    _write_minimal_config(tmp_path, declaration)
    _write_agents(tmp_path, ["alpha", "beta"])

    app_config = AppConfig.from_yaml(tmp_path / "config" / "bot_config.yml")
    provider = _ScriptedProvider()

    import bot.service.core as core_mod

    monkeypatch.setattr(
        core_mod.BotService, "_build_default_provider", lambda self: provider
    )
    monkeypatch.setattr(
        core_mod.BotService, "_project_dir", property(lambda self: tmp_path)
    )

    service: BotService | None = None
    try:
        service = await _build_and_initialize_service(tmp_path, app_config)
        # initialize() should have raised; reaching here is a failure.
        raise AssertionError("Expected ValueError during peer target assembly")
    except ValueError as exc:
        assert "Duplicate communication target name" in str(exc)
    finally:
        if service is not None:
            with contextlib.suppress(BaseException):
                await service.stop()


@pytest.mark.asyncio
async def test_declaration_dangling_peer_fails_with_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declaration peer naming a pool that does not exist aborts startup
    with the same-workspace guidance (loud miss — no silent skip, no bare
    KeyError).

    Semantics evolution: ticket 13 tested this against the legacy
    ``pool.yml`` peer list (the phase-2 resolver's miss); ticket 11 deleted
    the legacy format, so the same-workspace invariant is now enforced
    EARLIER — by phase-1 V5 validation over the declaration, raised as
    :class:`ScopeBootError` (a ValueError) whose message carries the rule,
    the offending pool, the missing peer name, and the N5 guidance.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "modexctl.bat").write_text("@echo off\n", encoding="utf-8")
    monkeypatch.setenv("MODEXBOT_BIN_DIR", str(bin_dir))
    declaration = _peered_pools_declaration({"alpha": ("alpha", ["ghost"])})
    _write_minimal_config(tmp_path, declaration)
    _write_agents(tmp_path, ["alpha"])

    app_config = AppConfig.from_yaml(tmp_path / "config" / "bot_config.yml")
    provider = _ScriptedProvider()

    import bot.service.core as core_mod

    monkeypatch.setattr(
        core_mod.BotService, "_build_default_provider", lambda self: provider
    )
    monkeypatch.setattr(
        core_mod.BotService, "_project_dir", property(lambda self: tmp_path)
    )

    service: BotService | None = None
    try:
        service = await _build_and_initialize_service(tmp_path, app_config)
        raise AssertionError("Expected ScopeBootError during declaration validation")
    except ValueError as exc:
        assert "same-workspace only" in str(exc)
        assert "ghost" in str(exc)
    finally:
        if service is not None:
            with contextlib.suppress(BaseException):
                await service.stop()


# ---------------------------------------------------------------------------
# Peer resolution round trip (both pools on the declaration road)
# ---------------------------------------------------------------------------


def _write_bare_pool_declaration() -> str:
    """A subagent-free declaration: two peered root agents, no helpers."""
    return (
        "workspace:\n"
        "  name: bot\n"
        "  pools:\n"
        "    default:\n"
        "      peers: [review]\n"
        "      agents:\n"
        "        default:\n"
        "          description: the default root\n"
        "    review:\n"
        "      peers: [default]\n"
        "      agents:\n"
        "        reviewer:\n"
        "          description: the review root\n"
    )


async def _until(predicate, *, timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


def _peer_context(session_id: str) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id),
        comm_kind=AgentCommKind.NORMAL,
    )


@pytest.mark.asyncio
async def test_peer_resolution_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ticket 13 AC (c), post-ticket-11: the review↔default peer pair —
    originally spanning BOTH boot roads (default declared, review legacy)
    — now boots BOTH pools from the one declaration (the legacy road is
    deleted; a dual-boot shape no longer exists). The behavior is
    unchanged:

    - bidirectional registration: each store holds the peer's NORMAL
      target whose tree reference is the peer pool's OWN tree manager
      (resolved from the same workspace bundle by the FW service);
    - message round-trip: default→review lands on a ROOT session
      (parent null) reusing the sender's prefix; review→default reply
      lands back in default's inbox on the same prefix.
    """

    async def body(service: BotService) -> None:
        resources = service._home_resources
        assert resources is not None
        default_instance = resources.pools["default"]
        review_instance = resources.pools["review"]

        # Both pools booted the declaration road — ticket 11 made it the
        # single road (the legacy side of the old cross-road pair is gone).
        assert default_instance.comm_tools_derived is True
        assert review_instance.comm_tools_derived is True

        # Bidirectional registration: tree references resolve from the
        # SAME bundle (each peer's own tree manager).
        target_review = default_instance.target_store.get("reviewer")
        assert target_review is not None
        assert target_review.kind is AgentCommKind.NORMAL
        assert target_review.pool_name == "review"
        assert target_review.tree_ref is review_instance.tree_manager
        target_default = review_instance.target_store.get("default")
        assert target_default is not None
        assert target_default.kind is AgentCommKind.NORMAL
        assert target_default.pool_name == "default"
        assert target_default.tree_ref is default_instance.tree_manager

        # default → review: the receiving session is a ROOT session
        # (parent null) reusing the sender's prefix (ADR-0019).
        ack = await default_instance.communication_service.send_async(
            target=target_review,
            content="hello from default",
            invocation_id=None,
            context=_peer_context("convPeer.default"),
        )
        assert "Error" not in ack
        review_registry = review_instance.pool.session_registry
        assert review_registry is not None

        async def _review_session_registered() -> bool:
            return await review_registry.get("convPeer.reviewer") is not None

        await _until(_review_session_registered)
        received = await review_registry.get("convPeer.reviewer")
        assert received is not None
        assert received.parent_session_id is None
        assert received.session_id_prefix == "convPeer"

        # review → default: the reply lands back in default's inbox on
        # the same prefix (the session group).
        ack2 = await review_instance.communication_service.send_async(
            target=target_default,
            content="reply from review",
            invocation_id=None,
            context=_peer_context("convPeer.reviewer"),
        )
        assert "Error" not in ack2
        default_registry = default_instance.pool.session_registry
        assert default_registry is not None

        async def _default_session_registered() -> bool:
            return await default_registry.get("convPeer.default") is not None

        await _until(_default_session_registered)

    await _run_with_service(
        tmp_path,
        monkeypatch,
        _write_bare_pool_declaration(),
        ["default", "reviewer"],
        body,
    )


__all__ = [
    "test_peer_targets_wired_after_assembly",
    "test_duplicate_peer_main_agent_name_raises",
    "test_declaration_dangling_peer_fails_with_clear_message",
    "test_peer_resolution_round_trip",
]
