"""Real feedback loop for the experience-injection channel (T14 rewrite).

The original regression this file pinned — mutating
``MemorySystemContextManager._experience_manager`` in place (the retired
``BotService._rebuild_experience`` shape) — died with the special case:
the injection now rides the capability-section channel
(``ExperienceCapability.supply`` → the manager → ``assemble``'s
content-hash provider → the capability-section anchor of ``load()``).

The channel-shaped equivalent of the same regression: the provider
instance is REUSED across ``load()`` calls (the capability-section
channel contract), so it must refresh when the experience set changes —
a mid-session EXPERIENCE.md write (what the review hook does) appears on
the next ``load()``, and removed experiences disappear. All existing
tests that used MagicMock for the context manager never exercised the
real chain; this file builds REAL objects and drives them end to end.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from modex_agent.memory.hooks import MemoryHookRunner
from modex_agent.memory.system import MemorySystemContextManager
from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps
from modex_agent.plugins.capability import CapabilityBinding, PromptSectionSpec
from modex_agent.plugins.defaults.capabilities.experience import (
    ExperienceCapability,
    ExperienceSupply,
)
from modex_agent.plugins.registry import ComponentRegistry

_EXP_MD_TEMPLATE = (
    "---\nname: {name}\ndescription: {desc}\nscenario: test\n---\n# {name}\n\nBody for {name}.\n"
)

_INJECTION_SECTION = PromptSectionSpec(section_id="experience.injection", order=50)


def _write_experience(root: Path, name: str, desc: str) -> None:
    exp_dir = root / "experiences" / "pool" / "main" / name
    exp_dir.mkdir(parents=True, exist_ok=True)
    (exp_dir / "EXPERIENCE.md").write_text(
        _EXP_MD_TEMPLATE.format(name=name, desc=desc), encoding="utf-8"
    )


def _make_mock_memory_system() -> MagicMock:
    """A mock MemorySystem exposing everything load() touches."""
    mock_system = MagicMock()
    mock_system.ensure_within_budget = AsyncMock()
    mock_system.retrieve_core_memory = AsyncMock(
        return_value=MagicMock(soul="", user="", memory="")
    )
    mock_system.get_core_memory_directory = AsyncMock(return_value=None)
    mock_system.get_storage_path = AsyncMock(return_value=None)
    mock_system.get_providers = MagicMock(return_value=[])
    mock_system.prefetch_memories = AsyncMock(return_value=None)
    mock_system.get_history = AsyncMock(return_value=[])
    mock_system.create_message_history = MagicMock(return_value=MagicMock())
    mock_system.hook_runner = MemoryHookRunner()
    # Avoid fooling hasattr checks in load().
    mock_system.pruned_manager = None
    return mock_system


def _supply_for(data_dir: Path) -> ExperienceSupply:
    """The REAL production construction: capability.supply(view)."""
    from modex_agent.plugins.capability import PoolSupplyAgentEntry, PoolSupplyView

    capability = ExperienceCapability()
    supply = capability.supply(
        PoolSupplyView(
            pool_name="pool",
            entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
            root_agent_name="main",
            data_dir=data_dir,
        )
    )
    assert isinstance(supply, ExperienceSupply)
    return supply


async def _channel_provider(supply: ExperienceSupply):
    """The REAL production path: assemble() wires the section provider."""
    capability = ExperienceCapability()
    wiring = await capability.assemble(
        CapabilityBinding(active_sections=(_INJECTION_SECTION,)),
        AgentContext(
            registry=ComponentRegistry(),
            workspace_ctx=MagicMock(),
            pool_runtime=PoolRuntimeDeps(capability_supply={"experience": supply}),
            agent_name="main",
        ),
    )
    assert len(wiring.prompt_providers) == 1
    return wiring.prompt_providers[0]


def _ctx_mgr(provider) -> MemorySystemContextManager:
    mgr = MemorySystemContextManager(
        memory_system=_make_mock_memory_system(),
        base_system_prompt="base",
    )
    mgr.set_capability_sections((provider,))
    return mgr


async def _prompt(mgr: MemorySystemContextManager, session_id: str) -> str:
    state = await mgr.load(session_id, tool_manager=MagicMock())
    assert state.system_prompt_pipeline is not None
    return await state.system_prompt_pipeline.get_or_refresh()


async def test_load_injects_experience_from_current_dir(tmp_path: Path) -> None:
    """Sanity: load() injects experience text through the capability channel."""
    _write_experience(tmp_path, "exp-alpha", "alpha experience")
    provider = await _channel_provider(_supply_for(tmp_path))

    prompt = await _prompt(_ctx_mgr(provider), "s1")

    assert "exp-alpha" in prompt, f"experience should be injected, got:\n{prompt}"


async def test_reused_provider_switches_injected_content(tmp_path: Path) -> None:
    """The channel regression: the SAME provider instance (reused across
    load()s — the capability-section contract) must pick up experiences
    written after the first load and drop removed ones — the content-hash
    refresh that replaced the retired in-place manager rebuild."""
    _write_experience(tmp_path, "exp-alpha", "alpha experience")
    provider = await _channel_provider(_supply_for(tmp_path))
    mgr = _ctx_mgr(provider)

    prompt_a = await _prompt(mgr, "s1")
    assert "exp-alpha" in prompt_a

    # Mid-session write (what the review hook does) + removal.
    _write_experience(tmp_path, "exp-beta", "beta experience")
    removed = tmp_path / "experiences" / "pool" / "main" / "exp-alpha"
    (removed / "EXPERIENCE.md").unlink()
    removed.rmdir()

    prompt_b = await _prompt(mgr, "s2")
    assert "exp-beta" in prompt_b, f"new experience must be injected, got:\n{prompt_b}"
    assert "exp-alpha" not in prompt_b, f"removed experience must be gone, got:\n{prompt_b}"


async def test_no_experiences_renders_no_section(tmp_path: Path) -> None:
    """Empty experience set → empty section content → nothing joins the
    prompt (the retired ``if experience_prompt:`` guard's parity — the
    pipeline skips empty providers)."""
    provider = await _channel_provider(_supply_for(tmp_path))
    mgr = _ctx_mgr(provider)

    prompt = await _prompt(mgr, "s1")

    assert "## Experiences" not in prompt
