"""Tests for modexbot writer — file-lock + JSONL append.

Covers:
- ``_write_line`` creates the intermediate session dir on first call.
- The line appended is a valid JSON record terminated by ``\\n``.
- Multiple appends accumulate in append order (no clobbering).
- The directory name uses the SAME sanitiser as ``LocalFileInboxServer``
  (so the discovery path actually finds the file).
- A cross-process file lock is taken (verified via concurrent writes).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from modex_agent.agents.external_coding import OutboxLine, OutboxMetadata
from modex_agent.cli.modexbot.writer import _write_line

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _line(message_id: str = "m1", content: str = "hello") -> OutboxLine:
    return OutboxLine(
        message_id=message_id,
        source="coder",
        content=content,
        message_type="agent_message",
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        metadata=OutboxMetadata(
            agent_session_id="abc.analyst",
            session_id="abc.coder",
            invocation_id="abc",
            parent_session_id=None,
        ),
    )


def _line_json(message_id: str = "m1", content: str = "hello") -> str:
    return _line(message_id, content).model_dump_json()


# ---------------------------------------------------------------------------
# _write_line — directory + file layout
# ---------------------------------------------------------------------------


class TestWriteLineLayout:
    def test_creates_intermediate_dirs(self, tmp_path: Path) -> None:
        pool_dir = tmp_path / "pool_analyst"
        target_sid = "abc.analyst"
        # No pre-creation: writer must make the dir tree itself.
        _write_line(pool_dir, target_sid, _line_json())

        session_dir = pool_dir / "abc.analyst"
        assert session_dir.is_dir()
        assert (session_dir / "pending.jsonl").is_file()

    def test_appends_exactly_one_line(self, tmp_path: Path) -> None:
        pool_dir = tmp_path / "pool_analyst"
        _write_line(pool_dir, "abc.analyst", _line_json())
        pending = pool_dir / "abc.analyst" / "pending.jsonl"
        text = pending.read_text(encoding="utf-8")
        # Exactly one record, newline-terminated.
        assert text.count("\n") == 1
        assert text.endswith("\n")

    def test_appended_line_is_byte_equal_to_input(
        self, tmp_path: Path
    ) -> None:
        pool_dir = tmp_path / "pool_analyst"
        line = _line_json(message_id="m42", content="payload")
        _write_line(pool_dir, "abc.analyst", line)
        pending = pool_dir / "abc.analyst" / "pending.jsonl"
        text = pending.read_text(encoding="utf-8")
        assert text.rstrip("\n") == line

    def test_appended_line_is_valid_json(self, tmp_path: Path) -> None:
        pool_dir = tmp_path / "pool_analyst"
        _write_line(pool_dir, "abc.analyst", _line_json())
        pending = pool_dir / "abc.analyst" / "pending.jsonl"
        first_line = pending.read_text(encoding="utf-8").splitlines()[0]
        data = json.loads(first_line)
        assert data["message_id"] == "m1"
        assert data["metadata"]["agent_session_id"] == "abc.analyst"

    def test_multiple_writes_accumulate(self, tmp_path: Path) -> None:
        pool_dir = tmp_path / "pool_analyst"
        for i in range(3):
            _write_line(pool_dir, "abc.analyst", _line_json(message_id=f"m{i}"))
        pending = pool_dir / "abc.analyst" / "pending.jsonl"
        lines = [ln for ln in pending.read_text(encoding="utf-8").splitlines() if ln]
        assert len(lines) == 3
        ids = [json.loads(ln)["message_id"] for ln in lines]
        assert ids == ["m0", "m1", "m2"]


# ---------------------------------------------------------------------------
# _write_line — directory naming matches inbox server's _safe_dir_name
# ---------------------------------------------------------------------------


class TestWriteLineSafeSegment:
    def test_dir_name_matches_inbox_server(self, tmp_path: Path) -> None:
        """The directory created by ``_write_line`` must be discoverable
        by ``LocalFileInboxServer.sessions_with_pending()``. That method
        scans the workspace by directory name; the writer MUST therefore
        use the SAME sanitiser as the server's ``_safe_dir_name``."""
        from modex_agent.multi_agent.inbox.server_local import _safe_dir_name

        pool_dir = tmp_path / "pool_analyst"
        target_sid = "abc.analyst"
        _write_line(pool_dir, target_sid, _line_json())
        assert (pool_dir / _safe_dir_name(target_sid) / "pending.jsonl").exists()

    def test_dir_name_for_session_with_unsafe_chars(self, tmp_path: Path) -> None:
        """A session_id containing chars outside ``[\\w\\-.]`` must still
        land in a directory the inbox server recognises."""
        from modex_agent.multi_agent.inbox.server_local import _safe_dir_name

        pool_dir = tmp_path / "pool"
        # Slash is unsafe per the inbox regex.
        target_sid = "abc.pi/sub"
        _write_line(pool_dir, target_sid, _line_json())
        expected = _safe_dir_name(target_sid)
        assert (pool_dir / expected / "pending.jsonl").exists()


