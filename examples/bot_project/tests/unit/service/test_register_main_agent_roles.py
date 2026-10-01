"""External main descriptor metadata propagation (W5 strategy-owned build).

``ExternalExecutionStrategy.assemble_main`` reads the declared root
``AgentSpec`` and writes it onto the main descriptor it constructs —
``roles`` and ``description`` must propagate from the declaration to the
runtime descriptor (the retired orchestrator-side
``_register_external_main_agent`` construction moved into the strategy).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[3]))

from modex_agent.core.agent import ProviderKind
from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.multi_agent import SessionRetentionPolicy
from modex_agent.multi_agent.execution_strategy import PoolAssemblyContext
from modex_agent.plugins.assembly.strategies.external import (
    ExternalExecutionStrategy,
)
from modex_agent.scope.spec import AgentSpec, PoolSpec


def _pool_ctx(main_spec: AgentSpec) -> PoolAssemblyContext:
    pool_spec = PoolSpec(name="default", agents=[main_spec])
    return PoolAssemblyContext(
        pool_name="default",
        pool_spec=pool_spec,
        project_dir=Path("."),
        data_dir=Path(".") / ".modex",
        broker=MagicMock(),
        inbox_server=MagicMock(),
        agent_bus=MagicMock(),
        output_adapter=MagicMock(),
        safety=RuntimeSafetyPolicy(),
        retention=SessionRetentionPolicy(),
        registry=MagicMock(),
    )


async def _assemble(main_spec: AgentSpec):
    strategy = ExternalExecutionStrategy()
    with patch.object(shutil, "which", lambda name: f"/fake/bin/{name}"):
        return await strategy.assemble_main(_pool_ctx(main_spec))


@pytest.mark.asyncio
async def test_external_main_roles_propagate_to_descriptor() -> None:
    assembly = await _assemble(
        AgentSpec(
            name="main",
            execution_strategy="external",
            provider_kind=ProviderKind.OPENCODE,
            roles=["coordinator"],
        )
    )
    assert assembly.main is not None
    assert assembly.main.descriptor.roles == ["coordinator"]


@pytest.mark.asyncio
async def test_external_main_roles_default_empty() -> None:
    """When the declared root omits roles, descriptor.roles defaults to []."""
    assembly = await _assemble(
        AgentSpec(
            name="main",
            execution_strategy="external",
            provider_kind=ProviderKind.OPENCODE,
        )
    )
    assert assembly.main is not None
    assert assembly.main.descriptor.roles == []


@pytest.mark.asyncio
async def test_external_main_preserves_multiple_roles() -> None:
    assembly = await _assemble(
        AgentSpec(
            name="main",
            execution_strategy="external",
            provider_kind=ProviderKind.OPENCODE,
            roles=["coordinator", "communicator", "custom-role"],
        )
    )
    assert assembly.main is not None
    assert assembly.main.descriptor.roles == ["coordinator", "communicator", "custom-role"]


@pytest.mark.asyncio
async def test_external_main_passes_description_to_descriptor() -> None:
    assembly = await _assemble(
        AgentSpec(
            name="main",
            execution_strategy="external",
            provider_kind=ProviderKind.OPENCODE,
            description="Pool main agent desc",
        )
    )
    assert assembly.main is not None
    assert assembly.main.descriptor.role_description == "Pool main agent desc"
