# OpenAI Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `OpenAIProvider` using the `openai` SDK as a native alternative to `LiteLLMProvider`, with shared intermediate types (`StreamDelta`, `ParsedResponse`) for future reuse.

**Architecture:** Flat file structure under `framework/providers/`. Shared types in `shared/delta.py` + `shared/errors.py`. `OpenAIProvider` extends `StreamingLLMProvider` and composes `shared/` types. Message preprocessing stays in pipeline layer; provider only handles SDK call/parse.

**Tech Stack:** Python 3.12+, openai 2.24.0 (Pydantic models), httpx (timeout), asyncio

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `framework/providers/shared/__init__.py` | Package init |
| Create | `framework/providers/shared/delta.py` | `StreamDelta`, `ParsedResponse`, `extract_reasoning()` |
| Create | `framework/providers/shared/errors.py` | `classify_openai_error()` |
| Create | `framework/providers/openai_provider.py` | `OpenAIProvider` class |
| Create | `tests/unit/providers/test_shared_delta.py` | Delta types unit tests |
| Create | `tests/unit/providers/test_shared_errors.py` | Error classification unit tests |
| Create | `tests/unit/providers/test_openai_provider.py` | OpenAIProvider unit tests |
| Modify | `framework/providers/__init__.py` | Add OpenAIProvider export |

---

### Task 1: Create `shared/` package init

**Files:**
- Create: `framework/providers/shared/__init__.py`

- [ ] **Step 1: Write the file**

```python
"""Provider shared types and utilities.

StreamDelta, ParsedResponse — intermediate response carriers.
classify_openai_error — structured error extraction from openai SDK exceptions.

These are NOT exported via framework/providers/__init__.py;
import directly from framework.providers.shared when needed.
"""
```

- [ ] **Step 2: Verify imports work**

Run: `python -c "from framework.providers.shared import __doc__; print('OK')"`
Expected: prints `OK`

- [ ] **Step 3: Commit**

```bash
git add framework/providers/shared/__init__.py
git commit -m "chore(providers): add shared/ package init"
```

---

### Task 2: Create shared types — `StreamDelta`, `ParsedResponse`, `extract_reasoning()`

**Files:**
- Create: `framework/providers/shared/delta.py`
- Create: `tests/unit/providers/test_shared_delta.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/providers/test_shared_delta.py`:

