"""PA-07 runtime-vs-declared gap: skipped external pools are UNAVAILABLE.

Real build shape: an external pool whose provider CLI is missing is declared
but its registration is SKIPPED at assembly (``ProviderUnavailableError`` in
``bot/service/pool/factory.py``) — it never enters the materialized
workspace's ``resources.pools``. The new-conversation selection must read
RUNNING pools from the service's materialized resources (not the declared
YAML), so a preference pointing at the skipped pool yields a clear
selection-needed error on REST and WS — never a silent substitute and never
a queue-into-unknown-pool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.config.domains.personal_assistant import PersonalAssistantPreferences
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.server import WebUIServer

from modex_agent.multi_agent.pool_router import LocalFilePoolRoutingStore


@dataclass
class _FakePoolInstance:
    """Minimal pool-instance stand-in keyed in ``resources.pools``."""

    root_agent_name: str = "agent"
    subagent_count: int = 0


@dataclass
class _FakeResources:
    """Materialized-workspace bundle shape consumed by the selection path."""

    target: Path
    # RUNNING pools only — the external pool skipped at assembly is absent,
    # exactly like the real PoolWorkspaceResources.pools after a
    # ProviderUnavailableError skip.
    pools: dict[str, _FakePoolInstance] = field(default_factory=dict)


class _FakeRegistry:
    """iter_materialized_resources owner (ScopeRegistry shape)."""

    def __init__(self, resources: _FakeResources) -> None:
        self._resources = resources

    def iter_materialized_resources(self):
        yield self._resources


def _make_service(tmp_path: Path, running: set[str]) -> object:
    """A REAL BotService object with materialized resources of the given
    RUNNING pools (home = tmp_path). Declared pools are supplied via the
    loaded scope spec — here represented by the real declaration loader
    contract through the service's _declared_pool_names input: we set the
    spec-like object the property reads.
    """
    from bot.service.core import BotService

    service = BotService.__new__(BotService)  # no adapter wiring needed
    resources = _FakeResources(
        target=tmp_path,
        pools={name: _FakePoolInstance() for name in running},
    )

    class _Stack:
        def __init__(self, registry: _FakeRegistry) -> None:
            self.registry = registry

    service.workspace_stack = _Stack(_FakeRegistry(resources))
    service._home_resources = resources

    from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec, WorkspaceSpec

    service._scope_spec = ScopeSpec(kind=ScopeKind.WORKSPACE, workspace=WorkspaceSpec(
        name="test", pools=[PoolSpec(name=name, agents=[AgentSpec(name=name)]) for name in ("default", "external")],
    ))
    service._personal_preferences = PersonalAssistantPreferences(
        tmp_path / "personal_assistant.yml"
    )
    return service


def _server_from_service(service: object, tmp_path: Path) -> WebUIServer:
    """Wire a WebUIServer the production way: ONE prefs object + the
    service's RUNNING-pool provider (not a declared-list lambda)."""
    inp = WebSocketInputAdapter()
    store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
    server = WebUIServer(
        inp,
        store,
        static_dist=None,
        home_sessions_dir=tmp_path / ".modex",
    )
    server.set_data_dir_name(".modex")
    server.set_pool_agent_names(["default"])
    server.set_agent_resolver(lambda pool_name: pool_name)
    server.set_personal_assistant_preferences(service.personal_preferences)  # type: ignore[attr-defined]
    server.set_available_pools_provider(service._running_pool_keys)  # type: ignore[attr-defined]
    routing = LocalFilePoolRoutingStore(tmp_path / "routing")
    server.set_pool_switch_callback(routing.set_pool)
    return server


@pytest.mark.asyncio
async def test_preference_pointing_at_skipped_external_pool_is_unavailable(
    tmp_path: Path,
) -> None:
    """Declaration carries 'external'; its CLI is missing so assembly skipped
    it — only 'default' is running. A saved preference for 'external' must
    produce selection-needed (REST 409 / WS error), never a substitute and
    never a queue-into-unknown-pool."""
    service = _make_service(tmp_path, running={"default"})
    prefs: PersonalAssistantPreferences = service.personal_preferences  # type: ignore[assignment]
    prefs.save(default_workspace=None, default_pool="external")

    # service-level decision reads RUNNING pools from materialized resources
    assert service._running_pool_keys() == {"default"}  # type: ignore[attr-defined]
    assert service._default_pool_name is None  # type: ignore[attr-defined]

    server = _server_from_service(service, tmp_path)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        # REST: clear 409 with the reason
        resp = await client.post("/api/sessions", json={})
        assert resp.status == 409
        assert "external" in (await resp.json())["error"]

        # WS attach (new conversation, no pool): clear error envelope
        ws = await client.ws_connect("/ws")
        await ws.send_json({"action": "attach", "uuid_prefix": "web-g"})
        err = await ws.receive_json()
        assert err["event_type"] == "error"
        assert "external" in err["payload"]["message"]
        # nothing routed anywhere
        assert server._resolve_pool_by_prefix("web-g") is None
        await ws.close()

        # Explicit valid choice still works; explicit unknown still 400
        resp = await client.post("/api/sessions", json={"pool": "default"})
        assert resp.status == 200
        resp = await client.post("/api/sessions", json={"pool": "external"})
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_running_preference_selects_after_cli_returns(
    tmp_path: Path,
) -> None:
    """The running set is re-read per call (existing owner refresh): once the
    external pool's assembly succeeds (pool present in materialized
    resources), the same saved preference starts selecting it — no restart,
    no cache."""
    service = _make_service(tmp_path, running={"default"})
    prefs: PersonalAssistantPreferences = service.personal_preferences  # type: ignore[assignment]
    prefs.save(default_workspace=None, default_pool="external")
    assert service._default_pool_name is None  # type: ignore[attr-defined]

    # CLI installed + pool assembled (materialization refresh — the existing
    # owner rebuilds resources; we simulate by the new running set)
    resources: _FakeResources = service._home_resources  # type: ignore[assignment]
    resources.pools["external"] = _FakePoolInstance()

    assert service._default_pool_name == "external"  # type: ignore[attr-defined]

    server = _server_from_service(service, tmp_path)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        resp = await client.post("/api/sessions", json={})
        assert resp.status == 200
        assert (await resp.json())["pool"] == "external"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_declared_yaml_provider_would_have_lied(tmp_path: Path) -> None:
    """Guard: the DECLARED set still contains the skipped external pool (it
    stays in the YAML until restarted) — proving the running-pool source, not
    the declared YAML, must drive availability."""
    service = _make_service(tmp_path, running={"default"})
    declared = service._declared_pool_names()  # type: ignore[attr-defined]
    assert "external" in declared  # still declared...
    assert "external" not in service._running_pool_keys()  # type: ignore[attr-defined]
    # ...but not running: the selection must refuse it
    prefs: PersonalAssistantPreferences = service.personal_preferences  # type: ignore[assignment]
    prefs.save(default_workspace=None, default_pool="external")
    assert service._default_pool_name is None  # type: ignore[attr-defined]
