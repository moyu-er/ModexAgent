"""LoopDetectedError and AgentControlError attribute tests."""
import pytest

from modex_agent.control.exceptions import (
    AgentCancelledError,
    AgentControlError,
    AgentTimeoutError,
    LoopDetectedError,
    PolicyViolationError,
)
from modex_agent.core.constants import StopReason


def test_base_defaults():
    err = AgentControlError("boom")
    assert err.user_content == ""
    assert err.stop_reason == StopReason.CANCELLED


def test_cancelled_defaults():
    assert AgentCancelledError().stop_reason == StopReason.CANCELLED
    assert AgentCancelledError().user_content == ""


def test_timeout_stop_reason():
    assert AgentTimeoutError().stop_reason == StopReason.TIMEOUT


def test_policy_violation_stop_reason():
    assert PolicyViolationError().stop_reason == StopReason.ERROR


def test_loop_detected_carries_content_and_reason():
    err = LoopDetectedError(user_content="<loop_detected/>", loop_type="tool")
    assert err.user_content == "<loop_detected/>"
    assert err.loop_type == "tool"
    assert err.stop_reason == StopReason.LOOP_DETECTED
    assert isinstance(err, AgentControlError)


def test_loop_detected_message():
    err = LoopDetectedError(user_content="x", loop_type="content")
    assert "content" in str(err)
