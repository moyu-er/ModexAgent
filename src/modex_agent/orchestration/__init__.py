"""Framework-level graph orchestration service (ticket 10 §3.6).

Provides `GraphOrchestrator` — wires `GraphSpec` → `CompiledGraph` →
`GraphInstance` → `GraphEngine` execution, with external control via
`GraphControlService` and recovery via `GraphRecoveryService`.

The bot factory (examples/bot_project/) calls this service; it does NOT
build REST endpoints, CLI commands, or business-level wiring.
"""

from modex_agent.orchestration.graph_orchestrator import GraphOrchestrator

__all__ = ["GraphOrchestrator"]
