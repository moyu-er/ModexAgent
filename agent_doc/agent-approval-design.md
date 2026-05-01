# Agent 控制交互体系完整设计

## 概述

本文是 ModexAgent 控制交互体系的**完整设计文档**，覆盖审批、用户介入、状态持久化、等待策略等全部控制交互场景。

**设计原则**：

- **一套 ABC 体系支撑全部控制交互场景**（审批、确认、选择等），不针对每个场景重复造轮子
- **ABC 驱动**：所有核心组件通过抽象类/接口定义，禁止依赖具体实现类
- **类型安全**：枚举代替硬编码字符串；frozen dataclass 代替 dict；函数签名完整类型标注
- **零向后兼容**：旧的错误实现和回退路径直接清理干净，不做兼容层

### 场景覆盖

| 场景 | 用户交互 | 等待方式 | 状态持久化 |
|------|---------|---------|-----------|
| Tool 审批 | 展示 tool + args，allow/deny | 阻塞等待 | 审批 pattern、YOLO |
| LLM 输出审批 | 展示输出内容，approve/reject | 阻塞等待 | 审批记录 |
| 资金交易确认 | 展示金额/地址，confirm/cancel | 阻塞等待 | 交易记录 |
| Steer 注入确认 | 展示 steer 内容，可选 skip | 不等待 | 无 |
| Tool 策略否决 | 无需交互 | 不等待 | 否决日志 |

### 与 agent-control-design.md 的关系

本文是 `agent-control-design.md` 的**细化实现文档**，聚焦于控制交互体系中用户介入相关能力的详细设计。两者互补：
- `agent-control-design.md`：整体三层架构总览、全部 Hook/Interceptor scope、HookPoint、执行时序
- 本文：用户介入子系统的 ABC 设计、IM/CLI 双模适配、checkpoint 存储、等待策略

### 参考项目对标

| 维度 | hermes-agent | nanobot | ModexAgent 设计 |
|------|-------------|---------|:--------------:|
| 审批入口 | `check_all_command_guards()` 在 tool 内部 | `_guard_command()` 在 shell tool 内部 | `TieredToolApprovalInterceptor` 在 interceptor 层 |
| 审批层级 | Hardline / YOLO / Smart / Manual | deny_patterns / allow_patterns | Hardline / Dangerous / Sensitive / Normal |
| 异步审批 | `_GatewayQueues` + `threading.Event` | — (CLI only) | `ControlWaitStrategy` ABC |
| IM 审批 | 5+ 平台 full support (Telegram, Discord, Slack, Feishu, Matrix) | 不支持 | `IMUserInterface` 跨平台 |
| CLI 审批 | `input()` 同步阻塞 | `AskUserInterrupt` 异常 | `CLIUserInterface` |
| 状态持久化 | SQLite `state.db` | SessionManager + checkpoint | `StateStore` ABC (InMemory/File/Redis) |
| 消息注入 | — | `_pending_queues` + `injection_callback` | `injection_queue` (已有设计) |
| Steer 引导 | `agent.steer()` | — | `SteerInjectInterceptor` (已有) |
| Hook 体系 | Plugin lifecycle hooks | `AgentHook` (5 点) | `HookPoint` (9 点) |

---

## 一、架构总览

### 1.1 新模块全景

```
framework/
├── control/                              # 已有，增强
│   ├── abc.py                            #   新增: ControlWaitStrategy ABC
│   ├── types.py                          #   已有: ControlCommand/Event/Scope
│   ├── channel.py                        #   已有: ControlChannel + InMemoryControlChannel
│   ├── event_bus.py                      #   已有: ControlEventBus
│   ├── exceptions.py                     #   已有: AgentControlError, ApprovalDenied, AgentCancelled
│   ├── preset.py                         #   已有: preset rules
│   ├── state_store/                      #   新增
│   │   ├── __init__.py
│   │   ├── abc.py                        #     StateStore ABC
│   │   ├── memory.py                     #     InMemoryStateStore
│   │   ├── file.py                       #     JsonFileStateStore
│   │   └── redis.py                      #     RedisStateStore
│   ├── ui/                               #   新增
│   │   ├── __init__.py
│   │   ├── abc.py                        #     ControlUserInterface ABC
│   │   ├── cli.py                        #     CLIUserInterface
│   │   ├── im.py                         #     IMUserInterface
│   │   └── noop.py                       #     NoopUserInterface
│   └── checkpoint/                       #   重构
│       ├── __init__.py
│       ├── abc.py                        #     CheckpointStore ABC
│       └── store.py                      #     StateStoreBackedCheckpointStore
│
├── approval/                             # 新增
│   ├── __init__.py
│   ├── abc.py                            #   ApprovalRequest/Response/Outcome, ApprovalStore ABC
│   ├── types.py                          #   ApprovalTier/DenyAction/TimeoutAction 枚举
│   └── builtin/
│       ├── __init__.py
│       ├── interceptor.py                #   TieredToolApprovalInterceptor (增强)
│       ├── store.py                      #   StateStoreBackedApprovalStore
│       └── matcher.py                    #   ExactNameMatcher, PatternMatcher
│
├── hook/                                 # 已有，不变
├── interceptor/                          # 已有，tool_approval.py 迁移至 approval/
└── ...
```

### 1.2 三层体系中审批的位置

```
Control Layer（命令/事件/状态存储）
    ├── ControlChannel          — 已有，APPROVAL_RESPONSE 命令走这里
    ├── ControlEventBus         — 已有，TOOL_APPROVAL_REQUESTED 事件走这里
    ├── StateStore              — 新增 ABC，统一 KV 持久化
    ├── CheckpointStore         — 重构，基于 StateStore
    └── ControlWaitStrategy     — 新增 ABC，等待策略

Interceptor Layer（AOP 包裹）
    └── TieredToolApprovalInterceptor  — 审批核心，依赖注入以下接口：
            ├── ControlUserInterface   — 新增 ABC，用户界面
            ├── ControlWaitStrategy    — 新增 ABC，等待方式
            └── ApprovalStore          — 新增 ABC，审批状态存储

Hook Layer（观察/修改）
    ├── ToolPolicyGuardHook      — 已有，静默否决
    └── ToolResultTransformHook  — 已有，结果脱敏

外部适配层（bot_project 侧）
    └── IMCommandRouter          — 新增，解析 /approve /deny → ControlCommand
```

### 1.3 设计约束

