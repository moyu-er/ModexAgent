from __future__ import annotations

from pathlib import Path

import pytest
from bot.service.pool.declaration import boot_scope_spec
from bot.service.pool.factory import _BOT_DEFAULT_LLM_PROVIDER
from plugins.bot_hooks import SEND_FILE_TO_USER_TOOL_NAME

from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.scope.compiler import CompiledAgent, compile_scope
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
from modex_agent.tools.presets import (
    EXPERIENCE_REVIEW_HOOK_NAME,
    ToolPreset,
    ToolSupplement,
    get_supplement_tool_names,
)
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

# The projections/constants are the single name authorities — no string
# literals for the experience / send_file names (task-2 file pattern).
EXPERIENCE_TOOL_NAME = get_supplement_tool_names([ToolSupplement.EXPERIENCE])[0]

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
    ("pool_name", "agent_name", "tools", "expected"),
    [
        # Contract change (task 5, glue-tool-roster-convergence): the
        # overlay no longer pre-merges tools against a declared roster —
        # entries are APPENDED and the compiler's single merge owns the
        # outcome (old expectations: ["read"] / ["web_search"]).
        ("alpha", "root-a", ["web_search"], ["read", "process", "terminal", "web_search"]),
        # None-declared roster: overlay entries pass through verbatim
        # (legacy branch, unchanged).
        ("beta", "root-b", ["-process"], ["-process"]),
    ],
)
def test_agent_overlay_tools_append_to_declared_roster(
    pool_name: str, agent_name: str, tools: list[str], expected: list[str]
) -> None:
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={pool_name: PoolOverlay(agents={agent_name: AgentOverlay(tools=tools)})}
    )

    result = apply_scope_overlay(spec, overlay)

    assert _agent(result, pool_name, agent_name).tools == expected


def test_wholesale_declared_roster_rejects_prefixed_overlay_entries() -> None:
    # F2 finding 1 (task-5 follow-up): root-a's unprefixed wholesale roster
    # + prefixed overlay entries is the ambiguous combination — the task-5
    # row pinned its append output (["read", "process", "terminal",
    # "-process", "-terminal"]), which the compiler's mixed-list rule then
    # silently flipped to incremental, reintroducing the full preset. The
    # combination is now rejected loudly at the declaration level.
    spec = _scope_spec()
    overlay = ScopeOverlay(
        pools={"alpha": PoolOverlay(agents={"root-a": AgentOverlay(tools=["-process"])})}
    )

    with pytest.raises(ValueError, match=r"agent 'root-a'.*wholesale"):
        apply_scope_overlay(spec, overlay)


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


# ── Full-path rows: apply_scope_overlay → compile_scope ─────────────────────
# Task 5 (glue-tool-roster-convergence): the overlay APPENDS tools entries to
# the declaration and the compiler's single _merge_tools pass owns the outcome
# over the full base (preset + derived + supplement-derived names).


def _workspace_ctx() -> WorkspaceContext:
    target = Path("/tmp/test_overlay_compile_convergence_ws")
    return WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)


def _overlay_and_compile(
    agent: AgentSpec, overlay: AgentOverlay
) -> tuple[ScopeSpec, CompiledAgent]:
    """Apply one agent overlay to a single-root pool, then compile it."""
    spec = ScopeSpec(kind=ScopeKind.POOL, pool=PoolSpec(name="p", agents=[agent]))
    adjusted = apply_scope_overlay(
        spec, ScopeOverlay(pools={"p": PoolOverlay(agents={agent.name: overlay})})
    )
    compilation = compile_scope(adjusted, workspace_ctx=_workspace_ctx())
    assert len(compilation.agents) == 1
    return adjusted, compilation.agents[0]


