from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from bot.eval.harbor import entry as entry_module


@pytest.mark.asyncio
async def test_environment_entry_dispatches_pool_mode_without_running_bare_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = {
        "MODEX_AGENT_MODE": "pool",
        "LLM_MODEL": "openai/scripted-model",
        "MODEX_EXPERIMENT_ID": "exp-id",
        "MODEX_EXPERIMENT_NAME": "terminal-bench.pool",
        "MODEX_EXPERIMENT_DATASET_ID": "dataset-id",
        "MODEX_EXPERIMENT_ITEM_ID": "item-id",
        "MODEX_MEMORY_NS": "pool-memory",
        "MODEX_TASK_INPUT_DIR": str(tmp_path),
        "MODEX_AGENT_OUTPUT_DIR": str(tmp_path / "logs"),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    pool_execute = AsyncMock()
    bare_execute = AsyncMock()

    with (
        patch("bot.eval.harbor.pool_mode.execute_pool_entry", pool_execute),
        patch.object(entry_module, "execute_entry", bare_execute),
    ):
        await entry_module._run_from_environment()

    pool_execute.assert_awaited_once()
    bare_execute.assert_not_awaited()