0. **Session 隔离为首要原则**：所有控制组件运行时状态按 `session_id` 隔离。
1. **ABC 驱动**：所有组件依赖抽象接口，不依赖具体实现类。便于扩展/拔插/自定义。
2. **控制命令统一走 `ControlChannel`**：cancel、steer、配置变更、审批响应等。
3. **事件统一走 `ControlEventBus`**：进度、审批请求、状态变更等。
4. **Hook vs Interceptor 不混淆**：轻量观察/修改用 Hook，复杂包裹/等待用 Interceptor。
5. **受控终止统一语义**：所有取消/拒绝/超时通过 `AgentControlError` 体系。
6. **Channel 按类型路由，禁止"放回"**：消费者只从自己关心的类型子队列消费。
7. **审批拒绝必须补齐历史**：所有未执行的 tool_call 补齐伪 ToolResult，保证声明=结果对应。
8. **零向后兼容**：旧的不合理实现和回退路径直接清除，不做 compat layer。

---

## 二、完整 ABC 清单

### 2.1 StateStore — 通用 KV 持久化

```python
# framework/control/state_store/abc.py

from abc import ABC, abstractmethod
from collections.abc import Sequence


class StateStore(ABC):
    """通用状态存储基类。

    所有需要跨进程持久化的状态（审批、checkpoint、session 元数据、
    动态配置等）都通过此接口。key 是命名空间化的字符串路径。

    实现：
    - InMemoryStateStore: 进程内 dict，重启丢失（开发/测试/单机简单场景）
    - JsonFileStateStore: 本地 JSON 文件（单机 bot 主场景，重启可恢复）
    - RedisStateStore: Redis 后端（分布式多 server 场景）
    """

    @abstractmethod
    async def get(self, key: str) -> object | None:
        """读取值，不存在返回 None。"""
        ...

    @abstractmethod
    async def set(
        self, key: str, value: object, ttl_seconds: float | None = None,
    ) -> None:
        """写入值，可选 TTL 自动过期。"""
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    async def list_keys(self, prefix: str) -> Sequence[str]:
        """列出指定前缀下的所有 key。"""
        ...
```

### 2.2 ControlUserInterface — 通用用户交互

```python
# framework/control/ui/abc.py

class ControlUserInterface(ABC):
    """控制场景的用户界面抽象。

    任意需要与用户交互的控制场景（审批、确认、选择等）都通过此接口。
    """

    @abstractmethod
    async def render_message(
        self,
        session_id: str,
        content: str,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        """向用户展示消息（无需回复）。

        Returns:
            消息 ID，用于后续 update_message。
        """
        ...

    @abstractmethod
    async def render_question(
        self,
        session_id: str,
        question: str,
        options: Sequence[str],
        timeout: float,
        metadata: Mapping[str, object] | None = None,
    ) -> object | None:
        """向用户展示问题，等待选择。

        Args:
            session_id: 会话标识
            question: 问题文本
            options: 可选项列表，如 ["allow", "deny"]
            timeout: 超时秒数
            metadata: 附加元数据（如 reply_to message_id）

        Returns:
            用户选择的选项字符串，超时返回 None。
        """
        ...

    @abstractmethod
    async def update_message(
        self, session_id: str, message_id: str, content: str,
    ) -> None:
        """更新已发送的消息（如审批完成后修改原消息移除按钮）。"""
        ...
```

### 2.3 ControlWaitStrategy — 通用等待策略

```python
# framework/control/abc.py

@dataclass(frozen=True)
class WaitResult:
    """等待策略的返回结果。"""
    value: object | None       # 用户响应值，超时/取消为 None


class ControlWaitStrategy(ABC):
    """控制场景的等待策略。

    定义「需要等待外部响应时，如何等待」。
    对 interceptor 透明——interceptor 只调用 wait()，不关心实现。

    职责边界：
    - INLINE 模式：ControlUserInterface.render_message 发送通知 →
      ControlWaitStrategy.wait 在进程内阻塞等待响应
    - SUSPEND 模式：ControlUserInterface.render_message 发送通知 →
      ControlWaitStrategy.wait 保存 checkpoint + raise AgentAwaitingApproval
    """

    @abstractmethod
    async def wait(
        self,
        *,
        session_id: str,
        ui: 'ControlUserInterface',
        timeout: float,
        poll_interval: float = 0.3,
    ) -> WaitResult:
        """等待外部响应。

        Returns:
            WaitResult(value=响应内容, None=超时)。

        InlineWaitStrategy: await channel.drain() 进程内阻塞
        SuspendResumeWaitStrategy: 保存 checkpoint → raise AgentAwaitingApproval
        """
        ...
```

### 2.4 CheckpointStore — 检查点存储

```python
# framework/control/checkpoint/abc.py

@dataclass(frozen=True)
class AgentCheckpoint:
    """Agent 执行检查点。"""
    checkpoint_id: str
    session_id: str
    turn_id: str
    agent_id: str
    messages: Sequence[Mapping[str, object]]
    iteration: int
    termination_reason: str | None
    denial_context: Mapping[str, object] | None
    cancelled_tool_ids: Sequence[str]
    partial_content: str | None
    created_at: float


class CheckpointStore(ABC):
    """检查点存储抽象。"""

    @abstractmethod
    async def save(self, checkpoint: AgentCheckpoint) -> None:
        ...

    @abstractmethod
    async def load(self, checkpoint_id: str) -> AgentCheckpoint | None:
        ...

    @abstractmethod
    async def delete(self, checkpoint_id: str) -> None:
        ...

    @abstractmethod
    async def list_by_session(self, session_id: str) -> Sequence[AgentCheckpoint]:
        ...
```

### 2.5 ApprovalStore — 审批状态存储

```python
# framework/approval/abc.py

class ApprovalStore(ABC):
    """审批状态存储。

    存储 session 级别的审批状态（已批准 pattern、YOLO 模式、
    pending approvals）。

    默认实现 StateStoreBackedApprovalStore 使用 StateStore 作为后端。
    """

    @abstractmethod
    async def is_pattern_approved(self, session_id: str, pattern_key: str) -> bool:
        ...

    @abstractmethod
    async def approve_pattern(self, session_id: str, pattern_key: str) -> None:
        ...

    @abstractmethod
    async def is_yolo_enabled(self, session_id: str) -> bool:
        ...

    @abstractmethod
    async def set_yolo(self, session_id: str, enabled: bool) -> None:
        ...

    @abstractmethod
    async def clear_session(self, session_id: str) -> None:
        ...
```