```python
"""Tests for framework.providers.shared.delta."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from framework.providers.shared.delta import (
    ParsedResponse,
    StreamDelta,
    extract_reasoning,
)


class TestExtractReasoning:
    """Unit tests for extract_reasoning()."""

    def test_returns_none_when_no_model_extra(self):
        model = MagicMock(spec=BaseModel)
        model.model_extra = None
        assert extract_reasoning(model) is None

    def test_returns_none_when_key_missing(self):
        model = MagicMock(spec=BaseModel)
        model.model_extra = {"other_field": "value"}
        assert extract_reasoning(model) is None

    def test_returns_reasoning_content(self):
        model = MagicMock(spec=BaseModel)
        model.model_extra = {"reasoning_content": "thinking..."}
        assert extract_reasoning(model) == "thinking..."

    def test_returns_none_for_none_input(self):
        assert extract_reasoning(None) is None


class TestStreamDeltaFromOpenai:
    """Unit tests for StreamDelta.from_openai()."""

    def test_extracts_content(self):
        delta = MagicMock()
        delta.content = "hello"
        delta.tool_calls = None
        delta.model_extra = None

        result = StreamDelta.from_openai(delta)
        assert result.content == "hello"
        assert result.reasoning_content is None
        assert result.tool_call_chunks == []
        assert result.finish_reason is None

    def test_extracts_reasoning_via_model_extra(self):
        delta = MagicMock()
        delta.content = None
        delta.tool_calls = None
        delta.model_extra = {"reasoning_content": "let me think..."}

        result = StreamDelta.from_openai(delta)
        assert result.reasoning_content == "let me think..."

    def test_extracts_tool_call_chunks(self):
        func_chunk = MagicMock()
        func_chunk.name = "search"
        func_chunk.arguments = '{"query": "weather"}'

        tc = MagicMock()
        tc.index = 0
        tc.id = "call_001"
        tc.function = func_chunk

        delta = MagicMock()
        delta.content = None
        delta.tool_calls = [tc]
        delta.model_extra = None

        result = StreamDelta.from_openai(delta)
        assert len(result.tool_call_chunks) == 1
        chunk = result.tool_call_chunks[0]
        assert chunk.index == 0
        assert chunk.id == "call_001"
        assert chunk.name == "search"
        assert chunk.args == '{"query": "weather"}'

    def test_handles_none_function(self):
        tc = MagicMock()
        tc.index = 0
        tc.id = "call_002"
        tc.function = None

        delta = MagicMock()
        delta.content = None
        delta.tool_calls = [tc]
        delta.model_extra = None

        result = StreamDelta.from_openai(delta)
        assert result.tool_call_chunks[0].name is None
        assert result.tool_call_chunks[0].args is None

    def test_default_values(self):
        d = StreamDelta()
        assert d.content is None
        assert d.reasoning_content is None
        assert d.tool_call_chunks == []


class TestParsedResponseFromOpenai:
    """Unit tests for ParsedResponse.from_openai()."""

    def test_extracts_simple_content_response(self):
        msg = MagicMock()
        msg.content = "Hello, world!"
        msg.tool_calls = None
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"

        usage = MagicMock()
        usage.prompt_tokens = 100
        usage.completion_tokens = 50
        usage.total_tokens = 150

        response = MagicMock()
        response.choices = [choice]
        response.usage = usage

        result = ParsedResponse.from_openai(response)
        assert result.content == "Hello, world!"
        assert result.finish_reason == "stop"
        assert result.usage == {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        }
        assert result.tool_calls == []

    def test_extracts_tool_calls(self):
        func = MagicMock()
        func.name = "search"
        func.arguments = '{"query": "weather"}'

        tc = MagicMock()
        tc.id = "call_003"
        tc.function = func

        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [tc]
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "tool_calls"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {"query": "weather"}
        assert result.tool_calls[0].call_id == "call_003"
        assert result.finish_reason == "tool_calls"

    def test_extracts_reasoning_content(self):
        msg = MagicMock()
        msg.content = "answer"
        msg.tool_calls = None
        msg.model_extra = {"reasoning_content": "step by step..."}

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert result.reasoning_content == "step by step..."

    def test_handles_none_usage(self):
        msg = MagicMock()
        msg.content = "ok"
        msg.tool_calls = None
        msg.model_extra = None

        choice = MagicMock()
        choice.message = msg
        choice.finish_reason = "stop"

        response = MagicMock()
        response.choices = [choice]
        response.usage = None

        result = ParsedResponse.from_openai(response)
        assert result.usage == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/providers/test_shared_delta.py -v`
Expected: FAIL with ImportError (module not yet created)

- [ ] **Step 3: Write `framework/providers/shared/delta.py`**

```python
"""Shared intermediate types for provider response parsing.

StreamDelta  — streaming chunk extraction result.
ParsedResponse — non-streaming response extraction result.
extract_reasoning — reasoning_content extraction from Pydantic model_extra.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from framework.core.tool_call_accumulator import ToolCallChunk
from framework.core.types import ToolCall

if TYPE_CHECKING:
    from pydantic import BaseModel


def extract_reasoning(model: BaseModel) -> str | None:
    """Extract reasoning_content from a Pydantic model's extra fields.

    reasoning_content is not in the openai SDK typed schema;
    it arrives via model_extra, Pydantic's official extension mechanism.
    No getattr/hasattr reflection is used.
    """
    if model is None:
        return None
    extra = model.model_extra
    if extra is None:
        return None
    return extra.get("reasoning_content")


@dataclass
class StreamDelta:
    """Structured extraction from a streaming chunk's delta.

    All fields are typed; no dicts, no hasattr probing.
    """

    content: str | None = None
    reasoning_content: str | None = None
    tool_call_chunks: list[ToolCallChunk] = field(default_factory=list)
    finish_reason: str | None = None

    @classmethod
    def from_openai(cls, delta) -> StreamDelta:
        """Build from openai SDK ChoiceDelta Pydantic object.

        Args:
            delta: openai.types.chat.chat_completion_chunk.ChoiceDelta

        All field access is via typed attribute access on the SDK model.
        reasoning_content uses extract_reasoning() via model_extra.
        """
        instance = cls()
        instance.content = delta.content

        if delta.tool_calls:
            instance.tool_call_chunks = [
                ToolCallChunk(
                    index=tc.index,
                    id=tc.id,
                    name=tc.function.name if tc.function else None,
                    args=tc.function.arguments if tc.function else None,
                )
                for tc in delta.tool_calls
            ]

        instance.reasoning_content = extract_reasoning(delta)
        return instance


@dataclass
class ParsedResponse:
    """Structured extraction from a non-streaming LLM response.

    Intermediate carrier between SDK response and framework LLMResponse.
    """

    content: str | None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_openai(cls, response) -> ParsedResponse:
        """Build from openai SDK ChatCompletion Pydantic object.

        Args:
            response: openai.types.chat.ChatCompletion

        All field access is via typed attribute access on the SDK model.
        Tool call arguments are json.loads() parsed from JSON strings.
        """
        choice = response.choices[0]
        msg = choice.message

        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(
                    tool_name=tc.function.name,
                    arguments=json.loads(tc.function.arguments),
                    call_id=tc.id,
                ))

        usage: dict[str, int] = {}
        if response.usage is not None:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return cls(
            content=msg.content,
            reasoning_content=extract_reasoning(msg),
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/providers/test_shared_delta.py -v`
Expected: all 12 tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/providers/shared/delta.py tests/unit/providers/test_shared_delta.py
git commit -m "feat(providers): add StreamDelta, ParsedResponse, extract_reasoning shared types"
```

---

### Task 3: Create error classification — `classify_openai_error()`

**Files:**
- Create: `framework/providers/shared/errors.py`
- Create: `tests/unit/providers/test_shared_errors.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/providers/test_shared_errors.py`:

```python
"""Tests for framework.providers.shared.errors."""
from __future__ import annotations

