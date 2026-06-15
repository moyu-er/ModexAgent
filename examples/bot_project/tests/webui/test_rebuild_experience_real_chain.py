"""Drive the REAL ``BotService._rebuild_experience`` against a REAL
``MemorySystemContextManager`` and then call ``load()``.

The existing workspace-switch tests use MagicMock for context_manager, so they
prove nothing about whether the rebuilt manager actually injects content.
This file wires a real MemorySystemContextManager into a fake PoolInstance,
runs the real rebuild, and asserts the new workspace's experience shows up
in the system prompt.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from framework.core.experience.manager import ExperienceManager
from framework.core.experience.source import FileExperienceSource
from framework.memory.system import MemorySystemContextManager

_EXP_MD = (
    "---\nname: {n}\ndescription: d\nscenario: s\n---\n# {n}\n\nbody\n"
)


def _mock_memory_system() -> MagicMock:
    m = MagicMock()
    m.ensure_within_budget = AsyncMock()
    m.retrieve_knowledge = AsyncMock(return_value=MagicMock(soul="", user="", memory=""))
    m.get_knowledge_directory = AsyncMock(return_value=None)
    m.get_storage_path = AsyncMock(return_value=None)
    m.get_providers = MagicMock(return_value=[])
    m.prefetch_memories = AsyncMock(return_value=None)
    m.get_history = AsyncMock(return_value=[])
    m.create_message_history = MagicMock(return_value=MagicMock())
    m.pruned_manager = None
    return m


def _write_exp(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "EXPERIENCE.md").write_text(_EXP_MD.format(n=name), encoding="utf-8")


def _make_pool_inst(ctx_mgr: MemorySystemContextManager, exp_dir: Path) -> SimpleNamespace:
    agent_cfg = MagicMock()
    agent_cfg.name = "main"
    cfg = MagicMock()
    cfg.agents = [agent_cfg]
    pool = MagicMock()
    pool._agents = {}
    return SimpleNamespace(
        name="main",
        config=cfg,
        pool=pool,
        context_manager=ctx_mgr,
        experience_dir_ref=[exp_dir],
        main_agent_name="main",
    )


@pytest.mark.asyncio
async def test_rebuild_experience_then_load_injects_new_workspace(tmp_path: Path):
    from bot.service.core import BotService

    # Workspace A has an experience, workspace B has a different one.
    home_dir = tmp_path / "home"
    home_exp = home_dir / "experiences" / "main" / "main"
    _write_exp(home_exp, "exp-home")

    ws_b_dir = tmp_path / "wsB"
    ws_b_exp = ws_b_dir / "experiences" / "main" / "main"
    _write_exp(ws_b_exp, "exp-wsb")

    # Build a REAL context manager pointing at home (simulating initial boot).
    ctx_mgr = MemorySystemContextManager(
        memory_system=_mock_memory_system(),
        base_system_prompt="base",
        experience_manager=ExperienceManager(
            source=FileExperienceSource(directories=[home_exp])
        ),
    )

    service = object.__new__(BotService)
    service._pools = {"main": _make_pool_inst(ctx_mgr, home_exp)}

    # Sanity: home experience injects before switch.
    state0 = await ctx_mgr.load("s0", tool_manager=MagicMock())
    assert "exp-home" in await state0.system_prompt_pipeline.get_or_refresh()

    # Run the REAL rebuild (the exact code the workspace switch calls).
    await service._rebuild_experience(ws_b_dir)

    # The manager must now resolve to wsB and inject its experience.
    mgr = ctx_mgr._experience_manager
    assert isinstance(mgr, ExperienceManager)
    # The FileExperienceSource resolved its dirs at construction.
    assert any("wsB" in str(d) for d in mgr._source.directories), (
        f"rebuild did not re-point source to wsB: {mgr._source.directories}"
    )

    state1 = await ctx_mgr.load("s1", tool_manager=MagicMock())
    prompt1 = await state1.system_prompt_pipeline.get_or_refresh()
    assert "exp-wsb" in prompt1, f"new workspace exp must inject, got:\n{prompt1}"
    assert "exp-home" not in prompt1
