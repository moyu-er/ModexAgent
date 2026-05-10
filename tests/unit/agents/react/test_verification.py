"""Verification tests for ReAct Graph refactoring — critical scenarios from spec & code review.

Tests the full approval flow, suspend-resume lifecycle, memory isolation,
event contract, engine routing, and bot_project wiring.
"""

import pytest
from framework.agents.react.approval import TieredToolApprovalClassifier
from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig
from framework.approval.constants import ApprovalDecision, ApprovalTier, ApprovalStatus
from framework.approval.response import parse_approval_action
from framework.approval.types import ApprovalAction
from framework.agents.react.constants import ReActNode, ReActReason
from framework.agents.react.nodes.tool import ToolNode
from framework.agents.react.nodes.start import StartNode
from framework.agents.react.nodes.llm import LLMNode
from framework.agents.react.nodes.end import EndNode
from framework.agents.react.graph import ReActGraph
from framework.agents.react.agent import ReActEvent
from framework.core.graph.node import NodeTransition
from framework.core.graph.graph import Graph, Edge
from framework.core.graph.engine import GraphEngine
from framework.core.graph.constants import GraphNode, GraphMetaKey
from framework.core.agent import AgentContext
from framework.core.graph.interrupt import GraphInterrupt

from framework.core.tool_manager import InMemoryToolManager
from framework.core.emitter import ToolCall, ToolResult
from framework.core.types import LLMResponse
from framework.core.constants import FinishReason
from framework.memory.history import ListMessageHistory
from framework.hook import HookPoint
from framework.runtime.enums import ApprovalSubjectType
from framework.runtime.models import ApprovalRequestState, ApprovalTransaction, ToolArguments

