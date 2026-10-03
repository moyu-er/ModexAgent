"""Framework-owned generic input-pipeline stages (W4b).

Channel/workspace-specific stages (environment control, session control,
workspace resolve, skill parse, model choice, enqueue) and the per-stage
handler tables stay in the deployment's input-pipeline package; the stages
here consume only the framework :class:`~modex_agent.pipeline.input.context.InputContext`
contract plus injected deployment callbacks. The approval onramp stage is
approval vocabulary — it lives in the approval capability bundle
(``plugins/defaults/capabilities/approval/stage.py``).
"""

from modex_agent.pipeline.input.stages.attachment_ingest import AttachmentIngestStage
from modex_agent.pipeline.input.stages.command import (
    CommandContext,
    CommandDispatchStage,
    CommandHandler,
)
from modex_agent.pipeline.input.stages.persist_user_message import (
    PersistUserMessageStage,
    UserMessageWriter,
)
from modex_agent.pipeline.input.stages.resolve_pool import (
    ResolvePoolStage,
    RoutingMeta,
    SelectionSource,
    SessionRouting,
    conversation_session_prefix,
    resolve_session_routing,
)
from modex_agent.pipeline.input.stages.set_channel import (
    ChannelRecorder,
    SetChannelStage,
)
from modex_agent.pipeline.input.stages.unsupported_command import (
    UnsupportedCommandStage,
)

__all__ = [
    "AttachmentIngestStage",
    "ChannelRecorder",
    "CommandContext",
    "CommandDispatchStage",
    "CommandHandler",
    "PersistUserMessageStage",
    "ResolvePoolStage",
    "RoutingMeta",
    "SelectionSource",
    "SessionRouting",
    "SetChannelStage",
    "UnsupportedCommandStage",
    "UserMessageWriter",
    "conversation_session_prefix",
    "resolve_session_routing",
]
