"""End-to-end overflow pipeline tests.

Covers: store (sync generation), path sanitisation, prefix correctness,
cleaner async cleanup, merge behaviour, max-count enforcement, and the
interceptor-level flow through ToolResultOverflowHandler.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.tools.overflow.cleaner import OverflowCleaner
from modex_agent.tools.overflow.handler import ToolResultOverflowHandler
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore

# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path: Path) -> LocalFileToolOverflowStore:
    return LocalFileToolOverflowStore(workspace=tmp_path)


@pytest.fixture
def cleaner(store: LocalFileToolOverflowStore) -> OverflowCleaner:
    return OverflowCleaner(store, merge_window=0.01)


@pytest.fixture
def handler(store: LocalFileToolOverflowStore, cleaner: OverflowCleaner) -> ToolResultOverflowHandler:
    return ToolResultOverflowHandler(store=store, cleaner=cleaner)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Store — synchronous generation
# ═══════════════════════════════════════════════════════════════════════════════

class TestStoreSyncGeneration:
    @pytest.mark.asyncio
    async def test_store_creates_directory_layout(self, store: LocalFileToolOverflowStore) -> None:
        """store() creates metadata and one complete output file."""
        await store.initialize()
        ref = await store.store(
            session_id="30932BC02F825E64D069B1E67347C8FF:main",
            tool_call_id="call_function_yq3r3mt0hx3g_1",
            tool_name="read_file",
            content="hello world!" * 200,
        )

        entry_dir = Path(ref.dir_path)
        assert entry_dir.exists(), f"dir should exist: {entry_dir}"
        assert entry_dir.name == "call_function_yq3r3mt0hx3g_1"

        assert (entry_dir / ".meta.json").exists()
        assert {path.name for path in entry_dir.iterdir()} == {".meta.json", "full.txt"}
        assert (entry_dir / "full.txt").read_text(encoding="utf-8") == "hello world!" * 200

        meta = await store.read_metadata(
            "30932BC02F825E64D069B1E67347C8FF:main",
            "call_function_yq3r3mt0hx3g_1",
        )
        assert meta is not None
        assert meta.tool_name == "read_file"
        assert meta.tool_call_id == "call_function_yq3r3mt0hx3g_1"
        assert meta.total_chars == 2400

    @pytest.mark.asyncio
    async def test_store_sanitizes_session_id_colon(self, store: LocalFileToolOverflowStore) -> None:
        """Session ID 'hash:role' has ':' replaced with '_' in the directory."""
        ref = await store.store(
            session_id="abc123:main",
            tool_call_id="call_0",
            tool_name="search",
            content="x" * 100,
        )
        dir_path = Path(ref.dir_path)
        assert "abc123_main" in str(dir_path)
        assert "abc123:main" not in str(dir_path)

    @pytest.mark.asyncio
    async def test_store_files_exist_immediately_after_await(self, store: LocalFileToolOverflowStore) -> None:
        """After await store(), files are on disk — no deferred I/O."""
        ref = await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="immediate" * 30,
        )
        entry_dir = Path(ref.dir_path)
        assert entry_dir.exists()
        assert (entry_dir / ".meta.json").exists()
        assert (entry_dir / "full.txt").read_text(encoding="utf-8") == "immediate" * 30


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Handler — end-to-end through ToolResultOverflowHandler
# ═══════════════════════════════════════════════════════════════════════════════

class TestHandlerEndToEnd:
    @pytest.mark.asyncio
    async def test_handler_returns_truncated_text_with_file_notice(
        self, handler: ToolResultOverflowHandler,
    ) -> None:
        # default max_chars=50_000 → head 5_000 / tail 7_500 (10%/15%), 47_505 elided
        content = "H" * 20_000 + "M" * 10_005 + "T" * 30_000
        notice, ref = await handler.store_overflow(
            session_id="sid_main",
            tool_call_id="call_handler_1",
            tool_name="read_file",
            content=content,
        )

        lines = notice.split("\n")
        assert len(lines) == 4
        assert lines[0] == "H" * 5_000
        assert lines[2] == "T" * 7_500
        assert "OUTPUT ELIDED: 47505 chars" in lines[1]
        assert lines[3].startswith(
            f"[Full output ({len(content)} chars total) saved to: {ref.dir_path}/full.txt"
        )
        assert not notice.startswith("<")

    @pytest.mark.asyncio
    async def test_handler_writes_full_content_to_disk(
        self, handler: ToolResultOverflowHandler,
    ) -> None:
        """The complete result is written to one file."""
        content = "FULL-" + ("Y" * 2500)
        _, ref = await handler.store_overflow(
            session_id="sid_full",
            tool_call_id="call_full",
            tool_name="bash",
            content=content,
        )

        entry_dir = Path(ref.dir_path)
        assert {path.name for path in entry_dir.iterdir()} == {".meta.json", "full.txt"}
        assert (entry_dir / "full.txt").read_text(encoding="utf-8") == content

    @pytest.mark.asyncio
    async def test_handler_marks_notice_with_saved_output_path(
        self, handler: ToolResultOverflowHandler,
    ) -> None:
        # exactly at the 50_000 budget → nothing elided, text unchanged
        content = "Z" * 50_000
        notice, ref = await handler.store_overflow(
            session_id="sid_short",
            tool_call_id="call_short",
            tool_name="search",
            content=content,
        )
        assert notice == content
        assert ref.total_chars == 50000

    @pytest.mark.asyncio
    async def test_handler_over_budget_notice_points_to_saved_output_path(
        self, handler: ToolResultOverflowHandler,
    ) -> None:
        content = "Z" * 60_000
        notice, ref = await handler.store_overflow(
            session_id="sid_over",
            tool_call_id="call_over",
            tool_name="search",
            content=content,
        )
        assert notice.startswith("Z" * 5_000)
        assert f"saved to: {ref.dir_path}/full.txt" in notice
        assert ref.total_chars == 60000


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Cleaner — fire-and-forget with merge
# ═══════════════════════════════════════════════════════════════════════════════

class TestCleanerRemovesStaleEntries:
    @pytest.mark.asyncio
    async def test_cleaner_removes_entries_not_in_kept(
        self, store: LocalFileToolOverflowStore, cleaner: OverflowCleaner,
    ) -> None:
        """Entries whose tool_call_id is NOT in kept_call_ids are deleted."""
        for i in range(4):
            await store.store("s_1", f"call_{i}", "t", f"c{i}" * 20)

        cleaner.schedule_cleanup("s_1", {"call_1", "call_3"})
        await cleaner.flush()

        remaining = await store.list_tool_call_ids("s_1")
        assert remaining == ["call_1", "call_3"]

    @pytest.mark.asyncio
    async def test_cleaner_enforces_max_count(
        self, store: LocalFileToolOverflowStore, cleaner: OverflowCleaner,
    ) -> None:
        """When entries exceed max_tool_call_ids, oldest are removed."""
        for i in range(8):
            await store.store("s_2", f"call_{i}", "t", f"c{i}" * 5)

        all_ids = await store.list_tool_call_ids("s_2")
        cleaner.schedule_cleanup("s_2", set(all_ids), max_tool_call_ids=3)
        await cleaner.flush()

        remaining = await store.list_tool_call_ids("s_2")
        assert remaining == ["call_5", "call_6", "call_7"]

    @pytest.mark.asyncio
    async def test_cleaner_merge_same_session_requests(
        self, store: LocalFileToolOverflowStore, cleaner: OverflowCleaner,
    ) -> None:
        """Two rapid cleanup schedules for the same session merge kept_call_ids."""
        for i in range(4):
            await store.store("s_3", f"call_{i}", "t", f"c{i}" * 5)

        cleaner.schedule_cleanup("s_3", {"call_0"})
        cleaner.schedule_cleanup("s_3", {"call_2"})
        await cleaner.flush()

        remaining = await store.list_tool_call_ids("s_3")
        assert remaining == ["call_0", "call_2"]

    @pytest.mark.asyncio
    async def test_cleaner_keeps_all_when_all_in_kept(
        self, store: LocalFileToolOverflowStore, cleaner: OverflowCleaner,
    ) -> None:
        """When all disk entries are in kept_call_ids, nothing is deleted."""
        for i in range(3):
            await store.store("s_4", f"call_{i}", "t", f"c{i}" * 5)

        all_ids = set(await store.list_tool_call_ids("s_4"))
        cleaner.schedule_cleanup("s_4", all_ids)
        await cleaner.flush()

        assert len(await store.list_tool_call_ids("s_4")) == 3

    @pytest.mark.asyncio
    async def test_cleaner_handles_empty_disk(
        self, store: LocalFileToolOverflowStore, cleaner: OverflowCleaner,
    ) -> None:
        """Cleaner does not crash when there are no overflow entries."""
        cleaner.schedule_cleanup("empty_session", {"call_x"})
        await cleaner.flush()
        assert await store.list_tool_call_ids("empty_session") == []

    @pytest.mark.asyncio
    async def test_cleaner_removes_all_when_kept_empty(
        self, store: LocalFileToolOverflowStore, cleaner: OverflowCleaner,
    ) -> None:
        """When kept_call_ids is empty, all entries for that session are deleted."""
        for i in range(3):
            await store.store("s_5", f"call_{i}", "t", f"c{i}" * 5)

        cleaner.schedule_cleanup("s_5", set())
        await cleaner.flush()

        assert await store.list_tool_call_ids("s_5") == []


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Cleaner — interceptor integration (simulates real flow)
# ═══════════════════════════════════════════════════════════════════════════════

class TestCleanerInterceptorIntegration:
    """Simulates the actual interceptor flow:

    1. Tool result arrives → overflow → store on disk
    2. _gather_kept_call_ids collects tool_call_ids from session history
    3. schedule_cleanup(session_id, kept_call_ids)
    4. Cleaner removes entries on disk NOT in kept_call_ids
    """

    @pytest.mark.asyncio
    async def test_simulated_interceptor_flow(
        self, store: LocalFileToolOverflowStore, cleaner: OverflowCleaner,
    ) -> None:
        # Step 1: Simulate 5 tool calls, each overflowing
        session_id = "abc:main"
        all_call_ids = []
        for i in range(5):
            cid = f"call_func_{i:04d}"
            all_call_ids.append(cid)
            await store.store(session_id, cid, "search", f"result_{i}_" * 200)

        assert len(await store.list_tool_call_ids(session_id)) == 5

        # Step 2: Simulate compression — only last 2 calls remain in session history
        kept_in_history = set(all_call_ids[-2:])  # call_func_0003, call_func_0004

        # Step 3: Schedule cleanup with kept_call_ids from history
        cleaner.schedule_cleanup(session_id, kept_in_history)
        await cleaner.flush()

        # Step 4: Old entries removed
        remaining = await store.list_tool_call_ids(session_id)
        assert remaining == ["call_func_0003", "call_func_0004"]

    @pytest.mark.asyncio
    async def test_simulated_overflow_then_compaction_cleanup(
        self, store: LocalFileToolOverflowStore, cleaner: OverflowCleaner,
    ) -> None:
        """After session compression drops old tool messages, overflow entries
        for those dropped tool_call_ids are cleaned."""
        sid = "compaction_sess"

        # 10 tool calls stored
        for i in range(10):
            await store.store(sid, f"tc_{i:02d}", "read_file", "x" * 1500)

        assert len(await store.list_tool_call_ids(sid)) == 10

        # Session compression keeps only recent 4 tool messages
        kept = {f"tc_{i:02d}" for i in range(6, 10)}

        cleaner.schedule_cleanup(sid, kept)
        await cleaner.flush()

        remaining = await store.list_tool_call_ids(sid)
        assert remaining == ["tc_06", "tc_07", "tc_08", "tc_09"]

    @pytest.mark.asyncio
    async def test_cleaner_respects_both_rules_simultaneously(
        self, store: LocalFileToolOverflowStore, cleaner: OverflowCleaner,
    ) -> None:
        """max_tool_call_ids=2 AND kept_call_ids filters both apply.
        Start with 6 entries, keep 3 explicitly, max=2 → newest 2 among kept 3 survive.
        """
        sid = "dual_rule"
        for i in range(6):
            await store.store(sid, f"call_{i}", "t", f"c{i}" * 5)

        # Keep only 0,2,4 in history, but max=2
        cleaner.schedule_cleanup(sid, {"call_0", "call_2", "call_4"}, max_tool_call_ids=2)
        await cleaner.flush()

        remaining = await store.list_tool_call_ids(sid)
        assert remaining == ["call_2", "call_4"]
