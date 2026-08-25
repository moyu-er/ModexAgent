"""Literal golden tests for the pruned markdown transcript renderer.

Scenarios (a)-(f) assert exact full-text equality (``assert rendered ==
EXPECTED``) — substring assertions are forbidden so that any dropped or
leaked field (``token_count``, ``message_id``, ``_pinned``, ...) breaks the
test. Scenario (g) is the sole exception: a 1001-message transcript asserts
only the first/last block headers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from modex_agent.memory.pruned.render import render_transcript

# ---------------------------------------------------------------------------
# Scenario (a) fixture: 12 mixed-role messages in ChatMessage.to_dict() shape
# (OpenAI wire tool_calls, "YYYY-MM-DD HH:MM:SS" created_at strings, and
# token_count values the renderer must drop).
# ---------------------------------------------------------------------------

GOLDEN_MESSAGES: list[dict[str, Any]] = [
    {
        "role": "user",
        "content": "登录之后过一会儿就提示 session expired,帮我看看是哪的问题",
        "created_at": "2026-08-19 10:31:00",
        "token_count": 23,
    },
    {
        "role": "assistant",
        "content": "收到,我来排查。先看配置和入口,再定位报错来源。",
        "created_at": "2026-08-19 10:32:00",
        "token_count": 18,
    },
    {
        "role": "assistant",
        "content": "我先并行查看入口文件和路由定义,再搜索错误关键字。",
        "tool_calls": [
            {
                "id": "call_a1",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": '{"path": "/src/app.py", "offset": 0, "limit": 50}',
                },
            },
            {
                "id": "call_a2",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": '{"path": "/src/routes/auth.py", "offset": 0, "limit": 80}',
                },
            },
            {
                "id": "call_a3",
                "type": "function",
                "function": {
                    "name": "grep",
                    "arguments": '{"pattern": "session expired", "include": "*.py"}',
                },
            },
        ],
        "created_at": "2026-08-19 10:32:00",
        "token_count": 96,
    },
    {
        "role": "tool",
        "name": "read",
        "tool_call_id": "call_a1",
        "content": "import os\nfrom routes import auth\n\ndef main():\n    app = auth.mount(app)\n    ...",
        "created_at": "2026-08-19 10:32:00",
        "token_count": 41,
    },
    {
        "role": "tool",
        "name": "read",
        "tool_call_id": "call_a2",
        "content": "SESSION_TTL = 3600\n\ndef login(req):\n    token = issue_token(req.user)\n    ...",
        "created_at": "2026-08-19 10:32:00",
        "token_count": 33,
    },
    {
        "role": "tool",
        "name": "grep",
        "tool_call_id": "call_a3",
        "content": 'src/routes/auth.py:42:  raise SessionExpired("session expired")\n'
        "src/middleware.py:17:  if age > SESSION_TTL:  # session expired guard",
        "created_at": "2026-08-19 10:32:00",
        "token_count": 52,
    },
    {
        "role": "assistant",
        "content": "TTL 是 3600 秒,报错在 auth.py:42 抛出。我再看一下 middleware 里 cookie 的刷新逻辑。",
        "tool_calls": [
            {
                "id": "call_a4",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": '{"path": "/src/middleware.py", "offset": 0, "limit": 60}',
                },
            },
        ],
        "created_at": "2026-08-19 10:33:00",
        "token_count": 47,
    },
    {
        "role": "tool",
        "name": "read",
        "tool_call_id": "call_a4",
        "content": "def session_guard(handler):\n    async def wrapped(req):\n"
        "        age = now() - req.session.issued_at\n"
        "        if age > SESSION_TTL:  # session expired guard\n"
        '            raise SessionExpired("session expired")\n'
        "        return await handler(req)\n    return wrapped",
        "created_at": "2026-08-19 10:33:00",
        "token_count": 88,
    },
    {
        "role": "assistant",
        "content": "定位到了:middleware 只校验 TTL,从不刷新 cookie,一小时后必过期。修复方案是在 wrapped 里滑动续期。",
        "created_at": "2026-08-19 10:34:00",
        "token_count": 41,
    },
    {
        "role": "system_reminder",
        "content": "<system-reminder>Conversation memory was consolidated. Your active todo list is "
        "preserved in todo_read; re-orient yourself before continuing.</system-reminder>",
        "created_at": "2026-08-19 10:35:00",
        "token_count": 55,
    },
    {
        "role": "agent",
        "name": "explorer",
        "content": "Searched the codebase: no existing sliding-renewal helper. Token issuance lives in "
        "auth.issue_token; adding a refresh call inside session_guard is the minimal change.",
        "created_at": "2026-08-19 10:35:00",
        "token_count": 62,
    },
    {
        "role": "assistant",
        "content": "好,方案确认:在 session_guard 的 wrapped 中,校验通过后调用 auth.issue_token 重发 cookie。我现在动手改 middleware.py。",
        "created_at": "2026-08-19 10:36:00",
        "token_count": 49,
    },
]

GOLDEN_EXPECTED = """\
# Transcript #3 · 排查 WebUI 登录后 session 过期报错
- range: 2026-08-19 10:31 ~ 2026-08-19 10:36
- messages: 12

