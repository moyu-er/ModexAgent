"""G4 tests — transcript id→path index for attachments.

Covers (4.1) Attachment records ride on ``UserMessageRecord`` /
``AttachmentCarrier`` transcript records and round-trip through the store,
the persist stage serializes ``envelope.resolved_attachments``; (4.2)
``find_attachment`` scans both record kinds and resolves an id → Attachment
VO (or None). No bytes are ever stored.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

import pytest
from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.persist_user_message import (
    write_user_message_to_transcript,
)
from bot.service.attachment_index import find_attachment
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.transcript_store import (
    AttachmentCarrier,
    JSONLTranscriptStore,
    UserMessageRecord,
)

from modex_agent.core.media import Attachment, AttachmentLocator, Kind
from modex_agent.pipeline.input.envelope import UserInputEnvelope
from modex_agent.pipeline.input.stages.persist_user_message import (
    PersistUserMessageStage,
)
from modex_agent.pipeline.input.stages.resolve_pool import RoutingMeta
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.runtime import bind_workspace_root

# ── Fixtures ────────────────────────────────────────────────────────────────


def _inbound_attachment() -> Attachment:
    return Attachment(
        id="att-inbound-1",
        kind=Kind.IMAGE,
        name="cat.png",
        mime="image/png",
        size=12345,
        path="media/main/uploads/s1/cat.png",
        locator=AttachmentLocator.MEDIA,
    )


def _outbound_attachment() -> Attachment:
    return Attachment(
        id="att-outbound-1",
        kind=Kind.EXTRACTABLE_DOCUMENT,
        name="report.pdf",
        mime="application/pdf",
        size=999,
        path="/abs/workspace/report.pdf",
        locator=AttachmentLocator.WORKSPACE,
    )


def _ctx(store: WorkspaceScopedTranscriptStore) -> BotInputContext:
    return BotInputContext(
        default_pool="main",
        available_pools=lambda: {"main"},
        pool_session_store=MagicMock(),
        agent_resolver=lambda p: p,
        transcript_store=store,
        enqueue_message=MagicMock(),
        command_adapter=MagicMock(),
    )


# ── 4.1: Attachment VO serialization ────────────────────────────────────────


class TestAttachmentSerialization:
    def test_to_dict_carries_only_metadata_fields(self) -> None:
        att = _inbound_attachment()
        d = att.to_dict()
        assert set(d.keys()) == {"id", "kind", "name", "mime", "size", "path", "locator"}
        # StrEnum wire form is the string value.
        assert d["kind"] == "image"
        assert d["locator"] == "media"

    def test_round_trip_reproduces_attachment_exactly(self) -> None:
        att = _inbound_attachment()
        rebuilt = Attachment.from_dict(att.to_dict())
        assert rebuilt == att

    def test_round_trip_preserves_none_mime(self) -> None:
        att = Attachment(
            id="x",
            kind=Kind.OTHER,
            name="blob",
            mime=None,
            size=0,
            path="/p",
            locator=AttachmentLocator.WORKSPACE,
        )
        assert Attachment.from_dict(att.to_dict()) == att


# ── 4.1: events carry attachments and round-trip ───────────────────────────


class TestEventAttachments:
    def test_user_message_round_trips_attachment(self) -> None:
        att = _inbound_attachment()
        record = UserMessageRecord(
            session_id="s1.main",
            agent_name="main",
            timestamp_ms=1,
            content="hi",
            attachments=[att.to_dict()],
        )
        loaded = UserMessageRecord.model_validate_json(record.model_dump_json())
        assert len(loaded.attachments) == 1
        assert Attachment.from_dict(loaded.attachments[0]) == att

    def test_user_message_defaults_to_empty_attachments(self) -> None:
        record = UserMessageRecord(session_id="s1.main", agent_name="main", content="hi")
        assert record.attachments == []
        # Omitted field survives a round-trip (default applies on load).
        loaded = UserMessageRecord.model_validate_json(record.model_dump_json())
        assert loaded.attachments == []

    def test_attachment_carrier_round_trips_attachment(self) -> None:
        att = _outbound_attachment()
        record = AttachmentCarrier(
            session_id="s1.main",
            agent_name="main",
            timestamp_ms=1,
            attachments=[att.to_dict()],
        )
        loaded = AttachmentCarrier.model_validate_json(record.model_dump_json())
        assert len(loaded.attachments) == 1
        assert Attachment.from_dict(loaded.attachments[0]) == att

    def test_attachment_carrier_defaults_to_empty_attachments(self) -> None:
        record = AttachmentCarrier(session_id="s1.main", agent_name="main", timestamp_ms=1)
        assert record.attachments == []
        loaded = AttachmentCarrier.model_validate_json(record.model_dump_json())
        assert loaded.attachments == []


# ── 4.1: persist stage wires resolved_attachments ──────────────────────────


class TestPersistStageWiring:
    @pytest.mark.asyncio
    async def test_persist_serializes_resolved_attachments(self) -> None:
        att = _inbound_attachment()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            env = UserInputEnvelope(external_id="u1", content="hello", channel="websocket")
            env.metadata[RoutingMeta.FULL_SESSION_ID] = "u1.main"
            env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
            env.metadata[RoutingMeta.WORKSPACE] = str(root)
            env.resolved_attachments = [att]
            with bind_workspace_root(root):
                await PersistUserMessageStage(write_user_message_to_transcript).process(env, _ctx(store))
                events = await store.load("u1.main")
            assert len(events) == 1
            persisted = events[0]
            assert isinstance(persisted, UserMessageRecord)
            assert len(persisted.attachments) == 1
            assert Attachment.from_dict(persisted.attachments[0]) == att

    @pytest.mark.asyncio
    async def test_persist_empty_when_no_attachments(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            env = UserInputEnvelope(external_id="u1", content="hello", channel="websocket")
            env.metadata[RoutingMeta.FULL_SESSION_ID] = "u1.main"
            env.metadata[RoutingMeta.RESOLVED_AGENT] = "main"
            env.metadata[RoutingMeta.WORKSPACE] = str(root)
            with bind_workspace_root(root):
                await PersistUserMessageStage(write_user_message_to_transcript).process(env, _ctx(store))
                events = await store.load("u1.main")
            assert len(events) == 1
            persisted = events[0]
            assert isinstance(persisted, UserMessageRecord)
            assert persisted.attachments == []


# ── 4.2: find_attachment scans both event types ────────────────────────────


class TestFindAttachment:
    @pytest.mark.asyncio
    async def test_hit_on_user_message(self) -> None:
        att = _inbound_attachment()
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            await store.append(
                "s1.main",
                UserMessageRecord(
                    session_id="s1.main",
                    agent_name="main",
                    content="hi",
                    attachments=[att.to_dict()],
                ),
            )
            found = await find_attachment(store, "s1.main", att.id)
        assert found == att

    @pytest.mark.asyncio
    async def test_miss_returns_none(self) -> None:
        att = _inbound_attachment()
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            await store.append(
                "s1.main",
                UserMessageRecord(
                    session_id="s1.main",
                    agent_name="main",
                    content="hi",
                    attachments=[att.to_dict()],
                ),
            )
            found = await find_attachment(store, "s1.main", "nonexistent-id")
        assert found is None

    @pytest.mark.asyncio
    async def test_hit_on_assistant_turn(self) -> None:
        """Outbound records (populated in G7) are found via the carrier."""
        att = _outbound_attachment()
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            await store.append(
                "s1.main",
                AttachmentCarrier(
                    session_id="s1.main",
                    agent_name="main",
                    attachments=[att.to_dict()],
                ),
            )
            found = await find_attachment(store, "s1.main", att.id)
        assert found == att

    @pytest.mark.asyncio
    async def test_empty_session_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            found = await find_attachment(store, "s1.main", "any-id")
        assert found is None

    @pytest.mark.asyncio
    async def test_skips_non_attachment_events(self) -> None:
        """Records without the attachments field are scanned past."""
        att = _inbound_attachment()
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            # A user_message WITHOUT the matching id, then one WITH it.
            await store.append(
                "s1.main",
                UserMessageRecord(
                    session_id="s1.main",
                    agent_name="main",
                    content="first",
                    attachments=[
                        Attachment(
                            id="other",
                            kind=Kind.OTHER,
                            name="o",
                            mime=None,
                            size=1,
                            path="/o",
                            locator=AttachmentLocator.WORKSPACE,
                        ).to_dict()
                    ],
                ),
            )
            await store.append(
                "s1.main",
                UserMessageRecord(
                    session_id="s1.main",
                    agent_name="main",
                    content="second",
                    attachments=[att.to_dict()],
                ),
            )
            found = await find_attachment(store, "s1.main", att.id)
        assert found == att

    @pytest.mark.asyncio
    async def test_workspace_store_load_with_explicit_sessions_dir(self) -> None:
        """Production branch: WorkspaceScopedTranscriptStore + sessions_dir kwarg.

        Mirrors the G6 HTTP-handler path: an out-of-turn caller passes the
        ``?ws=``-resolved ``sessions_dir`` explicitly. Covers the
        ``load(session_id, sessions_dir=...)`` branch that the 1-arg
        JSONLTranscriptStore tests do not.
        """
        att = _inbound_attachment()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws_store = WorkspaceScopedTranscriptStore(data_dir_name=".modex")
            sessions_dir = WorkspacePaths(root / ".modex").sessions_dir
            await ws_store.append(
                "u1.main",
                UserMessageRecord(
                    session_id="u1.main",
                    agent_name="main",
                    content="hi",
                    attachments=[att.to_dict()],
                ),
                sessions_dir=sessions_dir,
            )
            found = await find_attachment(ws_store, "u1.main", att.id, sessions_dir=sessions_dir)
        assert found == att

    @pytest.mark.asyncio
    async def test_earliest_match_wins_when_id_repeats(self) -> None:
        """When the same id appears on two records, the chronologically first is returned."""
        shared_id = "dup-id"
        first = Attachment(
            id=shared_id,
            kind=Kind.IMAGE,
            name="first.png",
            mime="image/png",
            size=1,
            path="media/main/uploads/s1/first.png",
            locator=AttachmentLocator.MEDIA,
        )
        second = Attachment(
            id=shared_id,
            kind=Kind.IMAGE,
            name="second.png",
            mime="image/png",
            size=2,
            path="media/main/uploads/s1/second.png",
            locator=AttachmentLocator.MEDIA,
        )
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            # First a user message record carrying `first`, then a carrier
            # carrying `second` — both under the same id. Append order is the
            # transcript's chronological order.
            await store.append(
                "s1.main",
                UserMessageRecord(
                    session_id="s1.main",
                    agent_name="main",
                    content="hi",
                    attachments=[first.to_dict()],
                ),
            )
            await store.append(
                "s1.main",
                AttachmentCarrier(
                    session_id="s1.main",
                    agent_name="main",
                    attachments=[second.to_dict()],
                ),
            )
            found = await find_attachment(store, "s1.main", shared_id)
        assert found == first
