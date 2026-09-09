"""Shared input preparation: typed prepare outcome, delivery as a separate action.

DESIGN.md §7 (T07/P01): the registered stage order is unchanged, but the
orchestration is split into **prepare** and **delivery**:

- :meth:`BotInputPreparation.prepare` runs the shared stage list (S4..S7; the
  ENQUEUE terminal is no longer part of any pipeline — its message construction
  lives in :func:`bot.input_pipeline.stages.enqueue.build_input_message`) and
  yields the typed bot-owned outcome ``Prepared(InputMessage) | Handled``. It
  NEVER touches ``ctx.enqueue_message``: the ACP entry awaits prepare, holds the
  admission reservation beforehand, and delivers via ``pool.run_input`` itself.
- :meth:`BotInputPreparation.handle` is the original adapter contract: it runs
  the SAME stage list, delivers the prepared message through the channel's
  original sync ``ctx.enqueue_message`` callback exactly once, and returns the
  ORIGINAL ``StageResult`` (Terminate keeps its exact reason/response payload —
  no lossy rebuild; adapters consume the same shapes as before).

Both delivery points (the early ``/continue`` command and the terminal S8
construction) converge onto one carriage (``RoutingMeta.PREPARED_MESSAGE``) and
one builder, so no second delivery point can be missed. S7 remains the single
user-transcript writer; approval decisions never persist as user messages.

ACP contract: the envelope carries the bound session via
``pre_resolved_session``; the workspace comes from ``ctx.current_ws_provider()``
(ResolveWorkspaceStage stamps it — no synthetic metadata). The first write step
inside prepare is S7 (persist_user_message): the caller must hold the request
admission reservation BEFORE invoking prepare (T05 run_input two-phase owner).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.enqueue import build_input_message
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.pipeline import UserInputPipeline
from modex_agent.input_pipeline.stage import StageResult, Terminate
from modex_agent.messaging.models import InputMessage

__all__ = [
    "BotInputPreparation",
    "Handled",
    "PrepareOutcome",
    "Prepared",
]


class Prepared(BaseModel):
    """A message is ready for delivery (pool queue callback / run_input)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["prepared"] = "prepared"
    message: InputMessage


class Handled(BaseModel):
    """The pipeline consumed the input; there is nothing to deliver.

    reason: the terminating stage's reason (``unsupported_command``,
    ``pool_unavailable``, ...). ``None`` means the input was quietly consumed
    (HANDLED without a prepared carriage) — the original pipeline shape for
    that case is Continue, never a Terminate.
    notice: user-facing message extracted from the terminating stage's response
    (``response["message"]``); converted exactly once, here at the typed
    boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["handled"] = "handled"
    reason: str | None = None
    notice: str | None = None


PrepareOutcome = Annotated[Prepared | Handled, Field(discriminator="kind")]


def _to_handled(terminate: Terminate) -> Handled:
    """Convert a stage Terminate into the typed outcome — the ONLY place the
    loose ``response`` payload is interpreted."""
    response = terminate.response
    notice: str | None = None
    if isinstance(response, dict):
        raw = response.get("message")
        notice = str(raw) if raw is not None else None
    return Handled(reason=terminate.reason, notice=notice)


class BotInputPreparation(UserInputPipeline):
    """One shared stage orchestration with typed preparation and separate delivery.

    Subclasses :class:`UserInputPipeline` so every existing pipeline call site
    (IM/WebUI adapters, WebUI server) keeps an unchanged ``handle`` contract;
    ``handle`` = the same raw stage run + the original sync enqueue callback.
    """

    async def prepare(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> PrepareOutcome:
        """Run the shared stages and return the typed outcome without delivering."""
        result = await super().handle(envelope, ctx)
        if isinstance(result, Terminate):
            return _to_handled(result)
        message = build_input_message(result.envelope(), ctx)
        if message is None:
            # HANDLED without a prepared carriage: the original pipeline shape
            # is a quiet Continue — consumed, nothing delivered, no Terminate.
            return Handled()
        return Prepared(message=message)

    async def handle(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        """Original adapter contract: prepare, then deliver via the original
        sync callback, returning the ORIGINAL StageResult shapes."""
        result = await super().handle(envelope, ctx)
        if result.should_continue():
            message = build_input_message(result.envelope(), ctx)
            if message is not None:
                ctx.enqueue_message(message)
        # Terminate objects pass through untouched (exact reason/response).
        return result
