"""MCP tool wrappers.

Wraps MCP tools, resources, and prompts as framework Tool objects.
"""

from typing import Any

from modex_agent.core.tool_manager import Tool, ToolConfig
from modex_agent.tools.mcp.backend import McpBackend
from modex_agent.tools.mcp.client import _DEFAULT_TOOL_TIMEOUT


def _extract_nullable_branch(options: Any) -> tuple[dict, bool] | None:
    """Return the single non-null branch for nullable unions."""
    if not isinstance(options, list):
        return None
    non_null: list[dict] = []
    saw_null = False
    for option in options:
        if not isinstance(option, dict):
            return None
        if option.get("type") == "null":
            saw_null = True
            continue
        non_null.append(option)
    if saw_null and len(non_null) == 1:
        return non_null[0], True
    return None


def _normalize_schema_for_openai(schema: Any) -> dict[str, Any]:
    """Normalize JSON Schema patterns for OpenAI tool definitions."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}

    normalized = dict(schema)

    for key in ("oneOf", "anyOf"):
        nullable_branch = _extract_nullable_branch(normalized.get(key))
        if nullable_branch is not None:
            branch, _ = nullable_branch
            merged = {k: v for k, v in normalized.items() if k != key}
            merged.update(branch)
            normalized = merged
            break

    raw_type = normalized.get("type")
    if isinstance(raw_type, list):
        non_null = [item for item in raw_type if item != "null"]
        if "null" in raw_type and len(non_null) == 1:
            normalized["type"] = non_null[0]

    if "properties" in normalized and isinstance(normalized["properties"], dict):
        normalized["properties"] = {
            name: _normalize_schema_for_openai(prop) if isinstance(prop, dict) else prop
            for name, prop in normalized["properties"].items()
        }

    if "items" in normalized and isinstance(normalized["items"], dict):
        normalized["items"] = _normalize_schema_for_openai(normalized["items"])

    if normalized.get("type") != "object":
        return normalized

    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


class MCPTool(Tool):
    """Wraps an MCP server tool as a framework Tool."""

    def __init__(
        self,
        server_name: str,
        tool_name: str,
        description: str,
        parameters: dict[str, Any],
        mcp_manager: McpBackend,
        config: ToolConfig | None = None,
        tool_timeout: int = _DEFAULT_TOOL_TIMEOUT,
        use_prefix: bool = True,
    ) -> None:
        full_name = f"mcp_{server_name}_{tool_name}" if use_prefix else tool_name

        normalized_params = _normalize_schema_for_openai(parameters)

        effective_config = config if config is not None else ToolConfig()

        super().__init__(
            name=full_name,
            description=description,
            parameters=normalized_params,
            config=effective_config,
        )

        self._server_name = server_name
        self._tool_name = tool_name
        self._mcp_manager = mcp_manager
        self._tool_timeout = tool_timeout

    async def execute(self, **kwargs: Any) -> str:
        """Execute the MCP tool.

        Reconnect-on-disconnect retry lives in the backend (``MCPClientManager``);
        this wrapper only invokes the backend once and formats the result.
        """
        result = await self._mcp_manager.execute_tool(
            server_name=self._server_name,
            tool_name=self._tool_name,
            params=kwargs,
            timeout=self._tool_timeout,
        )

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            return f"(MCP tool call failed: {error})"

        return str(result.get("result", ""))


class MCPResourceTool(Tool):
    """Wraps an MCP resource URI as a read-only Tool."""

    def __init__(
        self,
        server_name: str,
        resource_name: str,
        uri: str,
        description: str,
        mcp_manager: McpBackend,
        config: ToolConfig | None = None,
        resource_timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        full_name = f"mcp_{server_name}_resource_{resource_name}"

        effective_config = config if config is not None else ToolConfig()

        super().__init__(
            name=full_name,
            description=f"[MCP Resource] {description}\nURI: {uri}",
            parameters={"type": "object", "properties": {}, "required": []},
            config=effective_config,
        )

        self._server_name = server_name
        self._uri = uri
        self._mcp_manager = mcp_manager
        self._resource_timeout = resource_timeout

    async def execute(self, **kwargs: Any) -> str:
        """Execute the MCP resource read.

        Reconnect-on-disconnect retry lives in the backend; this wrapper only
        invokes the backend once and formats the result.
        """
        result = await self._mcp_manager.read_resource(
            self._server_name, self._uri, timeout=self._resource_timeout
        )

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            return f"(MCP resource read failed: {error})"

        return str(result.get("result", ""))


class MCPPromptTool(Tool):
    """Wraps an MCP prompt as a read-only Tool."""

    def __init__(
        self,
        server_name: str,
        prompt_name: str,
        description: str,
        arguments_def: list[dict[str, Any]],
        mcp_manager: McpBackend,
        config: ToolConfig | None = None,
        prompt_timeout: int = _DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        full_name = f"mcp_{server_name}_prompt_{prompt_name}"

        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in arguments_def or []:
            prop: dict[str, Any] = {"type": "string"}
            if arg.get("description"):
                prop["description"] = arg["description"]
            properties[arg["name"]] = prop
            if arg.get("required"):
                required.append(arg["name"])

        effective_config = config if config is not None else ToolConfig()

        super().__init__(
            name=full_name,
            description=f"[MCP Prompt] {description}\nReturns a filled prompt template that can be used as a workflow guide.",
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
            },
            config=effective_config,
        )

        self._server_name = server_name
        self._prompt_name = prompt_name
        self._mcp_manager = mcp_manager
        self._prompt_timeout = prompt_timeout

    async def execute(self, **kwargs: Any) -> str:
        """Execute the MCP prompt.

        Reconnect-on-disconnect retry lives in the backend; this wrapper only
        invokes the backend once and formats the result.
        """
        result = await self._mcp_manager.get_prompt(
            self._server_name, self._prompt_name, arguments=kwargs, timeout=self._prompt_timeout
        )

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            return f"(MCP prompt call failed: {error})"

        return str(result.get("result", ""))
