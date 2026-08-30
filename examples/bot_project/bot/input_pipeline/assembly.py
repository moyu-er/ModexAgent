"""Assemble IM (S2..S8) and WebUI (S4..S8) sub-pipelines.

Stage configs are ALWAYS constructed from the registry-resolved factory's
own ``config_model`` — never from an import of the plugin module. The real
service registry is built via directory discovery, which imports plugin
files under synthetic module names; an instance built from a direct
``plugins.im_input_stages`` import is therefore a structurally-identical
but distinct class to the factory's, and ``model_validate`` rejects it
(boot regression 2026-08-20). The registry is the single identity source.
"""

from __future__ import annotations

from typing import Any, Final

from plugins.im_input_stages import InputStageName
from pydantic import BaseModel

from bot.input_pipeline.stages.skill_parse import SkillRegistry
from bot.service.model_config import BotModelConfig
from modex_agent.input_pipeline.pipeline import UserInputPipeline
from modex_agent.input_pipeline.stage import InputStage
from modex_agent.plugins.abc import ComponentSlot
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.workspace.control import WorkspaceController

_IM_STAGE_SKELETON: Final[tuple[InputStageName, ...]] = (
    InputStageName.SET_CHANNEL,
    InputStageName.RESOLVE_WORKSPACE,
    InputStageName.ENVIRONMENT_CONTROL,
    InputStageName.SESSION_CONTROL,
    InputStageName.RESOLVE_POOL,
    InputStageName.COMMAND_DISPATCH,
    InputStageName.ATTACHMENT_INGEST,
    InputStageName.APPROVAL,
    InputStageName.SKILL_PARSE,
    InputStageName.UNSUPPORTED_COMMAND,
    InputStageName.PERSIST_USER_MESSAGE,
    InputStageName.ENQUEUE,
)

_WEBUI_STAGE_SKELETON: Final[tuple[InputStageName, ...]] = (
    InputStageName.SET_CHANNEL,
    InputStageName.RESOLVE_WORKSPACE,
    InputStageName.RESOLVE_POOL,
    InputStageName.MODEL_CHOICE,
    InputStageName.COMMAND_DISPATCH,
    InputStageName.ATTACHMENT_INGEST,
    InputStageName.APPROVAL,
    InputStageName.SKILL_PARSE,
    InputStageName.UNSUPPORTED_COMMAND,
    InputStageName.PERSIST_USER_MESSAGE,
    InputStageName.ENQUEUE,
)

_BUILTIN_STAGE_NAMES: Final[frozenset[str]] = frozenset(InputStageName)


def _stage_names(
    registry: ComponentRegistry,
    skeleton: tuple[InputStageName, ...],
) -> tuple[str, ...]:
    custom_names = tuple(
        name
        for name in registry.names(ComponentSlot.INPUT_STAGE)
        if name not in _BUILTIN_STAGE_NAMES
    )
    insertion_index = skeleton.index(InputStageName.UNSUPPORTED_COMMAND)
    return (
        *skeleton[:insertion_index],
        *custom_names,
        *skeleton[insertion_index:],
    )


async def _build_pipeline(
    registry: ComponentRegistry,
    ctx: AssemblyContext,
    skeleton: tuple[InputStageName, ...],
    configs: dict[str, dict[str, Any]],
) -> UserInputPipeline:
    """Build a pipeline from the skeleton + slot-resolved stage factories.

    ``configs`` maps stage names to CONSTRUCTOR KWARGS for that stage's
    config model — an open payload (one stage's config fields differ from
    another's) validated/typed by the factory's own ``config_model`` at
    construction. Values are applied to the registry-resolved factory's
    class, keeping a single module identity (see module docstring).
    """
    stages: list[InputStage] = []
    for name in _stage_names(registry, skeleton):
        factory = registry.resolve(ComponentSlot.INPUT_STAGE, name)
        config: BaseModel = factory.config_model(**configs.get(name, {}))
        stage = await factory.create(config, ctx)
        stages.append(stage)
    return UserInputPipeline(stages)


async def build_im_pipeline(
    *,
    registry: ComponentRegistry,
    ctx: AssemblyContext,
    skill_registry: SkillRegistry,
    known_pools: set[str],
    workspace_controller: WorkspaceController | None = None,
) -> UserInputPipeline:
    """IM pipeline: S4→S2→S3→S5→CommandDispatch→Ingest→Approval→Skill→Unsupported→Persist→Enqueue.

    S2 (EnvironmentControlStage) handles IM-only commands (/cd, /pool, /exit,
    /pwd). S3 (SessionControlStage) handles /stop. CommandDispatchStage handles
    cross-channel commands (/continue) shared with WebUI.
    """
    return await _build_pipeline(
        registry,
        ctx,
        _IM_STAGE_SKELETON,
        {
            InputStageName.ENVIRONMENT_CONTROL: {
                "known_pools": known_pools,
                "workspace_controller": workspace_controller,
            },
            InputStageName.SKILL_PARSE: {"skill_registry": skill_registry},
        },
    )


async def build_webui_pipeline(
    *,
    registry: ComponentRegistry,
    ctx: AssemblyContext,
    skill_registry: SkillRegistry,
    bot_model_config: BotModelConfig | None,
) -> UserInputPipeline:
    """WebUI pipeline: S4→S5→ModelChoice→CommandDispatch→Ingest→Approval→Skill→Unsupported→Persist→Enqueue.

    No S2/S3: the WebUI has GUI controls for workspace/pool/session. CommandDispatchStage
    handles cross-channel commands (/continue) shared with IM. Pool-switch
    shortcuts typed into the chat box reach the terminal Unsupported stage.

    ModelChoiceStage 仅在此 pipeline 注册：把 WebUI 选中的 provider/model 解析为
    ResolvedModel 写入 envelope.metadata，由 EnqueueStage 注册到 registry。IM
    pipeline 不注册（始终使用默认模型）。
    """
    return await _build_pipeline(
        registry,
        ctx,
        _WEBUI_STAGE_SKELETON,
        {
            InputStageName.MODEL_CHOICE: {"bot_model_config": bot_model_config},
            InputStageName.SKILL_PARSE: {"skill_registry": skill_registry},
        },
    )