---

## 三、完整枚举和数据结构

```python
# framework/approval/types.py

# ── 审批层级 ──

class ApprovalTier(StrEnum):
    HARDLINE = "hardline"    # 无条件拒绝，永不执行
    DANGEROUS = "dangerous"  # 必须审批，YOLO 不可跳过
    SENSITIVE = "sensitive"  # 需要审批，YOLO 可跳过
    NORMAL = "normal"        # 直接放行


# ── 审批动作 ──

class ApprovalAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


# ── 审批解决方式 ──

class ApprovalResolution(StrEnum):
    ALLOWED = "allowed"           # 用户明确同意
    DENIED = "denied"             # 用户明确拒绝
    TIMED_OUT = "timed_out"       # 超时
    IGNORED = "ignored"           # 用户发送其他消息（等效拒绝）
    PREEMPTED = "preempted"       # 批内前一个拒绝导致本条被跳过


# ── 拒绝后行为 ──

class DenyAction(StrEnum):
    TOOL_ERROR = "deny_as_tool_error"
    CANCEL_TURN = "deny_as_cancel"


class TimeoutAction(StrEnum):
    TOOL_ERROR = "timeout_as_tool_error"
    CANCEL_TURN = "timeout_as_cancel"


# ── 审批结果类型 ──

class ApprovalResultType(StrEnum):
    EXECUTED = "executed"
    RETURNED_ERROR = "returned_error"
    CANCELLED_TURN = "cancelled_turn"
```

```python
# framework/approval/abc.py

# ── 审批请求/响应/结果 ──

@dataclass(frozen=True)
class ApprovalRequest:
    """单条工具调用的审批请求。"""
    request_id: str
    tool_name: str
    tool_call_id: str
    tier: ApprovalTier
    redacted_arguments: Mapping[str, object]
    session_id: str
    turn_id: str
    iteration: int
    description: str = ""
    created_at: float = field(default_factory=lambda: __import__("time").monotonic())


@dataclass(frozen=True)
class ApprovalResponse:
    """用户对审批请求的响应。"""
    request_id: str
    action: ApprovalAction
    choice: str = ""
    responded_at: float = field(default_factory=lambda: __import__("time").monotonic())


@dataclass(frozen=True)
class ApprovalOutcome:
    """单条 tool call 审批的最终结果。"""
    request: ApprovalRequest
    resolution: ApprovalResolution
    response: ApprovalResponse | None = None
    reason: str = ""
    result_type: ApprovalResultType = ApprovalResultType.RETURNED_ERROR
```

---

## 四、ABC 实现详情

### 4.1 StateStore 实现

```python
# framework/control/state_store/memory.py

class InMemoryStateStore(StateStore):
    """进程内 dict 存储。重启丢失。"""
    def __init__(self) -> None:
        self._data: dict[str, object] = {}
        self._ttl: dict[str, float] = {}
        self._lock = asyncio.Lock()
    # get/set/delete/exists/list_keys ...
```

```python
# framework/control/state_store/file.py

class JsonFileStateStore(StateStore):
    """基于本地 JSON 文件的状态存储。

    单机 bot 主场景：重启后可恢复审批状态和 checkpoint。
    每个 key 对应一个 JSON 文件，按 key 的 / 分隔创建子目录。
    如 key="session/abc123/approval/xyz" → "<base_dir>/session/abc123/approval/xyz.json"

    TTL 通过文件 mtime 实现：读取时检查文件修改时间，超时返回 None。
    """
    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir
    # get/set/delete/exists/list_keys ...
```

```python
# framework/control/state_store/redis.py

class RedisStateStore(StateStore):
    """基于 Redis 的状态存储。

    分布式场景：多台 server 共享同一个 Redis 实例。
    key 前缀: "modex:{key}"
    TTL 通过 Redis EXPIRE 实现。
    """
    def __init__(self, redis_url: str) -> None:
        # 使用 redis.asyncio 客户端
        ...
    # get/set/delete/exists/list_keys ...
```

### 4.2 ControlUserInterface 实现

```python
# framework/control/ui/cli.py

class CLIUserInterface(ControlUserInterface):
    """终端命令行交互。

    render_question 用 print + input 同步阻塞获取用户输入。
    render_message 用 print 输出。
    """

    async def render_message(
        self, session_id: str, content: str, metadata: ... = None,
    ) -> str:
        print(content)
        return ""   # CLI 无 message_id

    async def render_question(
        self, session_id: str, question: str, options: Sequence[str],
        timeout: float, metadata: ... = None,
    ) -> str | None:
        print(question)
        prompt = f"[{'/'.join(options)}]: "
        try:
            answer = input(prompt).strip().lower()
            if answer in options:
                return answer
            return None
        except EOFError:
            return None
```

```python
# framework/control/ui/im.py

class IMUserInterface(ControlUserInterface):
    """IM 即时通讯交互（QQ/Discord/Telegram 等）。

    依赖 OutputAdapter 发送消息，ControlChannel 等待命令响应。
    """

    def __init__(
        self,
        *,
        output_adapter: OutputAdapter,
        channel: ControlChannel,
        command_router: 'IMCommandRouter',
    ) -> None:
        self._output = output_adapter
        self._channel = channel
        self._router = command_router

    async def render_message(
        self, session_id: str, content: str, metadata: ... = None,
    ) -> str:
        msg = OutputMessage(content=content, metadata=dict(metadata or {}))
        return await self._output.send(session_id, msg)

    async def render_question(
        self, session_id: str, question: str, options: Sequence[str],
        timeout: float, metadata: ... = None,
    ) -> str | None:
        # 1. 构造含按钮/内联键盘的审批消息
        msg = OutputMessage(
            content=question,
            metadata={
                **(metadata or {}),
                "_approval_options": list(options),
            },
        )
        msg_id = await self._output.send(session_id, msg)

        # 2. 轮询 ControlChannel 等待 APPROVAL_RESPONSE
        scope = ControlScope(session_id=session_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cmds = await self._channel.drain(
                scope, limit=1,
                command_types={ControlCommandType.APPROVAL_RESPONSE},
            )
            for cmd in cmds:
                action = str(cmd.payload.get("action", ""))
                if action in options:
                    return action
            await asyncio.sleep(0.3)

        # 3. 超时
        return None

    async def update_message(
        self, session_id: str, message_id: str, content: str,
    ) -> None:
        msg = OutputMessage(content=content, metadata={"_edit_id": message_id})
        await self._output.send(session_id, msg)
```

