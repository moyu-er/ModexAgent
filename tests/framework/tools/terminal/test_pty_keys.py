from __future__ import annotations

import pytest

from framework.tools.terminal.pty_keys import (
    CursorKeyMode,
    ProcessAction,
    _APPLICATION_ARROW,
    _BRACKETED_PASTE_DISABLE,
    _BRACKETED_PASTE_ENABLE,
    _BRACKETED_PASTE_END,
    _BRACKETED_PASTE_START,
    _CURSOR_SENSITIVE_KEYS,
    _FUNCTION_KEYS,
    _NAMED_KEYS,
    _NORMAL_ARROW,
    _RMKX,
    _SMKX,
    detect_bracketed_paste_mode,
    detect_cursor_key_mode,
    encode_key,
    encode_key_sequence,
    encode_paste,
    needs_cursor_mode,
    strip_dsr_and_respond,
    strip_smkx_rmkx,
)


# ---------------------------------------------------------------------------
# CursorKeyMode
# ---------------------------------------------------------------------------


class TestCursorKeyMode:
    def test_enum_values(self) -> None:
        assert CursorKeyMode.UNKNOWN.value == "unknown"
        assert CursorKeyMode.NORMAL.value == "normal"
        assert CursorKeyMode.APPLICATION.value == "application"

    def test_is_str_enum(self) -> None:
        assert isinstance(CursorKeyMode.NORMAL, str)


# ---------------------------------------------------------------------------
# ProcessAction
# ---------------------------------------------------------------------------


class TestProcessAction:
    def test_all_action_values(self) -> None:
        expected = {
            "list": "list",
            "log": "log",
            "write": "write",
            "submit": "submit",
            "send_keys": "send_keys",
            "paste": "paste",
            "interrupt": "interrupt",
            "kill": "kill",
            "clear": "clear",
            "remove": "remove",
        }
        for attr, value in expected.items():
            assert ProcessAction[attr.upper()].value == value

    def test_is_str_enum(self) -> None:
        assert isinstance(ProcessAction.LIST, str)


# ---------------------------------------------------------------------------
# encode_key – single printable chars
# ---------------------------------------------------------------------------


class TestEncodeKeyPrintable:
    def test_single_printable_char(self) -> None:
        assert encode_key("a") == b"a"
        assert encode_key("1") == b"1"
        assert encode_key(" ") == b" "

    def test_uppercase_char(self) -> None:
        assert encode_key("Z") == b"Z"


# ---------------------------------------------------------------------------
# encode_key – named keys
# ---------------------------------------------------------------------------


class TestEncodeKeyNamed:
    def test_enter_return(self) -> None:
        assert encode_key("enter") == b"\r"
        assert encode_key("return") == b"\r"

    def test_escape_aliases(self) -> None:
        assert encode_key("escape") == b"\x1b"
        assert encode_key("esc") == b"\x1b"

    def test_tab(self) -> None:
        assert encode_key("tab") == b"\t"

    def test_backspace(self) -> None:
        assert encode_key("backspace") == b"\x7f"

    def test_edit_keys(self) -> None:
        assert encode_key("delete") == b"\x1b[3~"
        assert encode_key("insert") == b"\x1b[2~"

    def test_page_keys(self) -> None:
        assert encode_key("pageup") == b"\x1b[5~"
        assert encode_key("pagedown") == b"\x1b[6~"

    def test_space(self) -> None:
        assert encode_key("space") == b" "


# ---------------------------------------------------------------------------
# encode_key – ctrl / alt / ctrl+alt
# ---------------------------------------------------------------------------


class TestEncodeKeyModifiers:
    def test_ctrl_keys(self) -> None:
        assert encode_key("c-c") == b"\x03"
        assert encode_key("c-d") == b"\x04"
        assert encode_key("c-z") == b"\x1a"
        assert encode_key("c-a") == b"\x01"

    def test_alt_keys(self) -> None:
        assert encode_key("m-x") == b"\x1bx"
        assert encode_key("alt-x") == b"\x1bx"

    def test_ctrl_alt_keys(self) -> None:
        assert encode_key("c-m-x") == b"\x1b\x18"
        assert encode_key("c-alt-x") == b"\x1b\x18"


# ---------------------------------------------------------------------------
# encode_key – function keys
# ---------------------------------------------------------------------------


