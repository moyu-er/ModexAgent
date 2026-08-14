"""Tests for bot.workspace.factory.PoolResourceFactory (closure-injected)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bot.workspace.factory import PoolResourceFactory

from modex_agent.workspace.context import WorkspaceContext


async def test_materialize_delegates_to_build_closure(tmp_path: Path) -> None:
    home = tmp_path / "proj"
    home.mkdir()
    target = tmp_path / "ws"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=home)

    calls: list[Any] = []
    sentinel: Any = object()

    async def build(c: WorkspaceContext) -> Any:
        calls.append(c)
        return sentinel

    async def stop(r: Any) -> None:
        calls.append(("stop", r))

    factory = PoolResourceFactory(build_resources=build, stop_resources=stop)  # type: ignore[arg-type]
    result = await factory.materialize(ctx)
    assert result is sentinel
    assert calls == [ctx]


async def test_evict_delegates_to_stop_closure(tmp_path: Path) -> None:
    sentinel: Any = object()
    stopped: list[Any] = []

    async def build(c: WorkspaceContext) -> Any:
        return sentinel

    async def stop(r: Any) -> None:
        stopped.append(r)

    factory = PoolResourceFactory(build_resources=build, stop_resources=stop)  # type: ignore[arg-type]
    await factory.evict(sentinel)  # type: ignore[arg-type]
    assert stopped == [sentinel]
