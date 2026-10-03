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


#: The architecture anchor allowlist — the ONLY approval/sandbox capability
#: modules an approval-free, sandbox-free boot may load. Entries:
#: - ``approval``/``sandbox`` package inits + their ``registration`` (the
#:   DefaultPlugin registration face) and ``capability`` (the registration
#:   entry's import);
#: - ``approval.config`` — pulled by ``approval.capability`` (config model);
#: - ``approval.commands``/``approval.response``/``approval.stage`` — the
#:   registration entry's command/input-stage products;
#: - ``sandbox.settings`` — pulled by ``sandbox.capability`` (config model);
#: - ``sandbox.tool_matrix`` — the tool-effect classification table the
#:   TOOLS layer reads in ``wrap_standard_tools`` (workspace path scoping
#:   for every native boot); light (pydantic + workspace.boundary only, no
#:   guard/decision/approval pull), shared data rather than feature
#:   implementation.
_BOOT_CAPABILITY_ALLOWLIST: tuple[str, ...] = (
    "modex_agent.plugins.defaults.capabilities.approval",
    "modex_agent.plugins.defaults.capabilities.approval.capability",
    "modex_agent.plugins.defaults.capabilities.approval.commands",
    "modex_agent.plugins.defaults.capabilities.approval.config",
    "modex_agent.plugins.defaults.capabilities.approval.registration",
    "modex_agent.plugins.defaults.capabilities.approval.response",
    "modex_agent.plugins.defaults.capabilities.approval.stage",
    "modex_agent.plugins.defaults.capabilities.sandbox",
    "modex_agent.plugins.defaults.capabilities.sandbox.capability",
    "modex_agent.plugins.defaults.capabilities.sandbox.registration",
    "modex_agent.plugins.defaults.capabilities.sandbox.settings",
    "modex_agent.plugins.defaults.capabilities.sandbox.tool_matrix",
)

_CAPABILITY_PREFIXES = (
    "modex_agent.plugins.defaults.capabilities.approval",
    "modex_agent.plugins.defaults.capabilities.sandbox",
)


#: Boot probe executed in a FRESH interpreter. ``sys.modules`` is
#: process-global, and CI runs this file in the same pytest process as
#: ``tests/unit/`` (whose approval/sandbox tests legitimately import
#: bundle implementation) — the anchor must observe an unpolluted boot.
_BOOT_PROBE_SCRIPT = """\
import asyncio
import importlib.util
import sys
from pathlib import Path

project = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("mini_anchor_main", project / "main.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["mini_anchor_main"] = mod
spec.loader.exec_module(mod)


async def _run() -> None:
    service = mod.build_service(project_dir=project)
    await service.initialize()
    try:
        await service.turn("anchor probe", session="anchor")
        for name in sorted(sys.modules):
            if name.startswith(__CAPABILITY_PREFIXES__):
                print("MODULE:" + name)
    finally:
        await service.stop()


asyncio.run(_run())
"""


def test_mini_project_boot_loads_no_undeclared_feature_modules(
    tmp_path: Path,
) -> None:
    """Architecture anchor: undeclared feature ⇒ no feature implementation loaded.

    mini_project declares NEITHER approval NOR a sandbox. After booting it
    exactly the way ``main.py`` does (in a fresh interpreter) and driving
    one scripted turn, the set of loaded ``...capabilities.approval`` /
    ``...capabilities.sandbox`` modules must be EXACTLY
    ``_BOOT_CAPABILITY_ALLOWLIST`` — what DefaultPlugin registration itself
    costs, plus the tools layer's shared tool-effect table. Any new leak (a
    lazily-imported guard, classifier, resumer, renderer, or UI reaching an
    undeclared boot) fails here with the full diff, instead of silently
    re-coupling every deployment to features it never declared.
    """
    import subprocess

    project = tmp_path / "mini_project"
    shutil.copytree(
        MINI_PROJECT_DIR,
        project,
        ignore=shutil.ignore_patterns("tests", "__pycache__", DATA_DIR_NAME),
    )
    script = tmp_path / "boot_probe.py"
    script.write_text(
        _BOOT_PROBE_SCRIPT.replace("__CAPABILITY_PREFIXES__", repr(_CAPABILITY_PREFIXES)),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(script), str(project)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        "boot probe failed:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    loaded = {
        line.removeprefix("MODULE:")
        for line in result.stdout.splitlines()
        if line.startswith("MODULE:")
    }

    allowlist = set(_BOOT_CAPABILITY_ALLOWLIST)
    assert loaded == allowlist, (
        "mini_project boot leaked approval/sandbox capability modules "
        "beyond the registration allowlist:\n"
        f"  extra loaded:   {sorted(loaded - allowlist)}\n"
        f"  allowlist miss: {sorted(allowlist - loaded)}\n"
        "An approval-free, sandbox-free deployment must not load feature "
        "implementation (guards, classifiers, resumers, renderers, UI); "
        "either fix the new leak or — only if the module is "
        "registration-required and light — extend the allowlist "
        "consciously in _BOOT_CAPABILITY_ALLOWLIST."
    )
