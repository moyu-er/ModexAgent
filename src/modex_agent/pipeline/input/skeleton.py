"""Input-pipeline stage skeletons + slot insertion rule (W4b).

The three skeletons below are the ONLY stage lists (DESIGN.md §7 — no
channel may copy the list): every builder resolves its stages through
:func:`resolve_stage_names` over the ``INPUT_STAGE`` component slot, which
inserts deployment-registered custom stages before the terminal
``UNSUPPORTED_COMMAND`` guard so a custom command is always claimed before
the generic rejection fires.

Stage ORDER is declaratively configurable (W6): an optional ordered
stage-name list (``order``) reorders the resolved set — including placing
custom stages anywhere, not just before the terminal guard. ``order=None``
(the default) keeps the code-defined order byte-identical; a declared
order must cover exactly the resolved stage set (no unknown names, no
omissions) or the build fails loudly. The declaration surface is the app
config's ``input_stage_order`` block (``AppConfig.input_stage_order``,
keyed per skeleton), threaded by each deployment's pipeline builder.

Stage configs are ALWAYS constructed from the registry-resolved factory's
own ``config_model`` — never from an import of the plugin module. The real
service registry is built via directory discovery, which imports plugin
files under synthetic module names; an instance built from a direct plugin
import is therefore a structurally-identical but distinct class to the
factory's, and ``model_validate`` rejects it (boot regression 2026-08-20).
The registry is the single identity source.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Final

from modex_agent.scope.component_registry import ComponentRegistry
from modex_agent.scope.components import ComponentSlot

__all__ = [
    "InputStageName",
    "IM_STAGE_SKELETON",
    "WEBUI_STAGE_SKELETON",
    "ACP_STAGE_SKELETON",
    "resolve_stage_names",
]


class InputStageName(StrEnum):
    """The built-in input-stage vocabulary (registration names)."""

    SET_CHANNEL = "set_channel"
    RESOLVE_WORKSPACE = "resolve_workspace"
    ENVIRONMENT_CONTROL = "environment_control"
    SESSION_CONTROL = "session_control"
    RESOLVE_POOL = "resolve_pool"
    MODEL_CHOICE = "model_choice"
    COMMAND_DISPATCH = "command_dispatch"
    ATTACHMENT_INGEST = "attachment_ingest"
    APPROVAL = "approval"
    SKILL_PARSE = "skill_parse"
    UNSUPPORTED_COMMAND = "unsupported_command"
    PERSIST_USER_MESSAGE = "persist_user_message"


#: IM pipeline: S4→S2→S3→S5→CommandDispatch→Ingest→Approval→Skill→Unsupported→Persist.
IM_STAGE_SKELETON: Final[tuple[InputStageName, ...]] = (
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
)

#: WebUI pipeline: S4→S5→ModelChoice→CommandDispatch→Ingest→Approval→Skill→Unsupported→Persist.
#: No S2/S3 — the WebUI has GUI controls for workspace/pool/session.
WEBUI_STAGE_SKELETON: Final[tuple[InputStageName, ...]] = (
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
)

#: Fixed-project preparation (ACP): the WebUI skeleton without per-turn
#: model choice — delivery remains owned by ``pool.run_input``.
ACP_STAGE_SKELETON: Final[tuple[InputStageName, ...]] = tuple(
    name for name in WEBUI_STAGE_SKELETON if name is not InputStageName.MODEL_CHOICE
)

_BUILTIN_STAGE_NAMES: Final[frozenset[str]] = frozenset(InputStageName)


def resolve_stage_names(
    registry: ComponentRegistry,
    skeleton: tuple[InputStageName, ...],
    *,
    order: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """The skeleton with deployment-registered custom stages inserted
    immediately before the terminal ``UNSUPPORTED_COMMAND`` guard.

    ``order`` (the W6 declarative face) overrides the resulting sequence:
    it must contain EXACTLY the stage names the default resolution
    produces (the skeleton's stages plus the deployment's custom stages)
    in the desired order — unknown or missing names fail loudly at build
    time so a stale declaration can never silently drop a stage.
    ``None`` keeps the code-defined order (zero behavior change).
    """
    custom_names = tuple(
        name
        for name in registry.names(ComponentSlot.INPUT_STAGE)
        if name not in _BUILTIN_STAGE_NAMES
    )
    insertion_index = skeleton.index(InputStageName.UNSUPPORTED_COMMAND)
    default_names: tuple[str, ...] = (
        *skeleton[:insertion_index],
        *custom_names,
        *skeleton[insertion_index:],
    )
    if order is None:
        return default_names
    missing = [name for name in default_names if name not in set(order)]
    unknown = [name for name in order if name not in set(default_names)]
    if missing or unknown:
        raise ValueError(
            f"input_stage_order must cover exactly the resolved stages; "
            f"missing={[str(n) for n in missing]} "
            f"unknown={[str(n) for n in unknown]} declared={list(order)} "
            f"resolved={[str(n) for n in default_names]}"
        )
    return tuple(order)
