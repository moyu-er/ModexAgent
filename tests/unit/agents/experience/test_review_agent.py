"""Tests for ExperienceReviewAgent."""
from pathlib import Path


def test_build_user_message_with_snapshot():
    from modex_agent.agents.experience.review_agent import ExperienceReviewAgent

    msg = ExperienceReviewAgent.build_user_message(
        conversation_snapshot="[user]: Hello\n[assistant]: Hi",
        existing_experiences="<experiences><experience name='test'/></experiences>",
    )
    assert "[user]: Hello" in msg
    assert "<experiences>" in msg


def test_build_user_message_without_experiences():
    from modex_agent.agents.experience.review_agent import ExperienceReviewAgent

    msg = ExperienceReviewAgent.build_user_message(
        conversation_snapshot="[user]: Hello",
    )
    assert "[user]: Hello" in msg


def test_build_system_prompt(tmp_path: Path):
    from modex_agent.agents.experience.review_agent import ExperienceReviewAgent

    prompt = ExperienceReviewAgent.build_system_prompt(tmp_path)
    assert "experience" in prompt.lower()
