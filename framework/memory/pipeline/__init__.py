"""System prompt pipeline — versioned, per-section refreshable prompt assembly."""

from framework.memory.pipeline.abc import SystemPromptProvider
from framework.memory.pipeline.pipeline import SystemPromptPipeline

__all__ = ["SystemPromptProvider", "SystemPromptPipeline"]
