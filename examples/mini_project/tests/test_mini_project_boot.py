"""CI anchor for examples/mini_project — the second-consumer proof.

ADR-0052's runnable-defaults claim: a NEW project boots from one scope
declaration + the framework ``AppService`` bootstrap + a few plugins, with
zero assembly code copied from examples/bot_project. This test boots the
example exactly the way its ``main.py`` does (against a tmp copy so the
in-repo tree stays read-only), drives ONE offline turn through the real
production road (declaration → component registry → ``create_pool`` →
dispatch → scripted LLM — no network, no keys, no real model), and asserts:

- the custom TOOL executed (its side-effect log file was written),
- the custom HOOK fired (the per-turn log file was written),
- the turn completed (the scripted final content came back) and the
  framework tools declared alongside the custom ones are registered,
- the example stays SMALL: the hand-written line count (everything except
  tests/ and README.md) stays under the ceiling — the cost tripwire that
  keeps "composability" honest; growth beyond it must be a conscious
  decision, so the count is printed in the failure message.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

MINI_PROJECT_DIR = Path(__file__).resolve().parents[1]

#: Hand-written example files the cost tripwire counts (everything except
#: tests/ and README.md — the example IS the deliverable, so its size is
#: the metric; the framework code it composes lives in src/).
TRIPWIRE_FILES: tuple[str, ...] = (
    "config/app.yml",
    "config/scopes/mini.yml",
    "agents/main.md",
    "mini_plugins/mini.py",
    "main.py",
)
LINE_CEILING = 400

#: Runtime data dir of the booted service (workspace home + data_dir_name).
DATA_DIR_NAME = ".modex"


def _load_main_module(project_dir: Path):
    """Import the example's ``main.py`` from the tmp copy under a stable name."""
    module_name = "mini_project_main"
    spec = importlib.util.spec_from_file_location(module_name, project_dir / "main.py")
    if spec is None or spec.loader is None:  # pragma: no cover - importlib contract
        raise RuntimeError(f"cannot import {project_dir / 'main.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


async def test_mini_project_boots_and_drives_one_offline_turn(tmp_path: Path) -> None:
    """Boot mini_project like main.py, drive one scripted turn, observe effects."""
    from modex_agent.multi_agent.session_tree.request_scope import RequestOutcome

    project = tmp_path / "mini_project"
    shutil.copytree(
        MINI_PROJECT_DIR,
        project,
        ignore=shutil.ignore_patterns("tests", "__pycache__", DATA_DIR_NAME),
    )

    main = _load_main_module(project)
    service = main.build_service(project_dir=project)
    await service.initialize()
    try:
        # The declared pool booted with both the custom and the framework tools.
        (pool_name, instance), = service.pools.items()
        registered = set(instance.tool_manager.list_tools())
        assert {"mini_echo", "read"} <= registered, (
            f"pool {pool_name!r} tool roster missing declared tools: {sorted(registered)}"
        )

        result = await service.turn("hello from the mini test", session="t1")

        assert result.outcome is RequestOutcome.FINISHED, (
            f"turn did not finish: outcome={result.outcome} "
            f"fail_reason={result.fail_reason!r}"
        )
        assert result.agent_result is not None
        assert "mini turn complete" in (result.agent_result.content or "")

        data_dir = project / DATA_DIR_NAME
        echo_log = (data_dir / "mini_echo.log").read_text(encoding="utf-8")
        assert "hello from the mini test" in echo_log, (
            f"custom echo tool did not run; log: {echo_log!r}"
        )
        turn_log = (data_dir / "mini_turns.log").read_text(encoding="utf-8")
        assert "t1" in turn_log, f"custom turn-logger hook did not fire; log: {turn_log!r}"
    finally:
        await service.stop()


def test_mini_project_hand_written_line_count_under_ceiling() -> None:
    """Cost tripwire: the example (minus tests/README) stays under the ceiling."""
    counts = {
        rel: sum(
            1
            for line in (MINI_PROJECT_DIR / rel).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        for rel in TRIPWIRE_FILES
    }
    total = sum(counts.values())
    assert total < LINE_CEILING, (
        f"mini_project grew to {total} hand-written non-blank lines "
        f"(ceiling {LINE_CEILING}); per file: {counts}. Either shrink the "
        "example or promote the missing generic piece into the framework "
        "and raise this ceiling consciously."
    )