```python
# framework/control/ui/noop.py

class NoopUserInterface(ControlUserInterface):
    """无用户界面（cron/sandbox/headless）。

    所有消息静默丢弃，所有问题返回默认选项或 None。
    """

    async def render_message(self, ...) -> str:
        return ""

    async def render_question(self, ...) -> str | None:
        return None  # 总是超时
```

### 4.3 ControlWaitStrategy 实现

```python
# framework/control/wait_strategy.py

class InlineWaitStrategy(ControlWaitStrategy):
    """进程内阻塞等待。

    通过 ControlChannel.drain() 轮询等待 APPROVAL_RESPONSE。
    协程挂起 = 等待状态。进程死则审批丢失。
    适用于单机 bot、CLI 场景。
    """

    def __init__(self, channel: ControlChannel) -> None:
        self._channel = channel

    async def wait(
        self, *, session_id: str, ui: ControlUserInterface,
        timeout: float, poll_interval: float = 0.3,
    ) -> WaitResult:
        scope = ControlScope(session_id=session_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cmds = await self._channel.drain(
                scope, limit=1,
                command_types={ControlCommandType.APPROVAL_RESPONSE},
            )
            for cmd in cmds:
                action = str(cmd.payload.get("action", ""))
                return WaitResult(value=action)
            await asyncio.sleep(poll_interval)
        return WaitResult(value=None)  # timeout


class SuspendResumeWaitStrategy(ControlWaitStrategy):
    """挂起-恢复等待。

    流程：
    1. 从 AgentContext 提取当前状态，构建 AgentCheckpoint
    2. 保存 AgentCheckpoint 到 CheckpointStore
    3. 抛出 AgentAwaitingApproval(checkpoint_id) 异常
    4. ReActAgent catch → 保存完整 AgentCheckpoint → 干净退出
    5. 外部 ResumeService 检测到审批响应 → load checkpoint → 继续执行

    适用于分布式多 server、需要跨进程恢复的场景。
    """

    def __init__(
        self,
        checkpoint_store: CheckpointStore,
        channel: ControlChannel,
    ) -> None:
        self._checkpoint_store = checkpoint_store
        self._channel = channel

    async def wait(
        self, *, session_id: str, ui: ControlUserInterface,
        timeout: float, poll_interval: float = 0.3,
    ) -> WaitResult:
        # 1. 轮询一小段时间（给用户快速响应的机会，避免不必要的挂起）
        scope = ControlScope(session_id=session_id)
        short_deadline = time.monotonic() + 2.0  # 2s 快速窗口
        while time.monotonic() < short_deadline:
            cmds = await self._channel.drain(
                scope, limit=1,
                command_types={ControlCommandType.APPROVAL_RESPONSE},
            )
            for cmd in cmds:
                action = str(cmd.payload.get("action", ""))
                return WaitResult(value=action)
            await asyncio.sleep(poll_interval)

        # 2. 没有快速响应 → 保存 checkpoint 并抛异常挂起
        checkpoint_id = uuid4().hex
        # 注意：此处 checkpoint 由调用者（interceptor）构建并保存
        # 此 wait 方法只负责抛出挂起信号
        raise AgentAwaitingApproval(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            timeout_at=time.monotonic() + timeout,
        )
```

### 4.4 CheckpointStore 实现

```python
# framework/control/checkpoint/store.py

class StateStoreBackedCheckpointStore(CheckpointStore):
    """基于 StateStore 的检查点存储。

    key 格式: checkpoint/{session_id}/{checkpoint_id}
    """

    def __init__(self, state_store: StateStore) -> None:
        self._store = state_store

    async def save(self, checkpoint: AgentCheckpoint) -> None:
        key = f"checkpoint/{checkpoint.session_id}/{checkpoint.checkpoint_id}"
        await self._store.set(key, checkpoint)

    async def load(self, checkpoint_id: str) -> AgentCheckpoint | None:
        # 通过 list_keys 搜索匹配此 checkpoint_id 的 key
        all_keys = await self._store.list_keys("checkpoint/")
        for key in all_keys:
            if checkpoint_id in key:
                cp = await self._store.get(key)
                if isinstance(cp, AgentCheckpoint):
                    return cp
        return None

    async def delete(self, checkpoint_id: str) -> None:
        all_keys = await self._store.list_keys("checkpoint/")
        for key in all_keys:
            if checkpoint_id in key:
                await self._store.delete(key)
                return

    async def list_by_session(self, session_id: str) -> Sequence[AgentCheckpoint]:
        keys = await self._store.list_keys(f"checkpoint/{session_id}/")
        result: list[AgentCheckpoint] = []
        for key in keys:
            cp = await self._store.get(key)
            if isinstance(cp, AgentCheckpoint):
                result.append(cp)
        return result
```

### 4.5 ApprovalStore 实现

```python
# framework/approval/builtin/store.py

class StateStoreBackedApprovalStore(ApprovalStore):
    """基于 StateStore 的审批状态存储。

    key 格式:
        approval/{session_id}/pattern/{pattern_key}  — 已批准 pattern
        approval/{session_id}/yolo                    — YOLO 模式开关
    """

    def __init__(self, state_store: StateStore) -> None:
        self._store = state_store

    async def is_pattern_approved(self, session_id: str, pattern_key: str) -> bool:
        return await self._store.exists(
            f"approval/{session_id}/pattern/{pattern_key}"
        )

    async def approve_pattern(self, session_id: str, pattern_key: str) -> None:
        await self._store.set(
            f"approval/{session_id}/pattern/{pattern_key}",
            True,
        )

    async def is_yolo_enabled(self, session_id: str) -> bool:
        val = await self._store.get(f"approval/{session_id}/yolo")
        return val is True

    async def set_yolo(self, session_id: str, enabled: bool) -> None:
        await self._store.set(f"approval/{session_id}/yolo", enabled)

    async def clear_session(self, session_id: str) -> None:
        keys = await self._store.list_keys(f"approval/{session_id}/")
        for key in keys:
            await self._store.delete(key)
```

### 4.6 ToolNameMatcher 实现

