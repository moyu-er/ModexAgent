from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modex_agent.hook import HookRunner
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.hook.builtin.length_guard import LengthGuardHook
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
from modex_agent.hook.wiring import register_tree_aware_hooks


def _make_tree_mock() -> MagicMock:
    tree = MagicMock()
    tree.tree_id_for_session = AsyncMock(return_value=None)
    tree.get_active_subtree_nodes = AsyncMock(return_value=[])
    return tree


def test_registers_all_hooks() -> None:
    runner = HookRunner()
    tree = _make_tree_mock()

    register_tree_aware_hooks(runner, tree)

    specs = runner.hook_specs
    hook_types = {type(spec.hook) for spec in specs}
    assert TodoContinuationHook in hook_types
    assert DeliverRetryHook in hook_types
    assert LengthGuardHook in hook_types


def test_todo_continuation_has_negative_priority() -> None:
    runner = HookRunner()
    tree = _make_tree_mock()

    register_tree_aware_hooks(runner, tree)

    specs = runner.hook_specs
    todo_spec = next(s for s in specs if isinstance(s.hook, TodoContinuationHook))
    assert todo_spec.priority == -1000


def test_deliver_retry_has_default_priority() -> None:
    runner = HookRunner()
    tree = _make_tree_mock()

    register_tree_aware_hooks(runner, tree)

    specs = runner.hook_specs
    deliver_spec = next(s for s in specs if isinstance(s.hook, DeliverRetryHook))
    assert deliver_spec.priority == 0


def test_length_guard_has_default_priority() -> None:
    runner = HookRunner()
    tree = _make_tree_mock()

    register_tree_aware_hooks(runner, tree)

    specs = runner.hook_specs
    guard_spec = next(s for s in specs if isinstance(s.hook, LengthGuardHook))
    assert guard_spec.priority == 0


def test_noop_when_hook_runner_is_none() -> None:
    tree = _make_tree_mock()

    register_tree_aware_hooks(None, tree)


def test_both_hooks_share_same_tree_instance() -> None:
    runner = HookRunner()
    tree = _make_tree_mock()

    register_tree_aware_hooks(runner, tree)

    specs = runner.hook_specs
    todo_hook = next(s for s in specs if isinstance(s.hook, TodoContinuationHook)).hook
    deliver_hook = next(s for s in specs if isinstance(s.hook, DeliverRetryHook)).hook
    assert todo_hook._tree is tree
    assert deliver_hook._tree is tree


def test_roster_named_hooks_are_skipped() -> None:
    runner = HookRunner()
    tree = _make_tree_mock()

    register_tree_aware_hooks(
        runner,
        tree,
        roster_hook_names=frozenset(
            {"todo_continuation", "deliver_retry", "length_guard"}
        ),
    )

    assert runner.hook_specs == []


def test_partial_roster_names_skip_only_named_hooks() -> None:
    runner = HookRunner()
    tree = _make_tree_mock()

    register_tree_aware_hooks(
        runner, tree, roster_hook_names=frozenset({"todo_continuation"})
    )

    hook_types = {type(spec.hook) for spec in runner.hook_specs}
    assert TodoContinuationHook not in hook_types
    assert DeliverRetryHook in hook_types
    assert LengthGuardHook in hook_types
