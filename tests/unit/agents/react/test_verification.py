"""Verification tests for ReAct Graph refactoring — critical scenarios from spec & code review.

Tests the full approval flow, suspend-resume lifecycle, memory isolation,
event contract, engine routing, and bot_project wiring.
"""

import pytest
from modex_agent.approval.runtime import TieredToolApprovalClassifier
from modex_agent.approval.config import AgentApprovalConfig, ToolApprovalConfig
from modex_agent.approval.constants import ApprovalDecision, ApprovalTier, ApprovalStatus
from modex_agent.approval.response import parse_approval_action
from modex_agent.approval.types import ApprovalAction
from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.agents.react.nodes.start import StartNode
from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.agents.react.nodes.end import EndNode
from modex_agent.agents.react.graph import build_react_graph
from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.agent import AgentContext
from modex_graph.engine import GraphEngine
from modex_graph.graph import Graph
from modex_graph.exceptions import GraphInterrupt
from modex_graph.result import NodeResult

from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import ToolCall
from modex_agent.core.tool_manager import ToolResult
from modex_agent.core.types import LLMResponse
from modex_agent.core.constants import FinishReason
from modex_agent.memory.history import ListMessageHistory
from modex_agent.hook import HookPoint
from modex_agent.runtime.enums import ApprovalSubjectType
from modex_agent.runtime.models import ApprovalRequestState, ApprovalTransaction, ToolArguments
