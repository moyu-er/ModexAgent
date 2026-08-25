from __future__ import annotations

from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock

import pytest
from bot.input_pipeline.assembly import build_im_pipeline, build_webui_pipeline
from bot.input_pipeline.stages.approval import ApprovalStage
from bot.input_pipeline.stages.attachment_ingest import AttachmentIngestStage
from bot.input_pipeline.stages.command import CommandDispatchStage
from bot.input_pipeline.stages.model_choice import ModelChoiceStage
from bot.input_pipeline.stages.resolve_workspace import ResolveWorkspaceStage
from bot.input_pipeline.stages.skill_parse import SkillParseStage, SkillRegistry
from bot.input_pipeline.stages.unsupported_command import UnsupportedCommandStage
from bot.service.model_config import BotModelConfig
from plugins.im_input_stages import IMInputStagesPlugin
from pydantic import BaseModel, ConfigDict

from modex_agent.input_pipeline.context import InputContext
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.pipeline import UserInputPipeline
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.loader import Plugin, PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.context import WorkspaceContext
from tests.input_pipeline.assembly_support import (
    TEST_ASSEMBLY_CTX,
    TEST_COMPONENT_REGISTRY,
)


class _CustomInputStageConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _CustomInputStage(InputStage):
    async def process(self, envelope: UserInputEnvelope, ctx: InputContext) -> StageResult:
        return Continue(value=envelope)


class _CustomInputStageFactory(ComponentFactory):
    config_model: ClassVar[type[BaseModel]] = _CustomInputStageConfig

    async def create(self, config: BaseModel, ctx: AssemblyContext) -> _CustomInputStage:
        return _CustomInputStage()


class _CustomInputStagePlugin(Plugin):
    config_model = _CustomInputStageConfig

    def register(self, ctx: PluginRegistrationContext) -> None:
        ctx.register_input_stage("custom_input_stage", _CustomInputStageFactory())


def _registry_with_custom_input_stage() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        IMInputStagesPlugin().register(registration)
    with PluginRegistrationContext(registry) as registration:
        _CustomInputStagePlugin().register(registration)
    return registry


def _write_cfg(tmp_path: Path) -> BotModelConfig:
    p = tmp_path / "model.yml"
    p.write_text(
        'models:\n  default_provider: "A"\n  default_model: "M1"\n  providers:\n'
        '    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: m1}]}\n',
        encoding="utf-8",
    )
    return BotModelConfig.from_yaml(p)


async def test_im_pipeline_consumes_custom_input_stage_plugin(tmp_path: Path) -> None:
    registry = _registry_with_custom_input_stage()
    assembly_ctx = AssemblyContext(
        registry=registry,
        workspace_ctx=WorkspaceContext.from_target(
            tmp_path,
            data_dir_name=".modex",
            home=tmp_path,
        ),
    )

    pipe = await build_im_pipeline(
        registry=registry,
        ctx=assembly_ctx,
        skill_registry=MagicMock(spec=SkillRegistry),
        known_pools={"main"},
    )

    assert any(isinstance(stage, _CustomInputStage) for stage in pipe._stages)


async def test_im_pipeline_order_and_count() -> None:
    pipe = await build_im_pipeline(
        registry=TEST_COMPONENT_REGISTRY,
        ctx=TEST_ASSEMBLY_CTX,
        skill_registry=MagicMock(spec=SkillRegistry),
        known_pools={"main", "coding"},
    )
    assert isinstance(pipe, UserInputPipeline)
    # SetChannel, ResolveWorkspace, EnvironmentControl, SessionControl,
    # ResolvePool, CommandDispatch, AttachmentIngest, Approval, SkillParse,
    # UnsupportedCommand, Persist, Enqueue.
    assert len(pipe._stages) == 12
    assert isinstance(pipe._stages[1], ResolveWorkspaceStage)
    assert isinstance(pipe._stages[5], CommandDispatchStage)
    assert isinstance(pipe._stages[6], AttachmentIngestStage)
    assert isinstance(pipe._stages[7], ApprovalStage)
    assert isinstance(pipe._stages[8], SkillParseStage)
    assert isinstance(pipe._stages[9], UnsupportedCommandStage)