---

## [001] user · 08-19 10:31

登录之后过一会儿就提示 session expired,帮我看看是哪的问题

---

## [002] assistant · 08-19 10:32

收到,我来排查。先看配置和入口,再定位报错来源。

---

## [003] assistant · 08-19 10:32

我先并行查看入口文件和路由定义,再搜索错误关键字。

[tool_call read · call_a1] {"path": "/src/app.py", "offset": 0, "limit": 50}
[tool_call read · call_a2] {"path": "/src/routes/auth.py", "offset": 0, "limit": 80}
[tool_call grep · call_a3] {"pattern": "session expired", "include": "*.py"}

---

## [004] tool · read · call_a1 · 08-19 10:32

import os
from routes import auth

def main():
    app = auth.mount(app)
    ...

---

## [005] tool · read · call_a2 · 08-19 10:32

SESSION_TTL = 3600

def login(req):
    token = issue_token(req.user)
    ...

---

## [006] tool · grep · call_a3 · 08-19 10:32

src/routes/auth.py:42:  raise SessionExpired("session expired")
src/middleware.py:17:  if age > SESSION_TTL:  # session expired guard

---

## [007] assistant · 08-19 10:33

TTL 是 3600 秒,报错在 auth.py:42 抛出。我再看一下 middleware 里 cookie 的刷新逻辑。

[tool_call read · call_a4] {"path": "/src/middleware.py", "offset": 0, "limit": 60}

---

## [008] tool · read · call_a4 · 08-19 10:33

def session_guard(handler):
    async def wrapped(req):
        age = now() - req.session.issued_at
        if age > SESSION_TTL:  # session expired guard
            raise SessionExpired("session expired")
        return await handler(req)
    return wrapped

---

## [009] assistant · 08-19 10:34

定位到了:middleware 只校验 TTL,从不刷新 cookie,一小时后必过期。修复方案是在 wrapped 里滑动续期。

---

## [010] system_reminder · 08-19 10:35

<system-reminder>Conversation memory was consolidated. Your active todo list is preserved in todo_read; re-orient yourself before continuing.</system-reminder>

---

## [011] agent · explorer · 08-19 10:35

Searched the codebase: no existing sliding-renewal helper. Token issuance lives in auth.issue_token; adding a refresh call inside session_guard is the minimal change.

---

## [012] assistant · 08-19 10:36

好,方案确认:在 session_guard 的 wrapped 中,校验通过后调用 auth.issue_token 重发 cookie。我现在动手改 middleware.py。

---"""


class TestGoldenTranscript:
    """Scenario (a): mixed-role fixture renders the plan's golden markdown."""

    def test_mixed_role_fixture_renders_golden_transcript(self) -> None:
        rendered = render_transcript(
            3,
            "排查 WebUI 登录后 session 过期报错",
            GOLDEN_MESSAGES,
            datetime(2026, 8, 19, 10, 31),
            datetime(2026, 8, 19, 10, 36),
        )
        assert rendered == GOLDEN_EXPECTED


class TestBodyEdgeCases:
    """Scenarios (b) and (c): body rendering of empty and multimodal content."""

    def test_content_none_without_tool_calls_renders_empty_marker(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "created_at": "2026-08-19 10:31:00",
                "token_count": 0,
                "_pinned": False,
                "message_id": "mid-77",
            },
        ]
        rendered = render_transcript(
            1,
            "empty body",
            messages,
            datetime(2026, 8, 19, 10, 31),
            datetime(2026, 8, 19, 10, 31),
        )
        assert rendered == (
            "# Transcript #1 · empty body\n"
            "- range: 2026-08-19 10:31 ~ 2026-08-19 10:31\n"
            "- messages: 1\n"
            "\n"
            "---\n"
            "\n"
            "## [001] assistant · 08-19 10:31\n"
            "\n"
            "(empty)\n"
            "\n"
            "---"
        )

    def test_content_list_renders_text_and_image_placeholder(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看看这张截图里的报错"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/screenshots/error.png"},
                    },
                ],
                "created_at": "2026-08-19 10:31:00",
            },
        ]
        rendered = render_transcript(
            1,
            "multimodal",
            messages,
            datetime(2026, 8, 19, 10, 31),
            datetime(2026, 8, 19, 10, 31),
        )
        assert rendered == (
            "# Transcript #1 · multimodal\n"
            "- range: 2026-08-19 10:31 ~ 2026-08-19 10:31\n"
            "- messages: 1\n"
            "\n"
            "---\n"
            "\n"
            "## [001] user · 08-19 10:31\n"
            "\n"
            "看看这张截图里的报错\n"
            "[image: https://example.com/screenshots/error.png]\n"
            "\n"
            "---"
        )


