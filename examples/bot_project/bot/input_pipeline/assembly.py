"""Assemble IM (S2..S7) and WebUI (S4..S7) input sub-pipelines.

Every builder returns the same :class:`BotInputPreparation` over ONE shared
stage-selection rule: the framework skeleton constants
(:mod:`modex_agent.pipeline.input.skeleton`) are the only stage lists, and
prepare/handle both consume the same resolved stage sequence (DESIGN.md §7
— no channel may copy the list). Delivery is not part of the list: handle
delivers the prepared message through the channel's sync enqueue callback;
prepare yields the typed ``Prepared | Handled`` outcome without delivering.

Stage configs are ALWAYS constructed from the registry-resolved factory's
own ``config_model`` — never from an import of the plugin module. The real
service registry is built via directory discovery, which imports plugin
files under synthetic module names; an instance built from a direct
``bot_plugins.im_input_stages`` import is therefore a structurally-identical
but distinct class to the factory's, and ``model_validate`` rejects it
(boot regression 2026-08-20). The registry is the single identity source.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from bot.input_pipeline.prepare import BotInputPreparation
from bot.input_pipeline.stages.skill_parse import PoolSkillResolverRegistry
from modex_agent.app.models.registry import ModelRegistry
from modex_agent.pipeline.input.skeleton import (
    ACP_STAGE_SKELETON,
    IM_STAGE_SKELETON,
    WEBUI_STAGE_SKELETON,
    InputStageName,
    resolve_stage_names,
)
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.scope.components import ComponentSlot
from modex_agent.workspace.control import WorkspaceController

if TYPE_CHECKING:
    from modex_agent.pipeline.input.stage import InputStage


async def _build_pipeline(
    registry: ComponentRegistry,
    ctx: AssemblyContext,
    skeleton: tuple[InputStageName, ...],
    configs: dict[str, dict[str, Any]],
    *,
    stage_order: Sequence[str] | None = None,
) -> BotInputPreparation:
    """Build a preparation from the framework skeleton + slot-resolved
    stage factories.

    ``configs`` maps stage names to CONSTRUCTOR KWARGS for that stage's
    config model — an open payload (one stage's config fields differ from
    another's) validated/typed by the factory's own ``config_model`` at
    construction. Values are applied to the registry-resolved factory's
    class, keeping a single module identity (see module docstring).

    ``stage_order`` is the optional declarative order (app config
    ``input_stage_order`` per skeleton; W6) — ``None`` keeps the
    code-defined skeleton order.
    """
    stages: list[InputStage] = []
    for name in resolve_stage_names(registry, skeleton, order=stage_order):
        factory = registry.resolve(ComponentSlot.INPUT_STAGE, name)
        config = factory.config_model.model_validate(configs.get(name, {}))
        stage = await factory.create(config, ctx)
        stages.append(stage)
    return BotInputPreparation(stages)


async def build_im_pipeline(
    *,
    registry: ComponentRegistry,
    ctx: AssemblyContext,
    skill_registry: PoolSkillResolverRegistry,
    known_pools: set[str],
    workspace_controller: WorkspaceController | None = None,
    stage_order: Sequence[str] | None = None,
) -> BotInputPreparation:
    """IM pipeline: S4→S2→S3→S5→CommandDispatch→Ingest→Approval→Skill→Unsupported→Persist.

    S2 (EnvironmentControlStage) handles IM-only commands (/cd, /pool, /exit,
    /pwd). S3 (SessionControlStage) handles /stop. CommandDispatchStage handles
    cross-channel commands (/continue) shared with WebUI.
    """
    return await _build_pipeline(
        registry,
        ctx,
        IM_STAGE_SKELETON,
        {
            InputStageName.ENVIRONMENT_CONTROL: {
                "known_pools": known_pools,
                "workspace_controller": workspace_controller,
            },
            InputStageName.SKILL_PARSE: {"skill_registry": skill_registry},
        },
        stage_order=stage_order,
    )


async def build_webui_pipeline(
    *,
    registry: ComponentRegistry,
    ctx: AssemblyContext,
    skill_registry: PoolSkillResolverRegistry,
    bot_model_config: ModelRegistry | None,
    stage_order: Sequence[str] | None = None,
) -> BotInputPreparation:
    """WebUI pipeline: S4→S5→ModelChoice→CommandDispatch→Ingest→Approval→Skill→Unsupported→Persist.

    No S2/S3: the WebUI has GUI controls for workspace/pool/session. CommandDispatchStage
    handles cross-channel commands (/continue) shared with IM. Pool-switch
    shortcuts typed into the chat box reach the terminal Unsupported stage.

    ModelChoiceStage 仅在此 pipeline 注册：把 WebUI 选中的 provider/model 解析为
    ResolvedModel 写入 envelope.metadata，由 S8 builder 注册到 registry。IM
    pipeline 不注册（始终使用默认模型）。
    """
    return await _build_pipeline(
        registry,
        ctx,
        WEBUI_STAGE_SKELETON,
        {
            InputStageName.MODEL_CHOICE: {"bot_model_config": bot_model_config},
            InputStageName.SKILL_PARSE: {"skill_registry": skill_registry},
        },
        stage_order=stage_order,
    )


async def build_acp_pipeline(
    *,
    registry: ComponentRegistry,
    ctx: AssemblyContext,
    skill_registry: PoolSkillResolverRegistry,
    stage_order: Sequence[str] | None = None,
) -> BotInputPreparation:
    """Fixed-project preparation; delivery remains owned by pool.run_input."""
    return await _build_pipeline(
        registry,
        ctx,
        ACP_STAGE_SKELETON,
        {InputStageName.SKILL_PARSE: {"skill_registry": skill_registry}},
        stage_order=stage_order,
    )
