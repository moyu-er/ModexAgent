"""Assemble IM (S2..S8) and WebUI (S4..S8) sub-pipelines."""
from __future__ import annotations
from bot.input_pipeline.stages.environment_control import EnvironmentControlStage
from bot.input_pipeline.stages.enqueue import EnqueueStage
from bot.input_pipeline.stages.persist_user_message import PersistUserMessageStage
from bot.input_pipeline.stages.resolve_pool import ResolvePoolStage
from bot.input_pipeline.stages.session_control import SessionControlStage
from bot.input_pipeline.stages.set_channel import SetChannelStage
from bot.input_pipeline.stages.skill_parse import SkillParseStage, SkillRegistry
from framework.input_pipeline.pipeline import UserInputPipeline

def build_im_pipeline(*, skill_registry: SkillRegistry, known_pools: set[str]) -> UserInputPipeline:
    """IM pipeline: S4→S2→S3→S5→S6→S7→S8.

    SetChannel runs first so that _try_intercept_control responses
    (sent by S2/S3 via ChannelRouterOutputAdapter) are routed to the
    correct per-channel output adapter.  Without it, get_conv_channel()
    defaults to ``"websocket"`` and IM users never see command notices.
    """
    return UserInputPipeline([
        SetChannelStage(),                                 # S4 (runs first — see docstring)
        EnvironmentControlStage(known_pools=known_pools),  # S2
        SessionControlStage(),                             # S3
        ResolvePoolStage(),                                # S5
        SkillParseStage(skill_registry, known_pools=known_pools),  # S6
        PersistUserMessageStage(),                         # S7
        EnqueueStage(),                                    # S8
    ])

def build_webui_pipeline(*, skill_registry: SkillRegistry, known_pools: set[str]) -> UserInputPipeline:
    return UserInputPipeline([
        SetChannelStage(),                                 # S4
        ResolvePoolStage(),                                # S5
        SkillParseStage(skill_registry, known_pools=known_pools),  # S6
        PersistUserMessageStage(),                         # S7
        EnqueueStage(),                                    # S8
    ])
