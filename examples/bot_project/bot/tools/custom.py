"""Custom tools for the bot project.

Contains user-facing tools (SendFileToUserTool).
"""

import logging
from pathlib import Path

from framework.core.tool_manager import (
    Tool,
    ToolConfig,
)
from framework.workspace.runtime import resolve_workspace_root
from framework.pipeline.adapters import OutputAdapter

logger = logging.getLogger(__name__)


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
            path = resolve_workspace_root() / path
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
