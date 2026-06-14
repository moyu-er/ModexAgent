"""Tests for workspace store rebase behavior.

These tests verify that transcript and relation stores can atomically switch
their backing directory (used during ``cd`` workspace switches) without leaking
cached state from the previous workspace.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from bot.service.session_relation_store import SessionRelationStore
from bot.service.web_ui_service import WebUIService
from bot.service.workspace_store import WorkspaceScopedTranscriptStore
from bot.webui.events import UserMessageEvent
from bot.webui.transcript_store import JSONLTranscriptStore


def test_transcript_store_rebase_switches_write_target() -> None:
    """After rebase, append() writes to the new directory, not the old one."""
    with tempfile.TemporaryDirectory() as tmp:
        base_a = Path(tmp) / "ws-a" / "sessions"
        base_b = Path(tmp) / "ws-b" / "sessions"

        store = WorkspaceScopedTranscriptStore(base_a, lambda: "")
        store.set_agent_pool_map({"main": "main"})

        sid = "conv.main"
        store.append(sid, UserMessageEvent(session_id=sid, agent_name="main", content="in-a"))

        assert (base_a / "main" / "conv.main.jsonl").exists()

        store.rebase(base_b)
        store.append(sid, UserMessageEvent(session_id=sid, agent_name="main", content="in-b"))

        # New write must land in workspace B.
        assert (base_b / "main" / "conv.main.jsonl").exists()
        events_b = list(JSONLTranscriptStore(base_b / "main").load(sid))
        assert len(events_b) == 1
        assert events_b[0].content == "in-b"

        # Workspace A must remain untouched.
        events_a = list(JSONLTranscriptStore(base_a / "main").load(sid))
        assert len(events_a) == 1
        assert events_a[0].content == "in-a"


def test_transcript_store_rebase_resets_session_list() -> None:
    """After rebase, list_sessions() scans the new directory, not the old one."""
    with tempfile.TemporaryDirectory() as tmp:
        base_a = Path(tmp) / "ws-a" / "sessions"
        base_b = Path(tmp) / "ws-b" / "sessions"

        store = WorkspaceScopedTranscriptStore(base_a, lambda: "")
        store.set_agent_pool_map({"main": "main"})

        sid_a = "conv-a.main"
        store.append(sid_a, UserMessageEvent(session_id=sid_a, agent_name="main", content="a"))
        assert sid_a in store.list_sessions()

        store.rebase(base_b)

        # Before any write in B, the session list must be empty.
        assert store.list_sessions() == set()

        sid_b = "conv-b.main"
        store.append(sid_b, UserMessageEvent(session_id=sid_b, agent_name="main", content="b"))
        assert store.list_sessions() == {sid_b}


def test_relation_store_rebase_switches_write_target() -> None:
    """After rebase, set_parent() writes to the new directory."""
    with tempfile.TemporaryDirectory() as tmp:
        base_a = Path(tmp) / "ws-a" / "sessions"
        base_b = Path(tmp) / "ws-b" / "sessions"

        rstore = SessionRelationStore(base_a)
        rstore.set_agent_pool_map({"main": "main"})

        rstore.set_parent("conv.main.child", "conv.main")
        assert (base_a / "main" / "_relations.json").exists()
        assert rstore.get_parent("conv.main.child") == "conv.main"

        rstore.rebase(base_b)

        # Reading before any write in B should not find the old relation.
        assert rstore.get_parent("conv.main.child") is None

        rstore.set_parent("conv-b.main.child", "conv-b.main")
        assert (base_b / "main" / "_relations.json").exists()
        assert rstore.get_parent("conv-b.main.child") == "conv-b.main"


def test_web_ui_service_update_session_stores_rebases_stores() -> None:
    """update_session_stores() must switch both transcript and relation stores."""
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        home_sessions = project_dir / ".modex" / "sessions"
        home_sessions.mkdir(parents=True)
        other_sessions = project_dir / "other" / ".modex" / "sessions"
        other_sessions.mkdir(parents=True)

        # Build a minimal service object without running the full __init__.
        service = object.__new__(WebUIService)
        service._transcript_store = WorkspaceScopedTranscriptStore(home_sessions, lambda: "")
        service._transcript_store.set_agent_pool_map({"main": "main"})
        service._relation_store = SessionRelationStore(home_sessions)
        service._server = None

        sid = "conv.main"
        service._transcript_store.append(
            sid, UserMessageEvent(session_id=sid, agent_name="main", content="home"),
        )
        assert (home_sessions / "main" / f"{sid}.jsonl").exists()

        WebUIService.update_session_stores(service, other_sessions.parent)

        # After rebase, writes go to the other workspace.
        service._transcript_store.append(
            sid, UserMessageEvent(session_id=sid, agent_name="main", content="other"),
        )
        assert (other_sessions / "main" / f"{sid}.jsonl").exists()
        events = list(JSONLTranscriptStore(other_sessions / "main").load(sid))
        assert len(events) == 1
        assert events[0].content == "other"

        # Relation store also follows the rebase.
        service._relation_store.set_parent("conv.main.child", "conv.main")
        assert (other_sessions / "main" / "_relations.json").exists()
