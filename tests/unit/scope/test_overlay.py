from __future__ import annotations

from pathlib import Path

import pytest
from bot.service.pool.declaration import boot_scope_spec
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER

from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.overlay import (
    AgentOverlay,
    PoolOverlay,
    ScopeOverlay,
    apply_scope_overlay,
)
from modex_agent.scope.spec import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    SessionMemoryOverride,
    WorkspaceSpec,
)
from modex_agent.tools.presets import ToolPreset

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BOT_BASE = PROJECT_ROOT / "examples" / "bot_project"


def _scope_spec() -> ScopeSpec:
    return ScopeSpec(
        kind=ScopeKind.WORKSPACE,
        workspace=WorkspaceSpec(
            name="test",
            pools=[
                PoolSpec(
                    name="alpha",
                    peers=["beta"],
                    agents=[
                        AgentSpec(
                            name="root-a",
                            tools=["read", "process", "terminal"],
                            mcp=["playwright", "fetch"],
                            memory=MemoryDeclaration(
                                archive_enabled=True,
                                core_enabled=True,
                                session=SessionMemoryOverride(max_context_tokens=32000),
                            ),
                            approval=ApprovalConfig(enabled=True),
                        ),
                        AgentSpec(name="child-a", parent="root-a"),
                    ],
                ),
                PoolSpec(
                    name="beta",
                    peers=["alpha"],
                    agents=[AgentSpec(name="root-b")],
                ),
            ],
        ),
    )


def _pool(spec: ScopeSpec, name: str) -> PoolSpec:
    assert spec.workspace is not None
    return next(pool for pool in spec.workspace.pools if pool.name == name)


def _agent(spec: ScopeSpec, pool_name: str, agent_name: str) -> AgentSpec:
    return next(agent for agent in _pool(spec, pool_name).agents if agent.name == agent_name)


def test_empty_overlay_preserves_every_scope_field() -> None:
    spec = _scope_spec()

    result = apply_scope_overlay(spec, ScopeOverlay())

    assert result == spec
    assert result.model_dump() == spec.model_dump()


def test_strip_peers_clears_every_bidirectional_pool_link() -> None:
    spec = _scope_spec()

    result = apply_scope_overlay(spec, ScopeOverlay(strip_peers=True))

    assert [_pool(result, name).peers for name in ("alpha", "beta")] == [[], []]


def test_keep_agents_can_reduce_pool_to_its_root() -> None:
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={"alpha": PoolOverlay(keep_agents=["root-a"])}
    )

    result = apply_scope_overlay(spec, overlay)

    assert [agent.name for agent in _pool(result, "alpha").agents] == ["root-a"]


def test_keep_agents_refuses_to_drop_pool_root() -> None:
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={"alpha": PoolOverlay(keep_agents=["child-a"])}
    )

    with pytest.raises(ValueError, match=r"alpha.*root-a"):
        apply_scope_overlay(spec, overlay)


def test_agent_overlay_replaces_toolset() -> None:
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={
            "alpha": PoolOverlay(
                agents={"root-a": AgentOverlay(toolset=ToolPreset.WEB)}
            )
        }
    )

    result = apply_scope_overlay(spec, overlay)

    assert _agent(result, "alpha", "root-a").toolset is ToolPreset.WEB


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (["-process", "-terminal"], ["read"]),
        (["web_search"], ["web_search"]),
    ],
)
def test_agent_overlay_tools_follow_shared_merge_contract(
    tools: list[str], expected: list[str]
) -> None:
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={"alpha": PoolOverlay(agents={"root-a": AgentOverlay(tools=tools)})}
    )

    result = apply_scope_overlay(spec, overlay)

    assert _agent(result, "alpha", "root-a").tools == expected


def test_agent_overlay_memory_merges_only_explicit_fields() -> None:
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={
            "alpha": PoolOverlay(
                agents={
                    "root-a": AgentOverlay(
                        memory=MemoryDeclaration(core_enabled=False)
                    )
                }
            )
        }
    )

    result = apply_scope_overlay(spec, overlay)

    memory = _agent(result, "alpha", "root-a").memory
    assert memory == MemoryDeclaration(
        archive_enabled=True,
        core_enabled=False,
        session=SessionMemoryOverride(max_context_tokens=32000),
    )


