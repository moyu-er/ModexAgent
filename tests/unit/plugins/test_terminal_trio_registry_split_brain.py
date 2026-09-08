"""Terminal-trio registry split-brain regression (the original P0 bug).

Registration-order mix, replicated through the real factory resolution
(``create_pool`` strategy assembly + native_core tool resolution):

1. A strategy (or third-party pool wiring) registers the
   Command/Process/Terminal trio into the tool manager, sharing one
   ``ProcessRegistry`` (R).
2. The FW assembly resolves the roster name ``"bash"`` through the real
   ``ComponentRegistry`` (``BashToolFactory``) against
   ``pool_runtime.process_registry`` — which ``PoolAssembleStage``
   harvested from the strategy's ``StrategyAssembly``.
3. The resolved tool is registered into the SAME tool manager, silently
   overwriting the trio's bash (``register`` is a dict assignment).

Before the fix, ``BashToolFactory`` built a PRIVATE registry, so the final
tool manager held ``bash → A`` and ``process → B``: commands registered in
A, interactive writes consulted B, and every password prompt died with
"No running process session found". The regression asserts the final
tool manager's trio holds ONE registry identity.

Since ticket 05 the shipped pools resolve the WHOLE trio through the
factories (the BIZ trio construction is deleted); this mix remains the
guard for strategies that still register their own trio while the roster
resolves ``bash``.

No command executes here — the PTY manager is a stand-in because the bug
lived entirely in wiring, not execution. (Mocking ``CommandTool.__init__``
would HIDE this bug — mocks don't preserve registry identity — which is
how it escaped the existing suite.)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import (
    PoolRuntimeDeps,
    resolution_context,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.defaults.tools import ToolConfig
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
)
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.tools.terminal import (
    CommandTool,
    ProcessRegistry,
    ProcessTool,
    TerminalTool,
)
from modex_agent.tools.terminal.managers import TerminalManagerBase
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


async def _load_default_registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(bundled_factories=(DefaultPlugin(),), project_plugin_paths=()),
    )
    return registry


def _trio_tool_manager(
    manager: TerminalManagerBase, registry: ProcessRegistry
) -> InMemoryToolManager:
    """A strategy-supplied trio registration (the pre-ticket-05 shape)."""
    tm = InMemoryToolManager()
    tm.register(CommandTool(manager=manager, registry=registry))
    tm.register(ProcessTool(registry=registry, manager=manager))
    tm.register(TerminalTool(manager, registry=registry))
    return tm


async def test_terminal_trio_shares_one_registry_after_fw_bash_overwrite() -> None:
    component_registry = await _load_default_registry()
    terminal_manager = MagicMock(spec=TerminalManagerBase)
    pool_registry = ProcessRegistry()

    # Stage 3 product: PoolAssembleStage harvested the strategy's registry.
    pool_runtime = PoolRuntimeDeps(
        terminal_manager=terminal_manager,
        process_registry=pool_registry,
    )
    ctx = resolution_context(
        component_registry,
        WorkspaceContext(target=Path("."), paths=WorkspacePaths(root=Path(".")), is_home=False),
        pool_runtime,
    )

    tm = _trio_tool_manager(terminal_manager, pool_registry)

    # Stage 4: native_core resolves "bash" via the real registry, then the
    # resolved tool overwrites the trio's bash in the same manager.
    factory = component_registry.resolve(ComponentSlot.TOOL, "bash")
    fw_bash = await factory.create(ToolConfig(), ctx)
    tm.register(fw_bash)

    bash = tm.get_tool("bash")
    process = tm.get_tool("process")
    terminal = tm.get_tool("terminal")
    assert isinstance(bash, CommandTool)
    assert isinstance(process, ProcessTool)
    assert isinstance(terminal, TerminalTool)

    assert bash._registry is pool_registry  # noqa: SLF001
    assert process._registry is pool_registry  # noqa: SLF001
    assert terminal._registry is pool_registry  # noqa: SLF001


async def test_fw_resolved_bash_type_survives_overwrite_order() -> None:
    """The overwrite order is irrelevant once the registry is shared:
    resolving FIRST and registering the trio afterwards converges to the
    same single-registry state."""
    component_registry = await _load_default_registry()
    terminal_manager = MagicMock(spec=TerminalManagerBase)
    pool_registry = ProcessRegistry()
    ctx = resolution_context(
        component_registry,
        WorkspaceContext(target=Path("."), paths=WorkspacePaths(root=Path(".")), is_home=False),
        PoolRuntimeDeps(terminal_manager=terminal_manager, process_registry=pool_registry),
    )

    tm = InMemoryToolManager()
    factory = component_registry.resolve(ComponentSlot.TOOL, "bash")
    tm.register(await factory.create(ToolConfig(), ctx))
    for tool in (
        CommandTool(manager=terminal_manager, registry=pool_registry),
        ProcessTool(registry=pool_registry, manager=terminal_manager),
        TerminalTool(terminal_manager, registry=pool_registry),
    ):
        tm.register(tool)

    bash = tm.get_tool("bash")
    process = tm.get_tool("process")
    assert isinstance(bash, CommandTool)
    assert isinstance(process, ProcessTool)
    assert bash._registry is pool_registry  # noqa: SLF001
    assert process._registry is pool_registry  # noqa: SLF001
