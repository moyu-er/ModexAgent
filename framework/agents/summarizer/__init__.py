"""SummarizerAgent — lightweight, tool-free agent for text summarization and analysis."""

from framework.agents.summarizer.agent import SummarizerAgent, SummarizerEvent
from framework.agents.summarizer.strategy import SummarizerStrategy

__all__ = ["SummarizerAgent", "SummarizerEvent", "SummarizerStrategy"]
