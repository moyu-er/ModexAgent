"""Divergence tests for inline image attachment enrichment (Unit 5).

Verifies the core invariant of ADR-0013 / OpenSpec ``native-multimodal-inline``
unit 5: the persisted history NEVER carries base64 / ``image_url`` — only the
text-reference string. Base64 is injected into the transient messages list
(governance output) at the LLM call boundary, attached to the LAST user-role
message, and is NOT re-inlined on later turns once the attachment is gone.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from modex_agent.agents.react.nodes.llm import LLMNode, enrich_inline_attachments
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.governance import ContextGovernance
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.media.models import Attachment, AttachmentLocator, Kind
from modex_agent.memory.default_system import ScopedMessageHistory
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.core.scope import MemoryContext
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices

# Minimal valid 1x1 PNG (transparent) — real magic bytes so the renderer
# produces a real ``image_url`` block rather than a ``<missing>`` note.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

_CAPABLE = ModelCapabilities(modalities=frozenset({Modality.TEXT, Modality.IMAGE}))
_TEXT_ONLY = ModelCapabilities(modalities=frozenset({Modality.TEXT}))


def _make_attachment(path: str, name: str = "cat.png", att_id: str = "att-1") -> Attachment:
    return Attachment(
        id=att_id,
        kind=Kind.IMAGE,
        name=name,
        mime="image/png",
        size=len(_PNG_BYTES),
        path=path,
        locator=AttachmentLocator.WORKSPACE,
    )


def _make_runtime(capabilities: ModelCapabilities | None) -> AgentRuntime:
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("s1"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    services = AgentRuntimeServices()
    services.model_info = ModelInfo(model_name="test", capabilities=capabilities) if capabilities else None
    runtime = AgentRuntime(services=services, state=state)
    # Ticket 04: nodes route AOP through ``runtime.graph_runtime``. Tests that
    # bypass ``ReActAgent.run()`` must set it themselves.
    runtime.graph_runtime = ReactGraphRuntime()
    return runtime


def _scoped_history(tmp_path: Path) -> ScopedMessageHistory:
    """A real ScopedMessageHistory backed by a file store.

    Using the real history (not a mock) is load-bearing for assertions (a)/(c):
    the test must read back what was *persisted*, proving the enrichment never
    mutated storage — only the transient messages list.
    """
    registry = DefaultMemoryStoreRegistry(tmp_path)
    layer_set = MemoryLayerFactory.single_user(registry=registry)
    ctx = MemoryContext(session_id="s1", user_id="u1")
    return ScopedMessageHistory(manager=layer_set.session, context=ctx)


class TestEnrichmentDivergence:
    @pytest.mark.asyncio
    async def test_history_persists_text_reference_not_base64(self, tmp_path: Path) -> None:
        """(a) Persisted user content is the text-reference string, no base64."""
        img_path = tmp_path / "cat.png"
        img_path.write_bytes(_PNG_BYTES)
        att = _make_attachment(str(img_path))

        runtime = _make_runtime(_CAPABLE)
        runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = [att]

        history = _scoped_history(tmp_path)
        persisted_user_content = "here is a cat"
        await history.append({"role": "user", "content": persisted_user_content})

        ctx = AgentContext(
            system_prompt="sys",
            history=history,
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("test.agent"),
            identity=runtime.state.identity,
            runtime=runtime,
        )

        node = LLMNode.__new__(LLMNode)
        messages = await node._build_messages(ctx)

        # The enriched current-turn message has the image_url tail.
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) >= 1
        enriched = user_msgs[-1]
        assert isinstance(enriched["content"], list)
        assert any(part.get("type") == "image_url" for part in enriched["content"])

        # (a) The PERSISTED history read back is still the plain string — no
        # base64, no image_url, no list content ever reached storage.
        persisted = await history.to_list()
        persisted_user_msgs = [m for m in persisted if m.role == "user"]
        assert len(persisted_user_msgs) == 1
        assert persisted_user_msgs[0].content == persisted_user_content
        dumped = persisted_user_msgs[0].to_dict()
        assert dumped["content"] == persisted_user_content
        assert "image_url" not in str(dumped)
        assert "base64" not in str(dumped)

    @pytest.mark.asyncio
    async def test_enriched_content_is_text_part_plus_image_tail(self, tmp_path: Path) -> None:
        """(b) Enriched content = [text part (persisted verbatim)] + caption+image_url tail."""
        img_path = tmp_path / "cat.png"
        img_path.write_bytes(_PNG_BYTES)
        att = _make_attachment(str(img_path))

        runtime = _make_runtime(_CAPABLE)
        runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = [att]

        messages_in: list[dict[str, Any]] = [
            {"role": "user", "content": "look at this"},
        ]
        ctx = AgentContext(
            system_prompt="sys",
            history=_scoped_history(tmp_path),
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("test.agent"),
            identity=runtime.state.identity,
            runtime=runtime,
        )

        out = enrich_inline_attachments(messages_in, ctx)

        # Only one user message; its content is now a list.
        assert len(out) == 1
        content = out[0]["content"]
        assert isinstance(content, list)

        # First part: the persisted string verbatim, as a text part.
        assert content[0] == {"type": "text", "text": "look at this"}

        # Tail: caption text part + image_url part (the §4 block pair).
        assert len(content) == 3
        assert content[1] == {"type": "text", "text": "<image: cat.png>"}
        assert content[2]["type"] == "image_url"
        url = content[2]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        # base64 round-trips to the real file bytes.
        payload = url.split(",", 1)[1]
        assert base64.b64decode(payload) == _PNG_BYTES

    @pytest.mark.asyncio
    async def test_past_image_not_inlined_on_later_turn(self, tmp_path: Path) -> None:
        """(c) On a later turn (no attachments), the old image is NOT re-inlined."""
        img_path = tmp_path / "cat.png"
        img_path.write_bytes(_PNG_BYTES)

        # Turn 1: image attachment present, persisted user message references it.
        runtime_t1 = _make_runtime(_CAPABLE)
        att = _make_attachment(str(img_path))
        runtime_t1.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = [att]

        history = _scoped_history(tmp_path)
        await history.append({"role": "user", "content": "look at this cat"})
        await history.append({"role": "assistant", "content": "nice cat"})

        ctx_t1 = AgentContext(
            system_prompt="sys",
            history=history,
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("test.agent"),
            identity=runtime_t1.state.identity,
            runtime=runtime_t1,
        )
        node = LLMNode.__new__(LLMNode)
        msgs_t1 = await node._build_messages(ctx_t1)
        # Sanity: turn 1 did inline.
        t1_user = [m for m in msgs_t1 if m["role"] == "user"][-1]
        assert isinstance(t1_user["content"], list)

        # Turn 2: NEW turn state, NO attachments. A fresh user message arrives.
        runtime_t2 = _make_runtime(_CAPABLE)
        # No INLINE_ATTACHMENTS key set on the new turn state.
        await history.append({"role": "user", "content": "what else?"})

        ctx_t2 = AgentContext(
            system_prompt="sys",
            history=history,
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("test.agent"),
            identity=runtime_t2.state.identity,
            runtime=runtime_t2,
        )
        msgs_t2 = await node._build_messages(ctx_t2)

        user_msgs_t2 = [m for m in msgs_t2 if m["role"] == "user"]
        # The OLD user message ("look at this cat") must remain a plain string —
        # its image was a turn-1 concern and is not re-inlined on turn 2.
        old = [m for m in user_msgs_t2 if m["content"] == "look at this cat"]
        assert len(old) == 1
        assert isinstance(old[0]["content"], str)
        # The NEW user message is also a plain string (no attachment this turn).
        new = [m for m in user_msgs_t2 if m["content"] == "what else?"]
        assert len(new) == 1
        assert isinstance(new[0]["content"], str)

    @pytest.mark.asyncio
    async def test_single_user_message_attaches_image(self, tmp_path: Path) -> None:
        """(d) COMMON CASE: a single user message in the turn gets the image.

        v1 targets the LAST user-role message. The rare "second user message
        within a single turn" edge case (turn-identity binding) is ACCEPTED for
        v1 and intentionally NOT solved here — see ADR-0013 §10 notes.
        """
        img_path = tmp_path / "cat.png"
        img_path.write_bytes(_PNG_BYTES)
        att = _make_attachment(str(img_path))

        runtime = _make_runtime(_CAPABLE)
        runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = [att]

        history = _scoped_history(tmp_path)
        await history.append({"role": "user", "content": "describe this"})
        await history.append({"role": "assistant", "content": "ok"})

        ctx = AgentContext(
            system_prompt="sys",
            history=history,
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("test.agent"),
            identity=runtime.state.identity,
            runtime=runtime,
        )
        node = LLMNode.__new__(LLMNode)
        messages = await node._build_messages(ctx)

        user_msgs = [m for m in messages if m["role"] == "user"]
        # The single (last) user message is the one that received the tail.
        assert isinstance(user_msgs[-1]["content"], list)
        assert any(p.get("type") == "image_url" for p in user_msgs[-1]["content"])

    @pytest.mark.asyncio
    async def test_no_image_capability_leaves_content_unchanged(self, tmp_path: Path) -> None:
        """Gate: text-only model → no enrichment, content stays a string."""
        img_path = tmp_path / "cat.png"
        img_path.write_bytes(_PNG_BYTES)
        att = _make_attachment(str(img_path))

        runtime = _make_runtime(_TEXT_ONLY)
        runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = [att]

        messages_in: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
        ctx = AgentContext(
            system_prompt="sys",
            history=_scoped_history(tmp_path),
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("test.agent"),
            identity=runtime.state.identity,
            runtime=runtime,
        )
        out = enrich_inline_attachments(messages_in, ctx)
        assert out == messages_in
        assert out[0]["content"] == "hi"


class TestApprovalResumeFallback:
    @pytest.mark.asyncio
    async def test_resumed_turn_does_not_re_inline(self, tmp_path: Path) -> None:
        """Accepted v1 trade-off (ADR-0014 §3): an approval-resumed turn does
        NOT re-inline images, because ``ReActSnapshotPolicy.state_from_snapshot``
        does not restore ``state.custom`` (``INLINE_ATTACHMENTS`` /
        ``INLINE_IMAGE_CACHE`` are absent on the reconstructed state).

        This simulates the post-snapshot state: a FRESH ``ReActTurnState`` with
        EMPTY ``custom`` (no attachment carriers), even though the history still
        carries the image-bearing user message from before the interrupt. The
        resumed turn must fall back to mechanism B — i.e.
        ``enrich_inline_attachments`` returns the messages UNCHANGED (no
        ``image_url`` block is added). This test pins that fallback so a future
        change that silently restores ``custom`` is caught.
        """
        img_path = tmp_path / "cat.png"
        img_path.write_bytes(_PNG_BYTES)

        # Fresh state mimicking state_from_snapshot reconstruction: no
        # INLINE_ATTACHMENTS key set, empty custom — exactly what a resumed
        # turn sees.
        runtime = _make_runtime(_CAPABLE)
        # Deliberately do NOT set runtime.state.custom[INLINE_ATTACHMENTS].

        messages_in: list[dict[str, Any]] = [
            {"role": "user", "content": "look at this cat"},
        ]
        ctx = AgentContext(
            system_prompt="sys",
            history=_scoped_history(tmp_path),
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("test.agent"),
            identity=runtime.state.identity,
            runtime=runtime,
        )
        out = enrich_inline_attachments(messages_in, ctx)

        # Unchanged: no enrichment, no image_url block — mechanism B floor.
        assert out is messages_in or out == messages_in
        assert out[0]["content"] == "look at this cat"
        assert "image_url" not in str(out)


class TestEnrichmentGuard:
    @pytest.mark.asyncio
    async def test_governance_runs_before_enrichment_on_text_form(self, tmp_path: Path) -> None:
        """(5.3) Governance must see the TEXT form, never base64 / image_url.

        Behavioral guard: a governance stub records the content it observed.
        The recorded content must be the plain text string (no image_url),
        proving enrichment runs AFTER governance.
        """
        img_path = tmp_path / "cat.png"
        img_path.write_bytes(_PNG_BYTES)
        att = _make_attachment(str(img_path))

        runtime = _make_runtime(_CAPABLE)
        runtime.state.custom[TurnCustomKey.INLINE_ATTACHMENTS] = [att]

        history = _scoped_history(tmp_path)
        await history.append({"role": "user", "content": "look"})

        seen: list[Any] = []

        class _RecordingGovernance(ContextGovernance):
            async def apply(
                self, messages: list[dict[str, Any]], ctx: AgentContext
            ) -> list[dict[str, Any]]:
                seen.extend(messages)
                return messages

        # Ticket 04: governance routes through ``ReactGraphRuntime`` — set it
        # on ``graph_runtime`` (not ``runtime.services``) so the node sees it.
        runtime.graph_runtime = ReactGraphRuntime(governance=_RecordingGovernance())  # type: ignore[arg-type]

        ctx = AgentContext(
            system_prompt="sys",
            history=history,
            tool_manager=None,  # type: ignore[arg-type]
            session=SessionInfo.from_str("test.agent"),
            identity=runtime.state.identity,
            runtime=runtime,
        )
        node = LLMNode.__new__(LLMNode)
        out = await node._build_messages(ctx)

        # Governance observed the text form only.
        gov_user = [m for m in seen if m["role"] == "user"][-1]
        assert gov_user["content"] == "look"
        assert "image_url" not in str(gov_user)
        assert "base64" not in str(gov_user)

        # But the final output (post-enrichment) DOES carry the image_url.
        out_user = [m for m in out if m["role"] == "user"][-1]
        assert isinstance(out_user["content"], list)
        assert any(p.get("type") == "image_url" for p in out_user["content"])
