"""Real feedback loop for the experience-injection-on-workspace-switch bug.

All existing tests in examples/bot_project use MagicMock for the context
manager, so they never exercise the real chain:

    MemorySystemContextManager.load()
      → step 8: if self._experience_manager is not None:
          → ExperienceManager.build_prompt(context=ctx)
            → FileExperienceSource.list_experiences(context=ctx)
      → experience text lands in the system-prompt pipeline

This file builds REAL objects and drives that chain end to end, then mutates
``_experience_manager`` exactly the way ``BotService._rebuild_experience``
does and asserts the NEW workspace's experience is injected.

This is the regression test the handoff doc claimed existed
(``test_experience_injection_e2e.py``) but never did.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.experience.manager import ExperienceManager
from modex_agent.core.experience.source import FileExperienceSource
from modex_agent.memory.system import MemorySystemContextManager

_EXP_MD_TEMPLATE = (
    "---\n"
    "name: {name}\n"
    "description: {desc}\n"
    "scenario: test\n"
    "---\n"
    "# {name}\n\n"
    "Body for {name}.\n"
)


def _write_experience(root: Path, name: str, desc: str) -> None:
    exp_dir = root / name
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "EXPERIENCE.md").write_text(
        _EXP_MD_TEMPLATE.format(name=name, desc=desc), encoding="utf-8"
    )


def _make_mock_memory_system() -> MagicMock:
    """A mock MemorySystem exposing everything load() touches."""
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.retrieve_knowledge = AsyncMock(
        return_value=MagicMock(soul="", user="", memory="")
    )
    mock_system.get_knowledge_directory = AsyncMock(return_value=None)
    mock_system.get_storage_path = AsyncMock(return_value=None)
    mock_system.get_providers = MagicMock(return_value=[])
    mock_system.prefetch_memories = AsyncMock(return_value=None)
    mock_system.get_history = AsyncMock(return_value=[])
    mock_system.create_message_history = MagicMock(return_value=MagicMock())
    # Avoid fooling hasattr checks in load().
    mock_system.pruned_manager = None
    return mock_system


def _ctx_mgr(exp_dir: Path) -> MemorySystemContextManager:
    return MemorySystemContextManager(
        memory_system=_make_mock_memory_system(),
        base_system_prompt="base",
        experience_manager=ExperienceManager(
            source=FileExperienceSource(directories=[exp_dir])
        ),
    )


@pytest.mark.asyncio
async def test_load_injects_experience_from_current_dir(tmp_path: Path) -> None:
    """Sanity: load() injects experience text for the configured directory."""
    exp_dir = tmp_path / "ws-a" / "experiences" / "pool" / "agent"
    _write_experience(exp_dir, "exp-alpha", "alpha experience")
    ctx_mgr = _ctx_mgr(exp_dir)

    state = await ctx_mgr.load("s1", tool_manager=MagicMock())
    prompt = await state.system_prompt_pipeline.get_or_refresh()

    assert "exp-alpha" in prompt, f"experience should be injected, got:\n{prompt}"


@pytest.mark.asyncio
async def test_rebuilt_experience_manager_switches_injected_content(
    tmp_path: Path,
) -> None:
    """The core regression: after replacing ``_experience_manager`` to point
    at a new workspace (mimicking ``_rebuild_experience``), load() must inject
    the NEW workspace's experience, not the old one — and not nothing."""
    dir_a = tmp_path / "ws-a" / "experiences" / "pool" / "agent"
    dir_b = tmp_path / "ws-b" / "experiences" / "pool" / "agent"
    _write_experience(dir_a, "exp-alpha", "alpha experience")
    _write_experience(dir_b, "exp-beta", "beta experience")

    ctx_mgr = _ctx_mgr(dir_a)

    # Before switch: alpha injected.
    state_a = await ctx_mgr.load("s1", tool_manager=MagicMock())
    prompt_a = await state_a.system_prompt_pipeline.get_or_refresh()
    assert "exp-alpha" in prompt_a

    # Mimic BotService._rebuild_experience: replace the manager in place.
    ctx_mgr._experience_manager = ExperienceManager(
        source=FileExperienceSource(directories=[dir_b])
    )

    # After switch: beta injected, alpha gone.
    state_b = await ctx_mgr.load("s2", tool_manager=MagicMock())
    prompt_b = await state_b.system_prompt_pipeline.get_or_refresh()

    assert "exp-beta" in prompt_b, (
        f"new workspace experience must be injected, got:\n{prompt_b}"
    )
    assert "exp-alpha" not in prompt_b, (
        f"old workspace experience must be gone, got:\n{prompt_b}"
    )