import pytest

from framework.core.llm_error import LLMErrorInfo, LLMErrorKind
from framework.providers.shared.errors import classify_openai_error


# Minimal fake openai-style exceptions for testing.
# These mimic the real openai SDK exception hierarchy without requiring the package.


class FakeOpenAIError(Exception):
    pass


class FakeAPIStatusError(FakeOpenAIError):
    def __init__(self, message="", status_code=0, body=None):
        self.status_code = status_code
        self.message = message
        self.body = body


class FakeRateLimitError(FakeAPIStatusError):
    pass


class FakeAPITimeoutError(FakeOpenAIError):
    pass


class FakeAuthenticationError(FakeAPIStatusError):
    pass


class FakeInternalServerError(FakeAPIStatusError):
    pass


class TestClassifyOpenaiError:
    """Unit tests for classify_openai_error()."""

    def test_rate_limit_transient(self):
        exc = FakeRateLimitError(
            message="rate limit", status_code=429,
            body={"error": {"type": "rate_limit_exceeded", "message": "too fast"}},
        )
        result = classify_openai_error(exc)
        assert result.kind == LLMErrorKind.RATE_LIMIT
        assert result.should_retry is True
        assert result.status_code == 429

    def test_rate_limit_quota(self):
        exc = FakeRateLimitError(
            message="quota", status_code=429,
            body={"error": {"type": "insufficient_quota", "message": "out of credits"}},
        )
        result = classify_openai_error(exc)
        assert result.kind == LLMErrorKind.QUOTA
        assert result.should_retry is False

    def test_timeout(self):
        exc = FakeAPITimeoutError("timed out")
        result = classify_openai_error(exc)
        assert result.kind == LLMErrorKind.TIMEOUT
        assert result.should_retry is True

    def test_auth(self):
        exc = FakeAuthenticationError(status_code=401, message="bad key")
        result = classify_openai_error(exc)
        assert result.kind == LLMErrorKind.AUTH
        assert result.should_retry is False

    def test_server_error(self):
        exc = FakeInternalServerError(status_code=500, message="boom")
        result = classify_openai_error(exc)
        assert result.kind == LLMErrorKind.SERVER
        assert result.should_retry is True

    def test_unknown_api_status(self):
        """APIStatusError with 404 — not in known mapping."""
        exc = FakeAPIStatusError(status_code=404, message="not found")
        result = classify_openai_error(exc)
        assert result.kind == LLMErrorKind.UNKNOWN
        assert result.should_retry is False

    def test_string_fallback_connection(self):
        exc = ValueError("connection refused")
        result = classify_openai_error(exc)
        assert result.kind == LLMErrorKind.CONNECTION
        assert result.should_retry is True

    def test_string_fallback_auth(self):
        exc = RuntimeError("HTTP 401 Unauthorized")
        result = classify_openai_error(exc)
        assert result.kind == LLMErrorKind.AUTH
        assert result.should_retry is False

    def test_string_fallback_unknown(self):
        exc = Exception("something completely unexpected")
        result = classify_openai_error(exc)
        assert result.kind == LLMErrorKind.UNKNOWN
        assert result.should_retry is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/providers/test_shared_errors.py -v`
Expected: FAIL with ImportError

- [ ] **Step 3: Write `framework/providers/shared/errors.py`**

```python
"""Error classification for openai SDK exceptions.

classify_openai_error — isinstance-based classification with string-scan fallback.
No getattr/hasattr. All attribute access is on known openai exception types.
"""
from __future__ import annotations

