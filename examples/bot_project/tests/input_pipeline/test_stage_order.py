"""W6 red anchor — declarative input-stage order through the production builder.

``AppConfig.input_stage_order`` (per-skeleton ordered stage lists) must take
effect through the REAL ``build_im_pipeline``/``build_webui_pipeline``
builders: a declaration reorders built-in stages and repositions a
deployment-registered custom stage anywhere — not just the default
before-the-guard insertion slot. Unconfigured = the code-defined skeleton
order (asserted here for regression).
"""

from __future__ import annotations

from typing import Final
from unittest.mock import MagicMock

import pytest
from bot.input_pipeline.prepare import BotInputPreparation
from bot.input_pipeline.stages.skill_parse import PoolSkillResolverRegistry
from bot_plugins.im_input_stages import IMInputStagesPlugin
from pydantic import BaseModel, ConfigDict

from modex_agent.pipeline.input.skeleton import (
    IM_STAGE_SKELETON,
    InputStageName,
)
from modex_agent.pipeline.input.stage import InputStage, StageResult
from modex_agent.plugins.assembly.context import AssemblyContext
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.scope.components import ComponentFactory
from modex_agent.workspace.context import WorkspaceContext


class _ProbeStageConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _ProbeStage(InputStage):
    async def process(self, envelope, ctx) -> StageResult:  # noqa: ANN001
        from modex_agent.pipeline.input.stage import Terminate

        return Terminate(reason="probe")


class _ProbeStageFactory(ComponentFactory):
    config_model = _ProbeStageConfig

    async def create(self, config: BaseModel, ctx) -> _ProbeStage:  # noqa: ARG002
        return _ProbeStage()


_CUSTOM_STAGE_NAME: Final = "vip_probe_stage"


def _registry_with_custom_stage() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        IMInputStagesPlugin().register(registration)
        registration.register_input_stage(
            _CUSTOM_STAGE_NAME, _ProbeStageFactory()
        )
    return registry


def _assembly_ctx(registry: ComponentRegistry, tmp_path) -> AssemblyContext:
    return AssemblyContext(
        registry=registry,
        workspace_ctx=WorkspaceContext.from_target(
            tmp_path, data_dir_name=".modex", home=tmp_path
        ),
    )


def _skeleton_names() -> list[str]:
    return [stage.value for stage in IM_STAGE_SKELETON]


async def test_unconfigured_order_is_the_code_defined_skeleton(tmp_path) -> None:
    """No custom stages, no declared order → the IM skeleton verbatim."""
    from bot.input_pipeline.assembly import build_im_pipeline

    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        IMInputStagesPlugin().register(registration)

    pipe = await build_im_pipeline(
        registry=registry,
        ctx=_assembly_ctx(registry, tmp_path),
        skill_registry=MagicMock(spec=PoolSkillResolverRegistry),
        known_pools={"main"},
    )
    assert len(pipe._stages) == len(IM_STAGE_SKELETON)  # noqa: SLF001


async def test_declared_order_reorders_and_inserts_custom_stage(tmp_path) -> None:
    """The red anchor: ``stage_order`` (what AppConfig.input_stage_order.im
    feeds the builder) reorders a built-in pair AND places the custom stage
    FIRST — impossible under the default before-the-guard insertion."""
    from bot.input_pipeline.assembly import build_im_pipeline

    registry = _registry_with_custom_stage()
    ctx = _assembly_ctx(registry, tmp_path)

    # Default (unconfigured): the custom stage sits immediately before the
    # unsupported-command guard, and persist_user_message is LAST.
    default_pipe = await build_im_pipeline(
        registry=registry,
        ctx=ctx,
        skill_registry=MagicMock(spec=PoolSkillResolverRegistry),
        known_pools={"main"},
    )
    default_stages = default_pipe._stages  # noqa: SLF001
    from modex_agent.pipeline.input.stages.unsupported_command import (
        UnsupportedCommandStage,
    )

    guard_index = next(
        i
        for i, stage in enumerate(default_stages)
        if isinstance(stage, UnsupportedCommandStage)
    )
    # The custom stage sits immediately before the guard (the default
    # insertion slot), and persist_user_message stays LAST.
    assert isinstance(default_stages[guard_index - 1], _ProbeStage)

    # Declared: custom stage FIRST, persist_user_message BEFORE the guard.
    names = _skeleton_names()
    guard = names.index(InputStageName.UNSUPPORTED_COMMAND)
    declared = [
        _CUSTOM_STAGE_NAME,
        *names[:guard],
        names[-1],  # persist_user_message hoisted before...
        names[guard],  # ...the unsupported guard
    ]
    ordered_pipe = await build_im_pipeline(
        registry=registry,
        ctx=ctx,
        skill_registry=MagicMock(spec=PoolSkillResolverRegistry),
        known_pools={"main"},
        stage_order=declared,
    )
    ordered_stages = ordered_pipe._stages  # noqa: SLF001
    assert isinstance(ordered_stages[0], _ProbeStage)
    from modex_agent.pipeline.input.stages.persist_user_message import (
        PersistUserMessageStage,
    )

    assert isinstance(ordered_stages[-2], PersistUserMessageStage)

    assert isinstance(ordered_stages[-1], UnsupportedCommandStage)
    assert isinstance(ordered_pipe, BotInputPreparation)


async def test_declared_order_with_missing_stage_fails_loudly(tmp_path) -> None:
    """A stale declaration that drops a stage must fail the build, never
    silently produce a thinner pipeline."""
    from bot.input_pipeline.assembly import build_im_pipeline

    registry = _registry_with_custom_stage()
    declared = [name for name in _skeleton_names() if name != "approval"]
    declared.insert(0, _CUSTOM_STAGE_NAME)
    with pytest.raises(ValueError, match="missing=\\['approval'\\]"):
        await build_im_pipeline(
            registry=registry,
            ctx=_assembly_ctx(registry, tmp_path),
            skill_registry=MagicMock(spec=PoolSkillResolverRegistry),
            known_pools={"main"},
            stage_order=declared,
        )
