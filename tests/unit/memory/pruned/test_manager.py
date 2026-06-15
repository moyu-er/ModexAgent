from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from framework.memory.pruned.manager import PrunedManager
from framework.utils.timezone import get_user_timezone

SID = "test-session"
TZ = get_user_timezone()
UTC = timezone.utc


@pytest.fixture()
def pruned_base_dir(tmp_path):
    return tmp_path / "pruned"


@pytest.fixture()
def manager(pruned_base_dir) -> PrunedManager:
    return PrunedManager(pruned_base_dir=pruned_base_dir)


@pytest.fixture()
def now() -> datetime:
    return datetime(2024, 6, 3, 10, 0, tzinfo=TZ)


def _messages(created_at_values: list[str | datetime]) -> list[dict]:
    return [{"role": "user", "content": f"msg{i}", "created_at": t} for i, t in enumerate(created_at_values)]


class TestFilenameGeneration:

    def test_both_times_present(self, manager: PrunedManager, now: datetime) -> None:
        start = datetime(2024, 6, 1, 9, 30, tzinfo=TZ)
        end = datetime(2024, 6, 2, 14, 45, tzinfo=TZ)
        result = manager._generate_filename(start, end, now)
        assert result == "pruned_2024-06-01_09.30-2024-06-02_14.45.jsonl"

    def test_start_missing(self, manager: PrunedManager, now: datetime) -> None:
        result = manager._generate_filename(None, datetime(2024, 6, 2, 14, 45, tzinfo=TZ), now)
        assert result == "pruned_2024-06-03_10.00.jsonl"

    def test_end_missing(self, manager: PrunedManager, now: datetime) -> None:
        result = manager._generate_filename(datetime(2024, 6, 1, 9, 30, tzinfo=TZ), None, now)
        assert result == "pruned_2024-06-03_10.00.jsonl"

    def test_both_missing(self, manager: PrunedManager, now: datetime) -> None:
        result = manager._generate_filename(None, None, now)
        assert result == "pruned_2024-06-03_10.00.jsonl"


class TestWritePruned:

    @pytest.mark.asyncio()
    async def test_with_topic(self, manager: PrunedManager, pruned_base_dir, now: datetime) -> None:
        msgs = _messages([
            datetime(2024, 6, 1, 9, 0, tzinfo=TZ),
            datetime(2024, 6, 1, 9, 5, tzinfo=TZ),
        ])
        await manager.write_pruned(msgs, "debugging session", now, session_id=SID)
        storage = manager._get_storage(SID)
        entries = storage.read_index()
        assert len(entries) == 1
        assert entries[0].topic == "debugging session"
        assert entries[0].message_count == 2
        assert entries[0].start_time == int(datetime(2024, 6, 1, 9, 0, tzinfo=TZ).timestamp())
        assert entries[0].end_time == int(datetime(2024, 6, 1, 9, 5, tzinfo=TZ).timestamp())

    @pytest.mark.asyncio()
    async def test_without_topic_time_fallback(self, manager: PrunedManager, pruned_base_dir, now: datetime) -> None:
        msgs = _messages([
            datetime(2024, 6, 1, 9, 0, tzinfo=TZ),
            datetime(2024, 6, 1, 9, 5, tzinfo=TZ),
        ])
        await manager.write_pruned(msgs, None, now, session_id=SID)
        storage = manager._get_storage(SID)
        entries = storage.read_index()
        assert entries[0].topic == "2024-06-01 09:00 ~ 2024-06-01 09:05 (2 messages)"

    @pytest.mark.asyncio()
    async def test_without_times_cleanup_fallback(self, manager: PrunedManager, pruned_base_dir, now: datetime) -> None:
        msgs = [{"role": "user", "content": "no timestamp"}]
        await manager.write_pruned(msgs, None, now, session_id=SID)
        storage = manager._get_storage(SID)
        entries = storage.read_index()
        assert entries[0].topic == "2024-06-03 10:00 (1 messages)"
        assert entries[0].start_time == 0
        assert entries[0].end_time == 0

    @pytest.mark.asyncio()
    async def test_id_auto_increments(self, manager: PrunedManager, pruned_base_dir, now: datetime) -> None:
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        await manager.write_pruned(msgs, "first", now, session_id=SID)
        await manager.write_pruned(msgs, "second", now, session_id=SID)
        storage = manager._get_storage(SID)
        entries = storage.read_index()
        assert entries[0].id == 1
        assert entries[1].id == 2

    @pytest.mark.asyncio()
    async def test_topic_truncation(self, pruned_base_dir, now: datetime) -> None:
        mgr = PrunedManager(pruned_base_dir=pruned_base_dir, topic_max_chars=10)
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        long_topic = "A" * 200
        await mgr.write_pruned(msgs, long_topic, now, session_id=SID)
        storage = mgr._get_storage(SID)
        entries = storage.read_index()
        assert len(entries[0].topic) == 10

    @pytest.mark.asyncio()
    async def test_string_created_at_parsed(self, manager: PrunedManager, pruned_base_dir, now: datetime) -> None:
        msgs = _messages(["2024-06-01T09:00:00+00:00", "2024-06-01T09:05:00+00:00"])
        await manager.write_pruned(msgs, "string times", now, session_id=SID)
        storage = manager._get_storage(SID)
        entries = storage.read_index()
        assert entries[0].start_time == int(datetime(2024, 6, 1, 9, 0, tzinfo=UTC).timestamp())

    @pytest.mark.asyncio()
    async def test_naive_string_gets_utc(self, manager: PrunedManager, pruned_base_dir, now: datetime) -> None:
        msgs = _messages(["2024-06-01T09:00:00"])
        await manager.write_pruned(msgs, "naive time", now, session_id=SID)
        storage = manager._get_storage(SID)
        entries = storage.read_index()
        assert entries[0].start_time_display == "2024-06-01 09:00"