import logging

from framework.core.llm_error import LLMErrorInfo, LLMErrorKind

logger = logging.getLogger(__name__)

try:
    from openai import (
        APIStatusError,
        APITimeoutError,
        APIConnectionError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        PermissionDeniedError,
        RateLimitError,
    )
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


def classify_openai_error(exc: Exception) -> LLMErrorInfo:
    """Classify an exception from openai SDK into structured LLMErrorInfo.

    Priority:
    1. isinstance matching on openai exception types → typed attribute access
    2. String-scan fallback for generic/non-openai exceptions

    Returns LLMErrorInfo with appropriate kind and should_retry.
    """
    message = str(exc)[:500]

    if HAS_OPENAI:
        # Tier 1: openai SDK typed exceptions
        if isinstance(exc, APITimeoutError):
            return LLMErrorInfo(LLMErrorKind.TIMEOUT, message, "openai", should_retry=True)

        if isinstance(exc, APIConnectionError):
            return LLMErrorInfo(LLMErrorKind.CONNECTION, message, "openai", should_retry=True)

        if isinstance(exc, InternalServerError):
            return LLMErrorInfo(LLMErrorKind.SERVER, message, "openai",
                                status_code=exc.status_code, should_retry=True)

        if isinstance(exc, RateLimitError):
            return _classify_rate_limit(exc, message)

        if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
            return LLMErrorInfo(LLMErrorKind.AUTH, message, "openai",
                                status_code=exc.status_code, should_retry=False)

        if isinstance(exc, BadRequestError):
            return LLMErrorInfo(LLMErrorKind.INVALID_REQUEST, message, "openai",
                                status_code=exc.status_code, should_retry=False)

        # Any other APIStatusError — extract body for structured details
        if isinstance(exc, APIStatusError):
            return _classify_api_status(exc, message)

    # Tier 2: String-scan fallback for non-openai exceptions
    return _classify_by_string(str(exc).lower(), message)


def _classify_rate_limit(exc, message: str) -> LLMErrorInfo:
    """Handle RateLimitError with quota vs. transient distinction."""
    body = exc.body
    if isinstance(body, dict):
        error_type = body.get("error", {}).get("type", "")
        if error_type and ("quota" in error_type.lower() or "insufficient" in error_type.lower()):
            return LLMErrorInfo(
                LLMErrorKind.QUOTA, message, "openai", exc.status_code, should_retry=False
            )
    return LLMErrorInfo(
        LLMErrorKind.RATE_LIMIT, message, "openai", exc.status_code, should_retry=True
    )


def _classify_api_status(exc, message: str) -> LLMErrorInfo:
    """Classify generic APIStatusError by status code and body."""
    status_code = exc.status_code
    body = exc.body
    if isinstance(body, dict):
        error_type = body.get("error", {}).get("type", "")
        if error_type and status_code == 429:
            if error_type == "insufficient_quota" or "quota" in error_type.lower():
                return LLMErrorInfo(LLMErrorKind.QUOTA, message, "openai", status_code, should_retry=False)
            return LLMErrorInfo(LLMErrorKind.RATE_LIMIT, message, "openai", status_code, should_retry=True)

    if status_code in (401, 403):
        return LLMErrorInfo(LLMErrorKind.AUTH, message, "openai", status_code, should_retry=False)
    if 500 <= status_code < 600:
        return LLMErrorInfo(LLMErrorKind.SERVER, message, "openai", status_code, should_retry=True)
    if status_code == 429:
        return LLMErrorInfo(LLMErrorKind.RATE_LIMIT, message, "openai", status_code, should_retry=True)

    return LLMErrorInfo(LLMErrorKind.UNKNOWN, message, "openai", status_code, should_retry=False)


