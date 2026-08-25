from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps
from modex_agent.plugins.defaults.hooks import MemoryTraceHookFactory
from modex_agent.trace.memory_trace_hook import MemoryTraceHook


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


async def test_factory_uses_pool_trace_store_when_roster_selects_hook() -> None:
    store = MagicMock()
    pool_data = MagicMock()
    pool_data.trace_store = store
    pool_assembly_ctx = MagicMock()
    pool_assembly_ctx.pool_data = pool_data
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(pool_assembly_ctx=pool_assembly_ctx),
        agent_name="probe-agent",
    )
    factory = MemoryTraceHookFactory()

    hook = await factory.create(factory.config_model(), ctx)

    assert isinstance(hook, MemoryTraceHook)
    assert hook._store is store  # noqa: SLF001