class TestOverlayToolsCompileConvergence:
    def test_overlay_minus_removes_declared_plus_and_supplement_names(self) -> None:
        # Row (i): declared +send_file_to_user + EXPERIENCE supplement, overlay
        # minus entries (the eval harness tools_remove shape) → compiled tools
        # contain NEITHER name and the experience_review hook goes with the
        # tool (binding follows the final tool list). The old pre-merge kept
        # both: "-send_file_to_user" never matched the literal "+send_file_to_user"
        # base entry.
        _adjusted, compiled = _overlay_and_compile(
            AgentSpec(
                name="root",
                tools=[f"+{SEND_FILE_TO_USER_TOOL_NAME}"],
                tool_supplements=[ToolSupplement.EXPERIENCE],
            ),
            AgentOverlay(
                tools=[
                    f"-{SEND_FILE_TO_USER_TOOL_NAME}",
                    f"-{EXPERIENCE_TOOL_NAME}",
                ]
            ),
        )
        assert SEND_FILE_TO_USER_TOOL_NAME not in compiled.spec.tools
        assert EXPERIENCE_TOOL_NAME not in compiled.spec.tools
        assert EXPERIENCE_REVIEW_HOOK_NAME not in compiled.spec.hooks

    def test_none_declared_roster_passes_minus_entries_to_compiler(self) -> None:
        # Row (ii): regression row mirroring the eval benchmark arm — declared
        # tools None + minus-entry overlay → the None branch is unchanged
        # (entries pass through verbatim; the compiler merge is the only
        # merge) and the compiled roster equals the un-overlaid compile
        # (process/terminal are not in the preset base today).
        adjusted, compiled = _overlay_and_compile(
            AgentSpec(name="root"), AgentOverlay(tools=["-process", "-terminal"])
        )
        assert adjusted.pool is not None
        assert adjusted.pool.agents[0].tools == ["-process", "-terminal"]
        _bare_spec, bare = _overlay_and_compile(AgentSpec(name="root"), AgentOverlay())
        assert compiled.spec.tools == bare.spec.tools
        assert not {"process", "terminal"} & set(compiled.spec.tools)

    def test_overlay_minus_removes_declared_plus_entry(self) -> None:
        # Row (iii): the prefixed-drop bug fixed — declared ["+x"] + overlay
        # ["-x"] → x absent (the old pre-merge compared the stripped "x"
        # against the literal "+x" base entry and never matched).
        _adjusted, compiled = _overlay_and_compile(
            AgentSpec(name="root", tools=["+x"]), AgentOverlay(tools=["-x"])
        )
        assert "x" not in compiled.spec.tools

    def test_unprefixed_overlay_on_unprefixed_declared_concatenates(self) -> None:
        # Row (iv): unprefixed overlay entries appended to an unprefixed
        # declared roster CONCATENATE (the old branch wholesale-replaced) —
        # duplicates are permitted and visible in the compiled roster. No
        # repo consumer constructs unprefixed overlay entries (all are
        # minus-only); this row pins the actual behavior.
        _adjusted, compiled = _overlay_and_compile(
            AgentSpec(name="root", tools=["read", "write"]),
            AgentOverlay(tools=["read", "search"]),
        )
        assert compiled.spec.tools == ["read", "write", "read", "search"]

    def test_wholesale_declared_roster_rejects_minus_overlay_entries(self) -> None:
        # F2 finding 1: wholesale (all-unprefixed) declared roster + prefixed
        # overlay entries → loud ValueError. The concatenation would produce
        # a MIXED list whose merge silently flips wholesale semantics to
        # incremental and reintroduces the full preset (silent capability
        # expansion — the worst failure class).
        with pytest.raises(ValueError, match=r"agent 'root'.*wholesale"):
            _overlay_and_compile(
                AgentSpec(name="root", tools=["read", "write"]),
                AgentOverlay(tools=["-read"]),
            )

    def test_wholesale_declared_roster_rejects_plus_overlay_entries(self) -> None:
        # Same rejection for + entries — any prefix flips the semantics.
        with pytest.raises(ValueError, match=r"agent 'root'.*wholesale"):
            _overlay_and_compile(
                AgentSpec(name="root", tools=["read", "write"]),
                AgentOverlay(tools=["+search"]),
            )

    def test_empty_wholesale_roster_rejects_prefixed_overlay_entries(self) -> None:
        # tools: [] is wholesale "no tools" — same rejection.
        with pytest.raises(ValueError, match=r"agent 'root'.*wholesale"):
            _overlay_and_compile(
                AgentSpec(name="root", tools=[]),
                AgentOverlay(tools=["-read"]),
            )
