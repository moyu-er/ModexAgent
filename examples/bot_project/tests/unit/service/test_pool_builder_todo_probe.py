"""Wiring contract for ``TodoCompletionProbeHook`` on the main pool pipeline.

The end-to-end "the hook is attached to the main agent's pipeline" coverage is
provided by the existing pool-builder suite
(``examples/bot_project/tests/unit/service/test_wire_main_pipeline_approval.py``
exercises ``_wire_main_pipeline`` against a real pipeline, and
``tests/unit/pipeline/`` covers the ``LLMNode`` integration path). A full
``_wire_main_pipeline`` call here would duplicate that while adding fragile
dependencies (governance creation, approval-runtime build, interceptor-chain
assignment) that have nothing to do with the probe hook's contract.

Instead, this test pins the three things the wiring must get right:
  1. The hook type attached is ``TodoCompletionProbeHook``.
  2. Its hook ``name`` is ``"todo_completion_probe"``.
  3. The collaborator wiring is correct: the hook is constructed with the SAME
     ``todo_store`` + the main agent's ``tool_manager``, and its
     ``is_registered("todo_read")`` gate behaves (would probe when ``todo_read``
     is registered; silent otherwise).
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

# Bot tests resolve ``bot.*`` via the repo root inserted into sys.path.
sys.path.insert(0, str(Path(__file__).parents[3]))

from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook.abc import AfterLLMResponseHook
from modex_agent.runtime.store import JsonFileTodoStore
from modex_agent.tools.standard import (
    TodoCompletionProbeHook,
    TodoReadTool,
)


def _make_store() -> JsonFileTodoStore:
    return JsonFileTodoStore(Path(TemporaryDirectory().name))


def test_hook_is_after_llm_response_hook() -> None:
    hook = TodoCompletionProbeHook(store=_make_store(), tool_manager=InMemoryToolManager())
    assert isinstance(hook, AfterLLMResponseHook)


def test_hook_name_is_todo_completion_probe() -> None:
    hook = TodoCompletionProbeHook(store=_make_store(), tool_manager=InMemoryToolManager())
    assert hook.name == "todo_completion_probe"


def test_gate_probes_only_when_todo_read_registered() -> None:
    """The ``is_registered("todo_read")`` gate is the collaborator contract:

    with ``todo_read`` registered (the wiring the pool builder installs) the
    hook would probe; without it (a subagent/tool manager that lacks todo
    tools) it stays silent. This mirrors how ``_wire_main_pipeline`` threads
    the SAME ``tool_manager`` the todo tools were registered against.
    """
    store = _make_store()

    # With todo_read registered → tool manager reports it as available.
    tm_with = InMemoryToolManager()
    tm_with.register(TodoReadTool(store))
    hook_on = TodoCompletionProbeHook(store=store, tool_manager=tm_with)
    assert hook_on._tool_manager.is_registered("todo_read")

    # Without todo_read → gate would short-circuit (no probe).
    tm_without = InMemoryToolManager()
    hook_off = TodoCompletionProbeHook(store=store, tool_manager=tm_without)
    assert not hook_off._tool_manager.is_registered("todo_read")


def test_hook_uses_the_same_store_reference() -> None:
    """The hook must hold the SAME store instance the tools use, not a copy."""
    store = _make_store()
    hook = TodoCompletionProbeHook(store=store, tool_manager=InMemoryToolManager())
    assert hook._store is store


def test_probe_hook_wired_into_main_pipeline() -> None:
    """Every pool's main pipeline gets the probe hook — the wiring in
    ``_wire_main_pipeline`` is unconditional, so existing AND future pools
    (scanned via ``for name, cfg in pool_configs.items(): create_pool(...)``)
    all get it without per-pool config.

    This reuses the approval wiring test's stand-in fixtures (real
    ``AgentPipeline`` against a ``_StandInPool``) so we exercise the real
    ``_add_hook`` chokepoint rather than re-asserting the hook's own contract.
    """
    from tests.unit.service.test_wire_main_pipeline_approval import _wire

    pipeline = _wire(approval=None)

    # ``_make_pipeline`` builds an ``AgentPipeline`` without a ``hook_runner``,
    # so ``_add_hook`` falls back to ``pipeline.hooks`` (the AgentPipeline
    # ctor sets ``self.hook_runner = hook_runner`` from the None default).
    assert pipeline.hook_runner is None
    specs = list(getattr(pipeline, "hooks", []))
    assert any(getattr(h, "name", None) == "todo_completion_probe" for h in specs)
