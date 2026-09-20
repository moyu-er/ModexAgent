from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from modex_agent.core.prompt import SystemPromptProvider
from modex_agent.core.tool_group import (
    ToolGroup,
    ToolGroupResource,
    ToolGroupSpec,
    ToolGroupVariant,
)
from modex_agent.core.tool_manager import Tool, ToolOrigin
from modex_agent.memory.context import InMemoryContextManager
from modex_agent.multi_agent.descriptor import AgentInstance
from modex_agent.multi_agent.factory import AgentFactory
from modex_agent.multi_agent.pool import AgentPool
from modex_agent.multi_agent.state import AgentState
from modex_agent.plugins.abc import AgentType, ComponentFactory, ComponentSlot, SimpleFactory
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.assembly.native_core import (
    LlmDefaults,
    NativeAssemblyInputs,
    assemble_native_agent,
)
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides, ToolEntry
from modex_agent.plugins.registry import ComponentNotFoundError, ComponentRegistry
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.trace.cassette import CassetteRecorder
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Tool(Tool):
    def __init__(self, name: str) -> None:
        super().__init__(name=name, description=name, parameters={"type": "object"})

    async def execute(self, **kwargs: object) -> str:
        return self.name


class _WrappedTool(_Tool):
    def __init__(self, inner: Tool) -> None:
        super().__init__(inner.name)
        self.inner = inner


class _Resource(ToolGroupResource):
    def __init__(self) -> None:
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


class _GroupFactory(ComponentFactory):
    config_model = _EmptyConfig

    def __init__(self, group: ToolGroup) -> None:
        self._group = group

    async def create(self, config: BaseModel, ctx: object) -> ToolGroup:
        del config, ctx
        return self._group


class _PromptProvider(SystemPromptProvider):
    async def _fetch_version(self) -> str:
        return "v1"

    async def _fetch_content(self) -> str:
        return "prompt"


_MANIFEST = ToolGroupSpec(
    anchor="shell",
    variants=(
        ToolGroupVariant(name="one_shot", tools=("shell",)),
        ToolGroupVariant(name="persistent", tools=("shell", "shell_input")),
    ),
)


def _workspace(tmp_path: Path) -> WorkspaceContext:
    return WorkspaceContext(
        target=tmp_path,
        paths=WorkspacePaths(root=tmp_path),
        is_home=False,
    )


def _group(resource: ToolGroupResource | None = None) -> ToolGroup:
    return ToolGroup(
        anchor="shell",
        variant="persistent",
        tools=(_Tool("shell"), _Tool("shell_input")),
        resource=resource,
    )


def _registry(group_product: Tool | ToolGroup) -> ComponentRegistry:
    registry = ComponentRegistry()
    if isinstance(group_product, ToolGroup):
        factory: ComponentFactory = _GroupFactory(group_product)
    else:
        factory = SimpleFactory(group_product, _EmptyConfig)
    registry.register(ComponentSlot.TOOL, "shell", factory)
    registry.register(
        ComponentSlot.SYSTEM_PROMPT_PROVIDER,
        "prompt",
        SimpleFactory(_PromptProvider(), _EmptyConfig),
    )
    return registry


def _spec(workspace: WorkspaceContext, *, groups: tuple[ToolGroupSpec, ...] = (_MANIFEST,)) -> AssemblySpec:
    return AssemblySpec(
        agent_type=AgentType.native_main,
        agent_name="worker",
        pool_name="pool",
        tools=[ToolEntry(name="shell", origin=ToolOrigin.CAPABILITY_DERIVED)],
        tool_groups=groups,
        hooks=[],
        llm_provider="unused",
        system_prompt_provider="prompt",
        system_prompt_config={},
        memory_overrides=MemoryOverrides(),
        execution_strategy="react",
        workspace_ctx=workspace,
    )


