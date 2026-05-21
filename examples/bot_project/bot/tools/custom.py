"""Custom tools for the bot project.

Contains SpawnSubagentTool and user-facing tools (SendFileToUserTool),
plus helper functions for subagent tool management and descriptor resolution.
"""

import dataclasses
import logging
from pathlib import Path
from typing import Any

from framework.core.tool_manager import (
    InMemoryToolManager,
    Tool,
    ToolConfig,
    ToolManagerConfig,
)
from framework.multi_agent import (
    AgentAddress,
    AgentDescriptor,
    SubagentService,
)
from framework.multi_agent.context import current_conversation_id
from framework.pipeline.adapters import OutputAdapter

logger = logging.getLogger(__name__)

# Tools that subagents must NOT hold (multi-agent communication is main-only)
_SUBAGENT_EXCLUDED_TOOLS: set[str] = {
    "send_message",
    "send_message_async",
    "spawn_subagent",
    "spawn_subagent_sync",
}


def _create_subagent_tool_manager(
    source_tm: InMemoryToolManager,
    descriptor: AgentDescriptor,
    caller_name: str,
    broker: Any,
    agent_bus: Any,
    registry: Any,
) -> InMemoryToolManager:
    """Create an independent ToolManager for a subagent.

    Inherits basic tools (file, shell) from the source ToolManager,
    but filters out multi-agent communication tools. Subagents return
    results via framework-level callbacks, not direct messaging.
    """
    cloned = InMemoryToolManager(
        config=source_tm.config if source_tm.config else ToolManagerConfig()
    )
    for name in source_tm.list_tools():
        if name in _SUBAGENT_EXCLUDED_TOOLS:
            continue
        tool = source_tm.get_tool(name)
        if tool is not None:
            copied = tool.clone() if getattr(type(tool), "clone", None) is not Tool.clone else tool
            cloned.register(copied)
    return cloned


def _resolve_subagent_descriptor(
    descriptor: AgentDescriptor,
    caller_name: str,
) -> AgentDescriptor:
    """Resolve dynamic placeholders in a subagent descriptor.

    Supports: {caller_name} -> actual caller agent name.
    """
    template = descriptor.system_prompt_template or ""
    resolved = template.replace("{caller_name}", caller_name)
    if resolved == template:
        return descriptor
    return dataclasses.replace(descriptor, system_prompt_template=resolved)


class SpawnSubagentTool(Tool):
    """Spawn a sub-agent to perform a task and wait for the result."""

    def __init__(
        self,
        service: SubagentService,
        default_parent_address: AgentAddress,
        descriptor: AgentDescriptor,
        tool_manager: Any | None = None,
        skill_manager: Any | None = None,
        broker: Any | None = None,
        agent_bus: Any | None = None,
        registry: Any | None = None,
    ):
        self._service = service
        self._default_parent_address = default_parent_address
        self._descriptor = descriptor
        self._tool_manager = tool_manager
        self._skill_manager = skill_manager
        self._broker = broker
        self._agent_bus = agent_bus
        self._registry = registry
        super().__init__(
            name="spawn_subagent",
            description="Spawn a sub-agent to perform a task and wait for the result.",
            parameters={
                "type": "object",
                "properties": {
                    "task_prompt": {
                        "type": "string",
                        "description": "The task description for the sub-agent.",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "Optional conversation ID. If omitted, the current session ID is used automatically.",
                    },
                },
                "required": ["task_prompt"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs) -> str:
        task_prompt = kwargs.get("task_prompt", "")
        caller_context = kwargs.get("caller_context", {})
        caller_name = caller_context.get("agent_name") or self._default_parent_address.name
        parent_address = AgentAddress(name=caller_name)
        conversation_id = (
            current_conversation_id.get() or kwargs.get("conversation_id", "") or caller_name
        )

        tool_manager = self._tool_manager
        if tool_manager is not None:
            tool_manager = _create_subagent_tool_manager(
                tool_manager,
                self._descriptor,
                caller_name,
                self._broker,
                self._agent_bus,
                self._registry,
            )

        descriptor = _resolve_subagent_descriptor(self._descriptor, caller_name)

        result = await self._service.create_and_wait(
            descriptor=descriptor,
            task_prompt=task_prompt,
            timeout=120.0,
        )
        partial = getattr(result, "partial_content", None) or ""
        content = result.content or partial or ""
        if not content and result.reasoning:
            content = result.reasoning
        if not content:
            content = "(no result)"
        return content


class SendFileToUserTool(Tool):
    """Send a local file to the current user."""

    def __init__(self, output_adapter: OutputAdapter):
        self._output_adapter = output_adapter
        super().__init__(
            name="send_file_to_user",
            description=(
                "Send a file from the local filesystem to the current user. "
                "Use this tool when you have created or found a file that the user should receive. "
                "The file path can be absolute or relative to the working directory. "
                "You may include a short message explaining what the file contains."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file to send (absolute or relative to working directory).",
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional accompanying message to send with the file.",
                        "default": "",
                    },
                },
                "required": ["file_path"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs) -> str:
        file_path = kwargs.get("file_path", "")
        message = kwargs.get("message", "")

        if not file_path:
            return "Error: file_path is required."

        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()

        if not path.exists():
            return f"Error: File not found: {file_path}"
        if not path.is_file():
            return f"Error: Not a regular file: {file_path}"

        from framework.core.agent import current_agent_context
        from framework.core.types import OutputMessage

        agent_ctx = current_agent_context.get(None)
        if agent_ctx is None:
            return "Error: No active agent context. Cannot send file."

        session_id = agent_ctx.session_id
        if not session_id:
            return "Error: No session_id in agent context. Cannot send file."

        try:
            await self._output_adapter.send(
                OutputMessage(
                    content=message,
                    attachments=[str(path)],
                ),
                session_id,
            )
            agent_ctx.add_attachment(str(path))
            return f"File sent successfully: {path.name}"
        except Exception as e:
            return f"Error sending file: {e}"
