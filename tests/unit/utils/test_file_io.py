"""Tests for framework.utils.file_io — encoding-resilient JSON/JSONL readers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.utils.file_io import read_json_robust, read_jsonl_robust


# ── read_json_robust ──────────────────────────────────────────────────────

class TestReadJsonRobust:
    def test_utf8_normal(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text('{"key": "你好"}', encoding="utf-8")
        result = read_json_robust(path)
        assert result == {"key": "你好"}

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        result = read_json_robust(tmp_path / "missing.json")
        assert result is None

    def test_gbk_fallback(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text('{"name": "李四"}', encoding="gbk")
        # Verify not valid UTF-8
        with pytest.raises(UnicodeDecodeError):
            path.read_text(encoding="utf-8")
        result = read_json_robust(path)
        assert result == {"name": "李四"}

    def test_gb18030_fallback(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text('{"city": "北京"}', encoding="gb18030")
        result = read_json_robust(path)
        assert result == {"city": "北京"}

    def test_corrupted_backup_and_return_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_bytes(b"\x80\x81\x82\x83\xff\xfe")
        result = read_json_robust(path)
        assert result == {}
        # Backup should exist
        backup = tmp_path / "data.json.bak"
        assert backup.exists()

    def test_json_parse_error_after_decode(self, tmp_path: Path) -> None:
        """If decoded successfully but JSON is malformed, treat as corrupt."""
        path = tmp_path / "data.json"
        path.write_text("not valid json at all", encoding="utf-8")
        result = read_json_robust(path)
        assert result == {}
        assert (tmp_path / "data.json.bak").exists()

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text("", encoding="utf-8")
        result = read_json_robust(path)
        assert result == {}


# ── read_jsonl_robust ─────────────────────────────────────────────────────

class TestReadJsonlRobust:
    def test_utf8_normal(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_text(
            '{"role":"user","content":"你好"}\n'
            '{"role":"assistant","content":"你好！"}\n',
            encoding="utf-8",
        )
        result = read_jsonl_robust(path)
        assert len(result) == 2
        assert result[0]["content"] == "你好"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = read_jsonl_robust(tmp_path / "missing.jsonl")
        assert result == []

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        result = read_jsonl_robust(path)
        assert result == []

    def test_gbk_fallback(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_text(
            '{"role":"user","content":"你是谁"}\n',
            encoding="gbk",
        )
        with pytest.raises(UnicodeDecodeError):
            path.read_text(encoding="utf-8")
        result = read_jsonl_robust(path)
        assert len(result) == 1
        assert result[0]["content"] == "你是谁"

    def test_corrupted_backup_and_return_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_bytes(b"\x80\x81\x82\x83\xff\xfe\xfd\xfc")
        result = read_jsonl_robust(path)
        assert result == []
        backup = tmp_path / "log.jsonl.bak"
        assert backup.exists()
        assert not path.exists()  # original removed after backup

    def test_skip_invalid_json_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_text(
            '{"valid":1}\n'
            'not valid json\n'
            '{"valid":2}\n',
            encoding="utf-8",
        )
        result = read_jsonl_robust(path)
        assert len(result) == 2
        assert result[0] == {"valid": 1}
        assert result[1] == {"valid": 2}

    def test_all_lines_invalid_triggers_backup(self, tmp_path: Path) -> None:
        """If every line fails JSON parse, backup the file."""
        path = tmp_path / "log.jsonl"
        path.write_text("garbage line 1\ngarbage line 2\n", encoding="utf-8")
        result = read_jsonl_robust(path)
        assert result == []
        assert (tmp_path / "log.jsonl.bak").exists()
