"""Tree-aware continuation hook wiring — shared by main-agent and subagent paths.

``register_tree_aware_hooks`` is the single convergence point for registering
``TodoContinuationHook`` and ``DeliverRetryHook`` with a ``SessionTreeManager``.
Both the main-agent pipeline (``_wire_main_pipeline`` in bot business code)
and the subagent pipeline (``AgentTemplate.materialize`` in framework code)
call it, so every agent that owns a turn lifecycle receives the same
tree-aware continuation hooks with the same priority ordering.

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
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook

if TYPE_CHECKING:
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager

_TREE_HOOK_PRIORITY = -1000


def register_tree_aware_hooks(
    hook_runner: HookRunner | None,
    tree: SessionTreeManager,
) -> None:
    """Register TodoContinuationHook + DeliverRetryHook on ``hook_runner``.

    ``TodoContinuationHook`` gets ``priority=-1000`` so it runs first among
    ``AfterTurnHook`` sources (it is the only hook that sets
    ``CONTINUATION_RENEW_MAX_TURNS``). ``DeliverRetryHook`` gets the default
    priority (0); it is a no-op for agents without a ``deliver`` tool
    (subagents in star topology).

    No-op when ``hook_runner`` is ``None`` (mirrors the ``_add_hook`` guard
    pattern — the runner is always present for react agents but defensive
    coverage costs nothing).
    """
    if hook_runner is None:
        return
    hook_runner.add(
        HookSpec(
            hook=TodoContinuationHook(tree=tree),
            on_error=HookErrorPolicy.LOG,
            priority=_TREE_HOOK_PRIORITY,
        )
    )
    hook_runner.add(
        HookSpec(
            hook=DeliverRetryHook(tree=tree),
            on_error=HookErrorPolicy.LOG,
        )
    )