class TestEncodeKeyFunction:
    def test_f1_through_f4(self) -> None:
        assert encode_key("f1") == b"\x1bOP"
        assert encode_key("f2") == b"\x1bOQ"
        assert encode_key("f3") == b"\x1bOR"
        assert encode_key("f4") == b"\x1bOS"

    def test_f5_through_f12(self) -> None:
        assert encode_key("f5") == b"\x1b[15~"
        assert encode_key("f12") == b"\x1b[24~"


# ---------------------------------------------------------------------------
# encode_key – arrow / cursor keys (DECCKM)
# ---------------------------------------------------------------------------


class TestEncodeKeyCursor:
    def test_arrow_keys_normal_mode(self) -> None:
        assert encode_key("up", CursorKeyMode.NORMAL) == b"\x1b[A"
        assert encode_key("down", CursorKeyMode.NORMAL) == b"\x1b[B"
        assert encode_key("right", CursorKeyMode.NORMAL) == b"\x1b[C"
        assert encode_key("left", CursorKeyMode.NORMAL) == b"\x1b[D"

    def test_arrow_keys_application_mode(self) -> None:
        assert encode_key("up", CursorKeyMode.APPLICATION) == b"\x1bOA"
        assert encode_key("down", CursorKeyMode.APPLICATION) == b"\x1bOB"
        assert encode_key("right", CursorKeyMode.APPLICATION) == b"\x1bOC"
        assert encode_key("left", CursorKeyMode.APPLICATION) == b"\x1bOD"

    def test_arrow_keys_unknown_defaults_normal(self) -> None:
        assert encode_key("up", CursorKeyMode.UNKNOWN) == b"\x1b[A"
        assert encode_key("left", CursorKeyMode.UNKNOWN) == b"\x1b[D"

    def test_home_end_normal(self) -> None:
        assert encode_key("home", CursorKeyMode.NORMAL) == b"\x1b[1~"
        assert encode_key("end", CursorKeyMode.NORMAL) == b"\x1b[4~"

    def test_home_end_application(self) -> None:
        assert encode_key("home", CursorKeyMode.APPLICATION) == b"\x1bOH"
        assert encode_key("end", CursorKeyMode.APPLICATION) == b"\x1bOF"


# ---------------------------------------------------------------------------
# encode_key – hex encoding
# ---------------------------------------------------------------------------


class TestEncodeKeyHex:
    def test_hex_encoding(self) -> None:
        assert encode_key("hex:1b") == b"\x1b"
        assert encode_key("hex:03") == b"\x03"
        assert encode_key("hex:0d") == b"\r"

    def test_hex_multibyte(self) -> None:
        assert encode_key("hex:4f") == b"O"


# ---------------------------------------------------------------------------
# encode_key – fallback
# ---------------------------------------------------------------------------


class TestEncodeKeyFallback:
    def test_unknown_key_returns_utf8(self) -> None:
        assert encode_key("unknownkey") == b"unknownkey"


# ---------------------------------------------------------------------------
# encode_key_sequence
# ---------------------------------------------------------------------------


class TestEncodeKeySequence:
    def test_multiple_keys(self) -> None:
        result = encode_key_sequence(["escape", "i"])
        assert result == b"\x1bi"

    def test_vim_save_quit(self) -> None:
        result = encode_key_sequence(["escape", ":wq", "enter"])
        assert result == b"\x1b:wq\r"

    def test_empty_sequence(self) -> None:
        assert encode_key_sequence([]) == b""


# ---------------------------------------------------------------------------
# needs_cursor_mode
# ---------------------------------------------------------------------------


class TestNeedsCursorMode:
    def test_arrow_key_needs_cursor_mode(self) -> None:
        assert needs_cursor_mode(["up", "enter"]) is True
        assert needs_cursor_mode(["down"]) is True

    def test_non_cursor_keys_do_not_need_mode(self) -> None:
        assert needs_cursor_mode(["enter", "escape"]) is False

    def test_empty_list(self) -> None:
        assert needs_cursor_mode([]) is False

    def test_home_end_need_cursor_mode(self) -> None:
        assert needs_cursor_mode(["home"]) is True
        assert needs_cursor_mode(["end"]) is True


# ---------------------------------------------------------------------------
# detect_cursor_key_mode
# ---------------------------------------------------------------------------