def test_agent_overlay_memory_creates_declaration_for_defaulted_root() -> None:
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={
            "beta": PoolOverlay(
                agents={
                    "root-b": AgentOverlay(
                        memory=MemoryDeclaration(core_enabled=False)
                    )
                }
            )
        }
    )

    result = apply_scope_overlay(spec, overlay)

    assert _agent(result, "beta", "root-b").memory == MemoryDeclaration(
        core_enabled=False
    )


def test_agent_overlay_replaces_prompt_provider_and_strips_approval() -> None:
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={
            "alpha": PoolOverlay(
                agents={
                    "root-a": AgentOverlay(
                        system_prompt_provider="file_prompt",
                        system_prompt_provider_config={"path": "agents/benchmark.md"},
                        strip_approval=True,
                    )
                }
            )
        }
    )

    result = apply_scope_overlay(spec, overlay)

    agent = _agent(result, "alpha", "root-a")
    assert agent.system_prompt_provider == "file_prompt"
    assert agent.system_prompt_provider_config == {"path": "agents/benchmark.md"}
    assert agent.approval is None


def test_agent_overlay_strips_mcp_selection() -> None:
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={
            "alpha": PoolOverlay(agents={"root-a": AgentOverlay(strip_mcp=True)})
        }
    )

    result = apply_scope_overlay(spec, overlay)

    assert _agent(result, "alpha", "root-a").mcp == []
    assert _agent(result, "alpha", "child-a").mcp == []


@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        (ScopeOverlay(pools={"missing": PoolOverlay()}), "missing"),
        (
            ScopeOverlay(
                pools={
                    "alpha": PoolOverlay(agents={"missing": AgentOverlay()})
                }
            ),
            "missing",
        ),
    ],
)
def test_unknown_overlay_names_fail_loudly(
    overlay: ScopeOverlay, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        apply_scope_overlay(_scope_spec(), overlay)


def test_apply_scope_overlay_does_not_mutate_input() -> None:
    spec = _scope_spec()
    original = spec.model_dump()
    overlay = ScopeOverlay(
        strip_peers=True,
        pools={
            "alpha": PoolOverlay(
                keep_agents=["root-a"],
                agents={"root-a": AgentOverlay(tools=["read"])},
            )
        },
    )

    apply_scope_overlay(spec, overlay)

    assert spec.model_dump() == original
    assert _pool(spec, "alpha").peers == ["beta"]
    assert [agent.name for agent in _pool(spec, "alpha").agents] == [
        "root-a",
        "child-a",
    ]


def test_benchmark_shaped_overlay_boots_real_bot_declaration(
    tmp_path: Path,
) -> None:
    spec = load_scope_declaration(BOT_BASE / "config" / "scopes" / "bot.yml")
    overlay = ScopeOverlay(
        strip_peers=True,
        pools={
            "default": PoolOverlay(
                keep_agents=["default"],
                agents={
                    "default": AgentOverlay(
                        tools=["-process", "-terminal"],
                        memory=MemoryDeclaration(core_enabled=False),
                        system_prompt_provider="file_prompt",
                        system_prompt_provider_config={"path": "agents/default.md"},
                    )
                },
            )
        },
    )

    adjusted = apply_scope_overlay(spec, overlay)
    boot = boot_scope_spec(
        adjusted,
        project_dir=BOT_BASE,
        data_dir=tmp_path / ".modex",
        graphs_dirs=(),
        default_llm_provider=_BOT_DEFAULT_LLM_PROVIDER,
    )

    root = next(
        agent
        for agent in boot.compilation.agents
        if agent.provenance.pool == "default"
    )
    assert not {
        "task",
        "send_to_agent",
        "send_to_peer",
        "process",
        "terminal",
    } & set(root.effective.tools)