async def test_webui_pipeline_order_and_count(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path)
    pipe = await build_webui_pipeline(
        registry=TEST_COMPONENT_REGISTRY,
        ctx=TEST_ASSEMBLY_CTX,
        skill_registry=MagicMock(spec=SkillRegistry),
        bot_model_config=cfg,
    )
    assert isinstance(pipe, UserInputPipeline)
    # SetChannel, ResolveWorkspace, ResolvePool, ModelChoice, CommandDispatch,
    # AttachmentIngest, Approval, SkillParse, UnsupportedCommand, Persist, Enqueue.
    assert len(pipe._stages) == 11
    assert isinstance(pipe._stages[1], ResolveWorkspaceStage)
    assert isinstance(pipe._stages[3], ModelChoiceStage)
    assert isinstance(pipe._stages[4], CommandDispatchStage)
    assert isinstance(pipe._stages[5], AttachmentIngestStage)
    assert isinstance(pipe._stages[6], ApprovalStage)
    assert isinstance(pipe._stages[7], SkillParseStage)
    assert isinstance(pipe._stages[8], UnsupportedCommandStage)


async def test_webui_pipeline_has_model_choice_stage(tmp_path: Path) -> None:
    cfg = _write_cfg(tmp_path)
    pipe = await build_webui_pipeline(
        registry=TEST_COMPONENT_REGISTRY,
        ctx=TEST_ASSEMBLY_CTX,
        skill_registry=MagicMock(spec=SkillRegistry),
        bot_model_config=cfg,
    )
    assert any(isinstance(s, ModelChoiceStage) for s in pipe._stages)


async def test_im_pipeline_has_no_model_choice_stage() -> None:
    pipe = await build_im_pipeline(
        registry=TEST_COMPONENT_REGISTRY,
        ctx=TEST_ASSEMBLY_CTX,
        skill_registry=MagicMock(spec=SkillRegistry),
        known_pools={"main"},
    )
    assert not any(isinstance(s, ModelChoiceStage) for s in pipe._stages)


async def test_empty_input_stage_registry_raises_loudly(tmp_path: Path) -> None:
    """A registry with NO INPUT_STAGE factories must fail loudly at the
    first skeleton resolve (ComponentNotFoundError) — never silently build
    an empty/near-empty pipeline."""
    from modex_agent.plugins.registry import ComponentNotFoundError

    empty_registry = ComponentRegistry()
    assembly_ctx = AssemblyContext(
        registry=empty_registry,
        workspace_ctx=WorkspaceContext.from_target(
            tmp_path,
            data_dir_name=".modex",
            home=tmp_path,
        ),
    )

    with pytest.raises(ComponentNotFoundError, match="set_channel"):
        await build_im_pipeline(
            registry=empty_registry,
            ctx=assembly_ctx,
            skill_registry=MagicMock(spec=SkillRegistry),
            known_pools={"main"},
        )


async def test_pipelines_build_with_directory_discovered_registry(tmp_path: Path) -> None:
    """Production boot regression (2026-08-20).

    The REAL service registry is built via directory discovery
    (``ComponentRegistryLoader`` over ``plugins/``), which imports each
    plugin file under a synthetic module name — while ordinary imports
    resolve the same file as the ``plugins.im_input_stages`` package. The
    pipeline builder must construct stage configs from the
    registry-resolved factory's OWN ``config_model``; constructing them
    from a direct plugin-module import makes the two module identities
    collide in ``model_validate`` and crashes boot with a paradoxical
    "Input should be a valid dictionary or instance of
    ModelChoiceStageConfig" on an actual ModelChoiceStageConfig instance.
    """
    from modex_agent.plugins.defaults import DefaultPlugin
    from modex_agent.plugins.loader import (
        ComponentRegistryLoader,
        PluginDiscoveryConfig,
    )

    registry = ComponentRegistry()
    await ComponentRegistryLoader.load(
        registry,
        PluginDiscoveryConfig(
            bundled_factories=(DefaultPlugin(),),
            project_plugin_paths=(Path(__file__).resolve().parents[2] / "plugins",),
        ),
    )
    assembly_ctx = AssemblyContext(
        registry=registry,
        workspace_ctx=WorkspaceContext.from_target(
            tmp_path,
            data_dir_name=".modex",
            home=tmp_path,
        ),
    )

    webui_pipe = await build_webui_pipeline(
        registry=registry,
        ctx=assembly_ctx,
        skill_registry=MagicMock(spec=SkillRegistry),
        bot_model_config=None,
    )
    assert any(isinstance(s, ModelChoiceStage) for s in webui_pipe._stages)

    im_pipe = await build_im_pipeline(
        registry=registry,
        ctx=assembly_ctx,
        skill_registry=MagicMock(spec=SkillRegistry),
        known_pools={"main"},
    )
    assert isinstance(im_pipe, UserInputPipeline)
