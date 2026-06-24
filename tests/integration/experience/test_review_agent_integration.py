from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.core.experience.meta import PerFileExperienceMetaStore
from modex_agent.core.provider import LLMProvider


@pytest.mark.integration
async def test_review_agent_noop_on_empty(tmp_path: Path):
    """Review agent returns True (success) for empty conversation."""
    from modex_agent.agents.experience.review_agent import ExperienceReviewAgent

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(return_value="Nothing to record.")

    agent = ExperienceReviewAgent(provider=provider)
    agent._run_agent = AsyncMock(return_value=True)

    meta_store = PerFileExperienceMetaStore(tmp_path)
    ok = await agent.review(
        conversation_snapshot="[user]: hello\n[assistant]: hi",
        experience_dir=tmp_path,
        meta_store=meta_store,
        invocation_id="test-001",
    )
    assert ok is True