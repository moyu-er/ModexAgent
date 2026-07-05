from __future__ import annotations

from bot.adapters.telegram import (
    TelegramMediaKind,
    classify_media,
    markdown_to_html,
    split_text,
    telegram_enabled,
)


def test_telegram_enabled_requires_token() -> None:
    assert telegram_enabled({}) is False
    assert telegram_enabled({"token": ""}) is False
    assert telegram_enabled({"token": "x"}, enabled=False) is False
    assert telegram_enabled({"token": "x"}, enabled=True) is True
    assert telegram_enabled({"enabled": True, "token": "x"}) is True
    assert telegram_enabled({"token": "x"}) is False


def test_split_text_respects_limit_and_boundaries() -> None:
    assert split_text("a" * 5) == ["a" * 5]
    long = "ab\n" * 3000
    parts = split_text(long)
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == long


def test_split_text_hard_splits_overlong_line() -> None:
    parts = split_text("x" * 10000, limit=4096)
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == "x" * 10000


def test_markdown_to_html_converts_basic_inline() -> None:
    assert markdown_to_html("**bold**") == "<b>bold</b>"
    assert markdown_to_html("`code`") == "<code>code</code>"


def test_classify_media_by_filename() -> None:
    assert classify_media("photo.JPG") is TelegramMediaKind.PHOTO
    assert classify_media("clip.mp4") is TelegramMediaKind.VIDEO
    assert classify_media("note.ogg") is TelegramMediaKind.VOICE
    assert classify_media("doc.pdf") is TelegramMediaKind.DOCUMENT