class TestDetectCursorKeyMode:
    def test_smkx_sets_application(self) -> None:
        assert detect_cursor_key_mode(_SMKX) == CursorKeyMode.APPLICATION

    def test_rmkx_sets_normal(self) -> None:
        assert detect_cursor_key_mode(_RMKX) == CursorKeyMode.NORMAL

    def test_no_sequence_returns_none(self) -> None:
        assert detect_cursor_key_mode(b"hello") is None

    def test_last_wins(self) -> None:
        data = _SMKX + _RMKX
        assert detect_cursor_key_mode(data) == CursorKeyMode.NORMAL

    def test_last_wins_reversed(self) -> None:
        data = _RMKX + _SMKX
        assert detect_cursor_key_mode(data) == CursorKeyMode.APPLICATION


# ---------------------------------------------------------------------------
# strip_smkx_rmkx
# ---------------------------------------------------------------------------


class TestStripSmkxRmkx:
    def test_strips_both(self) -> None:
        data = b"before" + _SMKX + b"mid" + _RMKX + b"after"
        assert strip_smkx_rmkx(data) == b"beforemidafter"

    def test_no_match(self) -> None:
        assert strip_smkx_rmkx(b"hello") == b"hello"


# ---------------------------------------------------------------------------
# strip_dsr_and_respond
# ---------------------------------------------------------------------------


class TestStripDsrAndRespond:
    def test_strips_dsr(self) -> None:
        data = b"hello\x1b[6nworld"
        cleaned, count = strip_dsr_and_respond(data)
        assert cleaned == b"helloworld"
        assert count == 1

    def test_strips_dsr_with_question_mark(self) -> None:
        data = b"hello\x1b[?6nworld"
        cleaned, count = strip_dsr_and_respond(data)
        assert cleaned == b"helloworld"
        assert count == 1

    def test_no_dsr(self) -> None:
        data = b"hello"
        cleaned, count = strip_dsr_and_respond(data)
        assert cleaned == b"hello"
        assert count == 0

    def test_responds_via_writer(self) -> None:
        written: list[bytes] = []

        class FakeWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

        data = b"\x1b[6n\x1b[?6n"
        strip_dsr_and_respond(data, FakeWriter())
        assert written == [b"\x1b[1;1R", b"\x1b[1;1R"]

    def test_no_writer_no_error(self) -> None:
        data = b"\x1b[6n"
        cleaned, count = strip_dsr_and_respond(data)
        assert count == 1


# ---------------------------------------------------------------------------
# detect_bracketed_paste_mode
# ---------------------------------------------------------------------------


class TestDetectBracketedPasteMode:
    def test_enable(self) -> None:
        assert detect_bracketed_paste_mode(_BRACKETED_PASTE_ENABLE) is True

    def test_disable(self) -> None:
        assert detect_bracketed_paste_mode(_BRACKETED_PASTE_DISABLE) is False

    def test_no_sequence(self) -> None:
        assert detect_bracketed_paste_mode(b"hello") is None

    def test_last_wins_enable(self) -> None:
        data = _BRACKETED_PASTE_DISABLE + _BRACKETED_PASTE_ENABLE
        assert detect_bracketed_paste_mode(data) is True

    def test_last_wins_disable(self) -> None:
        data = _BRACKETED_PASTE_ENABLE + _BRACKETED_PASTE_DISABLE
        assert detect_bracketed_paste_mode(data) is False


# ---------------------------------------------------------------------------
# encode_paste
# ---------------------------------------------------------------------------


class TestEncodePaste:
    def test_bracketed(self) -> None:
        result = encode_paste("hello", bracketed=True)
        assert result == _BRACKETED_PASTE_START + b"hello" + _BRACKETED_PASTE_END

    def test_unbracketed(self) -> None:
        result = encode_paste("hello", bracketed=False)
        assert result == b"hello"

    def test_unicode(self) -> None:
        result = encode_paste("café", bracketed=True)
        expected = _BRACKETED_PASTE_START + "café".encode("utf-8") + _BRACKETED_PASTE_END
        assert result == expected


# ---------------------------------------------------------------------------
# cursor sensitive keys constant
# ---------------------------------------------------------------------------


class TestCursorSensitiveKeys:
    def test_contains_arrow_and_home_end(self) -> None:
        for key in ("up", "down", "left", "right", "home", "end"):
            assert key in _CURSOR_SENSITIVE_KEYS

    def test_non_cursor_keys_not_in_set(self) -> None:
        for key in ("enter", "escape", "tab", "f1"):
            assert key not in _CURSOR_SENSITIVE_KEYS
