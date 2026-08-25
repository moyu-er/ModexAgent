"""Tickets 07/11 — production wiring: every declared pool boots from the
scope declaration through the real workspace-resource loop.

Covers the resources.py half: the global boot call (with the V1-V11 gate),
the per-pool declaration-road selection threaded into
``create_pool(declared=...)`` (ticket 11: every DECLARED pool — the
dual-road pivot set is gone), the Phase-2 ``register_communication_tools``
gate for derived pools, and the declaration-less deployment fallback
(every pool boots the legacy road, loudly — until the road's deletion).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bot.workspace.wiring.resources import (
    ScopeBootRequiredError,
    _build_resources,
    _stop_resources,
)

from modex_agent.ioc.configs.app import AppConfig
from modex_agent.multi_agent.pool_router import PoolRoutingStore
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.defaults.prompt import FilePromptProviderFactory
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext

_DECLARATION = """\
workspace:
  name: wiring-test
  pools:
    default:
      agents:
        default:
          description: wiring-test pivot root
          agents:
            office-expert:
              description: wiring-test subagent
    review:
      agents:
        reviewer:
          description: wiring-test legacy root
"""


def _service(home: Path, app_config: AppConfig) -> MagicMock:
    service = MagicMock()
    service._project_dir = home
    service.project_dir = home
    service._app_config = app_config
    service._home_persistence = None
    service._mcp_registry = None
    service._bot_model_config = None
    service._default_provider = None
    service._default_pool_name = "default"
    service._pool_session_store = MagicMock(spec=PoolRoutingStore)
    service._model_choice_registry = None
    service._transcript_store = None
    service._output_adapter_factory = None
    service._on_subagent_created = None
    service._strategy_registry = None
    service._component_registry = ComponentRegistry()
    service._component_registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "file_prompt",
        FilePromptProviderFactory(),
    )
    service.workspace_stack = None
    service.control_channel = None
    service.command_processor = None
    service.emitter_factory = None
    service._system_prompt_for = lambda _name: ""
    return service


def _pool_spec(name: str) -> MagicMock:
    spec = MagicMock()
    spec.name = name
    spec.peers = []
    # Ticket 14's single deps road synthesizes root position defaults from
    # these fields — they must be real values, not MagicMock attributes.
    spec.main.agent_name = name
    spec.main.memory.archive_enabled = False
    spec.main.memory.core_enabled = False
    return spec


def _instance(name: str, *, comm_tools_derived: bool) -> MagicMock:
    instance = MagicMock()
    instance.name = name
    instance.root_agent_name = name
    instance.comm_tools_derived = comm_tools_derived
    instance.requires_main_agent_tools = True
    instance.pool._agents = {}
    instance.pool.shutdown_all = AsyncMock(return_value=True)
    instance.pool.materialize_deps = None
    instance.broker_bridge.start = AsyncMock()
    instance.broker_bridge.stop = AsyncMock()
    instance.mcp_manager = None
    instance.terminal_manager = None
    instance.terminal_manager_close = None
    instance.pool_data = None
    return instance


async def _build_with_patched_pools(
    home: Path,
    target: Path,
    instances: dict[str, MagicMock],
) -> dict[str, Any]:
    """_build_resources with create_pool/background patched (a declaration
    must exist — it is the pool list source).

    Returns the ``declared`` kwarg each create_pool call received, keyed
    by pool name (the wiring under test).
    """
    service = _service(home, AppConfig.model_validate({}))
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=home)
    declared_by_pool: dict[str, Any] = {}

    async def create_pool(*args: object, pool_name: str, **kwargs: object) -> MagicMock:
        declared_by_pool[pool_name] = kwargs.get("declared")
        return instances[pool_name]

    with (
        patch(
            "bot.workspace.wiring.resources.build_pool_data",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("bot.service.pool.create_pool", side_effect=create_pool),
        patch("bot.workspace.wiring.resources.BackgroundTaskRunner") as background_type,
    ):
        background_type.return_value.start = AsyncMock()
        background_type.return_value.stop = AsyncMock()
        resources = await _build_resources(service, ctx)
    await _stop_resources(resources)
    return declared_by_pool


def _prepare_home(home: Path, *, with_declaration: bool) -> None:
    (home / "config").mkdir(parents=True)
    if with_declaration:
        (home / "config" / "scopes").mkdir()
        (home / "config" / "scopes" / "bot.yml").write_text(_DECLARATION, encoding="utf-8")


async def test_pivot_pool_boots_declared_through_resources_loop(
    tmp_path: Path,
) -> None:
    """Every pool the declaration hosts receives the declaration products
    (ticket 11: the dual-road pivot set is gone — review boots declared
    too)."""
    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    target.mkdir()
    _prepare_home(home, with_declaration=True)
    instances = {
        "default": _instance("default", comm_tools_derived=True),
        "review": _instance("review", comm_tools_derived=True),
    }
    declared_by_pool = await _build_with_patched_pools(home, target, instances)

    assert set(declared_by_pool) == {"default", "review"}
    default_declared = declared_by_pool["default"]
    assert default_declared is not None
    assert default_declared.root.provenance.agent == "default"
    assert [s.provenance.agent for s in default_declared.subagents] == ["office-expert"]
    review_declared = declared_by_pool["review"]
    assert review_declared is not None
    assert review_declared.root.provenance.agent == "reviewer"

    # Phase-2 gate: every derived pool skipped the legacy registration.
    instances["default"].tool_manager.register.assert_not_called()
    instances["review"].tool_manager.register.assert_not_called()


async def test_declaration_less_deployment_boots_all_legacy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A deployment without a declaration file (synthetic roots,
    pre-deployment checkouts) boots every pool on the legacy road —
    loudly."""
    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    target.mkdir()
    _prepare_home(home, with_declaration=False)
    instances = {"default": _instance("default", comm_tools_derived=False)}
    with pytest.raises(ScopeBootRequiredError, match="no scope declaration"):
        await _build_with_patched_pools(home, target, instances)


