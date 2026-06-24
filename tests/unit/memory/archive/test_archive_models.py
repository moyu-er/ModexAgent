from __future__ import annotations

from modex_agent.memory.archive_models import (
    ARCHIVE_SCHEMA,
    ArchiveBundleResult,
    ArchiveChannel,
    ArchiveGenerationInputs,
    ArchiveInputStats,
    ArchiveState,
    ArchiveWrite,
)


def test_archive_channel_values_are_protocol_constants() -> None:
    assert ArchiveChannel.CONTEXT.value == "context"
    assert ArchiveChannel.KNOWLEDGE.value == "knowledge"


def test_archive_write_normalizes_metadata_channel() -> None:
    write = ArchiveWrite(
        channel=ArchiveChannel.CONTEXT,
        summary="summary",
        metadata={"reason": "message_count"},
    )

    assert write.metadata["schema"] == ARCHIVE_SCHEMA
    assert write.metadata["channel"] == ArchiveChannel.CONTEXT.value
    assert write.metadata["reason"] == "message_count"


def test_archive_state_defaults_start_at_one() -> None:
    state = ArchiveState()

    assert state.next_archive_id == 1
    assert state.knowledge_consumed_archive_id == 0


def test_archive_generation_inputs_are_typed() -> None:
    stats = ArchiveInputStats(
        input_messages=4,
        context_messages=3,
        knowledge_messages=2,
        tool_chains=1,
        dropped_messages=1,
    )
    inputs = ArchiveGenerationInputs(
        context_transcript="[user]\nhello",
        knowledge_transcript="[fact]\nhello",
        stats=stats,
    )

    assert inputs.stats.tool_chains == 1
    assert inputs.context_transcript.startswith("[user]")


def test_bundle_result_tracks_shared_archive_id() -> None:
    result = ArchiveBundleResult(
        archive_id=7,
        written_channels=(ArchiveChannel.CONTEXT, ArchiveChannel.KNOWLEDGE),
    )

    assert result.archive_id == 7
    assert result.written_channels == (
        ArchiveChannel.CONTEXT,
        ArchiveChannel.KNOWLEDGE,
    )
