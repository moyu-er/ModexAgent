from bot.memory.token_estimator import TiktokenTokenEstimator


def test_tiktoken_estimator_text_positive() -> None:
    est = TiktokenTokenEstimator()
    assert est.estimate_text("hello world") > 0


def test_tiktoken_estimator_cjk() -> None:
    est = TiktokenTokenEstimator()
    assert est.estimate_text("你好") >= 2


def test_tiktoken_estimator_message_overhead() -> None:
    est = TiktokenTokenEstimator()
    msg = {"role": "user", "content": "hello"}
    est.estimate_message(msg)  # should not raise


def test_tiktoken_estimator_loads_offline_blob() -> None:
    est = TiktokenTokenEstimator()
    assert est._encoding.name == "cl100k_base"


def test_tiktoken_estimator_special_tokens_do_not_raise() -> None:
    # Regression (TB count-dataset-tokens crash): tool output echoing
    # tokenizer special tokens must be counted as plain text, never raise.
    # Iterates special_tokens_set (not a hand-picked list) so the full
    # cl100k_base set is covered: <|endoftext|>, <|endofprompt|>,
    # <|fim_prefix|>, <|fim_middle|>, <|fim_suffix|>.
    est = TiktokenTokenEstimator()
    assert est._encoding.special_tokens_set, "encoding must declare special tokens"
    for token in est._encoding.special_tokens_set:
        assert est.estimate_text(token) > 0
    text = "count <|endoftext|> tokens and <|endofprompt|> too"
    assert est.estimate_text(text) > est.estimate_text("count tokens and too")
    msg = {"role": "tool", "tool_call_id": "t1", "content": "<|endoftext|>"}
    assert est.estimate_message(msg) > 0
