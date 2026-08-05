from __future__ import annotations

from bot.workspace.wiring.stack import _build_assembly_deps_for_pools

from modex_agent.multi_agent.pool_config import MainAgentSpec, MemoryToggle, PoolSpec


def _pool_spec(name: str, memory: MemoryToggle) -> PoolSpec:
    return PoolSpec(
        name=name,
        main_agent_name=name,
        main=MainAgentSpec(agent_name=name, memory=memory),
    )


def test_archive_enabled_pool_builds_enabled_archive() -> None:
    pool_specs = {
        "archive": _pool_spec(
            "archive",
            MemoryToggle(archive_enabled=True),
        )
    }

    deps = _build_assembly_deps_for_pools(
        pool_specs=pool_specs,
        max_context_tokens=None,
    )

    archive = deps["archive"].memory.archive
    assert archive is not None
    assert archive.enabled is True


def test_default_memory_pool_builds_without_archive() -> None:
    pool_specs = {"default": _pool_spec("default", MemoryToggle())}

    deps = _build_assembly_deps_for_pools(
        pool_specs=pool_specs,
        max_context_tokens=None,
    )

    assert deps["default"].memory.archive is None


def test_archive_and_core_enabled_pool_builds_enabled_dream_engine() -> None:
    pool_specs = {
        "long-term": _pool_spec(
            "long-term",
            MemoryToggle(archive_enabled=True, core_enabled=True),
        )
    }

    deps = _build_assembly_deps_for_pools(
        pool_specs=pool_specs,
        max_context_tokens=None,
    )

    dream_engine = deps["long-term"].memory.dream_engine
    assert dream_engine is not None
    assert dream_engine.enabled is True
