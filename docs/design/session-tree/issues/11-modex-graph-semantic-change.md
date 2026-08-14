# T11: modex_graph semantic change — resolution

> Type: `wayfinder:grilling` (HITL)
> Status: **Resolved** (validated + hidden dependency documented 2026-08-11)
> Blocks: T08

## Question

How should modex_graph handle "graph scheduling ends without reaching END"?

## Resolution

**Graph scheduling loop exits with no executable node + END not reached → graph FAILED. Two changes required (NOT optional — both must ship together).**

### The problem

LinearScheduler (linear.py:126): node has no dispatches → `raise RoutingError`. This crashes the graph. But "no next node" just means the graph can't continue — it should be FAILED, not a crash.

### Changes (BOTH required)

**1. LinearScheduler — break instead of RoutingError** (`linear.py:126`):

```python
# Before (line 122-126):
previous = current
if self._dispatches:
    current = next(iter(self._dispatches.keys()))
else:
    raise RoutingError(f"Node {previous!r} did not deliver.")

# After:
previous = current
if self._dispatches:
    current = next(iter(self._dispatches.keys()))
else:
    break  # No executable node — graph ends here
```

**2. GraphOrchestrator — check END reached** (`graph_orchestrator.py:340-341`):

```python
# Before:
final_state = await GraphEngine(compiled).run_async(ctx)
status = GraphInstanceStatus.COMPLETED

# After:
final_state = await GraphEngine(compiled).run_async(ctx)
end_reached = ctx.coordinator.node_state_store.load_latest_completed(
    compiled.nodes[GraphNode.END].node_id
) is not None
status = GraphInstanceStatus.COMPLETED if end_reached else GraphInstanceStatus.FAILED
```

### Hidden dependency (validated 2026-08-11)

**Both changes MUST ship together.** The `break` alone is a **semantic change**, not a harmless refactor:

| Aspect | Current (RoutingError) | break alone (without END-check) | break + END-check |
|---|---|---|---|
| LinearScheduler exit | Raises → propagates | Returns `ctx.state` normally | Returns `ctx.state` normally |
| `ctx.set_current_instance(None)` (line 130) | Skipped (exception) | **Runs** | **Runs** |
| `return ctx.state` (line 131) | Skipped (exception) | **Runs** | **Runs** |
| GraphOrchestrator status (line 341) | CRASHED (via `except Exception`) | **COMPLETED** (unconditional!) | COMPLETED or FAILED (checked) |
| Dead-end graph | CRASHED | **COMPLETED** (wrong!) | FAILED (correct) |

**Without the END-check**, a dead-end graph (node didn't deliver, END never reached) would be marked COMPLETED instead of CRASHED. The `RoutingError` was the sole guard preventing this. Removing it without adding an END-check silently changes dead-end graphs from CRASHED to COMPLETED.

**`load_latest_completed` already exists** (node_state_store.py:162) — no new method needed. Verified: `GraphPersistenceCoordinator.node_state_store` property (persistence_coordinator.py:118-121) returns the `NodeStateStore` instance. The call `ctx.coordinator.node_state_store.load_latest_completed(node_id)` works as-is.

### What does NOT change

- Node.run retry loop — unchanged (node-level concern, not graph-level)
- ParallelScheduler — already exits naturally (`while self._ready or running:` — no RoutingError)
- RoutingError from `validate_dispatch_target` — stays (invalid edge target, different concern)
- Node.max_retry — stays
- Everything else in modex_graph — stays

### Scope

Prerequisite for T08 (graph node integration). Independent of Tree core. Can be delivered as a standalone modex_graph change.

**Must ship as ONE atomic change** — `break` + END-check. Not two separate commits.