def _inputs(
    workspace: WorkspaceContext,
    *,
    manager: InMemoryToolManager | None = None,
    transform=None,
) -> tuple[NativeAssemblyInputs, AgentInstance]:
    instance = AgentInstance(
        descriptor=MagicMock(),
        context_manager=InMemoryContextManager(base_system_prompt=""),
    )
    factory = MagicMock(spec=AgentFactory)
    factory.create_agent = AsyncMock(return_value=instance)
    return (
        NativeAssemblyInputs(
            agent_factory=factory,
            broker=None,
            llm_defaults=LlmDefaults(model="test/model"),
            context_manager=instance.context_manager,
            llm_provider=MagicMock(),
            tool_manager=manager,
            project_dir=workspace.target,
            tool_transform=transform,
        ),
        instance,
    )


async def test_real_native_assembly_transforms_group_and_transfers_resource(
    tmp_path: Path,
) -> None:
    resource = _Resource()
    registry = _registry(_group(resource))
    workspace = _workspace(tmp_path)
    inputs, instance = _inputs(workspace, transform=_WrappedTool)

    result = await assemble_native_agent(
        _spec(workspace),
        registry,
        inputs,
        ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
    )

    assert all(
        isinstance(result.tool_manager.get_tool(name), _WrappedTool)
        for name in ("shell", "shell_input")
    )
    assert result.tool_manager.tool_groups[0].tools == (
        result.tool_manager.get_tool("shell"),
        result.tool_manager.get_tool("shell_input"),
    )
    assert instance.resources == (resource,)
    assert result.tool_manager.tool_groups[0].resource is None

    await instance.stop()
    await instance.stop()
    assert resource.close_count == 1


async def test_real_native_assembly_accepts_cassette_wrapped_tool_manager(
    tmp_path: Path,
) -> None:
    registry = _registry(_group())
    workspace = _workspace(tmp_path)
    base = InMemoryToolManager()
    wrapped = CassetteRecorder(tmp_path / "cassette").wrap_tool_executor(base)
    inputs, _ = _inputs(workspace)
    inputs.tool_manager = wrapped  # type: ignore[assignment]

    result = await assemble_native_agent(
        _spec(workspace),
        registry,
        inputs,
        ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
    )

    assert result.tool_manager is wrapped
    assert wrapped.get_tool_group("shell_input") is wrapped.tool_groups[0]
    assert wrapped.origin_of("shell") is ToolOrigin.CAPABILITY_DERIVED


async def test_group_resource_closes_when_later_tool_resolution_fails(
    tmp_path: Path,
) -> None:
    resource = _Resource()
    registry = _registry(_group(resource))
    workspace = _workspace(tmp_path)
    inputs, _ = _inputs(workspace)
    spec = _spec(workspace).model_copy(
        update={
            "tools": [
                ToolEntry(name="shell", origin=ToolOrigin.CAPABILITY_DERIVED),
                ToolEntry(name="missing", origin=ToolOrigin.PRESET),
            ]
        }
    )

    with pytest.raises(ComponentNotFoundError, match="missing"):
        await assemble_native_agent(
            spec,
            registry,
            inputs,
            ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
        )

    assert resource.close_count == 1


async def test_native_assembly_rejects_undeclared_group(tmp_path: Path) -> None:
    registry = _registry(_group())
    workspace = _workspace(tmp_path)
    inputs, _ = _inputs(workspace)

    with pytest.raises(ValueError, match="undeclared"):
        await assemble_native_agent(
            _spec(workspace, groups=()),
            registry,
            inputs,
            ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
        )


async def test_native_assembly_rejects_scalar_for_group_anchor(tmp_path: Path) -> None:
    registry = _registry(_Tool("shell"))
    workspace = _workspace(tmp_path)
    inputs, _ = _inputs(workspace)

    with pytest.raises(ValueError, match="ToolGroup"):
        await assemble_native_agent(
            _spec(workspace),
            registry,
            inputs,
            ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
        )


async def test_native_assembly_rejects_group_variant_not_allowed_by_manifest(
    tmp_path: Path,
) -> None:
    product = ToolGroup(
        anchor="shell",
        variant="unsupported",
        tools=(_Tool("shell"),),
    )
    registry = _registry(product)
    workspace = _workspace(tmp_path)
    inputs, _ = _inputs(workspace)

    with pytest.raises(ValueError, match="not allowed"):
        await assemble_native_agent(
            _spec(workspace),
            registry,
            inputs,
            ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
        )


