"""Tree-aware continuation hook wiring — shared by main-agent and subagent paths.

``register_tree_aware_hooks`` is the single convergence point for registering
the tree-aware continuation hooks (``TodoContinuationHook`` and
``DeliverRetryHook``, both wired with a ``SessionTreeManager``) and the
tree-agnostic ``LengthGuardHook``. Both the main-agent pipeline
(``_wire_main_pipeline`` in bot business code) and the subagent pipeline
(``AgentTemplate.materialize`` in framework code) call it, so every agent
that owns a turn lifecycle receives the same continuation + length-guard
hooks with the same priority ordering.

Why a shared function: ``tree_manager`` is a per-pool resource created in
``factory.create_pool`` — it does not exist at workspace-level ``shared_hooks``
build time. Both registration sites (main + subagent) need it, so the hook
construction + ``HookSpec`` wrapping + priority assignment must converge to
one function to prevent divergence (architecture rule 15).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.hook import HookErrorPolicy, HookRunner, HookSpec
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.hook.builtin.length_guard import LengthGuardHook
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
from modex_agent.runtime.store import TodoStore

if TYPE_CHECKING:
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager

_TREE_HOOK_PRIORITY = -1000


def register_tree_aware_hooks(
    hook_runner: HookRunner | None,
    tree: SessionTreeManager,
    *,
    roster_hook_names: frozenset[str] = frozenset(),
    todo_store: TodoStore | None = None,
) -> None:
    """Register TodoContinuationHook + DeliverRetryHook + LengthGuardHook.

    ``TodoContinuationHook`` gets ``priority=-1000`` so it runs first among
    ``AfterTurnHook`` sources (its reminder, including the active todo list,
    should land before other hooks' reminders). ``DeliverRetryHook`` and
    ``LengthGuardHook`` get the default priority (0); the deliver hook is a
    no-op for agents without a ``deliver`` tool (subagents in star topology),
    and the length guard needs no tree — it acts on per-turn LLM state alone.

    ``todo_store`` is the pool-level store injected into
    ``TodoContinuationHook`` (same seam as the todo tool factory); ``None``
    yields a silently skipping hook.

    No-op when ``hook_runner`` is ``None`` (mirrors the ``_add_hook`` guard
    pattern — the runner is always present for react agents but defensive
    coverage costs nothing).

    A hook whose roster name (its ``name`` attribute equals the factory
    registration name for all hooks here) appears in ``roster_hook_names``
    was already dispatched onto the runner by the assembly core — the
    roster reference wins and the code-wired default is skipped (the same
    name-based dedup as the core's ``extra_hooks``).
    """
    if hook_runner is None:
        return
    todo_hook = TodoContinuationHook(tree=tree, todo_store=todo_store)
    if todo_hook.name not in roster_hook_names:
        hook_runner.add(
            HookSpec(
                hook=todo_hook,
                on_error=HookErrorPolicy.LOG,
                priority=_TREE_HOOK_PRIORITY,
            )
        )
    deliver_hook = DeliverRetryHook(tree=tree)
    if deliver_hook.name not in roster_hook_names:
        hook_runner.add(
            HookSpec(
                hook=deliver_hook,
                on_error=HookErrorPolicy.LOG,
            )
        )
    length_guard_hook = LengthGuardHook()
    if length_guard_hook.name not in roster_hook_names:
        hook_runner.add(
            HookSpec(
                hook=length_guard_hook,
                on_error=HookErrorPolicy.LOG,
            )
        )
