"""工具管理器 - 抽象契约与共享执行行为（C2: 具体实现移至 tools/manager.py）。

提供 ToolManager 抽象层，支持工具注册和执行调度。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from contextvars import ContextVar
from enum import StrEnum, nonmember
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.capabilities import Modality, ModelCapabilities, ModelInfo
from modex_agent.core.media import MediaStore
from modex_agent.core.message import ContentFormat, ContentPart, TextPart
from modex_agent.core.tool_group import ToolGroup

logger = logging.getLogger(__name__)


# ctx is delivered via contextvar (not as an execute parameter) because MCP
# tools forward **kwargs to the MCP server (tool.py:108 params=kwargs);
# mixing ctx into kwargs would pollute external calls.


class ToolExecutionContext(BaseModel):
    """tool 执行时的只读上下文。

    Frozen BaseModel (rule 10/12). 字段默认全 None → 现有 tool 零改动。
    需要感知模型多模态能力的 tool 通过 :func:`get_tool_execution_context` 读取，
    并用 :meth:`supports` 做声明式能力检查（参考 ADR-0014）。

    声明式能力模型: tool 通过 ``required_modalities`` / ``produced_modalities``
    frozenset 声明所需/所产出的模态；运行时由 :meth:`Tool.is_available` 与
    :meth:`supports` 协同完成可见性过滤与优雅降级。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    model_info: ModelInfo | None = None
    workspace_root: Path | None = None
    tool_call_id: str | None = None
    session_id: str | None = None
    media_store: MediaStore | None = None

    def supports(self, modality: Modality) -> bool:
        """Conservative modality check for tools running in this context.

        Returns ``False`` when ``model_info`` is None (no model bound — tools
        should degrade gracefully, matching ``test_read_image_no_ctx_returns_degradation_text``).
        Otherwise delegates to :meth:`ModelCapabilities.supports`.
        """
        if self.model_info is None:
            return False
        return self.model_info.capabilities.supports(modality)


_tool_execution_ctx: ContextVar[ToolExecutionContext | None] = ContextVar(
    "tool_execution_ctx", default=None
)


def get_tool_execution_context() -> ToolExecutionContext | None:
    """Get the current tool execution context, if any.

    Returns ``None`` outside a ``ToolManager.execute`` call.
    """
    return _tool_execution_ctx.get()


class ToolConfig(BaseModel):
    """单个工具的配置（C2: frozen Pydantic — rule 10/12）。

    ``enabled`` 通过替换整个 config 对象来切换（不可变值），例如
    ``tool.config = ToolConfig(enabled=False)``。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True  # 是否启用


class ExecutionMode(StrEnum):
    """Tool 执行模式声明（ADR-0048 D1）。

    PARALLEL: 无副作用调用，可与同批其他 PARALLEL 调用重叠执行。
    EXCLUSIVE: 独占执行，是批内栅栏（barrier）。

    v1 仅此两值。v2 的 ``conflict_scope`` 细化（如 ``file:<path>``、
    ``terminal:<session>``、``workspace``）将把 EXCLUSIVE 从全局栅栏变为
    作用域栅栏，届时今日的 EXCLUSIVE 即明日的
    ``EXCLUSIVE(scope="global")``，v1 语义不变。
    """

    PARALLEL = "parallel"
    EXCLUSIVE = "exclusive"


class ToolOrigin(StrEnum):
    """Where one effective tool entry came from — 同名覆盖的仲裁依据。

    name-slot overwrite 原则:``tool.name`` 是工具的唯一运行时身份,
    同名注册即占用同一槽位;同名谁该赢由 :attr:`OVERRIDE_PRIORITY`
    的 rank 决定。排序理由:**用户显式意志 > 能力包产品 > 位置默认**
    (与 ``PluginSource`` 的 nearest-to-user 精神一致)。INTERNAL 与
    EXTERNAL 不进优先级表、走专用语义:INTERNAL 名槽受保护、不可被
    覆盖;EXTERNAL 依赖命名空间前缀隔离、意外撞名时不覆盖既有名。
    平级同名竞争是配置错误(register 应 raise,而非静默择一)。

    本枚举原定义于 ``scope/compiler.py``;迁入 core 的原因:
    ``plugins/assembly/native_core.py``(后续步骤)需要引用它,而
    ``scope/compiler.py`` 已 import ``plugins.assembly.spec``,反向引用
    会成环 —— ``core/tool_manager.py`` 是依赖叶子,是正确归宿。
    """

    PRESET = "preset"
    """Framework toolset preset expansion."""
    PROFILE_TOOLS = "profile_tools"
    """Wholesale tools list from the bound profile."""
    LOCAL_TOOLS = "local_tools"
    """Wholesale or incremental local ``tools:`` declaration."""
    SUPPLEMENT = "supplement"
    """Legacy classification retained for pre-capability migration goldens."""
    CAPABILITY_DERIVED = "capability_derived"
    """Non-derived tool contributed by a named capability."""
    DERIVED_TASK = "derived_task"
    DERIVED_SEND_TO_AGENT = "derived_send_to_agent"
    DERIVED_SEND_TO_PEER = "derived_send_to_peer"
    INTERNAL = "internal"
    """框架结构性合成伴生工具(如 bash_input):名槽受保护,不可被覆盖。"""
    EXTERNAL = "external"
    """外部直注册来源(如 MCP):依赖命名空间前缀隔离;意外撞名时不覆盖既有名。"""

    #: 同名覆盖优先级表:同名注册占用同一 name 槽位时,rank 高者胜出。
    #: 排序理由:用户显式意志 > 能力包产品 > 位置默认(nearest-to-user)。
    #: INTERNAL/EXTERNAL 不进表,走专用语义(INTERNAL 名槽受保护;
    #: EXTERNAL 永不覆盖既有名)。只读;``nonmember`` 使其不进入枚举成员集。
    OVERRIDE_PRIORITY = nonmember(
        MappingProxyType(
            {
                LOCAL_TOOLS: 4,  # 用户显式 tools: 声明 — 最高
                PROFILE_TOOLS: 4,  # profile 宏同为用户显式
                CAPABILITY_DERIVED: 3,  # capability 产品(替换者)
                PRESET: 2,  # 位置/预设默认(被替换者)
                SUPPLEMENT: 2,  # legacy 分类,与 PRESET 同档
                DERIVED_TASK: 2,  # 通信派生——名字唯一,不实际参战
                DERIVED_SEND_TO_AGENT: 2,
                DERIVED_SEND_TO_PEER: 2,
            }
        )
    )


class ToolOverrideRecord(BaseModel):
    """一次同名槽位覆盖的审计记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    winner_origin: ToolOrigin
    displaced_origin: ToolOrigin | None = None  # None = 被覆盖的是未分类(legacy 直注)槽位


