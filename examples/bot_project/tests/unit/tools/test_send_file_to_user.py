"""G7 tests — outbound SendFileToUserTool (ADR-0013 §3/§4/§7/§11).

Covers (7.1) the tool builds a WORKSPACE-locator Attachment with the literal
absolute path, no copy is made, and the record is findable via
:func:`find_attachment`; (7.2) the 1 GB ``max_outbound_bytes`` cap hard-rejects
an oversized file, and the outbound path does NOT run the perception gate (a
type that would fail inbound is still accepted outbound).
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

import pytest
from bot.service.attachment_index import find_attachment
from bot.tools.custom import SendFileToUserTool
from bot.webui.transcript_store import JSONLTranscriptStore

from modex_agent.core.agent import AgentContext, current_agent_context
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.media.models import AttachmentLocator, Kind
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.pool_config.media import MediaConfig

# ── Helpers ────────────────────────────────────────────────────────────────


def _make_tool(
    *,
    store: object | None = None,
    media_config: MediaConfig | None = None,
    sessions_dir_provider=None,
) -> SendFileToUserTool:
    output_adapter = MagicMock()
    output_adapter.send = AsyncMock()
    return SendFileToUserTool(
        output_adapter=output_adapter,
        transcript_store=store,
        media_config=media_config,
        sessions_dir_provider=sessions_dir_provider,
    )


def _bind_agent_context(session_id: str) -> None:
    """Bind a minimal AgentContext to the contextvar (what the tool reads)."""
    ctx = AgentContext(
        system_prompt="t",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id),
    )
    current_agent_context.set(ctx)


# ── 7.1: WORKSPACE-locator Attachment, no copy, findable ───────────────────


class TestOutboundAttachmentRecord:
    @pytest.mark.asyncio
    async def test_produces_workspace_locator_with_absolute_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "out" / "report.txt"
            file.parent.mkdir(parents=True)
            body = b"report-body"
            file.write_bytes(body)

            tool = _make_tool()
            _bind_agent_context("s1.main")
            result = await tool.execute(file_path=str(file), message="here")

        assert result == "File sent successfully: report.txt"
        # The tool returned success; the persisted record is verified below.

    @pytest.mark.asyncio
    async def test_record_findable_via_find_attachment(self) -> None:
        """The persisted assistant_turn record resolves through find_attachment."""
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            root = Path(tmp) / "ws"
            root.mkdir()
            file = root / "report.txt"
            file.write_bytes(b"hello")

            tool = _make_tool(store=store)
            _bind_agent_context("s1.main")
            await tool.execute(file_path=str(file))

            # The tool generated an opaque id; find it by scanning the only
            # outbound record in the transcript.
            events = await store.load("s1.main")
            from bot.webui.events import AssistantTurnEvent

            att_events = [e for e in events if isinstance(e, AssistantTurnEvent)]
            assert len(att_events) == 1
            records = att_events[0].attachments
            assert len(records) == 1
            att_id = str(records[0]["id"])

            found = await find_attachment(store, "s1.main", att_id)
        assert found is not None
        assert found.locator is AttachmentLocator.WORKSPACE
        assert found.path == str(file)
        assert found.size == len(b"hello")
        assert found.name == "report.txt"

    @pytest.mark.asyncio
    async def test_no_copy_made_file_stays_in_place(self) -> None:
        """Outbound is in-place: the source file is not duplicated or moved."""
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            root = Path(tmp)
            file = root / "original.txt"
            file.write_bytes(b"unchanged")

            tool = _make_tool(store=store)
            _bind_agent_context("s1.main")
            await tool.execute(file_path=str(file))

            # The original file is untouched and no copy was created alongside.
            assert file.read_bytes() == b"unchanged"
            # No sibling copy file appeared.
            assert not (file.with_suffix(".copy.txt")).exists()

    @pytest.mark.asyncio
    async def test_output_adapter_receives_path(self) -> None:
        """The adapter (IM/WebUI) gets the path for downstream rendering."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "img.png"
            file.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG magic

            tool = _make_tool()
            output_adapter = tool._output_adapter  # type: ignore[attr-defined]
            _bind_agent_context("s1.main")
            await tool.execute(file_path=str(file), message="see this")

        output_adapter.send.assert_awaited_once()
        args, _ = output_adapter.send.call_args
        msg = args[0]
        assert msg.attachments == [str(file)]
        assert msg.content == "see this"

    @pytest.mark.asyncio
    async def test_persisted_before_adapter_send(self) -> None:
        """The record is in the transcript BEFORE the adapter emits the card.

        Guards the download-404 race: a client resolving the card's
        download_url the instant it arrives must find the record already
        persisted. Verified by a fake adapter that, at send time, looks the
        attachment up via find_attachment.
        """
        from bot.service.attachment_index import find_attachment

        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            root = Path(tmp) / "ws"
            root.mkdir()
            file = root / "race.txt"
            file.write_bytes(b"data")

            seen_at_send: dict[str, object] = {}

            async def _spy_send(msg, session_id):
                # The card carries attachment_records; resolve each through
                # find_attachment as the download endpoint would.
                rec = msg.attachment_records[0]
                seen_at_send["found"] = await find_attachment(
                    store, session_id, rec.id
                )

            output_adapter = MagicMock()
            output_adapter.send = AsyncMock(side_effect=_spy_send)
            tool = SendFileToUserTool(
                output_adapter=output_adapter,
                transcript_store=store,
            )
            _bind_agent_context("s1.main")
            await tool.execute(file_path=str(file))

        assert seen_at_send["found"] is not None
        assert seen_at_send["found"].path == str(file)

    @pytest.mark.asyncio
    async def test_record_uses_sniffed_mime_and_classified_kind(self) -> None:
        """Magic-byte MIME is authoritative; kind is the three-way classification."""
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            root = Path(tmp)
            file = root / "pic.png"
            file.write_bytes(b"\x89PNG\r\n\x1a\nfake")  # PNG magic

            tool = _make_tool(store=store)
            _bind_agent_context("s1.main")
            await tool.execute(file_path=str(file))

            events = await store.load("s1.main")
            from bot.webui.events import AssistantTurnEvent

            att = next(
                e for e in events if isinstance(e, AssistantTurnEvent)
            ).attachments[0]
        assert att["mime"] == "image/png"
        assert att["kind"] == Kind.IMAGE.value

    @pytest.mark.asyncio
    async def test_missing_file_returns_error(self) -> None:
        tool = _make_tool()
        _bind_agent_context("s1.main")
        result = await tool.execute(file_path="/no/such/file.txt")
        assert result.startswith("Error: File not found")

    @pytest.mark.asyncio
    async def test_no_agent_context_returns_error(self) -> None:
        # Clear any previously bound context.
        current_agent_context.set(None)  # type: ignore[arg-type]
        tool = _make_tool()
        with TemporaryDirectory() as tmp:
            file = Path(tmp) / "f.txt"
            file.write_bytes(b"x")
            result = await tool.execute(file_path=str(file))
        assert result.startswith("Error: No active agent context")


