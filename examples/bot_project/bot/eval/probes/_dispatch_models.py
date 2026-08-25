"""Typed environment and CLI contracts for manual probe dispatch."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.eval.evalenv import LangfuseCredentials

_DEFAULT_LANGFUSE_HOST: Final = "http://localhost:3000"
_DEFAULT_DATASET_NAME: Final = "memory-probes-frozen-v1"
_DEFAULT_MINIMUM_CALL_RESERVE_USD: Final = 0.001
_DEFAULT_MAX_OUTPUT_TOKENS: Final = 2_000


class ProbeDispatchError(RuntimeError):
    """A bounded manual-dispatch precondition or runtime contract failure."""


class ProbeRunEnvironment(BaseModel):
    """Answer-model and Langfuse settings parsed from the process environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    langfuse_host: str = Field(min_length=1)
    langfuse_public_key: str = Field(min_length=1)
    langfuse_secret_key: str = Field(min_length=1)
    dataset_name: str = Field(min_length=1)
    minimum_call_reserve_usd: float = Field(gt=0)
    answer_max_output_tokens: int = Field(ge=1)

    @classmethod
    def from_mapping(cls, environment: Mapping[str, str]) -> ProbeRunEnvironment:
        """Resolve PROBE_RUN_* first and reuse TEST_LLM_* only as fallback."""
        credentials = LangfuseCredentials.from_env(environment)
        values = {
            "model": environment.get("PROBE_RUN_MODEL") or environment.get("TEST_LLM_MODEL", ""),
            "api_key": environment.get("PROBE_RUN_API_KEY")
            or environment.get("TEST_LLM_API_KEY", ""),
            "base_url": environment.get("PROBE_RUN_BASE_URL")
            or environment.get("TEST_LLM_BASE_URL", ""),
            "langfuse_host": (
                credentials.host
                if credentials is not None and credentials.host is not None
                else environment.get("LANGFUSE_HOST", _DEFAULT_LANGFUSE_HOST)
            ),
            "langfuse_public_key": credentials.public_key if credentials is not None else "",
            "langfuse_secret_key": credentials.secret_key if credentials is not None else "",
            "dataset_name": environment.get("PROBE_RUN_DATASET", _DEFAULT_DATASET_NAME),
            "minimum_call_reserve_usd": environment.get(
                "PROBE_RUN_MINIMUM_CALL_RESERVE_USD",
                str(_DEFAULT_MINIMUM_CALL_RESERVE_USD),
            ),
            "answer_max_output_tokens": environment.get(
                "PROBE_RUN_MAX_OUTPUT_TOKENS",
                str(_DEFAULT_MAX_OUTPUT_TOKENS),
            ),
        }
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ProbeDispatchError(f"invalid probe dispatch environment: {exc}") from exc


class ProbeDispatchOptions(BaseModel):
    """Validated CLI values consumed by the live dispatch module."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    library: Path
    manifest: Path
    run_name: str = Field(min_length=1)
    max_cost_usd: float = Field(gt=0)


__all__ = ["ProbeDispatchError", "ProbeDispatchOptions", "ProbeRunEnvironment"]
