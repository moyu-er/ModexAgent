"""Model capability types — modality enum and capabilities value object.

These are core types (no ioc/config dependency). They live here so that
``core.tool_manager.ToolExecutionContext`` and ``runtime.services`` can
reference them without an upward import into ``ioc.configs``. The
``ioc.configs.llm`` module re-exports them for config-layer consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


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


@dataclass(frozen=True)
class ModelCapabilities:
    """Frozen value object exposing the modalities a model can consume.

    Read from per-pool config (``LLMConfig.capabilities``) and gates image
    inlining (ADR-0014 §1): a pool declaring ``IMAGE`` enables mechanism A;
    the default TEXT-only leaves every attachment on the mechanism-B tool
    path. Defaults to TEXT-only.
    """

    modalities: frozenset[Modality] = field(default_factory=lambda: frozenset({Modality.TEXT}))

    def supports(self, modality: Modality) -> bool:
        """True if ``modality`` is among this model's capabilities."""
        return modality in self.modalities
