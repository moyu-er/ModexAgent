"""`InterruptPolicy` ABC — graph-level `GraphInterrupt` handling policy.

When a node's `execute()` raises `GraphInterrupt` and the node does not
catch it, the exception propagates to the graph level. The scheduler
(under `ParallelScheduler`) or the engine (under `LinearScheduler`)
consults the configured `InterruptPolicy` to decide what happens to the
graph instance and any other running instances.

Design:

- `InterruptPolicy` is the ABC (rule 7: ABC, not Protocol). The graph
  instance carries a configurable policy. Business modules may subclass
  it to implement custom behavior (e.g. `WaitOthersPolicy` — let other
  running instances finish before pausing; `NodeOnlyPolicy` — only
  affect the interrupting node, others continue).

The policy is the EXTENSION POINT: a subclass overrides `handle_interrupt`
to inject custom behavior (e.g. record telemetry, emit an event, or
coordinate across instances) before the scheduler's default crash flow
takes effect. The scheduler remains responsible for instance cancellation
and status transition (its existing default behavior), unless the policy
actively overrides that flow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .exceptions import GraphInterrupt


class InterruptPolicy(ABC):
    """Graph-level `GraphInterrupt` handling policy.

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
    ) -> None:
        """Handle a `GraphInterrupt` that propagated to the graph level.

        Called by the scheduler when a node raises `GraphInterrupt` and
        it is not caught by the node. The policy decides:

        - Whether to pause/crash the graph instance.
        - Whether to cancel other running instances.
        - What status to set on the graph instance.

        The scheduler invokes this as part of its exception-handling
        flow; the policy is a hook, not the sole actor. The scheduler
        remains responsible for instance cancellation and status
        transition (its existing default behavior), unless the policy
        actively overrides that flow.
        """
        ...
