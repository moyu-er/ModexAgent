from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.registry import WorkspaceRegistry
from modex_agent.workspace.store import GlobalWorkspaceStore


class _FakeFactory:
    async def materialize(self, ctx): return {"t": ctx.target}
    async def evict(self, resources) -> None: return None


@pytest.mark.asyncio
async def test_open_workspace_registers_but_does_not_persist_session_map(tmp_path: Path) -> None:
    """open_workspace validates and registers a workspace without any session map."""
    home = tmp_path / "home"; home.mkdir()
    target = tmp_path / "wsB"; target.mkdir()
    reg = WorkspaceRegistry(home=home, data_dir_name=".modex", factory=_FakeFactory(),
                            store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"))
    controller = WorkspaceController(registry=reg, data_dir_name=".modex", enabled=True)
    res = await controller.open_workspace(str(target))
    assert res.success and res.current_path.resolve() == target.resolve()
    # Registry has the workspace registered
    assert (await reg.get_or_open(target)).target == target.resolve()
