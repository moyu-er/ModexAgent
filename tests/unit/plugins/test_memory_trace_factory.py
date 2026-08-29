from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps
from modex_agent.plugins.defaults.capabilities.tracing import TraceSupply
from modex_agent.plugins.defaults.hooks import MemoryTraceHookFactory
from modex_agent.trace.memory_trace_hook import MemoryTraceHook
from modex_agent.trace.otel_store import OtelSpanTraceStore


def test_registration_without_roster_selection_keeps_memory_trace_module_lazy() -> None:
    script = """
import sys
from modex_agent.plugins.defaults.hooks import register_default_hooks
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry

registry = ComponentRegistry()
with PluginRegistrationContext(registry) as ctx:
    register_default_hooks(ctx)
assert "modex_agent.trace.memory_trace_hook" not in sys.modules
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _trace_supply() -> TraceSupply:
    return TraceSupply(store=MagicMock(spec=OtelSpanTraceStore))


async def test_factory_reads_store_from_tracing_capability_supply() -> None:
    supply = _trace_supply()
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(capability_supply={"tracing": supply}),
        agent_name="probe-agent",
    )
    factory = MemoryTraceHookFactory()

    hook = await factory.create(factory.config_model(), ctx)

    assert isinstance(hook, MemoryTraceHook)
    assert hook._store is supply.store  # noqa: SLF001


async def test_factory_raises_loud_when_tracing_supply_missing() -> None:
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(),
        agent_name="probe-agent",
    )
    factory = MemoryTraceHookFactory()

    try:
        await factory.create(factory.config_model(), ctx)
    except ValueError as exc:
        assert "tracing" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing tracing supply")