def _classify_by_string(text: str, message: str) -> LLMErrorInfo:
    """String-scan fallback for non-openai exceptions."""
    if "timeout" in text or "timed out" in text:
        return LLMErrorInfo(LLMErrorKind.TIMEOUT, message, "openai", should_retry=True)

    if "rate" in text and "limit" in text:
        if "quota" in text or "insufficient" in text:
            return LLMErrorInfo(LLMErrorKind.QUOTA, message, "openai", should_retry=False)
        return LLMErrorInfo(LLMErrorKind.RATE_LIMIT, message, "openai", should_retry=True)

    if "auth" in text or "401" in text or "403" in text:
        return LLMErrorInfo(LLMErrorKind.AUTH, message, "openai", should_retry=False)

    if any(code in text for code in ("500", "502", "503", "504")):
        return LLMErrorInfo(LLMErrorKind.SERVER, message, "openai", should_retry=True)

    if "connection" in text or "connect" in text:
        return LLMErrorInfo(LLMErrorKind.CONNECTION, message, "openai", should_retry=True)

    return LLMErrorInfo(LLMErrorKind.UNKNOWN, message, "openai", should_retry=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/providers/test_shared_errors.py -v`
Expected: all 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/providers/shared/errors.py tests/unit/providers/test_shared_errors.py
git commit -m "feat(providers): add classify_openai_error for structured error extraction"
```

---

### Task 4: Create `OpenAIProvider`

**Files:**
- Create: `framework/providers/openai_provider.py`
- Create: `tests/unit/providers/test_openai_provider.py`

- [ ] **Step 1: Write the failing test (chat non-streaming)**

Create `tests/unit/providers/test_openai_provider.py`:

```python
"""Tests for framework.providers.openai_provider."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.core.constants import FinishReason
from framework.core.llm_error import (
    LLMErrorInfo,
    LLMErrorKind,
    LLMTimeoutPolicy,
    RuntimeSafetyPolicy,
    TurnTimeoutPolicy,
)
from framework.core.types import LLMResponse, ToolCall
from framework.providers.openai_provider import OpenAIProvider
from framework.providers.shared.delta import ParsedResponse, StreamDelta


def _make_mock_chat_completion(content="hello", tool_calls=None, finish_reason="stop",
                                reasoning_content=None, usage_tokens=(100, 50, 150)):
    """Build a mock ChatCompletion response with the right attributes."""
    tc_list = []
    if tool_calls:
        for tc in tool_calls:
            func = MagicMock()
            func.name = tc["name"]
            func.arguments = tc["arguments"]
            m = MagicMock()
            m.id = tc["id"]
            m.function = func
            tc_list.append(m)

    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tc_list if tc_list else None
    msg.model_extra = {"reasoning_content": reasoning_content} if reasoning_content else None

    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason

    usage = MagicMock()
    usage.prompt_tokens = usage_tokens[0]
    usage.completion_tokens = usage_tokens[1]
    usage.total_tokens = usage_tokens[2]

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


class TestOpenAIProviderChat:
    """Unit tests for OpenAIProvider.chat()."""

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=30),
            turn=TurnTimeoutPolicy(),
        )
        with patch("framework.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    @pytest.mark.asyncio
    async def test_chat_returns_content(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_make_mock_chat_completion(content="Hello, world!")
        )

        result = await provider.chat(messages=[{"role": "user", "content": "hi"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello, world!"
        assert result.finish_reason == "stop"
        assert result.usage == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}

    @pytest.mark.asyncio
    async def test_chat_with_tool_calls(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_make_mock_chat_completion(
                content=None,
                tool_calls=[{"id": "c1", "name": "search", "arguments": '{"query": "test"}'}],
                finish_reason="tool_calls",
            )
        )

        result = await provider.chat(messages=[{"role": "user", "content": "search"}])

        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {"query": "test"}
        assert result.finish_reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_chat_with_reasoning(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_make_mock_chat_completion(
                content="answer", reasoning_content="step by step..."
            )
        )

        result = await provider.chat(messages=[{"role": "user", "content": "?"}])
        assert result.reasoning_content == "step by step..."

    @pytest.mark.asyncio
    async def test_chat_error_returns_error_response(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            side_effect=Exception("connection refused")
        )

        result = await provider.chat(messages=[{"role": "user", "content": "hi"}])

        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info is not None
        assert result.error_info.kind == LLMErrorKind.CONNECTION

    @pytest.mark.asyncio
    async def test_chat_passes_parameters_correctly(self, provider):
        provider._client.chat.completions.create = AsyncMock(
            return_value=_make_mock_chat_completion(content="ok")
        )

        await provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            temperature=0.3,
            max_tokens=500,
            tools=[{"type": "function", "function": {"name": "t1", "parameters": {}}}],
        )

        call_kwargs = provider._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o-mini"
        assert call_kwargs["temperature"] == 0.3
        assert call_kwargs["max_tokens"] == 500
        assert len(call_kwargs["tools"]) == 1
        assert call_kwargs["stream"] is False


class TestOpenAIProviderChatStream:
    """Unit tests for OpenAIProvider.chat_stream()."""

    @pytest.fixture
    def provider(self):
        safety = RuntimeSafetyPolicy(
            llm=LLMTimeoutPolicy(request_timeout_seconds=10, stream_idle_timeout_seconds=0.1),
            turn=TurnTimeoutPolicy(),
        )
        with patch("framework.providers.openai_provider.AsyncOpenAI") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            p = OpenAIProvider(model="gpt-4o", api_key="sk-test", safety=safety)
            p._client = mock_client
            yield p

    def _make_chunk(self, content=None, finish_reason=None, reasoning=None, usage=None):
        """Build a mock ChatCompletionChunk."""
        delta = MagicMock()
        delta.content = content
        delta.tool_calls = None
        delta.model_extra = {"reasoning_content": reasoning} if reasoning else None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = usage
        return chunk

    async def _stream_chunks(self, chunks):
        for c in chunks:
            yield c

    @pytest.mark.asyncio
    async def test_chat_stream_content(self, provider):
        chunks = [
            self._make_chunk(content="Hello"),
            self._make_chunk(content=" world"),
            self._make_chunk(content="!", finish_reason="stop"),
        ]
        provider._client.chat.completions.create = MagicMock(
            return_value=self._stream_chunks(chunks)
        )

        deltas = []
        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            on_content_delta=lambda d: deltas.append(d),
        )

        assert result.content == "Hello world!"
        assert result.finish_reason == "stop"
        assert deltas == ["Hello", " world", "!"]

    @pytest.mark.asyncio
    async def test_chat_stream_with_reasoning(self, provider):
        chunks = [
            self._make_chunk(reasoning="let me think..."),
            self._make_chunk(content="42", finish_reason="stop"),
        ]
        provider._client.chat.completions.create = MagicMock(
            return_value=self._stream_chunks(chunks)
        )

        reasoning_parts = []
        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "?"}],
            on_reasoning_delta=lambda d: reasoning_parts.append(d),
        )

        assert result.content == "42"
        assert result.reasoning_content == "let me think..."
        assert reasoning_parts == ["let me think..."]

    @pytest.mark.asyncio
    async def test_chat_stream_with_tool_calls(self, provider):
        func = MagicMock()
        func.name = "search"
        func.arguments = '{"query": "x"}'

        tc = MagicMock()
        tc.index = 0
        tc.id = "call_x"
        tc.function = func

        delta = MagicMock()
        delta.content = None
        delta.tool_calls = [tc]
        delta.model_extra = None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = "tool_calls"

        chunk = MagicMock()
        chunk.choices = [choice]
        chunk.usage = None

        provider._client.chat.completions.create = MagicMock(
            return_value=self._stream_chunks([chunk])
        )

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "search"}],
        )

        assert result.has_tool_calls
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[0].arguments == {"query": "x"}

    @pytest.mark.asyncio
    async def test_chat_stream_idle_timeout(self, provider):
        """When stream produces nothing for too long, returns error response."""

        async def _slow_stream():
            # yield nothing — simulates a stalled stream
            if False:
                yield

        provider._client.chat.completions.create = MagicMock(
            return_value=_slow_stream()
        )

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
        )

        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info.kind == LLMErrorKind.TIMEOUT

    @pytest.mark.asyncio
    async def test_chat_stream_handles_empty_choices(self, provider):
        """Chunks with empty choices list should be skipped."""
        chunk_empty = MagicMock()
        chunk_empty.choices = []

        chunk_content = self._make_chunk(content="data", finish_reason="stop")

        provider._client.chat.completions.create = MagicMock(
            return_value=self._stream_chunks([chunk_empty, chunk_content])
        )

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
        )

        assert result.content == "data"

    @pytest.mark.asyncio
    async def test_chat_stream_error(self, provider):
        provider._client.chat.completions.create = MagicMock(
            side_effect=Exception("connection refused")
        )

        result = await provider.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
        )

        assert result.finish_reason == FinishReason.ERROR.value
        assert result.error_info.kind == LLMErrorKind.CONNECTION
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/providers/test_openai_provider.py -v`
Expected: FAIL with ImportError (OpenAIProvider not defined)

- [ ] **Step 3: Write `framework/providers/openai_provider.py`**

```python
"""OpenAI Provider — native openai SDK integration.

Uses openai.AsyncOpenAI for Chat Completions API.
All response parsing goes through shared intermediate types
(StreamDelta, ParsedResponse) — no hasattr/getattr, no bare dicts.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI
import httpx

from framework.core.constants import FinishReason
from framework.core.llm_error import (
    LLMErrorInfo,
    LLMErrorKind,
    RuntimeSafetyPolicy,
    build_timeout_response,
)
from framework.core.provider import StreamingLLMProvider
from framework.core.tool_call_accumulator import ToolCallAccumulator
from framework.core.types import LLMResponse
from framework.providers.shared.delta import ParsedResponse, StreamDelta
from framework.providers.shared.errors import classify_openai_error
from framework.utils.think_tag import ThinkTagExtractor

logger = logging.getLogger(__name__)


class OpenAIProvider(StreamingLLMProvider):
    """LLM provider using the openai official SDK.

    Supports:
    - Full Chat Completions API (streaming + non-streaming)
    - Custom base_url (proxies/gateways)
    - Custom headers (extra_headers)
    - reasoning_content extraction (via Pydantic model_extra)

    Example:
        provider = OpenAIProvider(model="gpt-4o", api_key="sk-...")
        response = await provider.chat([{"role": "user", "content": "Hello"}])
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        timeout: float = 45.0,
        stream_idle_timeout: float = 90.0,
        parse_think_tags: bool = False,
        reasoning_effort: str | None = None,
        extra_headers: dict[str, str] | None = None,
        safety: RuntimeSafetyPolicy | None = None,
    ):
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._extra_headers = extra_headers
        self._think_extractor = ThinkTagExtractor() if parse_think_tags else None

        if safety is not None:
            self._timeout = safety.llm.request_timeout_seconds
            self._stream_idle_timeout = safety.llm.stream_idle_timeout_seconds
        else:
            self._timeout = timeout
            self._stream_idle_timeout = stream_idle_timeout

        retry_backoff = (
            safety.llm.retry_backoff_seconds
            if safety is not None
            else (2.0, 8.0)
        )
        super().__init__(retry_backoff_seconds=retry_backoff)

        self._client = AsyncOpenAI(
            api_key=api_key or "not-needed",
            base_url=base_url,
            default_headers=extra_headers,
            timeout=httpx.Timeout(self._timeout),
            max_retries=0,
        )

    def get_default_model(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self.chat_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            **kwargs,
        )

    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 1,
        **kwargs,
    ) -> LLMResponse:
        return await self._execute_with_retry(
            self._chat_raw, messages, max_retries,
            model=model, temperature=temperature, max_tokens=max_tokens,
            tools=tools, **kwargs
        )

    async def _chat_raw(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        params = self._build_params(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, tools=tools, stream=False, **kwargs,
        )
        t0 = time.monotonic()
        logger.debug("OpenAI chat start: model=%s", params["model"])

        try:
            response = await self._client.chat.completions.create(**params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            error_info = classify_openai_error(exc)
            logger.warning(
                "OpenAI chat failed: kind=%s elapsed=%.0fms message=%s",
                error_info.kind.value, elapsed_ms, error_info.message[:200],
            )
            return LLMResponse(
                content=f"Error calling LLM: {error_info.message}",
                finish_reason=FinishReason.ERROR.value,
                error=error_info.message,
                error_info=error_info,
            )

        parsed = ParsedResponse.from_openai(response)

        # ThinkTag fallback for non-streaming response
        content = parsed.content or ""
        reasoning = parsed.reasoning_content
        if self._think_extractor and reasoning is None:
            clean_content, extracted_reasoning = ThinkTagExtractor.extract(content)
            content = clean_content
            reasoning = extracted_reasoning

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "OpenAI chat done: model=%s finish=%s elapsed=%.0fms",
            params["model"], parsed.finish_reason, elapsed_ms,
        )

        return LLMResponse(
            content=content,
            tool_calls=parsed.tool_calls,
            reasoning_content=reasoning,
            finish_reason=parsed.finish_reason,
            usage=parsed.usage,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self.chat_stream_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
            **kwargs,
        )

    async def chat_stream_with_retry(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        max_retries: int = 0,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        return await self._execute_with_retry(
            self._chat_stream_raw, messages, max_retries,
            model=model, temperature=temperature, max_tokens=max_tokens,
            tools=tools,
            on_content_delta=on_content_delta,
            on_reasoning_delta=on_reasoning_delta,
            **kwargs
        )

    async def _chat_stream_raw(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
        **kwargs,
    ) -> LLMResponse:
        params = self._build_params(
            messages=messages, model=model, temperature=temperature,
            max_tokens=max_tokens, tools=tools, stream=True, **kwargs,
        )
        t0 = time.monotonic()
        logger.debug("OpenAI stream start: model=%s", params["model"])

        try:
            stream = await self._client.chat.completions.create(**params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = (time.monotonic() - t0) * 1000
            error_info = classify_openai_error(exc)
            logger.warning(
                "OpenAI stream failed: kind=%s elapsed=%.0fms message=%s",
                error_info.kind.value, elapsed_ms, error_info.message[:200],
            )
            return LLMResponse(
                content=f"Error calling LLM: {error_info.message}",
                finish_reason=FinishReason.ERROR.value,
                error=error_info.message,
                error_info=error_info,
            )

        accumulator = ToolCallAccumulator()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, int] = {}
        has_native_reasoning = False

        iterator = stream.__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=self._stream_idle_timeout,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    await stream.close()
                partial_content = "".join(content_parts)
                logger.warning(
                    "OpenAI stream idle timeout after %.1fs, partial_content_len=%d",
                    self._stream_idle_timeout, len(partial_content),
                )
                return build_timeout_response(
                    provider="openai",
                    message="LLM stream idle timeout",
                    partial_content=partial_content,
                )

            if not chunk.choices:
                continue

            delta = StreamDelta.from_openai(chunk.choices[0].delta)

            chunk_finish = chunk.choices[0].finish_reason
            if chunk_finish is not None:
                finish_reason = chunk_finish
            delta.finish_reason = finish_reason

            if delta.reasoning_content:
                has_native_reasoning = True
                reasoning_parts.append(delta.reasoning_content)
                await self._invoke_callback(on_reasoning_delta, delta.reasoning_content)

            if delta.content:
                if self._think_extractor and not has_native_reasoning:
                    clean_delta, extracted_reasoning = self._think_extractor.feed(delta.content)
                    if extracted_reasoning:
                        reasoning_parts.append(extracted_reasoning)
                        await self._invoke_callback(on_reasoning_delta, extracted_reasoning)
                    if clean_delta:
                        content_parts.append(clean_delta)
                        await self._invoke_callback(on_content_delta, clean_delta)
                else:
                    content_parts.append(delta.content)
                    await self._invoke_callback(on_content_delta, delta.content)

            if delta.tool_call_chunks:
                for tc_chunk in delta.tool_call_chunks:
                    accumulator.add_chunk(tc_chunk)

            if chunk.usage is not None:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }

        pending_tools = accumulator.flush_pending()
        all_tool_calls = accumulator.get_completed() + pending_tools

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "OpenAI stream done: model=%s finish=%s content_len=%d elapsed=%.0fms",
            params["model"], finish_reason, len("".join(content_parts)), elapsed_ms,
        )

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=all_tool_calls,
            reasoning_content="".join(reasoning_parts) if reasoning_parts else None,
            finish_reason=finish_reason or "stop",
            usage=usage,
        )

    @staticmethod
    async def _invoke_callback(callback: Callable[[str], Any] | None, value: str) -> None:
        if callback is None or not value:
            return
        result = callback(value)
        if asyncio.iscoroutine(result):
            await result

    def _build_params(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        stream: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model or self._model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self._temperature,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "stream": stream,
        }

        if self._reasoning_effort is not None:
            params["reasoning_effort"] = self._reasoning_effort

        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        if stream:
            params["stream_options"] = {"include_usage": True}

        if self._extra_headers:
            params["extra_headers"] = self._extra_headers

        params.update(kwargs)
        return params
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/providers/test_openai_provider.py -v`
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add framework/providers/openai_provider.py tests/unit/providers/test_openai_provider.py
git commit -m "feat(providers): add OpenAIProvider using openai SDK"
```

---

### Task 5: Update `framework/providers/__init__.py` to export `OpenAIProvider`

**Files:**
- Modify: `framework/providers/__init__.py`

- [ ] **Step 1: Update the file**

Replace content with:

```python
"""LLM Provider implementations."""

__all__: list[str] = []

try:
    from .litellm_provider import LiteLLMProvider
    __all__.append("LiteLLMProvider")
except ImportError:
    pass

try:
    from .openai_provider import OpenAIProvider
    __all__.append("OpenAIProvider")
except ImportError:
    pass
```

- [ ] **Step 2: Verify imports work**

Run: `python -c "from framework.providers import OpenAIProvider, LiteLLMProvider; print('OK')"`
Expected: prints `OK`

- [ ] **Step 3: Run the full test suite**

Run: `pytest tests/unit/providers/ -v`
Expected: all tests PASS (3 test files)

- [ ] **Step 4: Commit**

```bash
git add framework/providers/__init__.py
git commit -m "feat(providers): export OpenAIProvider from providers package"
```