```python
# framework/approval/builtin/matcher.py

class ExactNameMatcher(ToolNameMatcher):
    """精确匹配工具名称。"""
    def __init__(self, names: set[str]) -> None:
        self._names = frozenset(names)

    def matches(self, tool_name: str) -> bool:
        return tool_name in self._names


class PatternMatcher(ToolNameMatcher):
    """通配符/前缀匹配工具名称。如 "mcp:*" 匹配所有 MCP 工具。"""
    def __init__(self, patterns: set[str]) -> None:
        self._exact: frozenset[str] = frozenset(
            p for p in patterns if "*" not in p
        )
        self._prefixes: tuple[str, ...] = tuple(
            p.rstrip("*") for p in patterns if p.endswith("*")
        )

    def matches(self, tool_name: str) -> bool:
        if tool_name in self._exact:
            return True
        for prefix in self._prefixes:
            if tool_name.startswith(prefix):
                return True
        return False


class CompositeMatcher(ToolNameMatcher):
    """组合多个匹配器，任一匹配即返回 True。"""
    def __init__(self, matchers: Sequence[ToolNameMatcher]) -> None:
        self._matchers = tuple(matchers)

    def matches(self, tool_name: str) -> bool:
        return any(m.matches(tool_name) for m in self._matchers)
```

---

## 五、TieredToolApprovalInterceptor（核心增强）

### 5.1 职责划分

Interceptor 中的审批流程分为三步，每步依赖不同的 ABC：

```
1. render_message (ControlUserInterface) — 通知用户：发送审批消息
2. wait (ControlWaitStrategy)           — 等待响应：阻塞 / 挂起-恢复
3. update_message (ControlUserInterface) — 标记完成：编辑原消息
```

`ControlUserInterface.render_question()` 是 CLI 场景的**便捷方法**（它内部就是 render_message + wait 的组合），对 CLI 用户而言审批是原子的。IM 场景下 render_message 和 wait 是分开的两步——render_message 发送消息后立即返回，wait 阻塞在 ControlChannel 上。

### 5.2 Interceptor 完整实现

```python
# framework/approval/builtin/interceptor.py

class TieredToolApprovalInterceptor:
    """三级审批拦截器（增强版）。

    关键设计：
    1. 全部依赖通过构造函数注入 ABC，零硬编码
    2. _batch_denied 标记：批内前一个拒绝后，后续 tool 自动拒绝不再发审批
    3. 永远返回合法 ToolResult（不抛异常），保证 history 完整性
    4. 审批流程分三步：render_message → wait → update_message，每个对应一个 ABC
    """

    scopes: frozenset[InterceptorScope] = frozenset([InterceptorScope.TOOL_CALL])

    def __init__(
        self,
        *,
        hardline_matcher: ToolNameMatcher | None = None,
        dangerous_matcher: ToolNameMatcher | None = None,
        sensitive_matcher: ToolNameMatcher | None = None,
        approval_ui: ControlUserInterface | None = None,
        approval_store: ApprovalStore | None = None,
        wait_strategy: ControlWaitStrategy | None = None,
        event_bus: ControlEventBus | None = None,
        approval_timeout: float = 300.0,
        on_denied: DenyAction = DenyAction.TOOL_ERROR,
        on_timeout: TimeoutAction = TimeoutAction.TOOL_ERROR,
    ) -> None:
        self._hardline_matcher = hardline_matcher
        self._dangerous_matcher = dangerous_matcher
        self._sensitive_matcher = sensitive_matcher
        self._ui: ControlUserInterface = approval_ui or NoopUserInterface()
        self._store: ApprovalStore = (
            approval_store
            or StateStoreBackedApprovalStore(InMemoryStateStore())
        )
        self._wait: ControlWaitStrategy = (
            wait_strategy
            or InlineWaitStrategy(InMemoryControlChannel())
        )
        self._event_bus = event_bus
        self._approval_timeout = approval_timeout
        self._on_denied = on_denied
        self._on_timeout = on_timeout

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        tool_name = call.tool_name

        # ── 0) 检查 batch 拒绝标记 ──
        denied = ctx.metadata.get("_approval_batch_denied", False)
        if denied:
            return ToolResult(
                tool_name=tool_name,
                call_id=call.tool_call.call_id or "",
                error=(
                    f"Tool '{tool_name}' was not executed because a prior tool "
                    f"in this batch was denied."
                ),
            )

        # ── 1) Hardline: 无条件拒绝 ──
        if self._hardline_matcher and self._hardline_matcher.matches(tool_name):
            return ToolResult(
                tool_name=tool_name,
                call_id=call.tool_call.call_id or "",
                error=f"Error: '{tool_name}' is blocked by safety policy (hardline).",
            )

        # ── 2) Dangerous: 必须审批 ──
        if self._dangerous_matcher and self._dangerous_matcher.matches(tool_name):
            return await self._request_approval(
                ctx, call, next_call, ApprovalTier.DANGEROUS,
            )

        # ── 3) Sensitive: YOLO 可跳过 ──
        if self._sensitive_matcher and self._sensitive_matcher.matches(tool_name):
            yolo = await self._store.is_yolo_enabled(ctx.session_id)
            if not yolo:
                return await self._request_approval(
                    ctx, call, next_call, ApprovalTier.SENSITIVE,
                )

        # ── 4) Normal: 直接放行 ──
        return await next_call()

    # ── 内部方法 ──

    async def _request_approval(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
        tier: ApprovalTier,
    ) -> ToolResult:
        request = self._build_request(ctx, call, tier)
        self._emit_event(ctx, request)
        message_id = await self._ui.render_message(
            session_id=ctx.session_id,
            content=self._format_approval_message(request),
            metadata={"_approval_request_id": request.request_id},
        )

        # ── 等待用户响应（唯一差异点：Inline vs SuspendResume） ──
        wait_result: WaitResult
        try:
            wait_result = await self._wait.wait(
                session_id=ctx.session_id,
                ui=self._ui,
                timeout=self._approval_timeout,
            )
        except AgentAwaitingApproval:
            # SuspendResume 策略：不在 interceptor 中处理，异常透传到 ReActAgent
            raise

        # 构建 ApprovalResponse
        choice = wait_result.value
        response: ApprovalResponse | None = None
        if choice is not None and isinstance(choice, str):
            action = (
                ApprovalAction.ALLOW if choice == "allow"
                else ApprovalAction.DENY
            )
            response = ApprovalResponse(
                request_id=request.request_id,
                action=action,
                choice=choice,
            )

        # 更新原消息状态
        if response is not None:
            await self._ui.update_message(
                session_id=ctx.session_id,
                message_id=message_id,
                content=self._format_resolved_message(request, response),
            )

        # 处理结果
        if response is None:
            return self._handle_timeout(ctx, call, request)
        if response.action == ApprovalAction.DENY:
            return self._handle_denied(ctx, call, request)
        return await next_call()

    def _build_request(
        self, ctx: AgentContext, call: ToolCallContext, tier: ApprovalTier,
    ) -> ApprovalRequest:
        return ApprovalRequest(
            request_id=uuid4().hex,
            tool_name=call.tool_name,
            tool_call_id=call.tool_call.call_id or "",
            tier=tier,
            redacted_arguments=MappingProxyType(
                self._redact_args(call.arguments)
            ),
            session_id=ctx.session_id,
            turn_id=call.turn_id,
            iteration=ctx.metadata.get("iteration", 0),
            description=(
                f"Tool '{call.tool_name}' requires approval (tier={tier.value})"
            ),
        )

    def _emit_event(
        self, ctx: AgentContext, request: ApprovalRequest,
    ) -> None:
        if not self._event_bus:
            return
        asyncio.create_task(self._event_bus.emit(ControlEvent(
            event_id=uuid4().hex,
            type=ControlEventType.TOOL_APPROVAL_REQUESTED,
            scope=ControlScope(session_id=ctx.session_id),
            correlation_id=request.request_id,
            payload={
                "tool_name": request.tool_name,
                "tier": request.tier.value,
                "request_id": request.request_id,
            },
        )))

    def _handle_denied(
        self, ctx: AgentContext, call: ToolCallContext,
        request: ApprovalRequest,
    ) -> ToolResult:
        ctx.metadata["_approval_batch_denied"] = True
        ctx.metadata["_approval_denial"] = ApprovalOutcome(
            request=request,
            resolution=ApprovalResolution.DENIED,
            reason=f"Tool '{call.tool_name}' denied by user",
        )
        return ToolResult(
            tool_name=call.tool_name,
            call_id=call.tool_call.call_id or "",
            error=(
                f"Tool '{call.tool_name}' was not approved by the user. "
                f"The tool was not executed."
            ),
        )

    def _handle_timeout(
        self, ctx: AgentContext, call: ToolCallContext,
        request: ApprovalRequest,
    ) -> ToolResult:
        ctx.metadata["_approval_batch_denied"] = True
        ctx.metadata["_approval_denial"] = ApprovalOutcome(
            request=request,
            resolution=ApprovalResolution.TIMED_OUT,
            reason=f"Approval timeout for '{call.tool_name}'",
        )
        return ToolResult(
            tool_name=call.tool_name,
            call_id=call.tool_call.call_id or "",
            error=(
                f"Tool approval timed out for '{call.tool_name}'. "
                f"The tool was not run."
            ),
        )

    @staticmethod
    def _redact_args(arguments: Mapping[str, object]) -> dict[str, object]:
        sensitive: frozenset[str] = frozenset({
            "api_key", "secret", "token", "password", "credential",
            "access_key", "private_key",
        })
        return {
            k: ("***" if k.lower() in sensitive else v)
            for k, v in arguments.items()
        }

    @staticmethod
    def _format_approval_message(request: ApprovalRequest) -> str:
        args_str = ", ".join(
            f"{k}={v}" for k, v in request.redacted_arguments.items()
        )
        return (
            f"⚠️ **Tool Approval Required** [{request.tier.value}]\n\n"
            f"**Tool:** `{request.tool_name}`\n"
            f"**Arguments:** `{args_str}`\n\n"
            f"Reply `/approve` to execute or `/deny` to reject."
        )

    @staticmethod
    def _format_resolved_message(
        request: ApprovalRequest, response: ApprovalResponse,
    ) -> str:
        status = "✅ Approved" if response.action == ApprovalAction.ALLOW else "❌ Denied"
        return (
            f"~~{TieredToolApprovalInterceptor._format_approval_message(request)}~~\n"
            f"{status}"
        )
```

