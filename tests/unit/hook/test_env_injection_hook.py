"""Tests for NativeEnvInjectionHook — populate _modex_env / _current_session_id.

The hook sets two contextvars at BEFORE_GRAPH so native agent bash/terminal
subprocess tools (SubprocessExecutor, CommandTool) read MODEX_* env overrides.

Scope: per-turn override logic — the spec template's pool-static fields are
preserved, while session_id / agent_name / comm_kind / parent_session_id are
sourced from ``ctx.session`` / ``ctx.comm_kind``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook.builtin.env_injection import NativeEnvInjectionHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.env_context import _current_session_id, _modex_env
from modex_agent.tools.manager import InMemoryToolManager


def _make_template(
    *,
    comm_kind: AgentCommKind = AgentCommKind.NORMAL,
    parent_session_id: str | None = None,
    session_id: str = "__pending__.main",
    agent_name: str = "main",
    control_origin: str = "",
) -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=Path("/tmp/ws"),
        inbox_root=Path("/tmp/ws/.modex"),
        workdir=Path("/tmp/ws"),
        session_id=session_id,
        agent_name=agent_name,
        provider_session_id="",
        agent_pool_map={agent_name: "default"},
        targets=[],
        modexctl_bin_dir=Path("/tmp/bin"),
        comm_kind=comm_kind,
        parent_session_id=parent_session_id,
        control_origin=control_origin,
    )


def _make_context(
    *,
    session_id: str = "real_prefix.main",
    agent_name: str = "main",
    parent_session_id: str | None = None,
    comm_kind: AgentCommKind | None = AgentCommKind.NORMAL,
) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo(
            session_id=session_id,
            agent_name=agent_name,
            parent_session_id=parent_session_id,
        ),
        comm_kind=comm_kind,
    )


@pytest.fixture(autouse=True)
def _reset_env_contextvars() -> Iterator[None]:
    """Reset _modex_env / _current_session_id around each test.

    ContextVars survive across tests within the same asyncio event loop; the
    hook sets them but never clears, so without a reset a passing test would
    mask a regression in the next one.
    """
    env_token = _modex_env.set(None)
    sid_token = _current_session_id.set(None)
    try:
        yield
    finally:
        _modex_env.reset(env_token)
        _current_session_id.reset(sid_token)


class TestNativeEnvInjectionHook:
    async def test_before_graph_sets_modex_env_contextvar(self) -> None:
        hook = NativeEnvInjectionHook(env_spec_template=_make_template())
        ctx = _make_context(session_id="abc123.main", agent_name="main")

        await hook.before_graph(ctx)

        env = _modex_env.get()
        assert env is not None
        assert env["MODEX_SESSION_ID"] == "abc123.main"
        assert env["MODEX_AGENT_NAME"] == "main"
        assert env["MODEX_WORKSPACE_ROOT"] == str(Path("/tmp/ws"))
        assert env["MODEX_INBOX_ROOT"] == str(Path("/tmp/ws/.modex"))
        assert env["MODEX_WORKDIR"] == str(Path("/tmp/ws"))
        assert env["MODEX_COMM_KIND"] == "normal"

    async def test_before_graph_sets_current_session_id_contextvar(self) -> None:
        hook = NativeEnvInjectionHook(env_spec_template=_make_template())
        ctx = _make_context(session_id="abc123.main")

        await hook.before_graph(ctx)

        assert _current_session_id.get() == "abc123.main"

    async def test_before_graph_overrides_session_id_from_ctx(self) -> None:
        # Template carries a placeholder; ctx carries the real per-turn value.
        template = _make_template(session_id="__pending__.main")
        hook = NativeEnvInjectionHook(env_spec_template=template)
        ctx = _make_context(session_id="live_prefix.main")

        await hook.before_graph(ctx)

        env = _modex_env.get()
        assert env is not None
        assert env["MODEX_SESSION_ID"] == "live_prefix.main"
        # Placeholder must NOT leak through.
        assert env["MODEX_SESSION_ID"] != "__pending__.main"

    async def test_before_graph_overrides_comm_kind_from_ctx(self) -> None:
        # Template is NORMAL; ctx carries SUBAGENT — ctx wins.
        template = _make_template(comm_kind=AgentCommKind.NORMAL)
        hook = NativeEnvInjectionHook(env_spec_template=template)
        ctx = _make_context(
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id="parent.sid",
        )

        await hook.before_graph(ctx)

        env = _modex_env.get()
        assert env is not None
        assert env["MODEX_COMM_KIND"] == "subagent"

    async def test_before_graph_overrides_parent_session_id_from_ctx(self) -> None:
        # Template parent_session_id is None; ctx.session.parent_session_id is set.
        # build_modex_vars only emits MODEX_PARENT_SESSION_ID when comm_kind is
        # SUBAGENT AND parent_session_id is not None, so ctx.comm_kind must be
        # SUBAGENT for this override to surface in the env dict.
        template = _make_template(
            comm_kind=AgentCommKind.NORMAL,
            parent_session_id=None,
        )
        hook = NativeEnvInjectionHook(env_spec_template=template)
        ctx = _make_context(
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id="parent.sid",
        )

        await hook.before_graph(ctx)

        env = _modex_env.get()
        assert env is not None
        assert env["MODEX_PARENT_SESSION_ID"] == "parent.sid"

    async def test_before_graph_comm_kind_none_uses_template(self) -> None:
        # ctx.comm_kind is None — template's comm_kind should win.
        template = _make_template(comm_kind=AgentCommKind.NORMAL)
        hook = NativeEnvInjectionHook(env_spec_template=template)
        ctx = _make_context(comm_kind=None)

        await hook.before_graph(ctx)

        env = _modex_env.get()
        assert env is not None
        assert env["MODEX_COMM_KIND"] == "normal"

    async def test_main_agent_template_has_complete_pool_map(self) -> None:
        # Mirrors the inline construction in pool_builder._wire_main_pipeline:
        # a main-agent hook template built from a pool_spec with subagents +
        # peers carries every routable agent in pool_map and all targets
        # through before_graph, so native bash tools can call
        # ``modexctl send --to <any agent>`` and ``modexctl agents``.
        main_name = "main"
        sub_name = "explore"
        peer_main = "peer_main"
        pool_name = "default"
        peer_pool = "peer_pool"

        agent_pool_map = {
            main_name: pool_name,
            sub_name: pool_name,
            peer_main: peer_pool,
        }
        targets = [
            (sub_name, "explore subagent"),
            (peer_main, "Peer pool peer_pool's main agent"),
        ]
        template = ExternalEnvSpec(
            workspace_root=Path("/tmp/ws"),
            inbox_root=Path("/tmp/ws/.modex/inbox"),
            workdir=Path("/tmp/ws"),
            session_id=f"__pending__.{main_name}",
            agent_name=main_name,
            provider_session_id="",
            agent_pool_map=agent_pool_map,
            targets=targets,
            modexctl_bin_dir=Path("/tmp/bin"),
            comm_kind=AgentCommKind.NORMAL,
        )
        hook = NativeEnvInjectionHook(env_spec_template=template)
        ctx = _make_context(session_id="conv1.main", agent_name=main_name)

        await hook.before_graph(ctx)

        env = _modex_env.get()
        assert env is not None
        # MODEX_AGENT_POOL_MAP serialises as "name=pool;..." (sorted by name).
        pool_map_str = env["MODEX_AGENT_POOL_MAP"]
        assert f"{main_name}={pool_name}" in pool_map_str
        assert f"{sub_name}={pool_name}" in pool_map_str
        assert f"{peer_main}={peer_pool}" in pool_map_str
        # MODEX_TARGETS serialises as "name=description;..." (order preserved).
        targets_str = env["MODEX_TARGETS"]
        assert f"{sub_name}=explore subagent" in targets_str
        assert f"{peer_main}=Peer pool peer_pool's main agent" in targets_str

    async def test_subagent_template_has_parent_in_pool_map(self) -> None:
        # Mirrors AgentTemplate.materialize: a subagent hook template carries
        # both itself and its parent in pool_map so ``modexctl send --to
        # <parent>`` routes correctly; the parent is the sole target per
        # star topology.
        sub_name = "explore"
        parent_name = "main"
        pool_name = "default"

        subagent_pool_map = {
            sub_name: pool_name,
            parent_name: pool_name,
        }
        subagent_targets: list[tuple[str, str]] = [(parent_name, "")]
        template = ExternalEnvSpec(
            workspace_root=Path("/tmp/ws"),
            inbox_root=Path("/tmp/ws/.modex/inbox"),
            workdir=Path("/tmp/ws"),
            session_id=f"__pending__.{sub_name}",
            agent_name=sub_name,
            provider_session_id="",
            agent_pool_map=subagent_pool_map,
            targets=subagent_targets,
            modexctl_bin_dir=Path("/tmp/bin"),
            comm_kind=AgentCommKind.SUBAGENT,
            parent_session_id=None,
        )
        hook = NativeEnvInjectionHook(env_spec_template=template)
        ctx = _make_context(
            session_id="inv1.explore",
            agent_name=sub_name,
            parent_session_id="conv1.main",
            comm_kind=AgentCommKind.SUBAGENT,
        )

        await hook.before_graph(ctx)

        env = _modex_env.get()
        assert env is not None
        pool_map_str = env["MODEX_AGENT_POOL_MAP"]
        assert f"{sub_name}={pool_name}" in pool_map_str
        assert f"{parent_name}={pool_name}" in pool_map_str
        # Empty description serialises as bare "name=".
        assert env["MODEX_TARGETS"] == f"{parent_name}="
        assert env["MODEX_COMM_KIND"] == "subagent"
        assert env["MODEX_PARENT_SESSION_ID"] == "conv1.main"

    async def test_name(self) -> None:
        hook = NativeEnvInjectionHook(env_spec_template=_make_template())
        assert hook.name == "native_env_injection"

    async def test_before_graph_prepends_modexctl_bin_dir_to_path(self) -> None:
        template = _make_template()
        hook = NativeEnvInjectionHook(env_spec_template=template)
        ctx = _make_context(session_id="s1", agent_name="main")

        await hook.before_graph(ctx)

        env = _modex_env.get()
        assert env is not None
        assert "PATH" in env
        assert str(template.modexctl_bin_dir) in env["PATH"]

    async def test_control_origin_propagates_to_modex_env(self) -> None:
        """control_origin from the spec template must reach MODEX_CONTROL_ORIGIN.

        Regression test for the native subagent ``modexctl send`` failure
        (``error: bot context not fully configured (MODEX_CONTROL_ORIGIN)``).
        AgentTemplate.materialize builds the subagent env spec WITHOUT setting
        control_origin, so it defaulted to empty string — the subagent's
        bash tools then inherited an empty MODEX_CONTROL_ORIGIN and modexctl
        could not locate the bot's HTTP listener.
        """
        template = _make_template(control_origin="http://127.0.0.1:21800")
        hook = NativeEnvInjectionHook(env_spec_template=template)
        ctx = _make_context(session_id="inv1.explore", agent_name="explore")

        await hook.before_graph(ctx)

        env = _modex_env.get()
        assert env is not None
        assert env["MODEX_CONTROL_ORIGIN"] == "http://127.0.0.1:21800"
