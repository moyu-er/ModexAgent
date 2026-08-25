"""Per-item memory probe execution, tracing, and score injection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Literal, assert_never
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from bot.eval.memory_harness import MemoryRuntimeServices
from bot.eval.probes._harness_langfuse import harness_source_hash
from bot.eval.probes._harness_models import (
    ExperimentSetup,
    MemorySnapshot,
    ProbeHarnessError,
    ProbeHarnessServices,
    ProbeRunRecord,
    ProbeScore,
)
from bot.eval.probes.budget import BudgetedProvider
from bot.eval.probes.schema import Probe, Speaker, WorldSpec
from modex_agent.core.constants import RuntimeInfoKey
from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import MemoryAgentRole, MemoryContext
from modex_agent.core.types import MessageRole
from modex_agent.trace.experiment_attrs import (
    ExperimentLinkage,
    attach_experiment_attrs,
)
from modex_agent.trace.score_injector import ScoreSpec
from modex_agent.trace.semconv import GenAiAttr, SpanKind
from modex_agent.trace.store import SpanModel

_ANSWER_SPAN_NAME: Final = "probe.answer"
_SCORE_VERSION: Final = "memory_probe_harness.v1"


class _ProbeAttr(StrEnum):
    ID = "memory.probe.id"
    TYPE = "memory.probe.type"


class _ProbeProvenance(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scorer: Literal["verifier"] = "verifier"
    version: str = _SCORE_VERSION
    report_source: Literal["counters"] = "counters"
    run_ref: str
    suite_version: str
    harness_hash: str


async def ingest_world(world: WorldSpec, bundle: MemoryRuntimeServices) -> int:
    """Append the frozen timeline through scoped production message histories."""
    ingested = 0
    for session in sorted(world.sessions, key=lambda item: (item.timestamp, item.session_id)):
        context = MemoryContext(
            session_id=session.session_id,
            user_id=session.persona_id,
            agent_id="react",
            agent_role=MemoryAgentRole.MAIN,
        )
        history = bundle.memory_system.create_message_history(context=context)
        for index, turn in enumerate(session.turns):
            match turn.speaker:
                case Speaker.USER:
                    role = MessageRole.USER
                case Speaker.ASSISTANT:
                    role = MessageRole.ASSISTANT
                case unreachable:
                    assert_never(unreachable)
            await history.append(
                ChatMessage(
                    role=role,
                    content=turn.text,
                    created_at=session.timestamp + timedelta(microseconds=index),
                )
            )
            ingested += 1
    return ingested


async def query_and_score(
    probe: Probe,
    snapshot: MemorySnapshot,
    experiment: ExperimentSetup,
    bundle: MemoryRuntimeServices,
    provider: BudgetedProvider,
    services: ProbeHarnessServices,
    max_output_tokens: int,
) -> tuple[ProbeRunRecord, ProbeScore]:
    """Assemble memory, request a bare answer, and attach one verifier score."""
    started = datetime.now(UTC)
    session_id = f"probe.{probe.probe_id}"
    state = await bundle.context_manager.load(
        session_id,
        runtime_info={RuntimeInfoKey.MESSAGE: probe.question},
        metadata={"user_id": probe.persona_id},
    )
    messages = ChatMessage.from_dicts(await state.to_messages())
    messages.append(ChatMessage(role=MessageRole.USER, content=probe.question))
    response = await provider.chat(
        messages,
        temperature=0.0,
        max_output_tokens=max_output_tokens,
        tools=None,
    )
    answer = response.content or ""
    span = attach_experiment_attrs(
        _answer_span(probe, session_id, provider.get_default_model(), answer, response.usage),
        experiment_linkage(experiment, experiment.item_id_for(probe.probe_id)),
    )
    trace_store = bundle.runtime_services.trace_store
    if trace_store is None:
        raise ProbeHarnessError("probe answer span requires an enabled trace store")
    await trace_store.save_span(span)
    record = ProbeRunRecord(
        probe=probe,
        answer=answer,
        assembled_context="\n".join(str(message.content or "") for message in messages[:-1]),
        trace_id=span.trace_id,
        span_id=span.span_id,
        started_at=started,
        completed_at=datetime.now(UTC),
        snapshot_captured_at=snapshot.captured_at,
    )
    score = services.score_fn(record)
    comment = _ProbeProvenance(
        run_ref=experiment.experiment_id,
        suite_version=snapshot.suite_version,
        harness_hash=harness_source_hash(),
    ).model_dump_json()
    await services.score_injector.inject_score_batch(
        span.trace_id,
        [
            ScoreSpec(
                name=score.name,
                value=score.value,
                data_type=score.data_type,
                comment=comment,
            )
        ],
        observation_id=span.span_id,
    )
    return record, score


def experiment_linkage(experiment: ExperimentSetup, item_id: str) -> ExperimentLinkage:
    """Build the five-attribute linkage source for one experiment item."""
    return ExperimentLinkage(
        experiment_id=experiment.experiment_id,
        experiment_name=experiment.experiment_name,
        dataset_id=experiment.dataset_id,
        item_id=item_id,
    )


def _answer_span(
    probe: Probe,
    session_id: str,
    model: str,
    answer: str,
    usage: dict[str, int],
) -> SpanModel:
    timestamp = datetime.now(UTC).timestamp()
    return SpanModel(
        trace_id=uuid4().hex,
        span_id=uuid4().hex[:16],
        name=_ANSWER_SPAN_NAME,
        kind=SpanKind.CLIENT,
        start_time=timestamp,
        end_time=timestamp,
        attributes={
            _ProbeAttr.ID: probe.probe_id,
            _ProbeAttr.TYPE: probe.probe_type.value,
            GenAiAttr.CONVERSATION_ID: session_id,
            GenAiAttr.REQUEST_MODEL: model,
            GenAiAttr.RESPONSE_MODEL: model,
            GenAiAttr.INPUT_MESSAGES: probe.question,
            GenAiAttr.OUTPUT_MESSAGES: answer,
            GenAiAttr.USAGE_INPUT_TOKENS: usage.get("prompt_tokens", 0),
            GenAiAttr.USAGE_OUTPUT_TOKENS: usage.get("completion_tokens", 0),
            GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS: usage.get("cache_read_input_tokens", 0),
            GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS: usage.get(
                "cache_creation_input_tokens", 0
            ),
        },
    )