class TestDegradation:
    """Scenarios (d) and (e): [raw] fallback and unhandled-role template."""

    def test_invalid_role_message_degrades_to_raw_block(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "first message", "created_at": "2026-08-19 10:31:00"},
            {
                "role": "not-a-role",
                "content": "broken message",
                "created_at": "2026-08-19 10:32:00",
            },
            {"role": "assistant", "content": "third message", "created_at": "2026-08-19 10:33:00"},
        ]
        rendered = render_transcript(
            1,
            "degraded",
            messages,
            datetime(2026, 8, 19, 10, 31),
            datetime(2026, 8, 19, 10, 33),
        )
        assert rendered == (
            "# Transcript #1 · degraded\n"
            "- range: 2026-08-19 10:31 ~ 2026-08-19 10:33\n"
            "- messages: 3\n"
            "\n"
            "---\n"
            "\n"
            "## [001] user · 08-19 10:31\n"
            "\n"
            "first message\n"
            "\n"
            "---\n"
            "\n"
            "## [002] raw\n"
            "\n"
            '{"role": "not-a-role", "content": "broken message", "created_at": "2026-08-19 10:32:00"}\n'
            "\n"
            "---\n"
            "\n"
            "## [003] assistant · 08-19 10:33\n"
            "\n"
            "third message\n"
            "\n"
            "---"
        )

    def test_unhandled_pending_role_uses_generic_template(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "pending",
                "content": "queued tool result",
                "created_at": "2026-08-19 10:31:00",
            },
        ]
        rendered = render_transcript(
            1,
            "unhandled role",
            messages,
            datetime(2026, 8, 19, 10, 31),
            datetime(2026, 8, 19, 10, 31),
        )
        assert rendered == (
            "# Transcript #1 · unhandled role\n"
            "- range: 2026-08-19 10:31 ~ 2026-08-19 10:31\n"
            "- messages: 1\n"
            "\n"
            "---\n"
            "\n"
            "## [001] pending · 08-19 10:31\n"
            "\n"
            "queued tool result\n"
            "\n"
            "---"
        )


class TestHeaderEdgeCases:
    """Scenario (f): missing time range renders ``- range: unknown``."""

    def test_no_time_range_renders_unknown(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "no timestamps here", "created_at": "2026-08-19 10:31:00"},
        ]
        rendered = render_transcript(1, "no time", messages, None, None)
        assert rendered == (
            "# Transcript #1 · no time\n"
            "- range: unknown\n"
            "- messages: 1\n"
            "\n"
            "---\n"
            "\n"
            "## [001] user · 08-19 10:31\n"
            "\n"
            "no timestamps here\n"
            "\n"
            "---"
        )

    def test_partial_time_range_still_renders_unknown(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "partial range", "created_at": "2026-08-19 10:31:00"},
        ]
        rendered = render_transcript(
            1,
            "partial time",
            messages,
            None,
            datetime(2026, 8, 19, 10, 31),
        )
        assert rendered.startswith(
            "# Transcript #1 · partial time\n- range: unknown\n- messages: 1\n"
        )


class TestSequenceWidth:
    """Scenario (g): seq width adapts past 999 messages."""

    def test_seq_width_adapts_for_1001_messages(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "m", "created_at": "2026-08-19 10:31:00"}
            for _ in range(1001)
        ]
        rendered = render_transcript(
            1,
            "bulk",
            messages,
            datetime(2026, 8, 19, 10, 31),
            datetime(2026, 8, 19, 10, 36),
        )
        assert rendered.startswith(
            "# Transcript #1 · bulk\n"
            "- range: 2026-08-19 10:31 ~ 2026-08-19 10:36\n"
            "- messages: 1001\n"
            "\n"
            "---\n"
            "\n"
            "## [0001] user · 08-19 10:31\n"
        )
        assert rendered.endswith("## [1001] user · 08-19 10:31\n\nm\n\n---")
        assert rendered.count("\n## [") == 1001
