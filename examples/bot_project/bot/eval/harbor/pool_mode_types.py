from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.harbor.entry import EntryConfig, TaskResultArtifact, UsageArtifact
from bot.service.model_config import BotModelConfig, ModelCfg, ProviderCfg
from modex_agent.core.constants import InterfaceFormat
from modex_agent.core.session_id import SessionInfo
from modex_agent.plugins.abc import ComponentFactory
from modex_agent.trace.pricing import PriceBook, load_pricebook
from modex_agent.trace.store import SpanModel

DEFAULT_PROJECT_DIR: Final = Path("/opt/modex/examples/bot_project")
DEFAULT_POOL_NAME: Final = "coder"

type SpanExporter = Callable[[SpanModel], Awaitable[None]]


class PoolApprovalMode(StrEnum):
    OFF = "off"
    ON = "on"


class PoolModeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: EntryConfig
    pool_name: str = Field(default=DEFAULT_POOL_NAME, min_length=1)
    project_dir: Path = DEFAULT_PROJECT_DIR
    approval: PoolApprovalMode = PoolApprovalMode.OFF
    budget_environment: Mapping[str, str]

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> PoolModeConfig:
        return cls(
            entry=EntryConfig.from_environment(environment),
            pool_name=environment.get("MODEX_POOL_NAME", DEFAULT_POOL_NAME),
            project_dir=Path(environment.get("MODEX_BOT_PROJECT_DIR") or DEFAULT_PROJECT_DIR),
            approval=PoolApprovalMode(environment.get("MODEX_APPROVAL", PoolApprovalMode.OFF.value)),
            budget_environment=environment,
        )

    @property
    def data_dir(self) -> Path:
        return self.entry.output_dir / "pool-data"


class SubagentSessionMetrics(BaseModel):
    """Delegation evidence for one subagent session observed by the harness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    agent_name: str | None = None
    turn_count: int = Field(default=0, ge=0)


class PoolDelegationMetrics(BaseModel):
    """Multi-agent session metrics aggregated from harness-observed events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    main_session_id: str = Field(min_length=1)
    subagent_sessions: tuple[SubagentSessionMetrics, ...] = ()
    total_sessions: int = Field(default=1, ge=1)
    delegation_count: int = Field(default=0, ge=0)


class PoolUsageArtifact(UsageArtifact):
    spent_usd: float = 0.0
    delegation: PoolDelegationMetrics


class PoolTaskResultArtifact(TaskResultArtifact):
    pool_name: str
    spent_usd: float = 0.0
    child_sessions: tuple[str, ...] = ()


class PoolModeDependencies:
    def __init__(
        self,
        provider_factory: ComponentFactory | None = None,
        pricebook: PriceBook | None = None,
        span_exporter: SpanExporter | None = None,
    ) -> None:
        self.provider_factory = provider_factory
        self.pricebook = pricebook or load_pricebook(yml_path=None)
        self.span_exporter = span_exporter


def build_model_config(config: EntryConfig) -> BotModelConfig:
    provider_key, separator, model_name = config.model.partition("/")
    if not separator:
        provider_key, model_name = "harbor", config.model
    interface_format = (
        InterfaceFormat.ANTHROPIC
        if provider_key == "anthropic"
        else InterfaceFormat.OPENAI_COMPATIBLE
    )
    return BotModelConfig(
        default_provider=provider_key,
        default_model=model_name,
        max_context_tokens=config.max_context_tokens,
        providers=[
            ProviderCfg(
                key=provider_key,
                name=provider_key,
                api_key=config.api_key or "",
                base_url=config.base_url or "",
                interface_format=interface_format,
                models=[
                    ModelCfg(
                        name=model_name,
                        model=model_name,
                        temperature=config.temperature,
                        reasoning_effort=config.reasoning_effort,
                        max_output_tokens=config.max_output_tokens,
                    )
                ],
            )
        ],
    )


def build_delegation_metrics(
    main_session_id: str,
    child_sessions: Sequence[str],
    turn_counts: Mapping[str, int],
) -> PoolDelegationMetrics:
    """Aggregate harness-observed session events into delegation metrics.

    ``turn_counts`` maps session id to the number of harness emitters created
    for that session — one per agent turn. ``agent_name`` is the session id's
    agent segment; an id without one records ``None`` instead of a guess.
    """
    subagents = tuple(
        SubagentSessionMetrics(
            session_id=child_id,
            agent_name=SessionInfo.from_str(child_id).agent_name or None,
            turn_count=turn_counts.get(child_id, 0),
        )
        for child_id in child_sessions
    )
    return PoolDelegationMetrics(
        main_session_id=main_session_id,
        subagent_sessions=subagents,
        total_sessions=1 + len(subagents),
        delegation_count=len(subagents),
    )
