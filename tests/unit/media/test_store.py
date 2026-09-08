"""Tests for media.store — LocalFileMediaStore save/read/delete/budget.

Covers the streaming-save behavior (no whole-file buffering), path-escape
rejection for session_id/attachment_id, and the oldest-by-mtime budget
eviction including subagent-session isolation.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from modex_agent.core.media import (
    MediaRefCollisionError,
    StoredFile,
    StoredMediaKind,
)
from modex_agent.media.store import LocalFileMediaStore

_MB: int = 1024 * 1024


def _store(tmp_path: Path) -> LocalFileMediaStore:
    media_dir = tmp_path / "media" / "main"
    media_dir.mkdir(parents=True)
    return LocalFileMediaStore(media_dir)


# ── save / read round-trip ──────────────────────────────────────────────────


class TestSaveReadRoundTrip:
    def test_save_bytes_then_read_returns_path(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        path = store.save("conv.main", "att-1", b"hello bytes")
        assert path.is_file()
        assert path.read_bytes() == b"hello bytes"
        assert store.read("conv.main", "att-1") == path

    def test_save_stream_then_read_returns_path(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        stream = io.BytesIO(b"stream payload")
        path = store.save("conv.main", "att-1", stream)
        assert path.read_bytes() == b"stream payload"
        assert store.read("conv.main", "att-1") == path

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.read("conv.main", "nope") is None

    def test_read_missing_when_session_dir_absent_returns_none(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        # No save ever happened → session dir does not exist.
        assert store.read("never.seen", "att-x") is None

    def test_save_overwrites_existing_attachment(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "att-1", b"v1")
        store.save("conv.main", "att-1", b"v2-replacement")
        assert store.read("conv.main", "att-1").read_bytes() == b"v2-replacement"

    def test_distinct_sessions_get_distinct_dirs(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "att-1", b"main-body")
        store.save("conv.reviewer", "att-1", b"reviewer-body")
        assert store.read("conv.main", "att-1").read_bytes() == b"main-body"
        assert store.read("conv.reviewer", "att-1").read_bytes() == b"reviewer-body"

    def test_no_part_file_left_after_successful_save(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "att-1", b"body")
        session_dir = store.media_dir / "uploads" / "conv.main"
        parts = list(session_dir.glob("*.part"))
        assert parts == []

    def test_partial_save_on_failure_is_cleaned(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        class BoomStream:
            def read(self, n: int = -1) -> bytes:
                raise OSError("disk full mid-write")

        with pytest.raises(OSError):
            store.save("conv.main", "att-1", BoomStream())
        # No live file, no .part leftover.
        assert store.read("conv.main", "att-1") is None
        session_dir = store.media_dir / "uploads" / "conv.main"
        assert list(session_dir.glob("*.part")) == []


class TestStoredMediaKinds:
    def test_reads_kind_round_trips_bytes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        path = store.save(
            "conv-main",
            "att-1",
            b"read snapshot",
            kind=StoredMediaKind.READS,
        )

        assert path == store.media_dir / "reads" / "conv-main" / "att-1"
        assert store.read(
            "conv-main",
            "att-1",
            kind=StoredMediaKind.READS,
        ) == path
        assert store.read_bytes(
            "conv-main",
            "att-1",
            kind=StoredMediaKind.READS,
        ) == b"read snapshot"

    def test_resolve_bytes_finds_upload(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save("conv-main", "upload-1", b"uploaded")

        resolved = store.resolve_bytes("conv-main", "upload-1")

        assert resolved == b"uploaded"

    def test_resolve_bytes_finds_read_snapshot(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save(
            "conv-main",
            "read-1",
            b"snapshot",
            kind=StoredMediaKind.READS,
        )

        resolved = store.resolve_bytes("conv-main", "read-1")

        assert resolved == b"snapshot"

    def test_resolve_bytes_missing_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        resolved = store.resolve_bytes("conv-main", "missing")

        assert resolved is None

    def test_read_bytes_missing_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)

        resolved = store.read_bytes(
            "conv-main",
            "missing",
            kind=StoredMediaKind.READS,
        )

        assert resolved is None

    def test_explicit_kind_reads_are_cross_kind_invisible(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save("conv-main", "upload-1", b"uploaded")
        store.save(
            "conv-main",
            "read-1",
            b"snapshot",
            kind=StoredMediaKind.READS,
        )

        upload_as_read = store.read_bytes(
            "conv-main",
            "upload-1",
            kind=StoredMediaKind.READS,
        )
        read_as_upload = store.read_bytes("conv-main", "read-1")

        assert upload_as_read is None
        assert read_as_upload is None

    def test_resolve_bytes_raises_typed_error_on_kind_collision(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.save("conv-main", "same-id", b"uploaded")
        store.save(
            "conv-main",
            "same-id",
            b"snapshot",
            kind=StoredMediaKind.READS,
        )

        with pytest.raises(MediaRefCollisionError) as raised:
            store.resolve_bytes("conv-main", "same-id")

        assert raised.value.session_id == "conv-main"
        assert raised.value.attachment_id == "same-id"

    def test_delete_reads_removes_only_reads_entry(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save("conv-main", "same-id", b"uploaded")
        store.save(
            "conv-main",
            "same-id",
            b"snapshot",
            kind=StoredMediaKind.READS,
        )

        removed = store.delete(
            "conv-main",
            "same-id",
            kind=StoredMediaKind.READS,
        )

        assert removed is True
        assert store.read("conv-main", "same-id") is not None
        assert store.read(
            "conv-main",
            "same-id",
            kind=StoredMediaKind.READS,
        ) is None

    def test_list_and_budget_ignore_reads(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save("conv-main", "upload-1", b"uploaded")
        read_path = store.save(
            "conv-main",
            "read-1",
            b"snapshot",
            kind=StoredMediaKind.READS,
        )

        listed = store.list_session("conv-main")
        evicted = store.enforce_budget("conv-main", 0)

        assert [entry.attachment_id for entry in listed] == ["upload-1"]
        assert [path.name for path in evicted] == ["upload-1"]
        assert read_path.is_file()


# ── streaming: save must NOT buffer the whole file in memory ─────────────────


class _ChunkRecordingStream:
    """Binary stream that yields a fixed total payload in bounded chunks and
    records the size of every ``read`` the store requested.

    If the store buffered the whole file, it would issue a single ``read`` of
    the full size (BytesIO's default ``read(-1)``). A streaming store pulls in
    bounded chunks. Asserting ``max(read_sizes)`` is bounded is therefore a
    real-behavior test of streaming — no monkeypatching of internals.
    """

    def __init__(self, total_bytes: int, chunk: int) -> None:
        self._remaining = total_bytes
        self._chunk = chunk
        self.read_sizes: list[int] = []

    def read(self, n: int = -1) -> bytes:
        # Emulate BytesIO semantics: ``read(-1)`` means "give me everything".
        # The store must NOT ask for everything on a stream of unknown size;
        # it must pass a bounded length. If it passes -1, we still only return
        # one chunk's worth so the test fails on the recorded call size.
        if n is None or n < 0:
            n = self._remaining  # would return everything; record the request
        take = min(n, self._remaining, self._chunk)
        self.read_sizes.append(n)
        self._remaining -= take
        return b"x" * take


class TestStreamingSave:
    def test_save_pulls_stream_in_bounded_chunks_not_one_read(
        self, tmp_path: Path
    ) -> None:
        """A large stream must be drained via many bounded-size reads, never a
        single ``read(-1)`` that would pull the whole payload into memory."""
        store = _store(tmp_path)
        total = 1024 * 1024  # 1 MB
        stream = _ChunkRecordingStream(total, chunk=32 * 1024)

        store.save("conv.main", "att-1", stream)

        # Many reads happened (chunked), and no single read covered the whole
        # payload — which is what would happen if the store buffered it whole.
        assert len(stream.read_sizes) > 1, (
            "stream save must drain in multiple bounded reads, not one"
        )
        assert max(stream.read_sizes) <= 64 * 1024, (
            f"store must request ≤ chunk size per read; "
            f"max requested {max(stream.read_sizes)}"
        )
        # Sanity: the full payload landed.
        assert store.read("conv.main", "att-1").stat().st_size == total

    def test_save_bytes_is_fast_path(self, tmp_path: Path) -> None:
        """A ``bytes`` payload is written directly (already in memory); this is
        the documented fast path, distinct from the streamed case."""
        store = _store(tmp_path)
        payload = b"y" * 200_000
        path = store.save("conv.main", "att-1", payload)
        assert path.read_bytes() == payload


# ── delete ───────────────────────────────────────────────────────────────────


class TestDelete:
    def test_delete_existing_returns_true(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "att-1", b"body")
        assert store.delete("conv.main", "att-1") is True
        assert store.read("conv.main", "att-1") is None

    def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.delete("conv.main", "nope") is False

    def test_delete_last_file_drops_empty_session_dir(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "att-1", b"body")
        store.delete("conv.main", "att-1")
        session_dir = store.media_dir / "uploads" / "conv.main"
        assert not session_dir.exists()

    def test_delete_keeps_dir_when_other_attachments_remain(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "att-1", b"body-1")
        store.save("conv.main", "att-2", b"body-2")
        store.delete("conv.main", "att-1")
        assert store.read("conv.main", "att-2") is not None


# ── list_session ─────────────────────────────────────────────────────────────


class TestListSession:
    def test_list_empty_when_nothing_saved(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.list_session("conv.main") == []

    def test_list_returns_all_entries_sorted_by_id(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "att-3", b"ccc")
        store.save("conv.main", "att-1", b"a")
        store.save("conv.main", "att-2", b"bb")
        entries = store.list_session("conv.main")
        assert [e.attachment_id for e in entries] == ["att-1", "att-2", "att-3"]
        assert all(isinstance(e, StoredFile) for e in entries)
        assert entries[0].size == 1
        assert entries[1].size == 2
        assert entries[2].size == 3

    def test_list_only_includes_target_session(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "att-1", b"main")
        store.save("conv.other", "att-2", b"other")
        entries = store.list_session("conv.main")
        assert [e.attachment_id for e in entries] == ["att-1"]


# ── path-escape rejection ────────────────────────────────────────────────────


class TestPathEscape:
    def test_session_id_dotdot_is_neutralized(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        # ".." survives safe_segment only as "" → "_"; the file must land
        # inside uploads, never above media_dir.
        path = store.save("../escape", "att-1", b"body")
        assert path.is_relative_to(store.media_dir)
        assert path.read_bytes() == b"body"

    def test_attachment_id_dotdot_cannot_escape(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        path = store.save("conv.main", "../../etc/passwd", b"body")
        # Must remain under media_dir/uploads/conv.main/.
        assert path.is_relative_to(store.media_dir)
        # And nothing landed outside media_dir.
        assert not (tmp_path / "etc").exists()

    def test_path_traversal_in_attachment_id_lands_inside(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "..%2f..%2fsecret", b"body")
        # Whatever the sanitized leaf name is, it has no path separator and the
        # file is reachable only via the store API (one leaf, not a nested tree).
        listing = store.list_session("conv.main")
        assert len(listing) == 1
        leaf = listing[0]
        assert "/" not in leaf.attachment_id
        assert "\\" not in leaf.attachment_id
        assert leaf.path.is_relative_to(store.media_dir)
        # And reading it round-trips via the store (not via a raw escaped path).
        assert store.read("conv.main", leaf.attachment_id).read_bytes() == b"body"


# ── enforce_budget: oldest-by-mtime eviction ─────────────────────────────────


def _save_at_mtime(
    store: LocalFileMediaStore,
    session_id: str,
    attachment_id: str,
    body: bytes,
    mtime: float,
) -> Path:
    """Persist via the store's public API then pin mtime.

    Writes go through ``save`` (the real path the store uses, sanitized
    consistently for both write and later read/enforce), then ``os.utime``
    pins the ordering. This is a real-behavior seed, not a raw-path bypass.
    """
    path = store.save(session_id, attachment_id, body)
    os.utime(path, (mtime, mtime))
    return path


class TestEnforceBudget:
    def test_under_budget_evicts_nothing(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save("conv.main", "att-1", b"x" * 100)
        evicted = store.enforce_budget("conv.main", 1000)
        assert evicted == []
        assert store.read("conv.main", "att-1") is not None

    def test_over_budget_evicts_oldest_until_under(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        # Seed: 490 MB across two older files, then a fresh 20 MB upload.
        # mtime: att-old earliest, att-mid next, att-new latest.
        _save_at_mtime(store, "conv.main", "att-old", b"o" * (300 * _MB), 1000.0)
        _save_at_mtime(store, "conv.main", "att-mid", b"m" * (190 * _MB), 2000.0)
        _save_at_mtime(store, "conv.main", "att-new", b"n" * (20 * _MB), 3000.0)

        # Total 510 MB; budget 500 MB → must evict att-old (300 MB), bringing
        # the total to 210 MB (under budget). One eviction suffices.
        evicted = store.enforce_budget("conv.main", 500 * _MB)
        evicted_names = [p.name for p in evicted]
        assert "att-old" in evicted_names
        assert store.read("conv.main", "att-old") is None
        # The newer two survive.
        assert store.read("conv.main", "att-mid") is not None
        assert store.read("conv.main", "att-new") is not None
        # Remaining total is within budget.
        remaining = sum(e.size for e in store.list_session("conv.main"))
        assert remaining <= 500 * _MB

    def test_eviction_requires_multiple_files_when_one_is_small(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        # Three small old files + one large new file, all over budget together.
        _save_at_mtime(store, "conv.main", "a", b"x" * 100, 1000.0)
        _save_at_mtime(store, "conv.main", "b", b"x" * 100, 2000.0)
        _save_at_mtime(store, "conv.main", "c", b"x" * 100, 3000.0)
        _save_at_mtime(store, "conv.main", "big", b"x" * 400, 4000.0)
        # Total 700; budget 250. big alone (400) > 250, so it must be evicted
        # too once the small ones are gone. Eviction order: a, b, c, big.
        evicted = store.enforce_budget("conv.main", 250)
        names = [p.name for p in evicted]
        assert names == ["a", "b", "c", "big"]
        # Everything gone because even the smallest single remaining (big, 400)
        # exceeds budget — total now 0 ≤ 250.
        assert store.list_session("conv.main") == []

    def test_subagent_session_isolated_from_main_eviction(
        self, tmp_path: Path
    ) -> None:
        """Evicting under {conv}.main must NOT touch {conv}.reviewer.x — they
        are different session_id keys (ADR-0013 §7: budget key is the main
        session id)."""
        store = _store(tmp_path)
        # Main session: huge, over a tiny budget.
        _save_at_mtime(store, "conv.main", "big-main", b"x" * 1000, 1000.0)
        # Reviewer subagent session: a file that must be untouched. It is older
        # than the main file but a different session key, so it is invisible to
        # an enforce_budget call keyed on conv.main.
        _save_at_mtime(store, "conv.reviewer.x", "rev-att", b"r" * 500, 500.0)

        evicted = store.enforce_budget("conv.main", 100)
        # Only the main file is evicted.
        assert [p.name for p in evicted] == ["big-main"]
        assert store.read("conv.main", "big-main") is None
        # Reviewer session intact.
        assert store.read("conv.reviewer.x", "rev-att") is not None
        assert store.read("conv.reviewer.x", "rev-att").read_bytes() == b"r" * 500

    def test_enforce_budget_empty_session_returns_empty(
        self, tmp_path: Path
    ) -> None:
        store = _store(tmp_path)
        assert store.enforce_budget("never.saved", 500 * _MB) == []

    def test_eviction_drops_empty_session_dir(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _save_at_mtime(store, "conv.main", "only", b"x" * 100, 1000.0)
        store.enforce_budget("conv.main", 0)
        session_dir = store.media_dir / "uploads" / "conv_main"
        assert not session_dir.exists()
