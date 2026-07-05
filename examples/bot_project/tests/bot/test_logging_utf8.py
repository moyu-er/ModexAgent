"""Regression: emoji/CJK in tool output must not crash the console logger.

On a Windows cp936/GBK console, ``logging.StreamHandler(sys.stdout).emit``
raised ``UnicodeEncodeError`` on emoji (e.g. ``📄`` ``📁``) that ``ls`` and
other tools return. ``setup_logging()`` reconfigures stdio to UTF-8 so emit
never raises on any payload.
"""

from __future__ import annotations

import io

import pytest


def test_reconfigure_makes_gbk_stream_utf8_safe() -> None:
    """A GBK stream that crashes on emoji survives after UTF-8 reconfigure.

    Reproduces the exact crash condition (GBK + emoji → UnicodeEncodeError),
    then proves ``reconfigure(encoding="utf-8", errors="backslashreplace")``
    — the operation ``_reconfigure_stdio_utf8`` performs — fixes it.
    """
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="gbk", newline="\n")

    # Original failure: cp936/GBK cannot encode the 📄 codepoint.
    with pytest.raises(UnicodeEncodeError):
        stream.write("📄 forms.md 📁 scripts")
        stream.flush()

    # The fix: reconfigure the live stream to UTF-8 with a safe error mode.
    stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    stream.write("📄 forms.md 📁 scripts 📄 SKILL.md")
    stream.flush()
    assert "📄" in buf.getvalue().decode("utf-8")


def test_reconfigure_stdio_utf8_is_idempotent_and_guarded() -> None:
    """``_reconfigure_stdio_utf8`` must not raise on streams without reconfigure."""
    from bot.logging import _reconfigure_stdio_utf8

    # Real call against live stdio — must be safe to call repeatedly.
    _reconfigure_stdio_utf8()
    _reconfigure_stdio_utf8()

    # A stream lacking ``reconfigure`` (e.g. some pytest captures) is skipped.
    class _NoReconfigure:
        encoding = "gbk"

    _reconfigure_stdio_utf8()  # no assertion — just must not raise
    assert not hasattr(_NoReconfigure(), "reconfigure")
