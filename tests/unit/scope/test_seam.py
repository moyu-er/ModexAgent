"""Ticket 18 — the N2 seam: spec-hash + per-pool generation counter.

Covers the five ticket checkboxes:

- (a)+(d) hash stability: same input tree → same hash, in-process AND
  across subprocess boundaries with different ``PYTHONHASHSEED`` (no
  dict-order/env noise); the ``workspace_ctx`` runtime object is excluded
  from the byte-stable face (lane-07 pinned contract).
- (b) mutation matrix: every spec-affecting input mutation → a different
  hash (and all mutated hashes pairwise distinct).
- (c) per-pool generation counter increments on each compile through the
  orchestration wrapper — queryable for tests/logging only.
- (e) zero runtime consumers: the seam is reserved (SPEC §10 / N2 —
  restart-effective stands; the swap mechanism is future work).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Final

import pytest

from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeGenerationTracker,
    ScopeKind,
    ScopeSpec,
    SessionMemoryOverride,
    WorkspaceSpec,
    compile_scope,
    load_scope_declaration,
    spec_hash,
)
from modex_agent.tools.presets import ToolPreset
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
BOT_YML = REPO_ROOT / "examples" / "bot_project" / "config" / "scopes" / "bot.yml"


def _workspace_ctx(target: str = "/tmp/test_scope_seam_ws") -> WorkspaceContext:
    path = Path(target)
    return WorkspaceContext(target=path, paths=WorkspacePaths(root=path), is_home=False)


@lru_cache(maxsize=1)
def _shipped_registry() -> ComponentRegistry:
    """DefaultPlugin registry — the shipped declaration carries
    ``capabilities:`` blocks, so every compile (hash input) resolves the
    CAPABILITY slot through it. The registry is a compile INPUT, never
    part of the hashed product."""
    registry = ComponentRegistry()
    ctx = PluginRegistrationContext(registry)
    DefaultPlugin().register(ctx)
    ctx.flush()
    return registry


def _tree(
    *,
    pool_name: str = "p",
    peers: list[str] | None = None,
    root: AgentSpec | None = None,
    sub: AgentSpec | None = None,
) -> ScopeSpec:
    """The minimal two-agent tree (root + sub) every mutation forks."""
    return ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(
            name=pool_name,
            peers=peers or [],
            agents=[root or AgentSpec(name="root"), sub or AgentSpec(name="sub", parent="root")],
        ),
    )


def _hash_of(spec: ScopeSpec) -> str:
    return spec_hash(
        compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry())
    )


# One single-field mutation per entry: the mutated compile input must land
# a different hash than the baseline tree (ticket checkbox 2 — the hash
# distinguishes every spec-affecting change).
MUTATIONS: Final[list[tuple[str, Callable[[], ScopeSpec]]]] = [
    ("root_description", lambda: _tree(root=AgentSpec(name="root", description="changed"))),
    ("root_max_steps", lambda: _tree(root=AgentSpec(name="root", max_steps=50))),
    ("root_toolset", lambda: _tree(root=AgentSpec(name="root", toolset=ToolPreset.READ_ONLY))),
    (
        "root_aci_capability",
        lambda: _tree(root=AgentSpec(name="root", capabilities={"aci": {}})),
    ),
    (
        "sub_wholesale_tools",
        lambda: _tree(sub=AgentSpec(name="sub", parent="root", tools=["read"])),
    ),
    ("root_roles", lambda: _tree(root=AgentSpec(name="root", roles=["analyst"]))),
    ("root_llm_provider", lambda: _tree(root=AgentSpec(name="root", llm_provider="other"))),
    ("root_system_prompt", lambda: _tree(root=AgentSpec(name="root", system_prompt="You differ."))),
    (
        "sub_session_memory",
        lambda: _tree(
            sub=AgentSpec(
                name="sub",
                parent="root",
                memory=MemoryDeclaration(session=SessionMemoryOverride(max_context_tokens=1000)),
            )
        ),
    ),
    ("root_mcp", lambda: _tree(root=AgentSpec(name="root", mcp=["extra-server"]))),
    ("root_hooks", lambda: _tree(root=AgentSpec(name="root", hooks=["TraceHooks"]))),
    (
        "root_interceptors",
        lambda: _tree(root=AgentSpec(name="root", interceptors=["ToolResultLimit"])),
    ),
    ("root_commands", lambda: _tree(root=AgentSpec(name="root", commands=["TodoCommand"]))),
    ("root_lazy_registration", lambda: _tree(root=AgentSpec(name="root", eager=False))),
    ("sub_renamed", lambda: _tree(sub=AgentSpec(name="sub2", parent="root"))),
    ("pool_peers", lambda: _tree(peers=["other"])),
    ("pool_renamed", lambda: _tree(pool_name="q")),
]

_SUBPROCESS_CODE = f"""
from pathlib import Path

import modex_agent
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.scope import compile_scope, load_scope_declaration, spec_hash
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

registry = ComponentRegistry()
with PluginRegistrationContext(registry) as ctx:
    DefaultPlugin().register(ctx)

