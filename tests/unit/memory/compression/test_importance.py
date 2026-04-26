"""Tests for importance scorers."""

import pytest

from framework.memory.compression.importance import HeuristicImportanceScorer, ImportanceScorer


class TestHeuristicImportanceScorer:
    def test_system_message_max_score(self):
        scorer = HeuristicImportanceScorer()
        assert scorer.score({"role": "system", "content": "You are helpful."}) == 1.0

    def test_assistant_tool_calls_high_score(self):
        scorer = HeuristicImportanceScorer()
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "function": {"name": "tool_a"}}],
        }
        assert scorer.score(msg) == 0.9

    def test_tool_message_medium_score(self):
        scorer = HeuristicImportanceScorer()
        assert scorer.score({"role": "tool", "tool_call_id": "call_1", "content": "result"}) == 0.5

    def test_user_message_question_bump(self):
        scorer = HeuristicImportanceScorer()
        base = scorer.score({"role": "user", "content": "hello"})
        with_question = scorer.score({"role": "user", "content": "hello?"})
        assert with_question > base

    def test_user_message_length_bump(self):
        scorer = HeuristicImportanceScorer()
        short_score = scorer.score({"role": "user", "content": "hi"})
        long_score = scorer.score({"role": "user", "content": "a" * 100})
        assert long_score > short_score

    def test_filler_message_low_score(self):
        scorer = HeuristicImportanceScorer()
        for filler in ["ok", "thanks", "好的", "明白了"]:
            assert scorer.score({"role": "user", "content": filler}) == 0.2

    def test_score_batch(self):
        scorer = HeuristicImportanceScorer()
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "hello"},
        ]
        scores = scorer.score_batch(messages)
        assert len(scores) == 3
        assert scores[0] == 1.0
        assert scores[1] == 0.2
        assert scores[2] == 0.55

    def test_abc_cannot_instantiate(self):
        with pytest.raises(TypeError):
            ImportanceScorer()
