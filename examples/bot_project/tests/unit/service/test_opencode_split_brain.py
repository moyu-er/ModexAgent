"""Ticket 10 — opencode (external root) split-brain.

The boot-manifest mechanism of tickets 05/07/09 extended to the EXTERNAL
pool: the baseline commit freezes the OLD (legacy roster) road's opencode
manifest BEFORE the declaration-path switch; the migration commit re-drives
the same configuration down the NEW (scope declaration) road and compares
field by field, with intentional differences in the explicit allowlist
(each entry names its reason).

The external pool's observable faces (vs the native default pool of
tickets 07/09): an external main agent registered through the
strategy-aware factory (no Stage 4), an empty tool surface, the pool-level
memory system built for ALL pools (hooks registered but structurally
inert for external), and the Phase-2 peer target (default) in the
communication store.

Determinism: the provider-availability gate (``shutil.which("opencode")``)
is pinned to "available" via a side-effect patch so the manifest never
depends on the host machine's PATH; ``modexctl`` resolution is pinned via
``MODEXBOT_BIN_DIR`` (the env override outranks any PATH lookup); MCP
loading is patched off at the BIZ seam (connections are not reproducible
in tests).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from bot.service.model_choice import ModelChoiceRegistry
from bot.service.pool import create_pool
from bot.service.pool.declaration import (
    boot_scope_declaration,
    declared_pool_build,
)
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER
from bot.workspace.pool_data import build_pool_data
from bot.workspace.wiring.stack import declared_assembly_deps

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.hook import HookRunner
from modex_agent.interceptor.chain import InterceptorChain
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.communication.peer_resolution import (
    peer_links_from_declaration,
    resolve_peer_targets,
)
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

from .assembly_manifest import (
    dump_assembly_manifest,
    dump_memory_hooks,
)

sys.path.insert(0, str(Path(__file__).parents[3]))

BOT_BASE = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).parent / "fixtures" / "split_brain_10"

_MAX_CONTEXT_TOKENS = 200000


async def _load_registry() -> ComponentRegistry:
    """DefaultPlugin + bot project plugins — the production factory set."""
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(BOT_BASE / "plugins",),
        ),
    )
    return registry


def _workspace_ctx(root: Path) -> WorkspaceContext:
    return WorkspaceContext(target=root, paths=WorkspacePaths(root=root), is_home=False)


def _fake_which(real_which: Any, bin_dir: Path):  # type: ignore[no-untyped-def]
    """Pin the opencode CLI as available; delegate every other lookup."""

    def which(cmd: str, path: str | None = None) -> str | None:
        if cmd == "opencode":
            return str(bin_dir / "opencode")
        return real_which(cmd, path=path)

    return which



# ── NEW road (scope declaration, ticket 10) ─────────────────────────────


def _boot_declaration(data_dir: Path):
    return boot_scope_declaration(
        declaration_path=BOT_BASE / "config" / "scopes" / "bot.yml",
        project_dir=BOT_BASE,
        data_dir=data_dir,
        graphs_dirs=(BOT_BASE / "config" / "graphs",),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )


async def _new_road_manifest(tmp_path: Path):
    """The opencode pool on the scope-declaration road.

    Supply shape: pool_data + the declaration-road Phase-2 peer tail (the
    FW resolution service over the declared links). The retired
    ``_wire_pool_to_resources`` glue never runs; the memory-hook face is
    the Stage-4-dispatch shape.
    """
    declared = declared_pool_build(_boot_declaration(tmp_path / ".modex"), "opencode")
    registry = await _load_registry()
    deps = declared_assembly_deps(
        declared.root, max_context_tokens=_MAX_CONTEXT_TOKENS
    )
    pool_data = await build_pool_data(
        _workspace_ctx(tmp_path / ".modex"),
        "opencode",
        declared.pool.root_agent,
        MagicMock(spec=LLMProvider),
        deps,
        "",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "modexctl.bat").write_text("@exit /b 0\n", encoding="ascii")
    broker = InMemoryMessageBroker()
    await broker.start()
    instance = None
    manifest = None
    try:
        with (
            patch.dict("os.environ", {"MODEXBOT_BIN_DIR": str(bin_dir)}),
            patch(
                "modex_agent.tools.mcp_loader.load_per_agent_mcp",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "shutil.which",
                side_effect=_fake_which(shutil.which, bin_dir),
            ),
        ):
            instance = await create_pool(
                pool_name="opencode",
                declared=declared,
                assembly_deps=deps,
                project_dir=BOT_BASE,
                workspace_registry=object(),
                workspace_resources=object(),
                data_dir=tmp_path / ".modex",
                broker=broker,
                output_adapter=MagicMock(spec=OutputAdapter),
                safety=RuntimeSafetyPolicy(),
                retention=SessionRetentionPolicy(),
                im_ui=MagicMock(),
                shared_hooks=[],
                shared_hook_runner=HookRunner(),
                shared_interceptor_chain=InterceptorChain(),
                workspace_resolver=None,
                bot_model_config=None,
                model_choice_registry=ModelChoiceRegistry(),
                component_registry=registry,
                pool_data=pool_data,
            )
        # Phase-2 peer tail (mirrors resources.py): the opencode pool's
        # declared peers resolve through the FW service.
        boot = _boot_declaration(tmp_path / ".modex")
        links = peer_links_from_declaration(boot.spec)
        if instance.name in links:
            pools = {instance.name: instance}
            assert boot.spec.workspace is not None
            spec_pools = {pool.name: pool for pool in boot.spec.workspace.pools}
            for pool_name, pool in spec_pools.items():
                if pool_name == instance.name:
                    continue
                peer_root = next(a for a in pool.agents if a.parent is None)
                stub = MagicMock()
                stub.root_agent_name = peer_root.name
                stub.tree_manager = MagicMock()
                pools[pool_name] = stub
            resolve_peer_targets(pools, links)

        manifest = dump_assembly_manifest(
            instance,
            data_dir=tmp_path / ".modex",
            source_of={},
            memory_hooks=dump_memory_hooks(pool_data),
        )
    finally:
        if instance is not None:
            await instance.pool.shutdown_all()
        await broker.stop()
        await pool_data.context_manager.memory_system.close()
    assert manifest is not None
    return manifest


async def test_split_brain_opencode_manifest_field_by_field(
    tmp_path: Path,
) -> None:
    """AC (d): the SAME opencode configuration, legacy roster boot vs scope
    declaration boot. Every face must match the frozen baseline except the
    explicit allowlist below."""
    new = await _new_road_manifest(tmp_path)
    golden = json.loads(
        (FIXTURES / "old_road_opencode_manifest.json").read_text("utf-8")
    )
    new_dump = new.model_dump(mode="json")

    # Intentional, explainable differences (allowlist — each entry names
    # its reason; anything else turning up is a regression):
    allowlist: dict[str, object] = {
        # The declaration road registers user_notice_cleanup ONLY via
        # Stage-4 roster dispatch — external mains run no Stage 4, so the
        # code-wired registration (legacy road, factory.py) is skipped.
        # Behavior-neutral: the hook never fires for external agents (the
        # external turn runner never invokes memory cleanup).
        "memory_hooks": [
            {"name": "TodoReorientationHook", "hook_class": "TodoReorientationHook", "runner": "memory"},
        ],
    }
    for field, expected in allowlist.items():
        assert new_dump[field] == expected, f"{field} diverged from allowlist"
        del new_dump[field]
        del golden[field]

    assert new_dump == golden