async def test_native_assembly_rejects_wrong_group_members_and_closes_resource(
    tmp_path: Path,
) -> None:
    resource = _Resource()
    product = ToolGroup(
        anchor="shell",
        variant="persistent",
        tools=(_Tool("shell"), _Tool("wrong_input")),
        resource=resource,
    )
    registry = _registry(product)
    workspace = _workspace(tmp_path)
    inputs, _ = _inputs(workspace)

    with pytest.raises(ValueError, match="members"):
        await assemble_native_agent(
            _spec(workspace),
            registry,
            inputs,
            ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
        )

    assert resource.close_count == 1


async def test_group_collision_is_atomic_and_closes_resource(tmp_path: Path) -> None:
    resource = _Resource()
    registry = _registry(_group(resource))
    workspace = _workspace(tmp_path)
    manager = InMemoryToolManager()
    blocker = _Tool("shell_input")
    manager.register(blocker, origin=ToolOrigin.LOCAL_TOOLS)
    inputs, _ = _inputs(workspace, manager=manager)

    with pytest.raises(ValueError, match="shell_input"):
        await assemble_native_agent(
            _spec(workspace),
            registry,
            inputs,
            ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
        )

    assert manager.list_tools() == ["shell_input"]
    assert manager.get_tool("shell_input") is blocker
    assert resource.close_count == 1


async def test_failure_after_agent_creation_cleans_attached_resource(
    tmp_path: Path,
) -> None:
    class AttachedResource(_Resource):
        def __init__(self) -> None:
            super().__init__()
            self.instance: AgentInstance | None = None
            self.attached_at_close = False

        async def aclose(self) -> None:
            assert self.instance is not None
            self.attached_at_close = self in self.instance.resources
            await super().aclose()

    resource = AttachedResource()
    registry = _registry(_group(resource))
    workspace = _workspace(tmp_path)
    inputs, instance = _inputs(workspace)
    resource.instance = instance
    pool = MagicMock()
    pool.register_resident = AsyncMock(side_effect=RuntimeError("registration failed"))
    inputs.pool = pool

    with pytest.raises(RuntimeError, match="registration failed"):
        await assemble_native_agent(
            _spec(workspace),
            registry,
            inputs,
            ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
        )

    assert resource.attached_at_close is True
    assert resource.close_count == 1
    assert instance.resources == ()


async def test_callback_failure_withdraws_created_resident(
    tmp_path: Path,
) -> None:
    resource = _Resource()
    registry = _registry(_group(resource))
    workspace = _workspace(tmp_path)
    inputs, instance = _inputs(workspace)
    pool = AgentPool(broker=MagicMock(), agent_factory=MagicMock())
    inputs.pool = pool
    failure = RuntimeError("callback failed")

    async def fail_after_registration(child_id: str, parent_id: str) -> None:
        assert (child_id, parent_id) == ("inv.worker", "parent.main")
        assert pool.get("worker") is instance
        raise failure

    inputs.on_subagent_created = fail_after_registration
    try:
        with pytest.raises(RuntimeError, match="callback failed") as raised:
            await assemble_native_agent(
                _spec(workspace),
                registry,
                inputs,
                ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
                parent_session="parent.main",
                invocation_id="inv",
            )

        assert raised.value is failure
        assert pool.get("worker") is None
        assert pool.get_status("worker") is AgentState.SHUTDOWN
        assert resource.close_count == 1
        assert instance.resources == ()
    finally:
        await pool.shutdown_all(timeout=0.1)


async def test_assembly_error_survives_attached_resource_cleanup_failure(
    tmp_path: Path,
) -> None:
    class FailingResource(_Resource):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("resource cleanup failed")

    resource = FailingResource()
    registry = _registry(_group(resource))
    workspace = _workspace(tmp_path)
    inputs, instance = _inputs(workspace)
    pool = MagicMock()
    pool.register_resident = AsyncMock(side_effect=RuntimeError("registration failed"))
    inputs.pool = pool

    with pytest.raises(RuntimeError, match="registration failed"):
        await assemble_native_agent(
            _spec(workspace),
            registry,
            inputs,
            ctx=AssemblyContext(registry=registry, workspace_ctx=workspace),
        )

    assert resource.close_count == 1
    assert instance.resources == (resource,)
