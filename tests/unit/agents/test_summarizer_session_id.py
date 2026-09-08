"""Regression: batch summarizers must build SessionInfo without warning spam.

ArchiveSummarizer / CoreMemoryConsolidator run as standalone one-shot agents
with synthetic, human-readable session ids (``archive-summarizer-3``) that
carry NO ``.`` separator.  These ids are correct (deterministic, trace-
correlatable to archive_id) — but they must NOT flow through
``SessionInfo.from_str`` (which warns on missing separator and drops the
agent_name).  The agent already knows its name; construct ``SessionInfo``
directly.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from modex_agent.agents.summarizer.scoped_file_agent import ScopedFileAgent
from modex_agent.core.provider import LLMProvider


class _CapturingReactAgent:
    """Fake ReActAgent that records the AgentContext passed to run()."""

    def __init__(self) -> None:
        self.captured_session: Any = None

    async def run(self, context, emitter) -> Any:  # noqa: ANN001
        self.captured_session = context.session
        return MagicMock(content="done")


@pytest.mark.asyncio
async def test_run_agent_preserves_agent_name_for_separatorless_session_id(tmp_path: Path) -> None:
    """A synthetic session id like ``archive-summarizer-3`` must keep its
    agent_name and emit no UserWarning."""
    agent = ScopedFileAgent(provider=MagicMock(spec=LLMProvider), max_iterations=1)
    fake = _CapturingReactAgent()
    agent._react_agent = fake  # type: ignore[assignment]

    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        await agent._run_agent(
            system_prompt="",
            user_msg="hi",
            allowed_dirs=[tmp_path],
            session_id="archive-summarizer-3",
            agent_name="ArchiveSummarizer",
            trace_path=tmp_path / "trace.jsonl",
            max_iterations=1,
        )

    assert fake.captured_session is not None
    assert str(fake.captured_session) == "archive-summarizer-3"
    assert fake.captured_session.agent_name == "ArchiveSummarizer", (
        "agent_name lost: _run_agent must construct SessionInfo directly, not via from_str"
    )
