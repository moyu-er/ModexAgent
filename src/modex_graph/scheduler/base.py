"""``Scheduler[S]`` ABC — the seam between ``GraphEngine`` and execution loops.

``GraphEngine`` reads ``CompiledGraph.scheduler`` (a ``SchedulerKind``) and
delegates ``run_async`` / ``run`` to the selected ``Scheduler`` implementation.
The ABC owns the concrete ``run`` (sync wrapper around ``run_async``);
subclasses override ``run_async`` only.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import GraphContext
    from ..state import GraphState


class Scheduler[S: "GraphState"](ABC):
    """Pluggable scheduling strategy for executing a `CompiledGraph`.

    `GraphEngine` delegates to a `Scheduler` instance selected by
    `CompiledGraph.scheduler` (`SchedulerKind`). The Scheduler owns the
    execution loop; `GraphEngine` is a thin entry point that forwards
    `run_async` / `run` calls.

    Implementations:

    - `LinearScheduler` — sequential execution (the original `GraphEngine`
      logic). Default.
    - `ParallelScheduler` — continuous multi-instance execution with
      `ctx.dispatch` routing.

    The ABC receives the `CompiledGraph` at construction time (mirroring the
    original `GraphEngine.__init__(graph)` pattern) and `GraphContext` per
    `run_async` / `run` call.
    """

    @abstractmethod
    async def run_async(self, ctx: GraphContext[S]) -> S:
        """Run the graph asynchronously. Returns the final state (`ctx.state`).

        The terminal node writes its result to a state field; the caller
        reads it after this returns.
        """
        ...

    def run(self, ctx: GraphContext[S]) -> S:
        """Run the graph synchronously. Returns the final state (`ctx.state`).

        Wraps `run_async` in `asyncio.run` (or a thread-pool fallback when a
        loop is already running).

        For standalone scripts / CLI / REPL usage. Event-loop-bound agent
        runtimes (ReAct) use `run_async` directly.

        If called from within a running event loop (e.g. pytest-asyncio auto
        mode), runs the coroutine in a separate thread with its own loop to
        avoid `asyncio.run() cannot be called from a running event loop`.

        Concrete on the ABC — subclasses override `run_async` only.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async(ctx))
        # There's a running loop — run in a separate thread.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, self.run_async(ctx))
            return future.result()


__all__ = ["Scheduler"]
