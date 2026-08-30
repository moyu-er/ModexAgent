"""Tests for modex_agent.providers.http.sse — SSE frame parsing.

Canned in-memory byte chunks feed the parser (async generator, zero network).
Covers both frame families (data-only = OpenAI chat, event+data =
Responses/Anthropic), chunk-split UTF-8, CRLF, comments, [DONE] passthrough,
non-SSE JSON whole-read, and empty/comment-only streams.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError

from modex_agent.providers.http.sse import DONE_SENTINEL, SseFrame, sse_frames


def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    """In-memory byte-chunk stream — zero network."""

    async def _gen() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    return _gen()


async def _collect(frames: AsyncIterator[SseFrame]) -> list[SseFrame]:
    return [frame async for frame in frames]


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        # data-only frames (OpenAI chat shape): event is None.
        (
            [b'data: {"a": 1}\n\ndata: {"b": 2}\n\n'],
            [SseFrame(data='{"a": 1}'), SseFrame(data='{"b": 2}')],
        ),
        # event + data dual-line frame (Responses / Anthropic shape).
        (
            [b'event: response.output_text.delta\ndata: {"t": "hi"}\n\n'],
            [SseFrame(event="response.output_text.delta", data='{"t": "hi"}')],
        ),
        # multiple data lines join with a single newline.
        ([b"data: first\ndata: second\ndata: third\n\n"], [SseFrame(data="first\nsecond\nthird")]),
        # CRLF terminators on data lines and dispatch blank lines.
        ([b"data: x\r\n\r\n"], [SseFrame(data="x")]),
        # CRLF and LF terminators mixed within one event block.
        ([b"data: x\r\ndata: y\n\n"], [SseFrame(data="x\ny")]),
        # comment lines produce no frames.
        ([b": ping\n: keepalive\n\n"], []),
        # non-data/event fields (id, retry) are ignored.
        ([b"id: 42\nretry: 100\ndata: x\n\n"], [SseFrame(data="x")]),
        # an event name without any data line is discarded, not dispatched.
        ([b"event: only_name\n\n"], []),
        # [DONE] sentinel passes through as an ordinary frame.
        ([b"data: [DONE]\n\n"], [SseFrame(data=DONE_SENTINEL)]),
        # colon with no space is legal.
        ([b"data:x\n\n"], [SseFrame(data="x")]),
        # only one space after the colon is stripped.
        ([b"data:  x\n\n"], [SseFrame(data=" x")]),
        # event name with no space after the colon.
        ([b"event:foo\ndata:bar\n\n"], [SseFrame(event="foo", data="bar")]),
        # BOM on the first line is tolerated.
        ([b"\xef\xbb\xbfdata: x\n\n"], [SseFrame(data="x")]),
        # a single line split across arbitrary chunk boundaries.
        ([b"dat", b"a: he", b"llo\n", b"\n"], [SseFrame(data="hello")]),
        # an event block split across chunk boundaries.
        ([b"event: e\nda", b"ta: a\n", b"\n"], [SseFrame(event="e", data="a")]),
        # unterminated tail at EOF is truncation, not a dispatchable frame.
        ([b"data: x\n"], []),
        # a bare `data:` line with empty value still dispatches a frame.
        ([b"data:\n\n"], [SseFrame(data="")]),
        # the event name resets after each dispatch.
        (
            [b"event: e\ndata: a\n\ndata: b\n\n"],
            [SseFrame(event="e", data="a"), SseFrame(data="b")],
        ),
    ],
)
async def test_frame_shapes(chunks: list[bytes], expected: list[SseFrame]) -> None:
    assert await _collect(sse_frames(_stream(*chunks))) == expected


class TestSplitUtf8:
    """Chunk boundaries may fall mid multi-byte UTF-8 sequence."""

    async def test_multibyte_char_split_across_chunks(self) -> None:
        # "你" is a 3-byte UTF-8 sequence; split it 1 + 2 across chunks.
        chunks = [b"data: \xe4", b"\xbd\xa0\n\n"]
        frames = await _collect(sse_frames(_stream(*chunks)))
        assert frames == [SseFrame(data="你")]

    async def test_bom_split_across_chunks(self) -> None:
        # A BOM split across chunks still resolves before frame parsing.
        chunks = [b"\xef\xbb", b"\xbfdata: x\n\n"]
        frames = await _collect(sse_frames(_stream(*chunks)))
        assert frames == [SseFrame(data="x")]


class TestNonSseJsonBody:
    """A 200 body starting with `{` is read whole as a single frame."""

    async def test_single_chunk_json_body(self) -> None:
        body = b'{"error": {"message": "quota exceeded", "type": "insufficient_quota"}}'
        frames = await _collect(sse_frames(_stream(body)))
        assert frames == [SseFrame(data=body.decode())]

    async def test_json_body_split_across_chunks(self) -> None:
        chunks = [b'{"error": {"mes', b'sage": "x"}}']
        frames = await _collect(sse_frames(_stream(*chunks)))
        assert frames == [SseFrame(data='{"error": {"message": "x"}}')]

    async def test_json_body_signals_activity_before_frame(self) -> None:
        calls: list[int] = []

        def _count() -> None:
            calls.append(1)

        frames = await _collect(sse_frames(_stream(b'{"a": 1}'), on_activity=_count))
        assert len(frames) == 1
        assert len(calls) == 2  # one per received chunk, one before the frame


class TestEmptyStream:
    """Empty or comment-only bodies end naturally: no frames, no error."""

    async def test_no_chunks(self) -> None:
        assert await _collect(sse_frames(_stream())) == []

    async def test_only_empty_chunk(self) -> None:
        assert await _collect(sse_frames(_stream(b""))) == []

    async def test_comment_only_stream(self) -> None:
        chunks = [b": ping\n\n", b": keepalive\n\n"]
        assert await _collect(sse_frames(_stream(*chunks))) == []


class TestActivityCallback:
    """on_activity fires per received chunk and before each yielded frame."""

    async def test_activity_count_is_chunks_plus_frames(self) -> None:
        calls = 0

        def _count() -> None:
            nonlocal calls
            calls += 1

        chunks = [b"data: a\n\n", b"data: b\n\ndata: c\n\n"]
        frames = await _collect(sse_frames(_stream(*chunks), on_activity=_count))
        assert len(frames) == 3
        assert calls == 2 + 3

    async def test_comment_only_stream_still_signals_activity(self) -> None:
        calls = 0

        def _count() -> None:
            nonlocal calls
            calls += 1

        frames = await _collect(sse_frames(_stream(b": ping\n\n"), on_activity=_count))
        assert frames == []
        assert calls >= 1  # transport activity without any frame

    async def test_empty_stream_never_signals_activity(self) -> None:
        calls = 0

        def _count() -> None:
            nonlocal calls
            calls += 1

        assert await _collect(sse_frames(_stream(), on_activity=_count)) == []
        assert calls == 0


class TestDoneSentinel:
    async def test_done_sentinel_passthrough(self) -> None:
        chunks = [b'data: {"delta":"hi"}\n\n', b"data: [DONE]\n\n"]
        frames = await _collect(sse_frames(_stream(*chunks)))
        assert [frame.data for frame in frames] == ['{"delta":"hi"}', DONE_SENTINEL]
        assert DONE_SENTINEL == "[DONE]"
        assert frames[1].event is None


class TestSseFrameValueObject:
    def test_frozen(self) -> None:
        frame = SseFrame(data="x")
        with pytest.raises(ValidationError):
            frame.data = "y"  # type: ignore[misc]

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            SseFrame(data="x", id="1")  # type: ignore[call-arg]

    def test_event_defaults_to_none(self) -> None:
        assert SseFrame(data="x").event is None
