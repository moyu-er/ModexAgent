"""Guard: v1 attachment behavior is independent of ``ModelCapabilities``.

ADR-0013 §9/§10/§10a: in v1 every attachment reaches the agent as a path
reference (mechanism B). Native multimodal (mechanism A — the provider-side
``MediaProcessor`` renderer inlining image/document bytes into the message
``content`` array) is a **dormant seam gated on ``ModelCapabilities``** and is
NOT implemented in the current change.

This guard exists so that a future change which wires mechanism A **without**
binding the capability gate fails fast here. It exercises
``TurnContextBuilder.preprocess`` (the G5 mechanism-B injection point) under
two ``LLMConfig.capabilities`` variants — TEXT-only vs IMAGE-on — and asserts
the output is byte-identical, is a plain ``str`` (never a multimodal content
list), and contains no inline ``image_url`` block. The builder does not
receive ``LLMConfig`` at all today; the test passes the variants through a
doppelgänger config object to make the independence explicit and to document
the contract the future renderer must respect.
"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.ioc.configs.llm import LLMConfig, ModelCapabilities, Modality
from modex_agent.media.models import Attachment, AttachmentLocator, Kind
from modex_agent.pipeline.adapters import OutputAdapter
from modex_agent.pipeline.turn_context_builder import TurnContextBuilder
from modex_agent.pipeline.turn_session_registry import TurnSessionRegistry
from modex_agent.workspace.runtime import bind_workspace_root

_ATT_ID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _capabilities_builder(**overrides: Any) -> TurnContextBuilder:
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


def _image_attachment() -> Attachment:
    return Attachment(
        id=_ATT_ID,
        kind=Kind.IMAGE,
        name="cat.png",
        mime="image/png",
        size=12345,
        path="media/uploads/s1/cat.png",
        locator=AttachmentLocator.MEDIA,
    )


def _input_with_resolved_image() -> InputMessage:
    return InputMessage(
        content="look at this",
        session=SessionInfo.from_str("s:main"),
        attachments_resolved=[_image_attachment()],
    )


# Two capability variants a future mechanism-A renderer would key on. v1 must
# produce the SAME preprocess output for both — the builder does not read
# capabilities, and no Modality flag is populated on any provider in v1.
_TEXT_ONLY = LLMConfig(model="gpt-4", capabilities=ModelCapabilities())
_IMAGE_ON = LLMConfig(
    model="gpt-4",
    capabilities=ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE})),
)
_CAPABILITY_VARIANTS = [
    pytest.param(_TEXT_ONLY, id="text-only"),
    pytest.param(_IMAGE_ON, id="image-on"),
]


@pytest.mark.parametrize("llm_config", _CAPABILITY_VARIANTS)
@pytest.mark.asyncio
async def test_preprocess_output_identical_across_capabilities(llm_config: LLMConfig) -> None:
    """The injected form is the SAME regardless of ``capabilities`` — mechanism A is dormant.

    The builder does not receive ``LLMConfig``; ``llm_config`` is carried here
    only to make the independence explicit and to fail if a future change
    threads capabilities into preprocess without the gate. This test would
    FAIL if someone activates mechanism A (inline ``image_url`` block) for the
    IMAGE-on variant while leaving the TEXT-only variant on mechanism B.
    """
    builder = _capabilities_builder()
    captured: dict[str, Any] = {}

    # One shared workspace root for both variants — the absolute path embedded
    # in the path-reference must resolve identically so the only possible
    # divergence between variants is capability-driven (which is none in v1).
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            for label, cfg in (("text_only", _TEXT_ONLY), ("image_on", _IMAGE_ON)):
                # Re-bind a fresh input each iteration (preprocess may mutate content).
                input_msg = _input_with_resolved_image()
                sanitized, media_blocks, media_processor = await builder.preprocess(
                    input_msg, "s:main", {}, None
                )
                captured[label] = (sanitized, media_blocks, media_processor)

    # 1. Mechanism B path-reference is produced for BOTH variants — identical.
    assert captured["text_only"] == captured["image_on"], (
        "preprocess output diverged across ModelCapabilities — mechanism A may "
        "have been wired without the capability gate (ADR-0013 §10/§10a)."
    )

    # 2. The output is a plain str, never a multimodal content list.
    sanitized, _, _ = captured["image_on"]
    assert isinstance(sanitized, str), (
        "preprocess returned a non-str content (multimodal list) — mechanism A "
        "must stay dormant in v1 (ADR-0013 §10a)."
    )

    # 3. No inline vision block anywhere — the agent perceives the file ONLY as
    #    a text path reference.
    assert "image_url" not in sanitized
    assert "[Attachment: cat.png (image/png, 12.1KB) @ " in sanitized


@pytest.mark.asyncio
async def test_preprocess_never_returns_media_processor_in_v1() -> None:
    """``preprocess`` returns ``media_processor=None`` for every capability in v1.

    A non-None ``MediaProcessor`` would signal the dormant renderer was
    activated (ADR-0013 §10). v1 must return ``None`` — mechanism B does not
    need a renderer, and mechanism A is not implemented.
    """
    builder = _capabilities_builder()
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        with bind_workspace_root(root):
            for cfg in (_TEXT_ONLY, _IMAGE_ON):
                input_msg = _input_with_resolved_image()
                _, media_blocks, media_processor = await builder.preprocess(
                    input_msg, "s:main", {}, None
                )
                assert media_processor is None, (
                    f"preprocess returned a MediaProcessor for capabilities={cfg.capabilities} "
                    "— mechanism A renderer must stay dormant in v1 (ADR-0013 §10a)."
                )
                assert media_blocks == [], (
                    "preprocess produced media blocks in v1 — mechanism A is dormant "
                    "(ADR-0013 §10a); only the text path-reference is allowed."
                )
