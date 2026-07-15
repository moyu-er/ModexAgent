"""Tests for bot.workspace.dispatch.WorkspaceMessageDispatcher."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from bot.workspace.dispatch import WorkspaceMessageDispatcher
from modex_agent.workspace.registry import WorkspaceRegistry
from modex_agent.workspace.store import GlobalWorkspaceStore
from modex_agent.workspace.routing import WorkspaceResolver
from modex_agent.workspace.runtime import resolve_workspace_root
from ._stubs import StubFactory, StubResources


def _resolver(tmp_path: Path) -> tuple[WorkspaceResolver[StubResources], Path]:
    home = tmp_path / "proj"
    home.mkdir()
    registry = WorkspaceRegistry(
        home=home, data_dir_name=".modex",
        factory=StubFactory(), store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"),
    )
    return WorkspaceResolver(registry=registry), home


async def test_dispatch_once_resolves_routes_and_binds_root(tmp_path: Path) -> None:
    resolver, home = _resolver(tmp_path)
    seen: dict[str, object] = {}

    async def receive() -> AsyncIterator[str]:
        yield "hello"

    def workspace_of(message: str) -> Path:
        return home

    async def route_one(resources: StubResources, message: str) -> None:
        seen["root"] = resolve_workspace_root()  # bound for this turn
        seen["resources_target"] = resources.target
        seen["message"] = message

    dispatcher = WorkspaceMessageDispatcher(
        receive=receive, resolver=resolver,
        workspace_of=workspace_of, route_one=route_one,
    )
    await dispatcher.dispatch_once()

    # message's workspace == home -> StubResources(home); root bound to home.
    assert seen["message"] == "hello"
    assert seen["resources_target"] == home
    assert seen["root"] == home


async def test_dispatch_once_with_async_generator_receive(tmp_path: Path) -> None:
    """Real InputAdapter.receive() is an async generator, not a coroutine."""
    resolver, home = _resolver(tmp_path)
    seen: dict[str, object] = {}

    async def receive() -> AsyncIterator[str]:
        yield "hello"

    def workspace_of(message: str) -> Path:
        return home

    async def route_one(resources: StubResources, message: str) -> None:
        seen["message"] = message

    dispatcher = WorkspaceMessageDispatcher(
        receive=receive, resolver=resolver,
        workspace_of=workspace_of, route_one=route_one,
    )
    await dispatcher.dispatch_once()
    assert seen["message"] == "hello"


async def test_run_ends_on_stop_async_iteration(tmp_path: Path) -> None:
    resolver, home = _resolver(tmp_path)

    async def receive() -> AsyncIterator[str]:
        yield "x"
        yield "y"

    calls: list[str] = []

    def workspace_of(message: str) -> Path:
        return home

    async def route_one(resources: StubResources, message: str) -> None:
        calls.append(message)

    dispatcher = WorkspaceMessageDispatcher(
        receive=receive, resolver=resolver,
        workspace_of=workspace_of, route_one=route_one,
    )
    await dispatcher.run()
    assert calls == ["x", "y"]
