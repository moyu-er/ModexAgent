import json
import os
from pathlib import Path

import pytest

from framework.workspace.context import DefaultWorkspaceContext
from framework.workspace.models import WorkspaceSwitchCallback


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home_project"
    h.mkdir()
    (h / ".modex").mkdir()
    return h


@pytest.fixture
def target(tmp_path):
    t = tmp_path / "target_project"
    t.mkdir()
    return t


class TestDefaultWorkspaceContext:
    @pytest.fixture(autouse=True)
    def _restore_cwd(self):
        original = os.getcwd()
        yield
        os.chdir(original)

    # -- Initial state --
    def test_initial_state(self, home):
        ctx = DefaultWorkspaceContext(home=home)
        assert ctx.home == home
        assert ctx.current == home
        assert ctx.data_dir == home / ".modex"
        assert ctx.is_home is True

    def test_data_dir_respects_env_var(self, home, monkeypatch):
        monkeypatch.setenv("MODEX_DATA_DIR", ".custom")
        ctx = DefaultWorkspaceContext(home=home)
        assert ctx.data_dir == home / ".custom"

    # -- cd --
    @pytest.mark.asyncio
    async def test_cd_success(self, home, target):
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.cd(str(target))
        assert result.success is True
        assert result.current_path == target
        assert ctx.current == target
        assert ctx.is_home is False
        assert ctx.data_dir == target / ".modex"
        assert (target / ".modex").is_dir()

    @pytest.mark.asyncio
    async def test_cd_creates_modex_dir(self, home, target):
        ctx = DefaultWorkspaceContext(home=home)
        await ctx.cd(str(target))
        assert (target / ".modex").is_dir()

    @pytest.mark.asyncio
    async def test_cd_nonexistent_path_fails(self, home):
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.cd("/nonexistent/path")
        assert result.success is False
        assert "path not found" in result.notice
        assert ctx.current == home

    @pytest.mark.asyncio
    async def test_cd_file_not_directory_fails(self, home, tmp_path):
        file_path = tmp_path / "a_file.txt"
        file_path.write_text("hello")
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.cd(str(file_path))
        assert result.success is False
        assert "not a directory" in result.notice

    @pytest.mark.asyncio
    async def test_cd_same_path_idempotent(self, home):
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.cd(str(home))
        assert result.success is True
        assert ctx.current == home

    @pytest.mark.asyncio
    async def test_cd_empty_path_fails(self, home):
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.cd("")
        assert result.success is False
        assert "invalid" in result.notice

    # -- active checker --
    @pytest.mark.asyncio
    async def test_cd_blocked_by_active_checker(self, home, target):
        ctx = DefaultWorkspaceContext(home=home, active_checker=lambda: True)
        result = await ctx.cd(str(target))
        assert result.success is False
        assert "agents are busy" in result.notice
        assert ctx.current == home

    @pytest.mark.asyncio
    async def test_cd_allowed_when_agent_idle(self, home, target):
        ctx = DefaultWorkspaceContext(home=home, active_checker=lambda: False)
        result = await ctx.cd(str(target))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_cd_allowed_when_no_checker(self, home, target):
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.cd(str(target))
        assert result.success is True

    # -- persistence --
    @pytest.mark.asyncio
    async def test_cd_persists_cwd_json(self, home, target):
        ctx = DefaultWorkspaceContext(home=home)
        await ctx.cd(str(target))
        cwd_file = home / ".modex" / "cwd.json"
        assert cwd_file.exists()
        data = json.loads(cwd_file.read_text(encoding="utf-8"))
        assert data["path"] == str(target)

    @pytest.mark.asyncio
    async def test_exit_clears_cwd_json(self, home, target):
        ctx = DefaultWorkspaceContext(home=home)
        await ctx.cd(str(target))
        cwd_file = home / ".modex" / "cwd.json"
        assert cwd_file.exists()
        await ctx.exit()
        assert not cwd_file.exists()

    # -- exit --
    @pytest.mark.asyncio
    async def test_exit_success(self, home, target):
        ctx = DefaultWorkspaceContext(home=home)
        await ctx.cd(str(target))
        result = await ctx.exit()
        assert result.success is True
        assert result.current_path == home
        assert ctx.current == home
        assert ctx.is_home is True

    @pytest.mark.asyncio
    async def test_exit_when_already_home(self, home):
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.exit()
        assert result.success is False
        assert "already at home" in result.notice

    # -- os.chdir --
    @pytest.mark.asyncio
    async def test_cd_changes_os_cwd(self, home, target):
        ctx = DefaultWorkspaceContext(home=home)
        await ctx.cd(str(target))
        assert os.getcwd() == str(target)

    @pytest.mark.asyncio
    async def test_exit_restores_os_cwd(self, home, target):
        ctx = DefaultWorkspaceContext(home=home)
        await ctx.cd(str(target))
        await ctx.exit()
        assert os.getcwd() == str(home)

    # -- callbacks --
    @pytest.mark.asyncio
    async def test_callback_called_on_cd(self, home, target):
        calls = []

        class Spy(WorkspaceSwitchCallback):
            async def on_workspace_switch(self, old_dir, new_dir):
                calls.append((old_dir, new_dir))

        ctx = DefaultWorkspaceContext(home=home)
        ctx.register_callback(Spy())
        await ctx.cd(str(target))
        assert len(calls) == 1
        assert calls[0][0] == home / ".modex"
        assert calls[0][1] == target / ".modex"

    @pytest.mark.asyncio
    async def test_callback_called_on_exit(self, home, target):
        calls = []

        class Spy(WorkspaceSwitchCallback):
            async def on_workspace_switch(self, old_dir, new_dir):
                calls.append((old_dir, new_dir))

        ctx = DefaultWorkspaceContext(home=home)
        ctx.register_callback(Spy())
        await ctx.cd(str(target))
        await ctx.exit()
        assert len(calls) == 2
        assert calls[0][1] == target / ".modex"
        assert calls[1][1] == home / ".modex"

    @pytest.mark.asyncio
    async def test_callback_failure_maintains_state(self, home, target):
        class Failing(WorkspaceSwitchCallback):
            async def on_workspace_switch(self, old_dir, new_dir):
                raise RuntimeError("boom")

        os.chdir(home)
        ctx = DefaultWorkspaceContext(home=home)
        ctx.register_callback(Failing())
        result = await ctx.cd(str(target))
        assert result.success is False
        assert "internal error" in result.notice
        assert ctx.current == home
        assert os.getcwd() == str(home)

    # -- restore --
    @pytest.mark.asyncio
    async def test_restore_no_cwd_file_returns_none(self, home):
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.restore()
        assert result is None

    @pytest.mark.asyncio
    async def test_restore_with_cwd_file(self, home, target):
        cwd_file = home / ".modex" / "cwd.json"
        cwd_file.write_text(json.dumps({"path": str(target)}), encoding="utf-8")
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.restore()
        assert result is not None
        assert result.success is True
        assert ctx.current == target

    @pytest.mark.asyncio
    async def test_restore_deleted_path_returns_none(self, home, tmp_path):
        gone = tmp_path / "deleted_project"
        gone.mkdir()
        cwd_file = home / ".modex" / "cwd.json"
        cwd_file.write_text(json.dumps({"path": str(gone)}), encoding="utf-8")
        gone.rmdir()
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.restore()
        assert result is None
        assert ctx.current == home

    @pytest.mark.asyncio
    async def test_restore_malformed_json_returns_none(self, home):
        cwd_file = home / ".modex" / "cwd.json"
        cwd_file.write_text("not json", encoding="utf-8")
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.restore()
        assert result is None
        assert ctx.current == home

    @pytest.mark.asyncio
    async def test_restore_missing_path_key_returns_none(self, home):
        cwd_file = home / ".modex" / "cwd.json"
        cwd_file.write_text(json.dumps({"other": "value"}), encoding="utf-8")
        ctx = DefaultWorkspaceContext(home=home)
        result = await ctx.restore()
        assert result is None
        assert ctx.current == home
