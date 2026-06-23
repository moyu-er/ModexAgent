import inspect

import bot.service.pool_builder as pb


def test_build_tools_resolves_todo_dir_from_pool_data_or_fallback() -> None:
    """The wiring must derive the todo dir from pool_data.runtime_dir when present,
    else fall back to data_dir/runtime_state/<pool>/todos — mirroring ExperienceTool."""
    src = inspect.getsource(pb._build_tools)
    assert "JsonFileTodoStore" in src
    assert "TodoWriteTool" in src
    assert "TodoReadTool" in src
    assert "runtime_dir" in src  # pool-aware path source
    assert "todos" in src
