"""`InterruptPolicy` ABC + `CrashPolicy` default (ticket 04).

When a node's `execute()` raises `GraphInterrupt` and the node does not
catch it, the exception propagates to the graph level. The scheduler
(under `ParallelScheduler`) or the engine (under `LinearScheduler`)
consults the configured `InterruptPolicy` to decide what happens to the
graph instance and any other running instances.

Per ticket 04:

- `InterruptPolicy` is the ABC (rule 7: ABC, not Protocol). The graph
  instance carries a configurable policy. Business modules may subclass
  it to implement custom behavior (e.g. `WaitOthersPolicy` — let other
  running instances finish before pausing; `NodeOnlyPolicy` — only
  affect the interrupting node, others continue).
- `CrashPolicy` is the default. Its observed behavior is: the graph
  instance transitions to `crashed`, a checkpoint is persisted, other
  running instances are cancelled (asyncio task cancellation), and the
  instance waits for external recovery (reload via `graph_instance_id`
  + restore from checkpoint).

`CrashPolicy.handle_interrupt` is intentionally a no-op. The actual
crash behavior — instance cancellation, checkpoint scheduling, and
status transition — is handled by the scheduler's existing exception
propagation path (which invokes the policy as part of that flow). The
policy is the EXTENSION POINT: a non-default subclass overrides
`handle_interrupt` to inject custom behavior (e.g. record telemetry,
emit an event, or coordinate across instances) before the scheduler's
default crash flow takes effect. `CrashPolicy` exists so that the
default path has a concrete, instantiable policy object and so that
subclasses have a stable base to inherit from.

The policy receives `graph_instance_id` and `checkpoint_store` so that
custom implementations can persist additional recovery metadata or
inspect the latest checkpoint. `CrashPolicy` does not use them — the
scheduler has already saved the checkpoint before invoking the policy.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .checkpoint_store import CheckpointStore
from .exceptions import GraphInterrupt


class InterruptPolicy(ABC):
    """Graph-level `GraphInterrupt` handling policy (ticket 04).

    When a node's `execute()` raises `GraphInterrupt` and the node does
    not catch it, the scheduler/engine consults the configured
    `InterruptPolicy` to decide what happens to the graph instance and
    any other running instances.

    The policy is per-graph-instance (configurable). Business modules
    may subclass to implement custom behavior.
    """

    @abstractmethod
    async def handle_interrupt(
        self,
        interrupt: GraphInterrupt,
        graph_instance_id: int,
        checkpoint_store: CheckpointStore,
    ) -> None:
        """Handle a `GraphInterrupt` that propagated to the graph level.

        Called by the scheduler when a node raises `GraphInterrupt` and
        it is not caught by the node. The policy decides:

        - Whether to pause/crash the graph instance.
        - Whether to cancel other running instances.
        - Whether to save a checkpoint.
        - What status to set on the graph instance.

        The scheduler invokes this as part of its exception-handling
        flow; the policy is a hook, not the sole actor. The scheduler
        remains responsible for instance cancellation and checkpoint
        scheduling (its existing default behavior), unless the policy
        actively overrides that flow.
        """
        ...


class CrashPolicy(InterruptPolicy):
    """Default `InterruptPolicy` — crash the graph instance (ticket 04).

    Observed behavior (delegated to the scheduler's default exception
    propagation):

    1. `GraphInterrupt` propagates to `GraphEngine` → graph instance
       status transitions to `crashed`.
    2. Checkpoint is persisted (the scheduler's `_schedule_checkpoint`
       runs before the policy is invoked).
    3. Other running instances are cancelled (asyncio task
       cancellation — handled by the scheduler's exception propagation).
    4. Wait for external recovery (`graph_instance_id` reload +
       checkpoint restore).
    5. On recovery: re-enter the interrupted node + re-dispatch other
       interrupted nodes.

    `handle_interrupt` is a no-op: the default crash behavior is what
    happens when the scheduler does not receive an overriding policy
    decision. `CrashPolicy` exists as the concrete default and as the
    stable base for business subclasses (`WaitOthersPolicy`,
    `NodeOnlyPolicy`, etc.).
    """

    async def handle_interrupt(
        self,
        interrupt: GraphInterrupt,
        graph_instance_id: int,
        checkpoint_store: CheckpointStore,
    ) -> None:
        """No-op. The scheduler handles cancellation, checkpoint, and status."""
        # Intentionally empty: the scheduler's default exception
        # propagation performs the crash (instance cancellation,
        # checkpoint save, status transition). This method is the
        # extension point — subclasses override it to inject custom
        # behavior. See the module docstring for the full rationale.
        return None
