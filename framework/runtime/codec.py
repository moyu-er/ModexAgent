"""Runtime state codec layer — schema versioning, enum serialization, payload validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .enums import AgentKind
from .models import JsonValue, TurnSnapshot


class RuntimeStateCodecError(Exception):
    """Raised when encoding/decoding fails for structural reasons."""


class UnsupportedAgentKindError(RuntimeStateCodecError):
    """Raised when no codec is registered for an agent kind."""


@dataclass(frozen=True)
class RuntimeStateCodecConfig:
    max_provider_payload_keys: int = 10


class RuntimeStateCodec:
    """Codec per agent kind. Shared helpers handle identity, enums, timestamps."""

    agent_kind: AgentKind

    def __init__(self, config: RuntimeStateCodecConfig | None = None) -> None:
        self._config = config or RuntimeStateCodecConfig()

    def encode_turn(self, snapshot: TurnSnapshot) -> Mapping[str, JsonValue]:
        raise NotImplementedError

    def decode_turn(self, payload: Mapping[str, JsonValue]) -> TurnSnapshot:
        raise NotImplementedError

    def _validate_provider_payload(self, provider_payload: Mapping[str, Any] | None) -> None:
        if provider_payload is not None and len(provider_payload) > self._config.max_provider_payload_keys:
            raise RuntimeStateCodecError(
                f"provider_payload has {len(provider_payload)} keys "
                f"(max {self._config.max_provider_payload_keys})"
            )

    @staticmethod
    def _serialize_enum(value: Any) -> str:
        if isinstance(value, StrEnum):
            return value.value
        return str(value)

    @staticmethod
    def _to_json_compatible(obj: Any) -> JsonValue:
        return json.loads(json.dumps(obj, default=str))


@dataclass
class RuntimeStateCodecRegistry:
    codecs: Mapping[AgentKind, RuntimeStateCodec]

    def get(self, agent_kind: AgentKind) -> RuntimeStateCodec:
        codec = self.codecs.get(agent_kind)
        if codec is None:
            raise UnsupportedAgentKindError(
                f"No codec registered for agent kind {agent_kind.value!r}"
            )
        return codec