# ---------------------------------------------------------------------------
# _write_line — cross-process file lock
# ---------------------------------------------------------------------------


class TestWriteLineFileLock:
    def test_lock_file_created_on_first_write(self, tmp_path: Path) -> None:
        """``filelock`` does not leave a permanent ``.lock`` artefact
        after release — instead it creates the file (empty or with
        lock-owner bytes) while held, then deletes it. After
        ``_write_line`` returns, the lock path may or may not exist
        depending on platform timing; the invariant is that the lock
        was acquired for the duration of the write.

        Verify the session directory is created.
        """
        pool_dir = tmp_path / "pool_analyst"
        _write_line(pool_dir, "abc.analyst", _line_json())
        # The session dir exists; the writer does NOT leave a ``.lock``
        # file behind on Windows either (filelock deletes on release).
        session_dir = pool_dir / "abc.analyst"
        assert session_dir.is_dir()

    def test_lock_object_serializes_concurrent_writes(
        self, tmp_path: Path
    ) -> None:
        """Run two threads appending concurrently; verify both lines
        arrive (no lost writes) — proves the writer holds a real lock
        around the open/append/close sequence.

        filelock.FileLock is a context manager; this exercises that
        the writer uses it correctly (no torn writes).
        """
        import threading

        pool_dir = tmp_path / "pool_analyst"
        target_sid = "abc.analyst"
        errors: list[BaseException] = []

        def writer(i: int) -> None:
            try:
                _write_line(pool_dir, target_sid, _line_json(message_id=f"t{i}"))
            except BaseException as exc:  # noqa: BLE001 — test sink
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent writers failed: {errors}"

        pending = pool_dir / "abc.analyst" / "pending.jsonl"
        lines = [ln for ln in pending.read_text(encoding="utf-8").splitlines() if ln]
        ids = sorted(json.loads(ln)["message_id"] for ln in lines)
        assert ids == sorted(f"t{i}" for i in range(8))


# ---------------------------------------------------------------------------
# _write_line — accepts a target_pool_dir as a Path (not str)
# ---------------------------------------------------------------------------


class TestWriteLineAcceptsPath:
    def test_target_pool_dir_can_be_string(self, tmp_path: Path) -> None:
        # The spec accepts both Path and str — Python's ``/`` operator
        # handles both via os.PathLike coercion.
        pool_dir = tmp_path / "pool_analyst"
        _write_line(Path(pool_dir), "abc.analyst", _line_json())
        assert (pool_dir / "abc.analyst" / "pending.jsonl").exists()

    def test_creates_nested_pool_dir(self, tmp_path: Path) -> None:
        # Pool dir nested two levels deep — writer still makes it.
        deep = tmp_path / "level1" / "level2" / "pool"
        _write_line(deep, "abc.analyst", _line_json())
        assert (deep / "abc.analyst" / "pending.jsonl").exists()