---

## 六、ReActAgent 批内补齐（已有增强）

```python
# ReActAgent._execute_tools() 中 tool 执行循环

tool_calls = ...  # LLM 返回的 tool_calls

for idx, tool_call in enumerate(tool_calls):
    result = await self._execute_tool(tool_call, context)
    # ... 写入 messages/history ...

    # ── 检测 batch_denied 标记 ──
    if context.metadata.get("_approval_batch_denied"):
        # 补齐所有后续未执行 tool_call 的伪结果
        for remaining_tc in tool_calls[idx + 1:]:
            synthetic = ToolResult(
                tool_name=remaining_tc.tool_name,
                call_id=remaining_tc.call_id or "",
                error=(
                    "Error: Not executed — a prior tool in this batch "
                    "was denied by the user."
                ),
            )
            messages.append(synthetic)
            await context.history.append(self._build_tool_message(
                synthetic, remaining_tc.call_id
            ))
        break

# finally 中清理标记
finally:
    context.metadata.pop("_approval_batch_denied", None)
    context.metadata.pop("_approval_denial", None)
```

---

## 七、IMCommandRouter — IM 命令解析路由

```python
# examples/bot_project/bot/command_router.py 或在框架层

class IMCommandRouter:
    """IM 控制命令路由。

    解析用户消息中的控制命令（/approve, /deny 等），
    转换为 ControlCommand 并发送到 ControlChannel。
    """

    def __init__(
        self,
        *,
        input_adapter: InputAdapter,
        channel: ControlChannel,
    ) -> None:
        self._input = input_adapter
        self._channel = channel

    async def handle_message(
        self,
        session_id: str,
        raw_text: str,
    ) -> bool:
        """尝试解析消息为控制命令。

        Returns:
            True = 已解析为控制命令（不要再走 agent pipeline）
            False = 普通消息，走正常 pipeline
        """
        text = raw_text.strip()

        # /approve — 允许当前待审批 tool
        if text.startswith("/approve"):
            await self._channel.send(ControlCommand(
                command_id=uuid4().hex,
                type=ControlCommandType.APPROVAL_RESPONSE,
                scope=ControlScope(session_id=session_id),
                payload={"action": "allow"},
            ))
            return True

        # /deny — 拒绝当前待审批 tool
        if text.startswith("/deny"):
            await self._channel.send(ControlCommand(
                command_id=uuid4().hex,
                type=ControlCommandType.APPROVAL_RESPONSE,
                scope=ControlScope(session_id=session_id),
                payload={"action": "deny"},
            ))
            return True

        # /yolo — 开启 YOLO 模式
        if text.startswith("/yolo"):
            await self._channel.send(ControlCommand(
                command_id=uuid4().hex,
                type=ControlCommandType.SET_DYNAMIC_CONFIG,
                scope=ControlScope(session_id=session_id),
                payload={"approval_yolo": True},
            ))
            return True

        return False
```