_POOL_AS_ROOT_DECLARATION = """\
pool:
  name: main
  agents:
    main:
      description: pool-as-root root
      agents:
        helper:
          description: pool-as-root child
"""


async def test_pool_as_root_declaration_boots_straight_through(
    tmp_path: Path,
) -> None:
    """Ticket 14: a pool-as-root declaration (no workspace layer) boots the
    single declared pool straight through the declaration road — the
    single-workspace deployment form."""
    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    target.mkdir()
    (home / "config").mkdir(parents=True)
    (home / "config" / "scopes").mkdir()
    (home / "config" / "scopes" / "bot.yml").write_text(_POOL_AS_ROOT_DECLARATION, encoding="utf-8")
    instances = {"main": _instance("main", comm_tools_derived=True)}
    declared_by_pool = await _build_with_patched_pools(home, target, instances)

    assert set(declared_by_pool) == {"main"}
    declared = declared_by_pool["main"]
    assert declared is not None
    assert declared.root.provenance.agent == "main"
    assert [s.provenance.agent for s in declared.subagents] == ["helper"]
    # Zero workspace awareness: no workspace layer rides the assembly chain.
    assert declared.root.spec.workspace_ctx is not None


async def test_pool_yml_only_tree_is_rejected_loudly(tmp_path: Path) -> None:
    """Ticket 11 old-format rejection: a config/pools-only tree (no scope
    declaration) boots NOTHING silently — the deployment refuses loudly
    with migration guidance instead of mis-reading the legacy format."""
    home, target = tmp_path / "home", tmp_path / "target"
    home.mkdir()
    target.mkdir()
    _prepare_home(home, with_declaration=True)
    # Ticket 11 final state: there is NO undeclared-disk-pool road — the
    # declaration is the pool list source, so a pool.yml-only tree boots
    # nothing and the declaration-less deployment refuses loudly (the
    # old-format rejection criterion).
    instances = {"default": _instance("default", comm_tools_derived=True)}
    home2, target2 = tmp_path / "home2", tmp_path / "target2"
    home2.mkdir()
    target2.mkdir()
    (home2 / "config" / "pools" / "stray").mkdir(parents=True)
    (home2 / "config" / "pools" / "stray" / "pool.yml").write_text(
        "main_agent_name: stray-main\n", encoding="utf-8"
    )
    with pytest.raises(ScopeBootRequiredError, match="config/pools"):
        await _build_with_patched_pools(home2, target2, instances)
