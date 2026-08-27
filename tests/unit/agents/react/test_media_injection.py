"""Tests for ``inject_multimodal`` — the LLM-boundary ``media://`` resolver.

The persisted history carries only ``media://<attachment_id>`` references
(bytes live in the MediaStore); :func:`inject_multimodal` resolves those
references into data-URL parts on the LLM-bound copy, gated on model
modalities and bounded by the injection budget (count + decoded bytes,
oldest-first offload). Covers: resolution, modality gate, budget two-pass,
placeholder degradation (no store / missing bytes / corrupt bytes), the
resolver cache key ``(id(store), session_id, attachment_id)``, and
copy-on-write passthrough for parts-free messages.
"""

from __future__ import annotations

import base64
import logging
import weakref
from pathlib import Path
from typing import Any

import pytest

import modex_agent.agents.react.media_injection as media_injection
from modex_agent.agents.react.media_injection import inject_multimodal
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.history import ListMessageHistory
from modex_agent.core.message import (
    ChatMessage,
    ImageUrl,
    ImageUrlPart,
    TextPart,
    build_media_ref,
)
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import MessageRole
from modex_agent.media.store import LocalFileMediaStore, StoredMediaKind
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

_LOGGER_NAME = "modex_agent.agents.react.media_injection"

_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_CAPABLE = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))
_TEXT_ONLY = ModelCapabilities(modalities=frozenset({Modality.TEXT}))


