"""Model capability types — modality enum, capabilities, and model info.

These are core types (no ioc/config dependency). They live here so that
``core.tool_manager.ToolExecutionContext`` and ``runtime.services`` can
reference them without an upward import into ``ioc.configs``. The
``ioc.configs.llm`` module re-exports them for config-layer consumers.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Modality(StrEnum):
    """A perceptual channel a model can accept.

    TEXT is always available for any text model; IMAGE/VIDEO/AUDIO are
    native-multimodal flags, default-off. Extensible — adding a modality is
    one enum member. See ADR-0013 §9.
    """

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class ModelCapabilities(BaseModel):
    """Frozen value object exposing the modalities a model can consume.

    Read from per-pool config (``LLMConfig.capabilities``) and gates image
    inlining (ADR-0014 §1): a pool declaring ``IMAGE`` enables mechanism A;
    the default TEXT-only leaves every attachment on the mechanism-B tool
    path. Defaults to TEXT-only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    modalities: frozenset[Modality] = Field(
        default_factory=lambda: frozenset({Modality.TEXT})
    )

    def supports(self, modality: Modality) -> bool:
        """True if ``modality`` is among this model's capabilities."""
        return modality in self.modalities


class ModelInfo(BaseModel):
    """Frozen value object describing the active model for the current turn.

    Carried in ``runtime_info`` (key ``RuntimeInfoKey.MODEL_INFO``) from
    ``TurnContextBuilder.assemble`` through ``load`` into the prompt
    pipeline's ``ModelInfoProvider``. Tools read it via
    ``ToolExecutionContext.model_info``.

    Budget profile (per-model context compaction PRD §4.2): the context /
    output limits ride the same per-turn pipe. ``None`` means "not declared
    for this model" — consumers fall back to the static pool config via
    ``modex_agent.memory.budget.resolve_effective_budget``, the sole
    definition of that priority chain.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str = ""
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    context_limit: int | None = Field(
        default=None,
        ge=1,
        description="Context-window limit of the active model (tokens); "
        "None = undeclared, fall back to pool-level config",
    )
    max_output_tokens: int | None = Field(
        default=None,
        ge=1,
        description="Output budget of the active model (tokens); "
        "None = undeclared, fall back to pool-level config",
    )