---

## 八、忽略审批处理

### 8.1 忽略审批的统一处理

```
工具审批等待中（协程阻塞在 ControlChannel.drain 或 ControlUserInterface.render_question）
  │
  ├─ 用户回复 /approve → approval_action=allow → 执行 tool ✓
  ├─ 用户回复 /deny   → approval_action=deny  → 返回 error ToolResult, batch_denied ✓
  ├─ 超时 (IM: 300s)  → ApprovalResolution.TIMED_OUT → 等效 deny ✓
  └─ 用户发普通消息   → BusyInputMode.INTERRUPT
                        → AgentPipeline 取消当前 turn task
                        → CancelledError → finally 补齐未执行 tool 的伪结果
                        → 新消息启动新 turn
```

### 8.2 忽略审批处理（IM 场景）

| 事件 | 处理 | Tool 结果保证 |
|------|------|:---------:|
| 用户点 Allow | 执行 tool | ToolResult(正常) |
| 用户点 Deny | 返回 error ToolResult | ToolResult(error=...) |
| 超时 (5 min) | 等效 deny, 返回 error ToolResult | ToolResult(error=...) |
| 用户发新消息 | INTERRUPT → cancel task → 补齐未执行 tool 的伪结果 → 新 turn | ToolResult(error=...) |

所有路径保证 assistant 声明的每条 tool_call 都有对应 tool role 消息。

---

## 九、完整装配示例

### 9.1 单机 Bot（bot_project 主场景）

```python
from pathlib import Path
from framework.control.state_store.file import JsonFileStateStore
from framework.control.ui.im import IMUserInterface
from framework.control.wait_strategy import InlineWaitStrategy
from framework.control.checkpoint.store import StateStoreBackedCheckpointStore
from framework.control.channel import InMemoryControlChannel
from framework.control.event_bus import CallbackControlEventBus
from framework.approval.builtin.interceptor import TieredToolApprovalInterceptor
from framework.approval.builtin.store import StateStoreBackedApprovalStore
from framework.approval.builtin.matcher import ExactNameMatcher, PatternMatcher

# ── 存储层：本地 JSON 文件（重启可恢复） ──
state_store = JsonFileStateStore(Path("data/state"))

# ── 控制平面 ──
channel = InMemoryControlChannel()
event_bus = CallbackControlEventBus()
checkpoint_store = StateStoreBackedCheckpointStore(state_store)

# ── 用户界面：IM ──
ui = IMUserInterface(
    output_adapter=qq_output_adapter,
    channel=channel,
    command_router=IMCommandRouter(
        input_adapter=qq_input_adapter,
        channel=channel,
    ),
)

# ── 审批 ──
approval = TieredToolApprovalInterceptor(
    hardline_matcher=ExactNameMatcher({
        "rm_rf_root", "dd_raw_device", "shell_unsafe",
    }),
    dangerous_matcher=ExactNameMatcher({
        "shell", "delete_file", "write_file",
    }),
    sensitive_matcher=PatternMatcher({
        "edit_file", "spawn_subagent", "mcp:*",
    }),
    approval_ui=ui,
    approval_store=StateStoreBackedApprovalStore(state_store),
    wait_strategy=InlineWaitStrategy(channel),
    event_bus=event_bus,
    approval_timeout=300.0,
    on_denied=DenyAction.TOOL_ERROR,
    on_timeout=TimeoutAction.TOOL_ERROR,
)
```

### 9.2 分布式多 Server 场景

```python
from framework.control.state_store.redis import RedisStateStore
from framework.control.wait_strategy import SuspendResumeWaitStrategy

state_store = RedisStateStore(redis_url="redis://cluster:6379/0")
channel = RedisControlChannel(redis_url="redis://cluster:6379/0")  # 跨 server

approval = TieredToolApprovalInterceptor(
    # ... 其他同上 ...
    wait_strategy=SuspendResumeWaitStrategy(
        checkpoint_store=StateStoreBackedCheckpointStore(state_store),
    ),
)
```

### 9.3 CLI 本地调试

```python
from framework.control.state_store.memory import InMemoryStateStore
from framework.control.ui.cli import CLIUserInterface

state_store = InMemoryStateStore()

approval = TieredToolApprovalInterceptor(
    # ... 同上 ...
    approval_ui=CLIUserInterface(),
    approval_store=StateStoreBackedApprovalStore(state_store),
    wait_strategy=InlineWaitStrategy(InMemoryControlChannel()),
    approval_timeout=60.0,  # CLI 场景超时更短
)
```

---

## 十、逐条审批时序（LLM 返回多个 tool_call）

```
assistant(tool_calls=[tc1(dangerous), tc2(normal), tc3(dangerous), tc4(sensitive)])

ReActAgent._execute_tools():
  ┌─ tc1(shell "rm -rf /cache") ──────────────────────────────────┐
  │   interceptor.around_tool_call(tc1)                             │
  │     → 匹配 dangerous                                            │
  │     → ui.render_question("Approve shell?", ["allow","deny"])    │
  │     → [用户点 ✅ Allow]                                         │
  │     → 执行 tc1 → ToolResult(content="deleted")   ✓              │
  └────────────────────────────────────────────────────────────────┘

  ┌─ tc2(read_file "config.py") ──────────────────────────────────┐
  │   → 匹配 normal → 直接执行                      ✓              │
  └────────────────────────────────────────────────────────────────┘

  ┌─ tc3(shell "docker restart") ─────────────────────────────────┐
  │   interceptor.around_tool_call(tc3)                             │
  │     → 匹配 dangerous                                            │
  │     → ui.render_question("Approve shell?", ["allow","deny"])    │
  │     → [用户点 ❌ Deny]                                         │
  │     → 返回 ToolResult(error="denied")                            │
  │     → 设置 ctx.metadata["_approval_batch_denied"] = True       │
  └────────────────────────────────────────────────────────────────┘

  ┌─ tc4(write_file "deploy.sh") ─────────────────────────────────┐
  │   → 检测 _approval_batch_denied = True                          │
  │     → 跳过审批，直接返回                                          │
  │       ToolResult(error="not executed — prior tool denied")      │
  │     → ReActAgent 补齐                                             │
  │     → break                                                      │
  └────────────────────────────────────────────────────────────────┘

  结果:
    tool(tc1, "deleted")           ✓
    tool(tc2, "file content...")   ✓
    tool(tc3, "error: denied")    ✗
    tool(tc4, "error: preempted") ✗
    4 声明 = 4 结果 ✓

  用户不再收到 tc4 的审批请求。批内拒绝终止。
```