class TestEviction:

    @pytest.mark.asyncio()
    async def test_removes_oldest_keeps_recent(self, pruned_base_dir, now: datetime) -> None:
        mgr = PrunedManager(pruned_base_dir=pruned_base_dir, max_files=2)
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        await mgr.write_pruned(msgs, "batch1", now, session_id=SID)
        await mgr.write_pruned(msgs, "batch2", now, session_id=SID)
        await mgr.write_pruned(msgs, "batch3", now, session_id=SID)
        storage = mgr._get_storage(SID)
        entries = storage.read_index()
        assert len(entries) == 2
        assert entries[0].topic == "batch2"
        assert entries[1].topic == "batch3"

    @pytest.mark.asyncio()
    async def test_deletes_pruned_files(self, pruned_base_dir, now: datetime) -> None:
        mgr = PrunedManager(pruned_base_dir=pruned_base_dir, max_files=2)
        msgs1 = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        msgs2 = _messages([datetime(2024, 6, 1, 10, 0, tzinfo=TZ)])
        msgs3 = _messages([datetime(2024, 6, 1, 11, 0, tzinfo=TZ)])
        await mgr.write_pruned(msgs1, "batch1", now, session_id=SID)
        await mgr.write_pruned(msgs2, "batch2", now, session_id=SID)
        await mgr.write_pruned(msgs3, "batch3", now, session_id=SID)
        session_dir = pruned_base_dir / SID
        content_files = [f for f in session_dir.iterdir() if f.suffix == ".jsonl" and f.name != "index.jsonl"]
        assert len(content_files) == 2

    @pytest.mark.asyncio()
    async def test_under_limit_no_eviction(self, manager: PrunedManager, pruned_base_dir, now: datetime) -> None:
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        await manager.write_pruned(msgs, "only one", now, session_id=SID)
        storage = manager._get_storage(SID)
        entries = storage.read_index()
        assert len(entries) == 1


class TestSessionIsolation:

    @pytest.mark.asyncio()
    async def test_different_sessions_have_separate_indices(self, pruned_base_dir, now: datetime) -> None:
        mgr = PrunedManager(pruned_base_dir=pruned_base_dir)
        msgs_a = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        msgs_b = _messages([datetime(2024, 6, 1, 10, 0, tzinfo=TZ)])
        await mgr.write_pruned(msgs_a, "session A", now, session_id="session-a")
        await mgr.write_pruned(msgs_b, "session B", now, session_id="session-b")
        assert mgr._get_storage("session-a").read_index()[0].topic == "session A"
        assert mgr._get_storage("session-b").read_index()[0].topic == "session B"