def _make_png(width: int, height: int) -> bytes:
    """A real, decodable PNG of the given size (distinct byte lengths)."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(120, 40, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _make_runtime(
    capabilities: ModelCapabilities | None,
    store: LocalFileMediaStore | None,
) -> AgentRuntime:
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    services.model_info = (
        ModelInfo(model_name="test", capabilities=capabilities) if capabilities else None
    )
    services.media_store = store
    return AgentRuntime(services=services, state=state)


def _make_ctx(runtime: AgentRuntime, session: str = "test.agent") -> AgentContext:
    return AgentContext(
        system_prompt="sys",
        history=ListMessageHistory(),
        tool_manager=None,  # type: ignore[arg-type]
        session=SessionInfo.from_str(session),
        identity=runtime.state.identity,
        runtime=runtime,
    )


def _store_with(tmp_path: Path, entries: dict[str, bytes]) -> LocalFileMediaStore:
    store = LocalFileMediaStore(tmp_path / "media")
    sid = str(SessionInfo.from_str("test.agent"))
    for aid, data in entries.items():
        store.save(sid, aid, data, kind=StoredMediaKind.READS)
    return store


def _media_msg(aid: str, text: str = "look", role: MessageRole = MessageRole.USER) -> ChatMessage:
    return ChatMessage(
        role=role,
        content=[TextPart(text=text), ImageUrlPart(image_url=ImageUrl(url=build_media_ref(aid)))],
    )


@pytest.fixture(autouse=True)
def _clear_resolver_cache() -> Any:
    media_injection._RESOLVED_URL_CACHE.clear()
    yield
    media_injection._RESOLVED_URL_CACHE.clear()


# ─── Resolution ───────────────────────────────────────────────────────────────


class TestResolution:
    async def test_media_ref_resolved_to_data_url(self, tmp_path: Path) -> None:
        store = _store_with(tmp_path, {"aid-1": _PNG_BYTES})
        ctx = _make_ctx(_make_runtime(_CAPABLE, store))
        messages = [ChatMessage(role=MessageRole.USER, content="hi"), _media_msg("aid-1")]

        out = inject_multimodal(list(messages), ctx)

        # Parts-free message passes through as the SAME object (copy-on-write).
        assert out[0] is messages[0]
        resolved = out[1].content
        assert isinstance(resolved, list)
        assert isinstance(resolved[0], TextPart)
        assert resolved[0].text == "look"
        image = resolved[1]
        assert isinstance(image, ImageUrlPart)
        assert image.image_url.url.startswith("data:image/png;base64,")
        payload = image.image_url.url.split(",", 1)[1]
        assert base64.b64decode(payload) == _PNG_BYTES
        # The input message was not mutated (persisted history keeps the ref).
        assert messages[1].content[1].image_url.url == build_media_ref("aid-1")

    async def test_tool_message_media_ref_resolved(self, tmp_path: Path) -> None:
        store = _store_with(tmp_path, {"aid-1": _PNG_BYTES})
        ctx = _make_ctx(_make_runtime(_CAPABLE, store))
        messages = [
            _media_msg(
                "aid-1",
                text="[Image read: cat.png (image/png)]",
                role=MessageRole.TOOL,
            ),
        ]

        out = inject_multimodal(messages, ctx)

        resolved = out[0].content
        assert isinstance(resolved, list)
        assert isinstance(resolved[1], ImageUrlPart)
        assert resolved[1].image_url.url.startswith("data:image/png;base64,")

    async def test_uploads_subtree_resolved_too(self, tmp_path: Path) -> None:
        """resolve_bytes is kind-agnostic: an uploads-subtree id resolves."""
        store = LocalFileMediaStore(tmp_path / "media")
        sid = str(SessionInfo.from_str("test.agent"))
        store.save(sid, "up-1", _PNG_BYTES)  # default kind=UPLOADS
        ctx = _make_ctx(_make_runtime(_CAPABLE, store))

        out = inject_multimodal([_media_msg("up-1")], ctx)

        resolved = out[0].content
        assert isinstance(resolved, list)
        assert isinstance(resolved[1], ImageUrlPart)
        assert resolved[1].image_url.url.startswith("data:image/png;base64,")

    async def test_parts_free_messages_returned_unchanged(self) -> None:
        ctx = _make_ctx(_make_runtime(_CAPABLE, None))
        messages = [
            ChatMessage(role=MessageRole.SYSTEM, content="sys"),
            ChatMessage(role=MessageRole.USER, content="hi"),
        ]

        out = inject_multimodal(messages, ctx)

        assert out is messages

    async def test_data_url_and_text_parts_pass_through(self) -> None:
        ctx = _make_ctx(_make_runtime(_CAPABLE, None))
        data_url = "data:image/png;base64,aGVsbG8="
        msg = ChatMessage(
            role=MessageRole.USER,
            content=[TextPart(text="hi"), ImageUrlPart(image_url=ImageUrl(url=data_url))],
        )

        out = inject_multimodal([msg], ctx)

        assert out[0] is msg


# ─── Modality gate ────────────────────────────────────────────────────────────


class TestModalityGate:
    async def test_text_only_model_drops_image_parts_with_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = _store_with(tmp_path, {"aid-1": _PNG_BYTES})
        ctx = _make_ctx(_make_runtime(_TEXT_ONLY, store))

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            out = inject_multimodal([_media_msg("aid-1")], ctx)

        content = out[0].content
        assert isinstance(content, list)
        assert content == [TextPart(text="look")]
        assert any(
            "image part unsupported" in record.getMessage() for record in caplog.records
        )

    async def test_missing_model_info_treats_images_unsupported(
        self, tmp_path: Path
    ) -> None:
        store = _store_with(tmp_path, {"aid-1": _PNG_BYTES})
        ctx = _make_ctx(_make_runtime(None, store))

        out = inject_multimodal([_media_msg("aid-1")], ctx)

        content = out[0].content
        assert isinstance(content, list)
        assert content == [TextPart(text="look")]


# ─── Placeholder degradation ──────────────────────────────────────────────────


class TestPlaceholders:
    async def test_no_store_yields_placeholder_with_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ctx = _make_ctx(_make_runtime(_CAPABLE, None))

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            out = inject_multimodal([_media_msg("aid-1")], ctx)

        content = out[0].content
        assert isinstance(content, list)
        assert content == [TextPart(text="look"), TextPart(text="[media unavailable: aid-1]")]
        assert any("aid-1" in record.getMessage() for record in caplog.records)

    async def test_missing_bytes_yields_placeholder_with_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = _store_with(tmp_path, {"other": _PNG_BYTES})
        ctx = _make_ctx(_make_runtime(_CAPABLE, store))

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            out = inject_multimodal([_media_msg("aid-1")], ctx)

        content = out[0].content
        assert isinstance(content, list)
        assert content == [TextPart(text="look"), TextPart(text="[media unavailable: aid-1]")]

    async def test_corrupt_bytes_yield_placeholder_with_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = _store_with(tmp_path, {"aid-1": b"\x00\x01not-an-image"})
        ctx = _make_ctx(_make_runtime(_CAPABLE, store))

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            out = inject_multimodal([_media_msg("aid-1")], ctx)

        content = out[0].content
        assert isinstance(content, list)
        assert content == [TextPart(text="look"), TextPart(text="[media unavailable: aid-1]")]
        assert any("aid-1" in record.getMessage() for record in caplog.records)


# ─── Budget (two-pass, oldest-first offload) ─────────────────────────────────


class TestBudget:
    async def test_count_budget_offloads_oldest_first(self, tmp_path: Path) -> None:
        entries = {f"aid-{i}": _PNG_BYTES for i in range(9)}
        store = _store_with(tmp_path, entries)
        ctx = _make_ctx(_make_runtime(_CAPABLE, store))
        messages = [_media_msg(f"aid-{i}") for i in range(9)]

        out = inject_multimodal(messages, ctx)

        # Oldest (first in message order) is offloaded; the newest 8 resolve.
        first = out[0].content
        assert isinstance(first, list)
        assert first == [TextPart(text="look"), TextPart(text="[media offloaded: aid-0]")]
        image_urls = [
            part.image_url.url
            for msg in out[1:]
            for part in msg.content  # type: ignore[union-attr]
            if isinstance(part, ImageUrlPart)
        ]
        assert len(image_urls) == 8
        assert all(url.startswith("data:image/png;base64,") for url in image_urls)

    async def test_bytes_budget_offloads_oldest_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        small = _make_png(8, 8)
        big = _make_png(64, 64)
        assert len(big) > len(small)
        # Budget fits the small image alone but never big+small together.
        monkeypatch.setattr(media_injection, "_MAX_INJECTED_MEDIA_BYTES", len(small))
        store = _store_with(tmp_path, {"big": big, "small": small})
        ctx = _make_ctx(_make_runtime(_CAPABLE, store))
        messages = [_media_msg("big"), _media_msg("small")]

        out = inject_multimodal(messages, ctx)

        first = out[0].content
        assert isinstance(first, list)
        assert first == [TextPart(text="look"), TextPart(text="[media offloaded: big]")]
        second = out[1].content
        assert isinstance(second, list)
        assert isinstance(second[1], ImageUrlPart)
        assert second[1].image_url.url.startswith("data:image/png;base64,")


# ─── Resolver cache ───────────────────────────────────────────────────────────


class _CountingStore(LocalFileMediaStore):
    """Real store that counts ``resolve_bytes`` calls (cache observability)."""

    def __init__(self, media_dir: Path) -> None:
        super().__init__(media_dir)
        self.resolve_calls = 0

    def resolve_bytes(self, session_id: str, attachment_id: str) -> bytes | None:
        self.resolve_calls += 1
        return super().resolve_bytes(session_id, attachment_id)


class TestResolverCache:
    async def test_duplicate_refs_resolve_once(self, tmp_path: Path) -> None:
        store = _CountingStore(tmp_path / "media")
        sid = str(SessionInfo.from_str("test.agent"))
        store.save(sid, "aid-1", _PNG_BYTES, kind=StoredMediaKind.READS)
        ctx = _make_ctx(_make_runtime(_CAPABLE, store))
        messages = [_media_msg("aid-1"), _media_msg("aid-1")]

        out = inject_multimodal(messages, ctx)

        assert store.resolve_calls == 1
        for msg in out:
            content = msg.content
            assert isinstance(content, list)
            assert isinstance(content[1], ImageUrlPart)

    async def test_cache_keyed_by_session_and_store(self, tmp_path: Path) -> None:
        store = _CountingStore(tmp_path / "media")
        sid_a = str(SessionInfo.from_str("session-a"))
        sid_b = str(SessionInfo.from_str("session-b"))
        store.save(sid_a, "aid-1", _PNG_BYTES, kind=StoredMediaKind.READS)
        store.save(sid_b, "aid-1", _make_png(16, 16), kind=StoredMediaKind.READS)
        ctx_a = _make_ctx(_make_runtime(_CAPABLE, store), session="session-a")
        ctx_b = _make_ctx(_make_runtime(_CAPABLE, store), session="session-b")

        out_a1 = inject_multimodal([_media_msg("aid-1")], ctx_a)
        inject_multimodal([_media_msg("aid-1")], ctx_b)
        out_a2 = inject_multimodal([_media_msg("aid-1")], ctx_a)

        # Three inject calls, but only two resolutions: the second call for
        # session A hits the cache keyed (id(store), session, aid).
        assert store.resolve_calls == 2
        url_a1 = out_a1[0].content[1].image_url.url  # type: ignore[index,union-attr]
        url_a2 = out_a2[0].content[1].image_url.url  # type: ignore[index,union-attr]
        assert url_a1 == url_a2

    async def test_cache_survives_store_gc_without_id_confusion(
        self, tmp_path: Path
    ) -> None:
        """A dead store whose id() is reused by a new store must not hit."""
        store_1 = _CountingStore(tmp_path / "media-1")
        sid = str(SessionInfo.from_str("test.agent"))
        store_1.save(sid, "aid-1", _PNG_BYTES, kind=StoredMediaKind.READS)
        ctx_1 = _make_ctx(_make_runtime(_CAPABLE, store_1))
        inject_multimodal([_media_msg("aid-1")], ctx_1)

        dead_ref = weakref.ref(store_1)
        del store_1, ctx_1
        assert dead_ref() is None  # the first store is really gone

        fresh_bytes = _make_png(32, 32)
        store_2 = _CountingStore(tmp_path / "media-2")
        store_2.save(sid, "aid-1", fresh_bytes, kind=StoredMediaKind.READS)
        ctx_2 = _make_ctx(_make_runtime(_CAPABLE, store_2))

        out = inject_multimodal([_media_msg("aid-1")], ctx_2)

        image = out[0].content[1]  # type: ignore[index]
        assert isinstance(image, ImageUrlPart)
        payload = image.image_url.url.split(",", 1)[1]
        assert base64.b64decode(payload) == fresh_bytes