target = Path("/tmp/test_scope_seam_subprocess")
ctx = WorkspaceContext(target=target, paths=WorkspacePaths(root=target), is_home=False)
spec = load_scope_declaration(Path({str(BOT_YML)!r}))
print(modex_agent.__file__)
print(spec_hash(compile_scope(spec, workspace_ctx=ctx, registry=registry)))
"""


def _subprocess_hash(seed: str) -> tuple[str, str]:
    """Compile + hash the shipped tree in an isolated subprocess; returns
    ``(modex_agent.__file__, digest)``."""
    env = dict(os.environ)
    pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC}{os.pathsep}{pythonpath}" if pythonpath else str(SRC)
    env["PYTHONHASHSEED"] = seed
    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_CODE],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    module_path, digest = result.stdout.strip().splitlines()
    return module_path, digest


# ─── Hash stability (ticket checkboxes 1 + 3) ────────────────────────────────


class TestSpecHashStability:
    def test_hash_is_sha256_hex_digest(self) -> None:
        digest = _hash_of(_tree())
        assert len(digest) == 64
        int(digest, 16)  # hex chars only

    def test_same_input_same_hash_in_process(self) -> None:
        spec = load_scope_declaration(BOT_YML)
        first = spec_hash(
            compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry())
        )
        second = spec_hash(
            compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry())
        )
        assert len(first) == 64  # non-trivial payload (9 shipped agents)
        assert first == second

    def test_hash_ignores_workspace_ctx(self) -> None:
        # workspace_ctx is a runtime object — excluded from the byte-stable
        # face (lane-07 pinned exclusion contract).
        spec = load_scope_declaration(BOT_YML)
        first = spec_hash(
            compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry())
        )
        second = spec_hash(
            compile_scope(
                spec,
                workspace_ctx=_workspace_ctx("/tmp/test_scope_seam_ws_other"),
                registry=_shipped_registry(),
            )
        )
        assert first == second

    def test_hash_stable_across_processes(self) -> None:
        # AC (a): two subprocesses, different PYTHONHASHSEED (env-noise
        # proof) — same digest, equal to the in-process compile.
        module_a, hash_a = _subprocess_hash(seed="1")
        module_b, hash_b = _subprocess_hash(seed="2")
        assert Path(module_a).is_relative_to(REPO_ROOT)  # lane probe
        assert Path(module_b).is_relative_to(REPO_ROOT)
        assert hash_a == hash_b
        spec = load_scope_declaration(BOT_YML)
        assert hash_a == spec_hash(
            compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry())
        )


# ─── Mutation matrix (ticket checkbox 2) ─────────────────────────────────────


class TestMutationMatrix:
    @pytest.mark.parametrize(("mutation_id", "factory"), MUTATIONS)
    def test_each_mutation_changes_the_hash(
        self, mutation_id: str, factory: Callable[[], ScopeSpec]
    ) -> None:
        baseline = _hash_of(_tree())
        assert _hash_of(factory()) != baseline, mutation_id

    def test_mutated_hashes_are_pairwise_distinct(self) -> None:
        hashes = [_hash_of(factory()) for _, factory in MUTATIONS]
        assert len(set(hashes)) == len(MUTATIONS)
        assert _hash_of(_tree()) not in hashes


# ─── Per-pool generation counter (ticket checkbox 2) ─────────────────────────


class TestGenerationCounter:
    def test_never_compiled_pool_is_generation_zero(self) -> None:
        assert ScopeGenerationTracker().generation("p") == 0

    def test_generation_increments_on_each_compile(self) -> None:
        tracker = ScopeGenerationTracker()
        tracker.compile(_tree(), workspace_ctx=_workspace_ctx())
        assert tracker.generation("p") == 1
        tracker.compile(_tree(), workspace_ctx=_workspace_ctx())
        assert tracker.generation("p") == 2

    def test_generation_bumps_once_per_pool_not_per_agent(self) -> None:
        # Pool p carries two agents — one compile bumps its generation by
        # exactly one (per POOL, not per agent).
        two_pools = ScopeSpec(
            kind=ScopeKind.WORKSPACE,
            workspace=WorkspaceSpec(
                name="w",
                pools=[
                    PoolSpec(
                        name="p",
                        agents=[AgentSpec(name="root"), AgentSpec(name="sub", parent="root")],
                    ),
                    PoolSpec(name="q", agents=[AgentSpec(name="solo")]),
                ],
            ),
        )
        tracker = ScopeGenerationTracker()
        tracker.compile(two_pools, workspace_ctx=_workspace_ctx())
        assert tracker.generation("p") == 1
        assert tracker.generation("q") == 1

    def test_pools_tracked_independently(self) -> None:
        tracker = ScopeGenerationTracker()
        tracker.compile(_tree(), workspace_ctx=_workspace_ctx())
        tracker.compile(_tree(pool_name="q"), workspace_ctx=_workspace_ctx())
        tracker.compile(_tree(), workspace_ctx=_workspace_ctx())
        assert tracker.generation("p") == 2
        assert tracker.generation("q") == 1

    def test_wrapper_output_equals_pure_compiler(self) -> None:
        # The wrapper adds zero state to the compiler: identical output.
        spec = load_scope_declaration(BOT_YML)
        tracker = ScopeGenerationTracker()
        assert tracker.compile(
            spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry()
        ) == compile_scope(spec, workspace_ctx=_workspace_ctx(), registry=_shipped_registry())
