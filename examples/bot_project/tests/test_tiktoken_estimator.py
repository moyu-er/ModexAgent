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
