# Unified Session ID & Communication 系统检视报告

> **检视日期**: 2026-05-22
> **检视基准**: `docs/superpowers/specs/2026-05-22-unified-session-id-communication-design.md`
> **检视范围**: 数据流架构、工具提示词设计、Agent 通信机制、通信记录管理、会话标识系统、历史实现清理、编码规范符合性、测试场景覆盖

---

## 目录

1. [检视总结](#1-检视总结)
2. [数据流架构](#2-数据流架构)
3. [工具提示词设计](#3-工具提示词设计)
4. [Agent 通信机制](#4-agent-通信机制)
5. [通信记录管理](#5-通信记录管理)
6. [会话标识系统](#6-会话标识系统)
7. [历史实现清理](#7-历史实现清理)
8. [编码规范符合性](#8-编码规范符合性)
9. [测试场景覆盖](#9-测试场景覆盖)
10. [问题汇总与优先级](#10-问题汇总与优先级)

---

## 1. 检视总结

### 1.1 整体评估

统一 Session ID 与 Agent 通信迁移的核心框架层改造已基本完成，包括 `AgentCommKind` 枚举、`AgentSessionMeta` 元数据、`DefaultSessionIdStrategy` 统一格式/解析、`AgentMessageEnvelope.uuid` 一等公民字段、`AgentCommunicationService` 内部路由服务、`SendToAgentTool`/`SendToAgentAsyncTool` 新工具等关键组件均已实现。

但检视发现 **17 个问题**（5 个严重、7 个重要、5 个一般），主要集中在：

- **`AgentSessionMeta.conversation_id` 赋值错误**（严重）：pipeline 将完整 `session_id` 赋给 `conversation_id`，导致通信服务使用错误的 conversation_id 构建目标 session
- **`bot_config.yml` 系统提示词未更新**（严重）：subagent 提示词仍引用 `send_message_async`，LLM 将调用不存在的工具
- **`core.py` 导入已删除的 `SendMessageAsyncTool`**（严重）：运行时将触发 `ImportError`
- **`CommunicationTracker` 未完成术语迁移**（重要）：仍使用 `invocation_id` 而非 `uuid`
- **`peer_validator.py` 检查旧工具名**（重要）：校验逻辑与实际工具不匹配

### 1.2 Spec 符合度矩阵

| Spec 要求 | 实现状态 | 符合度 |
|---|---|---|
| `AgentCommKind` 枚举 (NORMAL/SUBAGENT) | ✅ 已实现 | 完全符合 |
| `AgentSessionMeta` 在 `AgentContext` | ⚠️ 已实现但 `conversation_id` 赋值有误 | 部分符合 |
| 统一 Session ID 格式 `{conv}:{agent}[:{uuid}]` | ✅ 已实现 | 完全符合 |
| `DefaultSessionIdStrategy.format/parse` | ✅ 已实现 | 完全符合 |
| 移除 `main_session()`/`target_session()` | ✅ 已移除 | 完全符合 |
| 移除 peer-pair helpers | ✅ 已移除 | 完全符合 |
| `AgentMessageEnvelope.uuid` 一等公民字段 | ✅ 已实现 | 完全符合 |
| UUID 序列化/反序列化传播 | ✅ 已实现 | 完全符合 |
| `AgentCommunicationService` 内部服务 | ✅ 已实现 | 完全符合 |
| UUID 三态验证 (null/""/具体值) | ✅ 已实现 | 完全符合 |
| `SendToAgentTool`/`SendToAgentAsyncTool` | ✅ 已实现 | 完全符合 |
| 移除旧三个 LLM 工具类 | ⚠️ 类已移除，但引用未清理 | 部分符合 |
| bot_project 仅注册 async 工具 | ✅ 已实现 | 完全符合 |
| subagent 标记为 `AgentCommKind.SUBAGENT` | ✅ 已实现 | 完全符合 |
| 动态工具描述列出目标及类型 | ✅ 已实现 | 完全符合 |
| `CommunicationTracker` 使用 uuid 匹配 | ❌ 仍用 `invocation_id` | 不符合 |
| 旧工具名从所有生产代码移除 | ❌ 多处残留 | 不符合 |
| bot_project 提示词更新为新工具名 | ❌ 仍引用旧名 | 不符合 |

---

## 2. 数据流架构

### 2.1 UUID 传播链路检视

**检视路径**: Tool → `AgentCommunicationService._send()` → `AgentMessageEnvelope` → `InboxProducer.send()` / `MessageBroker.send_to()` → `AgentPool._consume_messages()` → `AgentMessageEnvelope.from_broker_message()` → dispatch

**结论**: UUID 在信封创建、序列化、传输、反序列化全链路中传播正确。

| 环节 | 文件 | 状态 | 说明 |
|---|---|---|---|
| 信封创建 | `communication.py:207-213` | ✅ | `envelope_uuid` 正确设置，subagent→normal 回复保留 caller uuid |
| Broker 序列化 | `envelope.py:53-55` | ✅ | `uuid` 写入 headers |
| Broker 反序列化 | `envelope.py:69` | ✅ | `headers.get("uuid") or None` 正确还原 |
| Inbox 序列化 | `inbox/producer.py:76-77` | ✅ | `uuid` 写入 metadata |
| Inbox 反序列化 | `bus.py:141` | ✅ | `uuid=msg.metadata.get("uuid")` 正确还原 |
| BrokerBridge 透传 | `broker_bridge.py:55` | ✅ | `uuid` 在 payload 和 headers 间透传 |

### 2.2 问题 D-1：`AgentSessionMeta.conversation_id` 赋值错误 [严重]

**文件**: `framework/pipeline/pipeline.py:579`

**问题描述**: `_build_runtime_and_context()` 中，`AgentSessionMeta.conversation_id` 被赋值为 `session_id`（完整 session ID，如 `conv-1:office-expert:a1b2c3`），而非纯粹的 `conversation_id`（如 `conv-1`）。

```python
agent_context.session_meta = AgentSessionMeta(
    conversation_id=session_id,  # ← 错误：这是完整 session_id，不是 conversation_id
    agent_name=getattr(self.agent, "name", "main"),
    comm_kind=...,
    uuid=...,
)
```

**影响**: `AgentCommunicationService._send()` 从 `context.session_meta.conversation_id` 读取 conversation_id 来构建目标 session ID。如果 conversation_id 包含 `:agent_name` 后缀，将导致构建出的 session ID 格式错误，如 `conv-1:office-expert:a1b2c3:main` 而非 `conv-1:main`。

**修复方案**: 从 `session_id` 中解析出 `conversation_id`：

```python
from framework.multi_agent.session_id import DefaultSessionIdStrategy

strategy = DefaultSessionIdStrategy()
parts = strategy.parse(session_id)
agent_context.session_meta = AgentSessionMeta(
    conversation_id=parts.conversation_id,
    agent_name=parts.agent_name,
    comm_kind=self.agent_descriptor.comm_kind if self.agent_descriptor else AgentCommKind.NORMAL,
    uuid=parts.uuid,
)
```

**验证**: 构造 `session_id="conv-1:office-expert:a1b2c3"` 的场景，验证 `session_meta.conversation_id == "conv-1"`。

### 2.3 问题 D-2：`AgentSessionMeta.uuid` 仅从 `input_metadata` 提取 [重要]

**文件**: `framework/pipeline/pipeline.py:581-583`

**问题描述**: 当前 uuid 仅在 `comm_kind == SUBAGENT` 时从 `input_metadata.get("uuid")` 提取。但对于 NORMAL agent，uuid 应始终为 None。对于 SUBAGENT，如果 `input_metadata` 中没有 `uuid` 字段（如从 inbox 唤醒路径），则 uuid 将丢失。

```python
uuid=(input_metadata or {}).get("uuid") if (
    self.agent_descriptor and self.agent_descriptor.comm_kind == AgentCommKind.SUBAGENT
) else None,
```

**影响**: 当 SUBAGENT 通过 inbox 唤醒处理消息时，如果 `input_metadata` 未传递 `uuid`，则 `session_meta.uuid` 将为 None，导致该 SUBAGENT 在后续调用通信工具时无法正确保留其 task uuid。

**修复方案**: 优先从 `session_id` 解析获取 uuid（如 D-1 修复方案所示），`input_metadata` 仅作为备选。

### 2.4 问题 D-3：`_execute_turn` 中 `conversation_id` 解析冗余 [一般]

**文件**: `framework/pipeline/pipeline.py:678`

**问题描述**: `_execute_turn()` 方法中重新解析 `session_id` 获取 `conversation_id` 并设置 `current_conversation_id` contextvar。如果 D-1 修复后 `session_meta` 已包含正确的 `conversation_id`，此处可直接使用 `ctx.session_meta.conversation_id`。

**修复方案**: 在 D-1 修复后，将 `current_conversation_id.set()` 改为从 `agent_context.session_meta.conversation_id` 读取，避免重复解析。

---

## 3. 工具提示词设计

### 3.1 工具参数检视

**检视对象**: `SendToAgentTool` 和 `SendToAgentAsyncTool` 的参数定义

| 参数 | 类型 | 必填 | Spec 要求 | 实现状态 |
|---|---|---|---|---|
| `target_agent` | string | ✅ | ✅ | 符合 |
| `content` | string | ✅ | ✅ | 符合 |
| `uuid` | ["string", "null"] | ✅ | ✅ | 符合 |

**UUID 参数描述**:

```python
_UUID_PARAM = {
    "type": ["string", "null"],
    "description": (
        "Routing selector. Use null for normal-agent delivery. "
        "Use an empty string to start a new subagent task. "
        "Use a concrete uuid to continue an existing subagent task."
    ),
}
```

**结论**: 参数定义与 Spec §5.2 完全一致。

### 3.2 动态工具描述检视

**文件**: `communication.py:247-261`

`build_targets_description()` 方法正确列出了可用目标及其类型，并提供了 uuid 使用指引。与 Spec §5.4 一致。

### 3.3 问题 T-1：`bot_config.yml` 系统提示词引用旧工具名 [严重]

**文件**: `examples/bot_project/config/bot_config.yml:128-192`

**问题描述**: `office-expert` 和 `query-12306` 的 `system_prompt` 中多处引用 `send_message_async`，而非新的 `send_to_agent_async`。具体包括：

- 第 128 行: `唯一能让 main 收到结果的方式是发起 send_message_async 工具调用`
- 第 135 行: `send_message_async(target_agent="main", content="...")`
- 第 141 行: `没有 send_message_async 调用的回复`
- 第 145 行: `作为 send_message_async 的 content 参数发送`
- 第 151 行: `通过 send_message_async 向 main 发送结果`
- 第 169-192 行: `query-12306` 同样的问题

**影响**: LLM 将尝试调用 `send_message_async` 工具，但该工具已不存在，导致工具调用失败。subagent 的结果将无法回传给 main agent。

**修复方案**: 将所有 `send_message_async` 替换为 `send_to_agent_async`，并更新调用示例以包含 `uuid` 参数：

```yaml
send_to_agent_async(
  target_agent="main",
  content="任务执行摘要：...",
  uuid=null
)
```

同时在提示词中说明 `uuid=null` 用于向 normal agent 发送消息。

### 3.4 问题 T-2：`bot_config.yml` 注释矩阵引用旧工具名 [一般]

**文件**: `examples/bot_project/config/bot_config.yml:16-32`

**问题描述**: 配置文件顶部的 Agent Capability Matrix 注释中，工具列仍显示 `send_message_async`、`spawn_subagent` 等旧名称。

**修复方案**: 更新注释矩阵，将工具名替换为 `send_to_agent_async`。

### 3.5 问题 T-3：工具返回消息缺少 subagent 任务创建提示 [一般]

**文件**: `framework/multi_agent/communication.py:131-134`

**问题描述**: `send_async()` 返回消息格式为 `"Message sent to {target_agent}. uuid: {uuid}"`，但缺少对 LLM 的后续操作指引。Spec §7.2 要求返回新创建的 uuid 以便 LLM 继续同一任务，但当前返回文本未提示 LLM 可以使用该 uuid 发送后续消息。

**修复方案**: 当 `created_new_task=True` 时，返回更详细的指引：

```python
if result.created_new_task:
    return (
        f"Message sent to {result.target_agent}. "
        f"uuid: {result.uuid}\n"
        f"Use this uuid to send follow-up messages to the same task."
    )
```

---

## 4. Agent 通信机制

### 4.1 通信路由检视

**检视对象**: `AgentCommunicationService._send()` 的路由逻辑

| 场景 | Spec 要求 | 实现状态 | 符合度 |
|---|---|---|---|
| NORMAL→NORMAL (uuid=None) | session=`conv:target`, envelope.uuid=None | ✅ | 符合 |
| NORMAL→SUBAGENT 新任务 (uuid="") | 生成新 uuid, session=`conv:target:{uuid}` | ✅ | 符合 |
| NORMAL→SUBAGENT 已有任务 (uuid="abc") | session=`conv:target:abc` | ✅ | 符合 |
| SUBAGENT→NORMAL 回复 (uuid=None) | session=`conv:main`, envelope.uuid=caller_uuid | ✅ | 符合 |
| NORMAL+uuid="" → 错误 | 验证拒绝 | ✅ | 符合 |
| NORMAL+uuid="abc" → 错误 | 验证拒绝 | ✅ | 符合 |
| SUBAGENT+uuid=None → 错误 | 验证拒绝 | ✅ | 符合 |
| 不存在的目标 → 错误 | agent-not-found | ✅ | 符合 |

**结论**: 核心路由逻辑与 Spec §5.3 和 §7 完全一致。

### 4.2 问题 C-1：`core.py` 导入已删除的 `SendMessageAsyncTool` [严重]

**文件**: `examples/bot_project/bot/service/core.py:72`

**问题描述**: `core.py` 仍从 `framework.multi_agent` 导入 `SendMessageAsyncTool`：

```python
from framework.multi_agent import (
    ...
    SendMessageAsyncTool,
    ...
)
```

并在第 605-611 行使用：

```python
sub_tool_manager.register(SendMessageAsyncTool(
    broker=self.broker, self_address=sub_address,
    allowed_targets=[parent_agent_name], agent_bus=self.agent_bus,
    registry=self.agent_pool, session_strategy=strategy,
))
```

但 `SendMessageAsyncTool` 类已从框架中删除，`__init__.py` 也不再导出。这将导致 **运行时 ImportError**。

**影响**: 任何使用 `core.py` 中 subagent 注册路径的部署将无法启动。

**修复方案**:
1. 移除 `SendMessageAsyncTool` 导入
2. 将第 605-611 行替换为使用 `SendToAgentAsyncTool` + `AgentCommunicationService` 的模式（与 `builders.py:_initialize_peer_agents()` 一致）

```python
from framework.multi_agent.communication import AgentCommunicationService
from framework.multi_agent.tools import SendToAgentAsyncTool

sub_address = AgentAddress(name=descriptor.address.name)
strategy = DefaultSessionIdStrategy(main_agent_name=parent_agent_name)
sub_service = AgentCommunicationService(
    source=sub_address, broker=self.broker, registry=self.agent_pool,
    agent_bus=self.agent_bus, session_strategy=strategy,
    comm_tracker=self.communication_tracker,
)
sub_tool_manager.register(SendToAgentAsyncTool(
    source=sub_address, broker=self.broker, registry=self.agent_pool,
    agent_bus=self.agent_bus, service=sub_service,
    comm_tracker=self.communication_tracker,
))
```

### 4.3 问题 C-2：`peer_validator.py` 检查旧工具名 [重要]

**文件**: `framework/multi_agent/peer_validator.py:34-36`

**问题描述**: 校验器检查 `denied_tools` 中是否包含 `send_message_async`：

```python
if "send_message_async" in denied:
    raise ValueError(
        f"Peer '{peer_name}' must not deny 'send_message_async' (needed to reply to main)"
    )
```

但实际注册的工具名已改为 `send_to_agent_async`。此校验将无法检测到 peer 错误地 deny 了新工具。

**影响**: 如果 peer 的 `denied_tools` 包含 `send_to_agent_async`，校验器不会报错，但 peer 将无法回复 main agent。

**修复方案**: 将检查的工具名更新为 `send_to_agent_async`：

```python
if "send_to_agent_async" in denied:
    raise ValueError(
        f"Peer '{peer_name}' must not deny 'send_to_agent_async' (needed to reply to main)"
    )
```

### 4.4 问题 C-3：`subagent_service.py` 文档字符串引用旧工具名 [一般]

**文件**: `framework/multi_agent/subagent_service.py:93`

**问题描述**: `admit_dynamic()` 方法的文档字符串仍引用 `send_message_async`：

```python
The subagent is now addressable via ``send_message_async`` like any
resident subagent.
```

**修复方案**: 更新为 `send_to_agent_async`。

### 4.5 问题 C-4：`communication.py` 中 inline import [一般]

**文件**: `framework/multi_agent/communication.py:207-208`

**问题描述**: `_send()` 方法中使用 inline import：

```python
from framework.multi_agent.address import AgentAddress
from framework.multi_agent.envelope import AgentMessageEnvelope
```

这些类型已在文件顶部通过 `TYPE_CHECKING` 导入，运行时 import 应移至文件顶部或使用已有的 TYPE_CHECKING 导入。

**修复方案**: 将 `AgentAddress` 和 `AgentMessageEnvelope` 的运行时导入移至文件顶部（非 TYPE_CHECKING 块内），因为 `_send()` 方法在运行时需要实例化这些类。

---

## 5. 通信记录管理

### 5.1 CommunicationTracker 检视

**检视对象**: `framework/multi_agent/comm_tracker.py`

### 5.2 问题 CT-1：`CommRecord.invocation_id` 未迁移为 `uuid` [重要]

**文件**: `framework/multi_agent/comm_tracker.py:38`

**问题描述**: `CommRecord` 数据类仍使用 `invocation_id` 字段名：

```python
@dataclass
class CommRecord:
    record_id: str
    owner_agent: str
    direction: CommDirection
    target_agent: str
    invocation_id: str | None  # ← 应为 uuid
    session_id: str | None
    ...
```

Spec §8 明确要求 `uuid` 替代 `invocation_id` 成为一等公民字段。`CommunicationTracker` 作为核心通信跟踪组件，应使用统一术语。

**影响**:
1. 术语不一致增加理解和维护成本
2. `build_prompt_section()` 向 LLM 展示 `invocation_id`，与新工具的 `uuid` 参数不一致，可能混淆 LLM
3. `communication.py:221` 调用 `comm_tracker.record_send(invocation_id=envelope.uuid)` 时需要术语转换

**修复方案**:
1. 将 `CommRecord.invocation_id` 重命名为 `uuid`
2. 更新所有引用该字段的方法签名和调用点
3. 更新 `build_prompt_section()` 中的提示文本，将 `invocation_id` 替换为 `uuid`

### 5.3 问题 CT-2：`build_prompt_section()` 提示文本与新工具不一致 [重要]

**文件**: `framework/multi_agent/comm_tracker.py:237-253`

**问题描述**: 提示文本仍指导 LLM 使用 `invocation_id`：

```python
f"(invocation_id: {record.invocation_id or 'N/A'})\n"
f"  Status: awaiting reply — use this invocation_id in responses"
```

```python
f"(invocation_id: {record.invocation_id or 'N/A'})\n"
f"  Status: needs acknowledgment — reply with matching invocation_id"
```

但新工具的参数名为 `uuid`，LLM 应使用 `uuid` 参数而非 `invocation_id`。

**影响**: LLM 可能尝试在工具调用中使用 `invocation_id` 参数名，导致参数错误。

**修复方案**: 更新提示文本：

```python
f"(uuid: {record.uuid or 'N/A'})\n"
f"  Status: awaiting reply — use this uuid in send_to_agent_async"
```

```python
f"(uuid: {record.uuid or 'N/A'})\n"
f"  Status: needs acknowledgment — reply with matching uuid"
```

### 5.4 问题 CT-3：`communication.py` 调用 tracker 时术语转换 [重要]

**文件**: `framework/multi_agent/communication.py:221`

**问题描述**: `AgentCommunicationService._send()` 调用 `comm_tracker.record_send()` 时使用 `invocation_id` 参数名：

```python
self._comm_tracker.record_send(
    agent_name=self._source.name,
    target_agent=target_agent,
    invocation_id=envelope.uuid,  # ← 术语转换
    session_id=session_id,
    content_summary=content[:500],
)
```

**修复方案**: 在 CT-1 修复后，将参数名改为 `uuid`。

---

## 6. 会话标识系统

### 6.1 Session ID 格式与解析检视

**检视对象**: `framework/multi_agent/session_id.py`

| 功能 | Spec 要求 | 实现状态 | 符合度 |
|---|---|---|---|
| NORMAL 格式 `{conv}:{agent}` | ✅ | ✅ | 符合 |
| SUBAGENT 格式 `{conv}:{agent}:{uuid}` | ✅ | ✅ | 符合 |
| 空 uuid 拒绝 | ✅ | ✅ | 符合 |
| 2-part/3-part 解析 | ✅ | ✅ | 符合 |
| 移除 `main_session()`/`target_session()` | ✅ | ✅ | 符合 |

**结论**: `DefaultSessionIdStrategy` 实现完全符合 Spec §4。

### 6.2 问题 S-1：`AgentSessionMeta` 缺少 `session_id` 属性 [重要]

**文件**: `framework/core/agent.py:30-37`

**问题描述**: Plan Task 1 中定义 `AgentSessionMeta` 应包含 `session_id` 属性：

```python
@dataclass(frozen=True)
class AgentSessionMeta:
    conversation_id: str
    agent_name: str
    comm_kind: AgentCommKind
    uuid: str | None = None

    @property
    def session_id(self) -> str:
        return DefaultSessionIdStrategy().format(
            conversation_id=self.conversation_id,
            agent_name=self.agent_name,
            uuid=self.uuid,
        )
```

但当前实现缺少此属性。多处代码需要从 `session_meta` 构建 session_id 时，必须手动调用 `DefaultSessionIdStrategy().format()`。

**影响**: 增加了重复代码和出错概率。例如 `subagent_auto_send.py:100-101` 中：

```python
strategy = DefaultSessionIdStrategy(main_agent_name=self._parent_name)
parts = strategy.parse(session_id)
conversation_id = parts.conversation_id
inbox_key = strategy.format(conversation_id=conversation_id, agent_name=self._parent_name)
```

如果有 `session_meta.session_id` 属性，可以直接使用。

**修复方案**: 在 `AgentSessionMeta` 中添加 `session_id` 属性（与 Plan 一致）。

### 6.3 问题 S-2：`AgentContext.session_id` 与 `session_meta` 信息冗余 [一般]

**文件**: `framework/core/agent.py:50`

**问题描述**: `AgentContext` 同时维护 `session_id: str` 和 `session_meta: AgentSessionMeta | None`。两者表达的是同一信息，但可能不一致。

**修复方案**: 长期应将 `session_id` 的权威来源统一为 `session_meta.session_id`（在 S-1 修复后），`AgentContext.session_id` 可作为向后兼容的便捷属性，从 `session_meta` 派生。

---

## 7. 历史实现清理

### 7.1 旧 API 残留检视

| 旧名称 | 残留位置 | 类型 | 严重度 |
|---|---|---|---|
| `SendMessageAsyncTool` | `examples/bot_project/bot/service/core.py:72,608` | 导入+使用 | 严重 |
| `send_message_async` | `examples/bot_project/config/bot_config.yml` (多处) | 提示词 | 严重 |
| `send_message_async` | `framework/multi_agent/peer_validator.py:34` | 校验逻辑 | 重要 |
| `send_message_async` | `framework/multi_agent/subagent_service.py:93` | 文档字符串 | 一般 |
| `invocation_id` | `framework/multi_agent/comm_tracker.py` (全文) | 字段/方法/提示 | 重要 |
| `invocation_id` | `framework/multi_agent/communication.py:221` | 参数名 | 重要 |
| `invocation_id` | `framework/multi_agent/pool.py:531,597,662,670` | 参数名 | 重要 |
| `send_message_async`/`dispatch_task` | `framework/multi_agent/AGENTS.md:53-54` | 文档 | 一般 |
| `send_message_async` | `examples/bot_project/AGENTS.md:43` | 文档 | 一般 |
| `send_message_async` | `examples/bot_project/README.md` (多处) | 文档 | 一般 |
| `send_message_async` | `examples/bot_project/tests/` (多处) | 测试 | 重要 |
| `current_conversation_id` | `framework/multi_agent/context.py` | ContextVar | 一般 |

### 7.2 问题 H-1：`core.py` 中 `SendMessageAsyncTool` 残留 [严重]

（同 C-1，此处不重复）

### 7.3 问题 H-2：测试文件引用旧工具名 [重要]

**文件**: `examples/bot_project/tests/test_agent_communication.py` (多处)

**问题描述**: 测试文件中大量引用 `send_message_async`，包括测试名称、文档字符串和断言。例如：

- 第 178 行: `# 2. Async send_message_async — main → inbox → peer`
- 第 183 行: `"""Verify send_message_async routes messages through inbox."""`
- 第 384 行: `tool_name="send_message_async", arguments={"target_agent": "main"}, result="ok"`

**影响**: 测试与实际实现不匹配，可能导致测试通过但功能不正确，或测试直接失败。

**修复方案**: 更新所有测试引用为新工具名 `send_to_agent_async`，并调整参数以包含 `uuid`。

### 7.4 问题 H-3：`current_conversation_id` ContextVar 仍存在 [一般]

**文件**: `framework/multi_agent/context.py:9-10`

**问题描述**: Spec §3.2 提到 `current_conversation_id` contextvar 可以在迁移完成后移除。当前 `pipeline.py:678` 仍在使用它。在 D-1 修复后，`session_meta.conversation_id` 可替代此 contextvar。

**修复方案**: 在所有使用 `current_conversation_id` 的地方改用 `session_meta.conversation_id`，然后移除 `context.py` 中的 contextvar 定义和 `__init__.py` 中的导出。

---

## 8. 编码规范符合性

### 8.1 规范检视结果

| 规则 | 检视结果 | 问题 |
|---|---|---|
| `from __future__ import annotations` | ✅ 所有框架模块均已添加 | 无 |
| 枚举/常量替代原始字符串 | ✅ `AgentCommKind` 使用 StrEnum | 无 |
| 类型化结构替代松散字典 | ✅ `AgentSessionMeta`, `AgentSendResult` 等使用 dataclass | 无 |
| 函数签名声明参数和返回类型 | ⚠️ 部分方法缺少返回类型 | 见下 |
| ABC/Protocol 用于扩展点 | ✅ `AgentRegistry` 使用 Protocol | 无 |
| 框架/示例代码分离 | ⚠️ `core.py` 中有框架级逻辑 | 见下 |
| 避免 `getattr`/`hasattr` | ⚠️ 多处使用 | 见下 |

### 8.2 问题 E-1：`pipeline.py` 中 `getattr` 使用 [一般]

**文件**: `framework/pipeline/pipeline.py:568,579`

**问题描述**: 使用 `getattr(self.agent, "name", "main")` 获取 agent 名称：

```python
agent_name=getattr(self.agent, "name", "main"),
```

根据编码规范，应避免 `getattr` 除非在真正的扩展边界。Agent 基类应提供 `name` 属性。

**修复方案**: 在 `Agent` 基类中添加 `name: str` 属性声明，然后直接访问 `self.agent.name`。

### 8.3 问题 E-2：`pool.py` 中中英文注释混用 [一般]

**文件**: `framework/multi_agent/pool.py` (多处)

**问题描述**: pool.py 中大量使用中文注释（如 `"""Agent 生命周期管理池。"""`、`"""注册常驻 Agent。"""` 等），与项目其他模块的英文注释风格不一致。

**修复方案**: 统一为英文注释，或根据团队约定统一为中文。建议框架层代码使用英文注释。

### 8.4 问题 E-3：`envelope.py` 中文 docstring [一般]

**文件**: `framework/multi_agent/envelope.py:18`

**问题描述**: `AgentMessageEnvelope` 的 docstring 为中文：`"""强制携带多 Agent 路由信息的通用消息信封。"""`

**修复方案**: 改为英文 docstring，如 `"""Universal message envelope carrying multi-agent routing information."""`

---

## 9. 测试场景覆盖

### 9.1 现有测试文件

| 测试文件 | 覆盖范围 | 状态 |
|---|---|---|
| `test_comm_kind_session_id.py` | AgentCommKind 枚举、Session ID 格式/解析 | ✅ 新增 |
| `test_envelope_uuid.py` | UUID 序列化/反序列化 | ✅ 新增 |
| `test_communication_service.py` | 通信服务路由逻辑 | ✅ 新增 |
| `test_send_to_agent_tools.py` | 新工具参数验证 | ✅ 新增 |
| `test_core_runtime.py` | 运行时上下文 | ⚠️ 引用旧名 |
| `test_subagent_auto_send_hook.py` | 自动转发 hook | ⚠️ 引用旧名 |
| `test_pool.py` | AgentPool 生命周期 | ⚠️ 引用旧名 |
| `test_peer_agent_messaging.py` | Peer 消息传递 | ⚠️ 引用旧名 |
| `test_agent_communication.py` (bot_project) | 端到端通信 | ❌ 引用旧名 |

### 9.2 缺失测试场景

| 场景 | Spec 参考 | 优先级 | 说明 |
|---|---|---|---|
| 并发创建多个 subagent 独立会话 | §15 | 高 | 同一 main 同时向同一 subagent 发送多个 `uuid=""` 请求 |
| 跨 agent 类型通信 (SUBAGENT→NORMAL) | §7.4 | 高 | subagent 回复 main 时 uuid 保留在 envelope |
| 跨 agent 类型通信 (SUBAGENT→SUBAGENT) | §7.5 (Spec §14 #5) | 高 | subagent 向另一个 subagent 发起任务 |
| 会话上下文恢复 | §9.1 | 中 | SUBAGENT 在同一 uuid 会话中保持上下文连续性 |
| 异常处理与错误恢复 | §5.3 | 中 | 无效 uuid 组合的精确错误消息 |
| `AgentSessionMeta.conversation_id` 正确性 | §3.2 | 高 | 验证从完整 session_id 解析出 conversation_id |
| `CommunicationTracker` uuid 匹配 | §15 | 中 | 使用 uuid（非 invocation_id）匹配 send/ack |
| bot_project 仅注册 async 工具 | §10 | 中 | 验证无旧工具注册 |
| 旧工具名不存在于导入链 | §13 | 中 | 验证 `SendMessageTool` 等不可导入 |

### 9.3 问题 TE-1：测试引用旧工具名 [重要]

（同 H-2，此处不重复）

### 9.4 问题 TE-2：缺少并发 subagent 会话测试 [重要]

**问题描述**: Spec §15 明确要求测试"同时调用单个 sub-agent 创建多个独立会话的并发处理能力"，但当前测试套件中无此场景。

**修复方案**: 添加测试用例：

```python
async def test_concurrent_subagent_sessions():
    """Main sends two new-task messages to the same subagent concurrently."""
    # Send uuid="" twice to office-expert
    # Verify two different uuids are generated
    # Verify two different session IDs are created
    # Verify messages are routed to correct sessions
```

### 9.5 问题 TE-3：缺少 SUBAGENT→SUBAGENT 通信测试 [重要]

**问题描述**: Spec §14 验证矩阵 #5 要求测试 subagent 向另一个 subagent 发起任务的场景，但当前测试未覆盖。

**修复方案**: 添加测试用例：

```python
async def test_subagent_to_subagent_communication():
    """Subagent office-expert sends new task to subagent query-12306."""
    # office-expert (SUBAGENT) sends to query-12306 (SUBAGENT) with uuid=""
    # Verify new uuid is generated for the 12306 task
    # Verify session ID is conv:query-12306:{new_uuid}
```

---

## 10. 问题汇总与优先级

### 10.1 严重问题 (P0) — 必须立即修复

| ID | 模块 | 问题 | 影响 |
|---|---|---|---|
| D-1 | 数据流 | `AgentSessionMeta.conversation_id` 赋值为完整 session_id 而非 conversation_id | 通信服务构建错误的目标 session ID |
| T-1 | 提示词 | `bot_config.yml` 系统提示词引用 `send_message_async` | LLM 调用不存在的工具，subagent 无法回传结果 |
| C-1/H-1 | 通信/清理 | `core.py` 导入已删除的 `SendMessageAsyncTool` | 运行时 ImportError |

### 10.2 重要问题 (P1) — 应在合并前修复

| ID | 模块 | 问题 | 影响 |
|---|---|---|---|
| D-2 | 数据流 | `session_meta.uuid` 仅从 input_metadata 提取，inbox 唤醒路径可能丢失 | SUBAGENT 通过 inbox 唤醒时 uuid 为 None |
| CT-1 | 通信记录 | `CommRecord.invocation_id` 未迁移为 `uuid` | 术语不一致，增加维护成本 |
| CT-2 | 通信记录 | `build_prompt_section()` 提示文本使用 `invocation_id` | 混淆 LLM，可能导致参数名错误 |
| CT-3 | 通信记录 | `communication.py` 调用 tracker 时术语转换 | 与新术语不一致 |
| C-2 | 通信 | `peer_validator.py` 检查旧工具名 `send_message_async` | 无法检测 peer deny 新工具 |
| H-2/TE-1 | 清理/测试 | 测试文件引用旧工具名 | 测试与实现不匹配 |
| TE-2 | 测试 | 缺少并发 subagent 会话测试 | 并发场景未验证 |
| TE-3 | 测试 | 缺少 SUBAGENT→SUBAGENT 通信测试 | 跨类型通信未验证 |
| S-1 | 会话标识 | `AgentSessionMeta` 缺少 `session_id` 属性 | 重复代码，增加出错概率 |

### 10.3 一般问题 (P2) — 建议修复

| ID | 模块 | 问题 |
|---|---|---|
| D-3 | 数据流 | `_execute_turn` 中 conversation_id 解析冗余 |
| T-2 | 提示词 | `bot_config.yml` 注释矩阵引用旧工具名 |
| T-3 | 提示词 | 工具返回消息缺少 subagent 任务创建后续指引 |
| C-3 | 通信 | `subagent_service.py` 文档字符串引用旧工具名 |
| C-4 | 通信 | `communication.py` 中 inline import |
| H-3 | 清理 | `current_conversation_id` ContextVar 仍存在 |
| S-2 | 会话标识 | `AgentContext.session_id` 与 `session_meta` 信息冗余 |
| E-1 | 编码规范 | `pipeline.py` 中 `getattr` 使用 |
| E-2 | 编码规范 | `pool.py` 中中英文注释混用 |
| E-3 | 编码规范 | `envelope.py` 中文 docstring |

### 10.4 修复建议执行顺序

1. **P0 修复** (D-1 → C-1 → T-1): 先修数据流错误，再修导入错误，最后修提示词
2. **P1 修复** (CT-1/CT-2/CT-3 → C-2 → S-1 → D-2 → TE-2/TE-3 → H-2): 先统一术语，再修校验逻辑，补属性，修数据提取，补测试，最后清测试引用
3. **P2 修复**: 按模块逐步清理

---

## 附录 A：Spec 验证矩阵对照

以下对照 Spec §14 的验证矩阵，逐项检查实现状态：

| # | Caller | Target kind | uuid argument | Expected session | Expected result | 实现状态 |
|---|---|---|---|---|---|---|
| 1 | main | NORMAL | `None` | `conv:target` | sent, no uuid | ✅ 符合 |
| 2 | main | SUBAGENT | `""` | `conv:office:<new>` | sent, new uuid returned | ✅ 符合 |
| 3 | main | SUBAGENT | `"abc123"` | `conv:office:abc123` | sent, same uuid returned | ✅ 符合 |
| 4 | subagent | NORMAL | `None` | `conv:main` | sent, uuid preserved in envelope | ✅ 符合 |
| 5 | subagent | SUBAGENT | `""` | `conv:query-12306:<new>` | sent, new uuid returned | ✅ 符合 |
| 6 | any | NORMAL | `""` | none | parameter error | ✅ 符合 |
| 7 | any | NORMAL | `"abc123"` | none | parameter error | ✅ 符合 |
| 8 | any | SUBAGENT | `None` | none | parameter error | ✅ 符合 |
| 9 | any | nonexistent | any | none | agent-not-found error | ✅ 符合 |

**注意**: 矩阵中的路由逻辑在 `AgentCommunicationService` 中正确实现，但由于 D-1 问题（`conversation_id` 赋值错误），实际运行时构建的 session ID 可能不正确。

## 附录 B：文件修改影响范围

### P0 修复涉及文件

| 文件 | 修改内容 |
|---|---|
| `framework/pipeline/pipeline.py` | 修复 `AgentSessionMeta.conversation_id` 赋值逻辑 |
| `examples/bot_project/bot/service/core.py` | 移除 `SendMessageAsyncTool` 导入，替换为新工具注册 |
| `examples/bot_project/config/bot_config.yml` | 更新系统提示词中的工具名 |

### P1 修复涉及文件

| 文件 | 修改内容 |
|---|---|
| `framework/multi_agent/comm_tracker.py` | `invocation_id` → `uuid` 术语迁移 |
| `framework/multi_agent/communication.py` | 更新 tracker 调用参数名，移除 inline import |
| `framework/multi_agent/pool.py` | 更新 tracker 调用参数名 |
| `framework/multi_agent/peer_validator.py` | 更新工具名校验 |
| `framework/core/agent.py` | 添加 `AgentSessionMeta.session_id` 属性 |
| `tests/unit/multi_agent/` | 更新旧工具名引用，添加缺失测试场景 |
| `examples/bot_project/tests/` | 更新旧工具名引用 |