class Tool(ABC):
    """工具基类

    所有工具应继承此类并实现 execute 方法。

    ``name`` 是工具的运行时身份,用于同名槽位覆盖(见 :class:`ToolOrigin`)。

    支持两种使用方式：
    1. 新方式：直接传入参数到 __init__
    2. 旧方式（兼容）：继承后通过 @property 定义 name, description, parameters

    动态 schema（C2 折叠自原独立 ABC）：覆写
    ``get_dynamic_schema()`` 以返回上下文感知的描述；默认返回静态
    ``get_schema()``。
    """

    required_modalities: frozenset[Modality] = frozenset()
    """Modalities the model MUST support for this tool to be usable.

    Empty (default) = modality-agnostic; the tool is visible to every model.
    Drives :meth:`is_available` filtering in :meth:`ToolManager.get_tool_descriptions`.
    """

    produced_modalities: frozenset[Modality] = frozenset()
    """Modalities this tool may produce in its output.

    Declarative metadata for downstream consumers (e.g. governance, result
    routing). Not used for visibility filtering — a tool that *produces*
    images is still listed for a text-only model (it just degrades at runtime).
    """

    _default_execution_mode: ClassVar[ExecutionMode] = ExecutionMode.EXCLUSIVE
    """Class-level execution-mode default — fail-closed EXCLUSIVE (ADR-0048 D1).

    A tool that declares nothing can never be overlapped by accident.
    Marker ABCs (:class:`ParallelTool` / :class:`ExclusiveTool`) restate it
    for ergonomic grouping; existing tools migrate by changing their parent
    (ticket 2), not their bodies. Never read directly —
    :attr:`execution_mode` resolves it.
    """

    _execution_mode_override: ExecutionMode | None = None
    """Instance-level override slot (``MCPTool`` adapter registration).

    ``None`` (default) = no override; :attr:`execution_mode` falls back to
    ``_default_execution_mode``. Declared as a class-level annotated default
    (like :attr:`required_modalities`) so the slot exists even on subclasses
    whose ``__init__`` bypasses ``Tool.__init__`` (e.g.
    ``WorkspaceScopedTool``).
    """

    def __init__(
        self,
        name: str | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
        config: ToolConfig | None = None,
    ) -> None:
        # 如果子类已经定义了 name/description/parameters 作为属性，则使用它们
        # 否则使用传入的参数
        self._name = name
        self._description = description
        self._parameters = parameters
        self.config = config or ToolConfig()

    @property
    def name(self) -> str:
        """工具名称"""
        if self._name is not None:
            return self._name
        raise NotImplementedError("Tool must define 'name' either via __init__ or as a property")

    @property
    def description(self) -> str:
        """工具描述"""
        if self._description is not None:
            return self._description
        raise NotImplementedError(
            "Tool must define 'description' either via __init__ or as a property"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """工具参数定义"""
        if self._parameters is not None:
            return self._parameters
        raise NotImplementedError(
            "Tool must define 'parameters' either via __init__ or as a property"
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        """执行模式解析：实例覆盖优先，否则用类级默认（ADR-0048 D1）。

        Read-only declaration surface; the scheduler (ADR-0048 D2) reads it
        to segment a batch. ``WorkspaceScopedTool`` overrides this property
        to delegate to its inner tool — inner tools span both modes, so the
        wrapper must not statically inherit a marker.
        """
        return self._execution_mode_override or type(self)._default_execution_mode

    cancel_note: ClassVar[str | None] = None
    """取消合成 ``<tool_cancelled>`` 结果时追加的说明文本（ADR-0048 D6）。

    For tools whose external effects survive cancellation (e.g. the terminal
    trio leaves the command running in the tab) — tells the model what state
    it is in. ``None`` (default) = nothing to note.
    """

    async def on_cancel(self) -> None:
        """在飞执行被取消时恢复工具自有的外部状态（ADR-0048 D6 契约）。

        The scheduler owns exactly one cancellation action — cancelling the
        asyncio task — and never touches external resources. Tools that hold
        external state (persistent sessions, child processes) override this
        to return it to a known-clean condition. Default: no-op (stateless
        tools).
        """

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """执行工具

        Args:
            **kwargs: 工具参数

        Returns:
            工具执行结果
        """
        pass

    def get_schema(self) -> dict[str, Any]:
        """获取工具 Schema（供 LLM 使用）

        Returns:
            OpenAI 格式的工具定义
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def get_dynamic_schema(self) -> dict[str, Any]:
        """Dynamic schema 默认实现 — 返回静态 schema。

        Override in subclasses for context-aware descriptions.
        """
        return self.get_schema()

    def get_dynamic_schema_for(
        self, caps: ModelCapabilities | None = None
    ) -> dict[str, Any]:
        """Return the tool schema, optionally adapted to model capabilities.

        Default implementation ignores ``caps`` and delegates to
        :meth:`get_dynamic_schema`, so existing subclasses keep working
        unchanged. Subclasses that produce capability-aware schemas (e.g.
        hiding image parameters when the active model is text-only) override
        this method instead of ``get_dynamic_schema``.
        """
        return self.get_dynamic_schema()

    def is_available(self, caps: ModelCapabilities | None) -> bool:
        """Visibility gate used by :meth:`ToolManager.get_tool_descriptions`.

        Returns ``True`` when ``caps is None`` — don't hide tools when the
        active model's capabilities are unknown (back-compat). Otherwise
        returns ``True`` iff every modality in :attr:`required_modalities`
        is among ``caps.modalities``.
        """
        if caps is None:
            return True
        return self.required_modalities <= caps.modalities

    def result_metadata(self, result: Any) -> tuple[ContentFormat | None, list[str] | None]:
        """Declare content metadata for a tool result, for governance truncation.

        Default: no metadata. Terminal-style tools override to return
        ``(ContentFormat.XML, <truncatable paths>)`` for their XML output.
        """
        return (None, None)


class ParallelTool(Tool):
    """Marker base for stateless read-type tools (ADR-0048 D1).

    Carries no behavior — only flips the class-level execution-mode default
    to PARALLEL. Existing tools migrate by changing their parent, not their
    bodies. v2 conflict_scope extension point: this class is where a scoped
    refinement (``PARALLEL(scope=...)``) would land without touching members.
    """

    _default_execution_mode = ExecutionMode.PARALLEL


class ExclusiveTool(Tool):
    """Marker base for tools with side effects or shared state (ADR-0048 D1).

    Restates the fail-closed EXCLUSIVE default for explicitness — migrating
    a tool to this parent documents the classification even though ``Tool``
    already defaults to EXCLUSIVE. v2 conflict_scope extension point: today's
    EXCLUSIVE is tomorrow's ``EXCLUSIVE(scope="global")``; a scoped subclass
    would refine here.
    """

    _default_execution_mode = ExecutionMode.EXCLUSIVE


class ToolResult(BaseModel):
    """工具执行结果

    统一的工具执行结果类，兼容所有场景：
    - ToolManager 执行结果
    - Agent 工具调用结果
    - LLM message 格式转换

    Content is the source of truth: ``content: list[ContentPart]`` holds
    TextPart / ImageUrlPart produced by the tool. ``message_content()``
    renders the LLM-facing text (joined TextParts); multimodal consumers
    read the parts from ``content`` directly.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    tool_name: str
    error: str | None = None
    execution_time: float = 0.0
    call_id: str | None = None
    overflow_processed: bool = False
    content_format: ContentFormat | None = None
    truncatable_paths: list[str] | None = None
    content: list[ContentPart] = Field(default_factory=list)

    @classmethod
    def from_text(cls, tool_name: str, text: str, **kwargs: Any) -> ToolResult:
        """Build a text-only ToolResult.

        ``text`` is wrapped in a :class:`TextPart` and stored in ``content``.
        """
        return cls(tool_name=tool_name, content=[TextPart(text=text)], **kwargs)

    @property
    def success(self) -> bool:
        """执行是否成功"""
        return self.error is None

    def message_content(self) -> str:
        """Unified LLM-facing content rendering.

        Priority chain:
        1. ``content_format=XML`` with TextParts in ``content`` → render verbatim.
        2. ``content`` has TextParts → join them.
        3. ``error`` → ``"Error: {error}"``.
        4. ``""``.
        """
        text_parts = [p.text for p in self.content if isinstance(p, TextPart)]
        if self.content_format is ContentFormat.XML and text_parts:
            return "".join(text_parts)
        if text_parts:
            return "".join(text_parts)
        if self.error is not None:
            return f"Error: {self.error}"
        return ""

    def __repr__(self) -> str:
        status = "error" if self.error else "success"
        return f"ToolResult({self.tool_name}, {status}, {self.execution_time:.2f}s)"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "tool_name": self.tool_name,
            "error": self.error,
            "execution_time": self.execution_time,
            "call_id": self.call_id,
            "success": self.success,
            "content": [p.model_dump() for p in self.content],
        }

    def to_message(self) -> dict[str, Any]:
        """转换为 LLM message 格式 (OpenAI tool message).

        Terminal-tool results carry content_format/truncatable_paths metadata
        (declared by the tool via result_metadata) for governance truncation.
        """
        from .message import MessageRole

        msg: dict[str, Any] = {
            "role": MessageRole.TOOL.value,
            "tool_call_id": self.call_id or "",
            "name": self.tool_name,
            "content": self.message_content(),
        }
        if self.content_format is not None and self.truncatable_paths is not None:
            msg["content_format"] = self.content_format.value
            msg["truncatable_paths"] = self.truncatable_paths
        return msg


class ToolManager(ABC):
    """工具管理器抽象基类

    职责：
    1. 工具注册/注销（动态扩展）
    2. 工具执行调度
    3. 生成工具描述给 LLM

    不处理：
    - 具体的工具实现（由 Tool 子类实现）
    - LLM 调用
    """

    # ---- 工具注册/注销 ----

    @abstractmethod
    def register(
        self,
        tool: Tool,
        config: ToolConfig | None = None,
        *,
        origin: ToolOrigin | None = None,
    ) -> None:
        """注册工具

        ``tool.name`` 是工具的唯一运行时身份:同名注册即占用同一槽位
        (name-slot overwrite)。``origin`` 声明本次注册的来源,决定同名
        覆盖的仲裁:

        - ``origin=None``:legacy 直注路径 — 后写覆盖、调用方自管冲突
          (历史行为,生产路径零变化);
        - 带 origin 的注册按 :attr:`ToolOrigin.OVERRIDE_PRIORITY` 的
          rank 仲裁同名胜负(高者胜,顺序无关);
        - :attr:`ToolOrigin.INTERNAL` 名槽受保护,任何来源(含 legacy)
          不得覆盖,再注册 raise;
        - :attr:`ToolOrigin.EXTERNAL` 永不覆盖既有名(撞名时跳过);
        - 平级同名竞争是配置错误,应 raise。

        Args:
            tool: 工具实例
            config: 工具特定配置（可选）
            origin: 本次注册的来源分类;None = legacy 直注(未分类)
        """
        pass

    @abstractmethod
    def unregister(self, tool_name: str) -> bool:
        """注销工具

        Args:
            tool_name: 工具名称

        Returns:
            是否成功注销
        """
        pass

    @abstractmethod
    def get_tool(self, tool_name: str) -> Tool | None:
        """获取工具实例"""
        pass

    @abstractmethod
    def list_tools(self) -> list[str]:
        """列出所有已注册的工具名称"""
        pass

    @abstractmethod
    def is_registered(self, tool_name: str) -> bool:
        """检查工具是否已注册"""
        pass

    @abstractmethod
    def register_group(
        self,
        group: ToolGroup,
        *,
        origin: ToolOrigin | None = None,
    ) -> None:
        """Atomically register every member of one tool group."""
        pass

    @property
    @abstractmethod
    def tool_groups(self) -> tuple[ToolGroup, ...]:
        """Registered group metadata without resource ownership."""
        pass

    @abstractmethod
    def get_tool_group(self, tool_name: str) -> ToolGroup | None:
        """Return the group containing *tool_name*, if one is registered."""
        pass

    @abstractmethod
    def origin_of(self, tool_name: str) -> ToolOrigin | None:
        """Return the registration origin for one runtime name slot."""
        pass

    @property
    def override_records(self) -> tuple[ToolOverrideRecord, ...]:
        """同名槽位覆盖的审计记录(只读快照,按发生顺序)。

        Base default: empty — a manager that never arbitrates name-slot
        overwrite has no records. ``InMemoryToolManager`` overrides this
        with its audit log; wrapper managers (:class:`FilteredToolManager`
        and the cassette record/replay wrappers) delegate to their wrapped
        manager so the audit stays visible through the wrapper.
        """
        return ()

    # ---- 工具执行 ----

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """执行单个工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            ctx: 工具执行上下文（模型能力等），通过 contextvar 交付给工具

        Returns:
            ToolResult: 执行结果
        """
        tool = self.get_tool(tool_name)

        if tool is None:
            available_tools = self.list_tools()
            logger.warning(f"Tool not found: {tool_name}. Available tools: {available_tools}")
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' not found. Available: {available_tools}",
            )

        if not tool.config.enabled:
            logger.warning(f"Tool disabled: {tool_name}")
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' is disabled",
            )

        start_time = asyncio.get_event_loop().time()
        token = _tool_execution_ctx.set(ctx)
        try:
            result = await tool.execute(**arguments)
            execution_time = asyncio.get_event_loop().time() - start_time
            # Exact-type check (not isinstance): a subclass returned by a tool
            # should not silently bypass the content-copy re-wrap path, which
            # normalizes content_format/truncatable_paths via result_metadata.
            if type(result) is ToolResult:
                content_format, truncatable_paths = tool.result_metadata(result.message_content())
                return ToolResult(
                    tool_name=result.tool_name,
                    error=result.error,
                    execution_time=execution_time,
                    call_id=result.call_id,
                    content_format=content_format,
                    truncatable_paths=truncatable_paths,
                    content=result.content,
                )
            content_format, truncatable_paths = tool.result_metadata(result)
            if isinstance(result, str):
                return ToolResult(
                    tool_name=tool_name,
                    content=[TextPart(text=result)],
                    content_format=content_format,
                    truncatable_paths=truncatable_paths,
                    execution_time=execution_time,
                )
            if result is None:
                return ToolResult(
                    tool_name=tool_name,
                    execution_time=execution_time,
                    content_format=content_format,
                    truncatable_paths=truncatable_paths,
                )
            return ToolResult(
                tool_name=tool_name,
                content=[TextPart(text=str(result))],
                execution_time=execution_time,
                content_format=content_format,
                truncatable_paths=truncatable_paths,
            )
        except Exception as e:
            execution_time = asyncio.get_event_loop().time() - start_time
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool '{tool_name}' execution failed: {str(e)}",
                execution_time=execution_time,
            )
        finally:
            _tool_execution_ctx.reset(token)

    # ---- 工具描述生成 ----

    def get_tool_descriptions(
        self, caps: ModelCapabilities | None = None
    ) -> list[dict[str, Any]]:
        """获取所有工具的描述（供 LLM 使用）

        Args:
            caps: 当前模型的 capabilities。``None`` 时不过滤（back-compat，
                与现有调用方一致）。非 None 时，跳过 :meth:`Tool.is_available`
                返回 False 的工具，并把 ``caps`` 传给
                :meth:`Tool.get_dynamic_schema_for` 以生成能力感知的 schema。

        Returns:
            OpenAI 格式的工具定义列表
        """
        descriptions = []
        for tool_name in self.list_tools():
            tool = self.get_tool(tool_name)
            if tool is None:
                continue
            if tool.config.enabled and tool.is_available(caps):
                descriptions.append(tool.get_dynamic_schema_for(caps))
        return descriptions
