"""Tests for RuntimeStateCodecRegistry — dispatch by AgentKind."""
from __future__ import annotations

import pytest

from framework.runtime.codec import RuntimeStateCodecRegistry, UnsupportedAgentKindError
from framework.runtime.enums import AgentKind


class _FakeCodec:
    agent_kind = AgentKind.REACT

    def encode_turn(self, snapshot):
        return {}

    def decode_turn(self, payload):
        return None


def test_codec_registry_dispatches_by_agent_kind() -> None:
    react_codec = _FakeCodec()
    registry = RuntimeStateCodecRegistry({AgentKind.REACT: react_codec})

    assert registry.get(AgentKind.REACT) is react_codec


def test_codec_registry_rejects_missing_agent_kind() -> None:
    registry = RuntimeStateCodecRegistry({})

    with pytest.raises(UnsupportedAgentKindError, match="react"):
        registry.get(AgentKind.REACT)