class TestCrossPlatformSessionId:
    """Session IDs use ``{conversation_id}:{agent_name}`` format — colons and
    other characters must be sanitized for filesystem-safe directory names."""

    @pytest.mark.asyncio()
    async def test_colon_in_session_id(self, pruned_base_dir, now: datetime) -> None:
        mgr = PrunedManager(pruned_base_dir=pruned_base_dir)
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        sid = "30932BC02F825E64D069B1E67347C8FF:main"
        await mgr.write_pruned(msgs, "test", now, session_id=sid)
        assert mgr.get_injection_xml(session_id=sid) is not None

    @pytest.mark.asyncio()
    async def test_multiple_colons_subagent_session(self, pruned_base_dir, now: datetime) -> None:
        """Subagent sessions: conversation_id:parent_agent:invocation_id."""
        mgr = PrunedManager(pruned_base_dir=pruned_base_dir)
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        sid = "ABC123:main:inv_def456"
        await mgr.write_pruned(msgs, "sub", now, session_id=sid)
        assert mgr.get_injection_xml(session_id=sid) is not None

    @pytest.mark.asyncio()
    async def test_empty_session_id(self, pruned_base_dir, now: datetime) -> None:
        mgr = PrunedManager(pruned_base_dir=pruned_base_dir)
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        await mgr.write_pruned(msgs, "empty", now, session_id="")
        assert mgr.get_injection_xml(session_id="") is not None

    @pytest.mark.asyncio()
    async def test_windows_reserved_chars(self, pruned_base_dir, now: datetime) -> None:
        """Characters < > : \" / \\ | ? * are forbidden in Windows filenames."""
        mgr = PrunedManager(pruned_base_dir=pruned_base_dir)
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        sid = "test<agent>:main\\sub"
        await mgr.write_pruned(msgs, "reserved", now, session_id=sid)
        assert mgr.get_injection_xml(session_id=sid) is not None

    @pytest.mark.asyncio()
    async def test_very_long_session_id(self, pruned_base_dir, now: datetime) -> None:
        """Session IDs exceeding 100 chars are truncated with MD5 hash suffix."""
        mgr = PrunedManager(pruned_base_dir=pruned_base_dir)
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        sid = "x" * 200 + ":main"
        await mgr.write_pruned(msgs, "long", now, session_id=sid)
        assert mgr.get_injection_xml(session_id=sid) is not None


class TestInjectionXml:

    def test_returns_none_when_empty(self, manager: PrunedManager) -> None:
        assert manager.get_injection_xml(session_id=SID) is None

    @pytest.mark.asyncio()
    async def test_returns_xml_when_content_exists(self, manager: PrunedManager, now: datetime) -> None:
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        await manager.write_pruned(msgs, "test", now, session_id=SID)
        xml = manager.get_injection_xml(session_id=SID)
        assert xml is not None
        from framework.memory.tags import PrunedTag

        assert f"<{PrunedTag.CONTAINER.value}>" in xml
        assert xml.strip().endswith(f"</{PrunedTag.CONTAINER.value}>")
        assert "### Previous Conversation Transcripts" in xml
        assert "index.jsonl" in xml

    @pytest.mark.asyncio()
    async def test_contains_absolute_path(self, manager: PrunedManager, pruned_base_dir, now: datetime) -> None:
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        await manager.write_pruned(msgs, "test", now, session_id=SID)
        xml = manager.get_injection_xml(session_id=SID)
        assert xml is not None
        expected_path = str((pruned_base_dir / SID).resolve())
        assert '<directory path="' in xml
        import html
        assert html.escape(expected_path) in xml


class TestGetVersion:

    def test_returns_zero_when_no_entries(self, manager: PrunedManager) -> None:
        """get_version returns '0' when no entries exist."""
        assert manager.get_version(session_id=SID) == "0"

    @pytest.mark.asyncio()
    async def test_returns_max_entry_id(self, manager: PrunedManager, now: datetime) -> None:
        """get_version returns str(max_entry_id)."""
        msgs = _messages([datetime(2024, 6, 1, 9, 0, tzinfo=TZ)])
        await manager.write_pruned(msgs, "topic1", now, session_id=SID)
        assert manager.get_version(session_id=SID) == "1"

        await manager.write_pruned(msgs, "topic2", now, session_id=SID)
        assert manager.get_version(session_id=SID) == "2"

    @pytest.mark.asyncio()
    async def test_returns_empty_on_exception(self, manager: PrunedManager, now: datetime) -> None:
        """get_version returns '' when _get_storage raises."""
        with patch.object(manager, "_get_storage", side_effect=OSError("disk error")):
            assert manager.get_version(session_id=SID) == ""
