# bot/input_pipeline/stages/model_choice.py
"""WebUI pipeline stage：把 payload 中的 provider/model 选择解析为 ResolvedModel。

仅注册在 WebUI pipeline；IM pipeline 不注册（始终默认）。无效选择 fallback 默认并告警。
不设 ContextVar（跨 broker 会丢失）；解析结果写入 envelope.metadata[RESOLVED_MODEL]，
由 EnqueueStage 注册到 ModelChoiceRegistry。
"""

from __future__ import annotations

import logging

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.model_config import BotModelConfig
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult

logger = logging.getLogger(__name__)


class ModelChoiceStage(InputStage):
    """Resolve the WebUI-selected provider/model into a ResolvedModel on the envelope."""

    def __init__(self, model_config: BotModelConfig | None) -> None:
        self._model_config = model_config

    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        # When no model.yml is configured (fresh install), model_config is None.
        # Skip model resolution and let the pipeline continue so PersistUserMessageStage
        # saves the session and EnqueueStage delivers the message to the target pool.
        #
        # Downstream behavior per pool:
        # - external: ExternalTurnRunner does not read RESOLVED_MODEL, so the
        #   external CLI (opencode/pi) executes normally with its own model.
        # - react: EnqueueStage does not register into ModelChoiceRegistry; at turn
        #   start, ModelChoiceBindHook falls back to the placeholder default (built by
        #   pool_builder._resolved_or_placeholder) whose empty api_key causes
        #   BotModelProvider to emit LLMResponse(finish_reason=ERROR). The user sees an
        #   explicit "model not configured" turn error instead of a silent failure.
        if self._model_config is None:
            return Continue(value=envelope)
        provider_name = envelope.metadata.get(RoutingMeta.MODEL_PROVIDER)
        model_name = envelope.metadata.get(RoutingMeta.MODEL_MODEL)
        resolved = self._model_config.resolve(provider_name, model_name)
        if resolved is None:
            if provider_name or model_name:
                logger.warning(
                    "Invalid model choice (%r, %r) — falling back to default",
                    provider_name,
                    model_name,
                )
            resolved = self._model_config.default_resolved()
        envelope.metadata[RoutingMeta.RESOLVED_MODEL] = resolved
        return Continue(value=envelope)
