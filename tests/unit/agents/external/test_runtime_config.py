"""Unit tests for the AGENTS.md marker-block writer (:mod:`runtime_config`)."""

from __future__ import annotations

from pathlib import Path

from modex_agent.agents.external.runtime_config import (
    BEGIN_MARKER,
    END_MARKER,
    default_runtime_block,
    read_runtime_block,
    write_runtime_block,
)


class TestWriteRuntimeBlockCreate:
    def test_creates_file_when_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        write_runtime_block(target)
        assert target.exists()

    def test_new_file_contains_both_markers(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        write_runtime_block(target)
        text = target.read_text(encoding="utf-8")
        assert BEGIN_MARKER in text
        assert END_MARKER in text

    def test_new_file_contains_default_body(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        write_runtime_block(target)
        text = target.read_text(encoding="utf-8")
        assert "modexctl send" in text


class TestWriteRuntimeBlockIdempotent:
    def test_rewrite_replaces_only_block_content(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        write_runtime_block(target)
        # Rewrite with custom content.
        write_runtime_block(target, content="CUSTOM BODY v2")
        text = target.read_text(encoding="utf-8")
        assert "CUSTOM BODY v2" in text
        assert "CUSTOM BODY v2" in (read_runtime_block(target) or "")

    def test_rewrite_preserves_user_content_outside_block(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        target.write_text("# My Project\n\nUser notes here.\n", encoding="utf-8")
        write_runtime_block(target)
        text = target.read_text(encoding="utf-8")
        assert "# My Project" in text
        assert "User notes here." in text
        assert BEGIN_MARKER in text

    def test_rewrite_after_user_edit_keeps_user_text(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        write_runtime_block(target)
        # Simulate a user adding content above the block.
        text = target.read_text(encoding="utf-8")
        target.write_text("# Project Notes\n\n" + text, encoding="utf-8")
        # Rewrite the block.
        write_runtime_block(target, content="REFRESHED")
        final = target.read_text(encoding="utf-8")
        assert "# Project Notes" in final
        assert "REFRESHED" in final
        # Only one begin marker remains.
        assert final.count(BEGIN_MARKER) == 1
        assert final.count(END_MARKER) == 1

    def test_double_write_is_stable(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        write_runtime_block(target, content="STABLE")
        first = target.read_text(encoding="utf-8")
        write_runtime_block(target, content="STABLE")
        second = target.read_text(encoding="utf-8")
        assert first == second


class TestWriteRuntimeBlockCustomContent:
    def test_custom_content_used(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        write_runtime_block(target, content="HELLO CUSTOM")
        body = read_runtime_block(target)
        assert body is not None
        assert "HELLO CUSTOM" in body

    def test_multiline_custom_content_preserved(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        multi = "line one\nline two\nline three"
        write_runtime_block(target, content=multi)
        body = read_runtime_block(target)
        assert body is not None
        assert "line one" in body
        assert "line three" in body


class TestReadRuntimeBlock:
    def test_returns_none_when_file_absent(self, tmp_path: Path) -> None:
        assert read_runtime_block(tmp_path / "nope.md") is None

    def test_returns_none_when_no_block(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        target.write_text("just user notes, no markers", encoding="utf-8")
        assert read_runtime_block(target) is None

    def test_returns_body_between_markers(self, tmp_path: Path) -> None:
        target = tmp_path / "AGENTS.md"
        write_runtime_block(target, content="THE BODY")
        assert read_runtime_block(target) == "THE BODY"


class TestDefaultRuntimeBlock:
    def test_mentions_modexbot_send(self) -> None:
        assert "modexctl send" in default_runtime_block()

    def test_mentions_modex_protection(self) -> None:
        block = default_runtime_block()
        assert ".modex" in block
        assert ".modex/external" not in block

    def test_mentions_modexctl_agents(self) -> None:
        assert "modexctl agents" in default_runtime_block()