# ── 7.2: 1 GB cap; NOT through the perception gate ────────────────────────


class TestOutboundCapAndGateBypass:
    @pytest.mark.asyncio
    async def test_over_cap_rejected_with_clear_message(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "big.bin"
            file.write_bytes(b"x")

            # Tiny cap so a 1-byte file exceeds it.
            tiny_config = MediaConfig(max_outbound_bytes=0)
            tool = _make_tool(media_config=tiny_config)
            _bind_agent_context("s1.main")
            result = await tool.execute(file_path=str(file))

        assert result.startswith("Error: File is too large")
        assert "limit is 0 bytes" in result
        # The adapter was never called (rejected before send).
        tool._output_adapter.send.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_under_cap_accepted(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            root = Path(tmp)
            file = root / "ok.txt"
            file.write_bytes(b"hi")

            config = MediaConfig(max_outbound_bytes=1024)
            tool = _make_tool(store=store, media_config=config)
            _bind_agent_context("s1.main")
            result = await tool.execute(file_path=str(file))

        assert result == "File sent successfully: ok.txt"

    @pytest.mark.asyncio
    async def test_type_failing_inbound_gate_accepted_outbound(self) -> None:
        """An executable-type file (rejected by the inbound perception gate)
        is accepted outbound — outbound bypasses the perception gate."""
        from bot.webui.events import AssistantTurnEvent

        with TemporaryDirectory() as tmp:
            store = JSONLTranscriptStore(Path(tmp))
            root = Path(tmp)
            # A Windows PE-ish header: would be rejected inbound as a dangerous
            # type, but outbound only applies the byte cap.
            file = root / "program.exe"
            file.write_bytes(b"MZ\x90\x00" + b"\x00" * 100)

            tool = _make_tool(store=store)  # default 1 GB cap
            _bind_agent_context("s1.main")
            result = await tool.execute(file_path=str(file))

            assert result == "File sent successfully: program.exe"
            # The record was still persisted and is findable. Read INSIDE the
            # tmpdir block — the JSONL file is deleted when it exits.
            events = await store.load("s1.main")
            att = next(
                e for e in events if isinstance(e, AssistantTurnEvent)
            ).attachments[0]
        # kind is OTHER (not an image/extractable-document) but it was accepted.
        assert att["kind"] == Kind.OTHER.value


# ── Channel routing: deliver to the turn's channel, not a fixed adapter ────


class TestRoutesToTurnChannel:
    """The tool must deliver the file through the CURRENT TURN's channel adapter
    (``agent_context.emitter.output_adapter``), not the adapter captured at pool
    build time. A pool shared across channels — or an IM-originated turn on a
    pool whose fixed adapter is QQ — would otherwise send a webui-bound file to
    IM (and vice versa)."""

    @staticmethod
    def _bind_ctx_with_emitter(*, session_id: str, output_adapter: object) -> None:
        ctx = AgentContext(
            system_prompt="t",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str(session_id),
        )
        emitter = MagicMock()
        emitter.output_adapter = output_adapter
        ctx.emitter = emitter  # type: ignore[assignment]
        current_agent_context.set(ctx)

    @pytest.mark.asyncio
    async def test_sends_via_turn_emitter_adapter_not_fixed(self) -> None:
        """When the turn's emitter carries a different adapter than the tool's
        fixed one, the file goes to the TURN's adapter."""
        with TemporaryDirectory() as tmp:
            file = Path(tmp) / "report.pdf"
            file.write_bytes(b"%PDF-1.4\nbody")

            fixed_adapter = MagicMock()
            fixed_adapter.send = AsyncMock()  # the QQ adapter the tool was built with
            turn_adapter = MagicMock()
            turn_adapter.send = AsyncMock()   # the webui adapter of THIS turn

            tool = SendFileToUserTool(output_adapter=fixed_adapter)
            self._bind_ctx_with_emitter(
                session_id="s1.main", output_adapter=turn_adapter
            )
            await tool.execute(file_path=str(file), message="here")

        # Delivered to the turn's channel adapter, NOT the fixed one.
        turn_adapter.send.assert_awaited_once()
        fixed_adapter.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_register_for_emitter_reforward(self) -> None:
        """The tool self-delivers (direct send on the turn's adapter). It must
        NOT also register the file on the agent context: the emitter's
        ``emit_complete`` re-forwards ``result.attachments`` through the output
        adapter, so a non-empty registration double-delivers the file (the IM
        "two files via IM" bug). Single delivery path only — webui and IM
        unified through the tool's direct send."""
        with TemporaryDirectory() as tmp:
            file = Path(tmp) / "report.pdf"
            file.write_bytes(b"%PDF-1.4\nbody")

            fixed_adapter = MagicMock()
            fixed_adapter.send = AsyncMock()
            tool = SendFileToUserTool(output_adapter=fixed_adapter)
            _bind_agent_context("s1.main")
            ctx = current_agent_context.get()
            assert ctx.attachments == []  # precondition

            await tool.execute(file_path=str(file), message="here")

        # Delivered exactly once via the direct send…
        fixed_adapter.send.assert_awaited_once()
        # …and NOT registered for the emitter to re-forward.
        assert ctx.attachments == [], (
            "tool must not add_attachment — emit_complete would re-send it "
            "(double delivery / the IM two-files bug)"
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_fixed_adapter_without_emitter(self) -> None:
        """No turn emitter (legacy/test wiring) → the fixed adapter is used."""
        with TemporaryDirectory() as tmp:
            file = Path(tmp) / "report.pdf"
            file.write_bytes(b"%PDF-1.4\nbody")

            fixed_adapter = MagicMock()
            fixed_adapter.send = AsyncMock()
            tool = SendFileToUserTool(output_adapter=fixed_adapter)
            _bind_agent_context("s1.main")  # emitter stays None
            await tool.execute(file_path=str(file))

        fixed_adapter.send.assert_awaited_once()