---

## 十一、恢复流程（SuspendResumeWaitStrategy）

```python
# 审批响应到达后的恢复流程

async def resume_after_approval_response(
    checkpoint_id: str,
    checkpoint_store: CheckpointStore,
    agent_factory: AgentFactory,
) -> None:
    """外部系统检测到审批响应后，从 checkpoint 恢复 agent 继续执行。"""

    # 1. 加载 agent 执行检查点
    agent_cp = await checkpoint_store.load(checkpoint_id)
    if not agent_cp:
        logger.error("Checkpoint not found: %s", checkpoint_id)
        return

    # 2. 重建 agent context
    ctx = await build_agent_context(agent_cp)

    # 3. 恢复 agent 执行（从断点继续）
    agent = agent_factory.create(ctx)
    await agent._continue_after_approval(ctx, agent_cp)
```

---

## 十二、新增/修改文件清单

| 文件 | 操作 | 说明 |
|------|:----:|------|
| `framework/control/abc.py` | 新增 | `ControlWaitStrategy` ABC |
| `framework/control/wait_strategy.py` | 新增 | `InlineWaitStrategy`, `SuspendResumeWaitStrategy` |
| `framework/control/state_store/abc.py` | 新增 | `StateStore` ABC |
| `framework/control/state_store/memory.py` | 新增 | `InMemoryStateStore` |
| `framework/control/state_store/file.py` | 新增 | `JsonFileStateStore` |
| `framework/control/state_store/redis.py` | 新增 | `RedisStateStore` |
| `framework/control/ui/abc.py` | 新增 | `ControlUserInterface` ABC |
| `framework/control/ui/cli.py` | 新增 | `CLIUserInterface` |
| `framework/control/ui/im.py` | 新增 | `IMUserInterface` |
| `framework/control/ui/noop.py` | 新增 | `NoopUserInterface` |
| `framework/control/checkpoint/abc.py` | 重构 | `CheckpointStore` ABC + `AgentCheckpoint` dataclass |
| `framework/control/checkpoint/store.py` | 新增 | `StateStoreBackedCheckpointStore` |
| `framework/control/checkpoint.py` | 删除 | **已有文件重写** — 迁移至 checkpoint/ 子包 |
| `framework/approval/__init__.py` | 新增 | 包初始化 + 公开 API re-export |
| `framework/approval/abc.py` | 新增 | `ApprovalStore` ABC + `ApprovalRequest`/`Response`/`Outcome` dataclasses |
| `framework/approval/types.py` | 新增 | `ApprovalTier`, `DenyAction`, `TimeoutAction`, `ApprovalResolution` 等枚举 |
| `framework/approval/builtin/interceptor.py` | 新增 | `TieredToolApprovalInterceptor`（从 `framework/interceptor/builtin/tool_approval.py` 迁移增强） |
| `framework/approval/builtin/store.py` | 新增 | `StateStoreBackedApprovalStore` |
| `framework/approval/builtin/matcher.py` | 新增 | `ExactNameMatcher`, `PatternMatcher`, `CompositeMatcher` |
| `framework/interceptor/builtin/tool_approval.py` | 删除 | **迁移至 approval/ 包** |
| `examples/bot_project/bot/command_router.py` | 新增 | `IMCommandRouter`（/approve, /deny, /yolo 命令解析） |
| `examples/bot_project/bot/service/core.py` | 修改 | 装配新审批组件 |
| `examples/bot_project/bot/service/builders.py` | 修改 | 移除硬编码的 tool 限制，改用审批体系 |
| `framework/control/types.py` | 修改 | `ControlCommandType` + `IGNORED` 等 |
| `framework/control/channel.py` | 不变 | 已有实现满足需求 |
| `framework/control/event_bus.py` | 不变 | 已有实现满足需求 |
| `framework/control/exceptions.py` | 修改 | 新增 `AgentAwaitingApproval` 异常 |
| `framework/interceptor/abc.py` | 不变 | 已有实现满足需求 |
| `framework/interceptor/chain.py` | 不变 | 已有实现满足需求 |
| `framework/hook/builtin/dynamic_tool_filter.py` | 修改 | 移除目录限制逻辑，改用审批体系 |
| `framework/agents/react/agent.py` | 修改 | `_batch_denied` 标记检测 + 补齐逻辑 + `finally` 清理 |
| `framework/pipeline/pipeline.py` | 修改 | `BusyInputMode` 集成 |

---

## 十三、实施优先级

| 优先级 | 内容 | 依赖 |
|:------:|------|------|
| **P0** | `StateStore` ABC + `InMemoryStateStore` + `JsonFileStateStore` | 无 |
| **P0** | `ControlUserInterface` ABC + `CLIUserInterface` + `NoopUserInterface` | 无 |
| **P0** | `ControlWaitStrategy` ABC + `InlineWaitStrategy` | 无 |
| **P0** | `CheckpointStore` ABC + `StateStoreBackedCheckpointStore` | StateStore |
| **P0** | `ApprovalStore` ABC + `StateStoreBackedApprovalStore` | StateStore |
| **P0** | `ToolNameMatcher` 体系 (`ExactNameMatcher`, `PatternMatcher`, `CompositeMatcher`) | 无 |
| **P0** | `TieredToolApprovalInterceptor` 增强（ABC 注入 + `_batch_denied` 标记） | 全部以上 |
| **P0** | ReActAgent `_batch_denied` 补齐逻辑 + `finally` 清理 | TieredToolApprovalInterceptor |
| **P0** | 旧 `framework/interceptor/builtin/tool_approval.py` 删除 | TieredToolApprovalInterceptor |
| **P1** | `IMUserInterface` + `IMCommandRouter` | ControlUserInterface, OutputAdapter |
| **P1** | bot_project 装配（移除目录限制，接入审批体系） | IMUserInterface, IMCommandRouter |
| **P1** | `SuspendResumeWaitStrategy` + `AgentAwaitingApproval` 异常 | CheckpointStore |
| **P1** | `BusyInputMode` Pipeline 集成 | AgentPipeline |
| **P2** | `RedisStateStore` + `RedisControlChannel` | StateStore ABC |
