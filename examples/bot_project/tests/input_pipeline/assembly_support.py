from __future__ import annotations

from pathlib import Path
from typing import Final

from plugins.im_input_stages import IMInputStagesPlugin

from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext

TEST_COMPONENT_REGISTRY: Final = ComponentRegistry()
with PluginRegistrationContext(TEST_COMPONENT_REGISTRY) as registration:
    IMInputStagesPlugin().register(registration)

TEST_ASSEMBLY_CTX: Final = AssemblyContext(
    registry=TEST_COMPONENT_REGISTRY,
    workspace_ctx=WorkspaceContext.from_target(
        Path.cwd(),
        data_dir_name=".modex",
        home=Path.cwd(),
    ),
)
