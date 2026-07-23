"""Mechanism-B attachment path-reference injection (ADR-0013 §1/§10).

``TurnContextBuilder.preprocess`` appends a transient ``[Attachment: ...]``
reference for each gate-accepted inbound Attachment on the InputMessage so the
agent perceives the file and inspects it with tools. The injection enters the
agent LLM history (via ``assemble_context``), NOT the persisted transcript
user-message content (that is the regression assertion in the bot tree).

These unit tests target the preprocess contract directly.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.media.models import Attachment, AttachmentLocator, Kind
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.pipeline.turn_context_builder import (
    TurnContextBuilder,
    _attachment_reference,
    _human_byte_size,
)
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.workspace.runtime import bind_workspace_root, resolve_workspace_root

_ATT_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _make_builder(**overrides: Any) -> TurnContextBuilder:
    defaults: dict[str, Any] = dict(
        agent=MagicMock(name="agent"),
        tool_manager=MagicMock(name="tool_manager"),
        sanitizer=None,
        command_processor=None,
        skill_manager=None,
        context_builder=None,
        agent_descriptor=None,
        max_iterations=5,
        safety=MagicMock(name="safety"),
        runtime_services=None,
        runtime_context_manager=None,
        governance=None,
        hook_runner=None,
        interceptor_chain=None,
        control_channel=None,
        emitter_factory=None,
        output_adapter=MagicMock(spec=OutputAdapter),
        turn_store=None,
        registry=TurnSessionRegistry(),
    )
    defaults.update(overrides)
    return TurnContextBuilder(**defaults)


def _attachment(
    *,
    name: str = "cat.png",
    mime: str | None = "image/png",
    size: int = 12345,
    rel_path: str = "media/uploads/s1/cat.png",
) -> Attachment:
    return Attachment(
        id=_ATT_ID,
        kind=Kind.IMAGE,
        name=name,
        mime=mime,
        size=size,
        path=rel_path,
        locator=AttachmentLocator.MEDIA,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_human_byte_size_units() -> None:
    assert _human_byte_size(0) == "0B"
    assert _human_byte_size(512) == "512B"
    assert _human_byte_size(2048) == "2.0KB"
    assert _human_byte_size(int(2.3 * 1024 * 1024)) == "2.3MB"
    assert _human_byte_size(int(1.5 * 1024 * 1024 * 1024)) == "1.5GB"


def test_attachment_reference_resolves_workspace_relative_path() -> None:
    """The reference carries a tool-usable ABSOLUTE path resolved against the ws root."""
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            ref = _attachment_reference(_attachment(), resolve_workspace_root())
            expected_abs = (root / "media/uploads/s1/cat.png").resolve()

    # The path in the reference, parsed back, must equal the ws-root-resolved abs path.
    # (Compare as PurePath so forward-slash vs backslash differences do not matter.)
    ref_path_part = ref.split(" @ ", 1)[1].rstrip("]")
    assert Path(ref_path_part) == expected_abs, (
        f"reference path {ref_path_part!r} != expected {expected_abs}"
    )
    # Shape contract (ADR §1): name + mime + human_size + absolute_path; NO id.
    assert "[Attachment: cat.png (image/png, 12.1KB) @ " in ref
    assert _ATT_ID not in ref, "attachment_id must NOT leak"


def test_attachment_reference_uses_unknown_when_mime_missing() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            ref = _attachment_reference(_attachment(mime=None), resolve_workspace_root())
    assert "(unknown, 12.1KB)" in ref


# ---------------------------------------------------------------------------
# preprocess integration of the injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preprocess_injects_reference_for_resolved_attachments() -> None:
    """One reference line per resolved attachment; appended to sanitized content."""
    builder = _make_builder()
    input_msg = InputMessage(
        content="look at this",
        session=SessionInfo.from_str("s:main"),
        attachments_resolved=[
            _attachment(name="a.png"),
            _attachment(name="b.txt", mime="text/plain", size=100, rel_path="media/uploads/s1/b"),
        ],
    )

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            sanitized, media_blocks, media_processor = await builder.preprocess(
                input_msg, "s:main", {}, None
            )

    # The original content is preserved verbatim, with the injection appended.
    assert sanitized is not None
    assert sanitized.startswith("look at this\n")
    assert sanitized.count("[Attachment:") == 2
    assert "[Attachment: a.png (image/png, 12.1KB) @ " in sanitized
    assert "[Attachment: b.txt (text/plain, 100B) @ " in sanitized
    assert "look at this" in sanitized  # original content intact
    # Mechanism A is dormant in v1: no vision blocks are produced.
    assert media_blocks == []
    assert media_processor is None


@pytest.mark.asyncio
async def test_preprocess_no_injection_when_no_resolved_attachments() -> None:
    """Without resolved attachments the content is untouched (legacy envelopes)."""
    builder = _make_builder()
    input_msg = InputMessage(content="plain text", session=SessionInfo.from_str("s:main"))

    sanitized, media_blocks, media_processor = await builder.preprocess(
        input_msg, "s:main", {}, None
    )

    assert sanitized == "plain text"
    assert "[Attachment:" not in sanitized
    assert media_blocks == []
    assert media_processor is None


@pytest.mark.asyncio
async def test_preprocess_injection_does_not_carry_attachment_id() -> None:
    """The transient reference carries name/mime/size/path but NEVER the id."""
    builder = _make_builder()
    input_msg = InputMessage(
        content="hi",
        session=SessionInfo.from_str("s:main"),
        attachments_resolved=[_attachment()],
    )

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            sanitized, _, _ = await builder.preprocess(input_msg, "s:main", {}, None)

    assert sanitized is not None
    assert _ATT_ID not in sanitized
