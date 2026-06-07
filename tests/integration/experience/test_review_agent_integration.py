import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.experience.usage import ExperienceUsageTracker
from framework.core.provider import LLMProvider


@pytest.mark.integration
async def test_review_agent_noop_on_empty():
    """Review agent returns True (success) for empty conversation."""
    from framework.agents.experience.review_agent import ExperienceReviewAgent

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(return_value="Nothing to record.")

    agent = ExperienceReviewAgent(provider=provider)
    agent._run_agent = AsyncMock(return_value=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tracker = ExperienceUsageTracker(Path(tmpdir) / ".usage.json")
        ok = await agent.review(
            conversation_snapshot="[user]: hello\n[assistant]: hi",
            experience_dir=Path("/tmp/test-experiences"),
            tracker=tracker,
            invocation_id="test-001",
        )
        assert ok is True
