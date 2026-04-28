# Hook / Interceptor / Control 实现检视报告（第二轮）

> 检视日期：2026-04-29
> 对照文档：`design_doc/implementation-review.md`（第一轮检视）
> 检视范围：已修复项验证 + 全量回归检查

---

## 一、第一轮 P0 修复验证

| P0 | 问题 | 状态 | 验证 |
|----|------|------|------|
| P0-1 | AgentSession 未接入 | ✅ 已修复 | 构造函数接受 hook_runner/interceptor_chain/checkpoint_store（agent_session.py:81-83），AgentContext 构建时传递三项（agent_session.py:378-380） |
| P0-2 | ReActAgent 散落字符串 | ✅ 已修复 | 全部 7 处调用改为 HookPoint 常量（agent.py:140,146,156,212,228-229,234,309） |
| P0-3 | on_checkpoint 未迁移 | ✅ 已修复 | _save_checkpoint/_clear_checkpoint 优先使用 checkpoint_store，生成 checkpoint_id=`"{session_id}:latest"`，fallback 到 on_checkpoint（agent.py:311-331） |

**三个 P0 全部正确修复。**

---

## 二、第一轮 P1 修复验证

| P1 | 问题 | 状态 | 验证 |
|----|------|------|------|
| P1-1 | InMemoryControlChannel TTL 未实现 | ✅ 已修复 | send() 记录 `_created[command_id] = time.monotonic()`，drain() 检查 `now - created > ttl_seconds` 并丢弃过期命令（channel.py:41-66） |
| P1-2 | ControlCommandHandler 注册未实现 | ✅ 已修复 | 新文件 `framework/interceptor/handler.py`：ControlCommandHandler 协议 + CommandHandlerRegistry + DefaultCancelHandler；ControlDrainInterceptor 通过 registry 分发（control_drain.py:19-22,45-51） |
| P1-3 | AgentRuntimeConfig 缺失 | ✅ 已修复 | 新文件 `framework/core/agent_runtime_config.py`：AgentRuntimeConfig + RuntimeControl dataclass |
| P1-4 | Pool control_channel 共享 | ❌ 未修复 | builders.py 无 ControlDrainInterceptor 注入，peer agent 无 control channel |
| P1-5 | Payload 解包摩擦 | ❌ 未修复 | _call_hooks 仍用硬编码 key 名构造 payload_data |
| P1-6 | Redact args 过于简陋 | ❌ 未修复 | 无 redact_args 参数、无嵌套/模式匹配 |

**P1-1/P1-2/P1-3 正确修复。P1-4/P1-5/P1-6 未修复。**

---

## 三、新增问题

### P0-NEW. channel.py 缺少 `import time` — 运行时 NameError

**位置**: `framework/control/channel.py`

文件使用了 `time.monotonic()`（第 44 行、第 50 行），但未导入 `time` 模块。当前导入列表：

```python
from __future__ import annotations
import logging
from collections.abc import Sequence
from typing import Protocol
from framework.control.types import ControlCommand, ControlScope
```

**无 `import time`。** 当 `InMemoryControlChannel.send()` 或 `drain()` 被调用时，将触发 `NameError: name 'time' is not defined`。

这是一个阻断性 bug——TTL 修复虽然逻辑正确，但因为缺少导入而完全不可用。

**修复**: 在文件顶部添加 `import time`。

---

### P2-NEW-1. interceptor/__init__.py 未导出 handler 模块

**位置**: `framework/interceptor/__init__.py`

新增的 `framework/interceptor/handler.py`（ControlCommandHandler、CommandHandlerRegistry、DefaultCancelHandler）未在包的 `__init__.py` 中导出。用户需直接 `from framework.interceptor.handler import ...`。

**建议**: 在 `__init__.py` 中增加导出，或至少在 `builtin/__init__.py` 中导出。

---

### P2-NEW-2. AgentRuntimeConfig 未接入 Pipeline / AgentFactory / AgentPool

**位置**: `framework/core/agent_runtime_config.py`

AgentRuntimeConfig 已创建，但 Pipeline、AgentFactory、AgentPool、AgentSession 的构造函数仍分别接受 hooks/hook_runner/interceptor_chain/checkpoint_store 独立参数，不接受 AgentRuntimeConfig。

当前 AgentRuntimeConfig 是一个"定义了但未使用"的类型。

**建议**: 至少在一个入口（如 Pipeline）中支持从 AgentRuntimeConfig 解构组件。

---

## 四、第一轮 P2 状态回顾

| P2 | 问题 | 状态 | 备注 |
|----|------|------|------|
| P2-1 | TaskProgressHook/TaskInterventionHook 未迁移 | 未修复 | 仍在 multi_agent/hooks.py |
| P2-2 | ControlEventBus handler 同步 | 未修复 | 仍为 `Callable[[ControlEvent], None]` |
| P2-3 | Protocol 缺 unsubscribe | 未修复 | |
| P2-4 | InterceptorChain 返回类型校验 | 未修复 | |
| P2-5 | TurnTimeout NOTIFY 不可用 | 未修复 | wait_for 已取消协程 |
| P2-6 | TokenBudgetControlRule 无法触发 | 未修复 | metadata.usage 未维护 |
| P2-7 | 测试覆盖不足 | 未修复 | 无 tests/unit/hook/、interceptor/、control/ 目录 |
| P2-8 | FINALIZE_CONTENT 在 dispatch 中不适用 | 未修复 | |

---

## 五、额外改进确认

以下改进在修复过程中正确完成：

1. **ToolApprovalInterceptor**: _handle_denied 和 _handle_timeout 现在在 ToolResult 中包含 `call_id`（tool_approval.py:178-184,192-198）✅
2. **InterceptorChain**: around_tool_call 兜底 ToolResult 现在包含 `call_id`（chain.py:81-82）✅
3. **ReActAgent._call_hooks**: 签名从 `method_name: str` 改为 `hook_point: HookPoint`（agent.py:349）✅

---

## 六、结论

**第一轮 3 个 P0 全部正确修复。** 新发现 1 个 P0 级运行时 bug（channel.py 缺 `import time`）。3 个 P1 已修复、3 个 P1 未修复。P2 项均未修复。

**必须立即修复**: channel.py 添加 `import time`。

**建议后续优先级**:
1. `import time` — 一行修复
2. P1-4: Pool peer agent control_channel 共享
3. P2-7: 补齐新模块测试
4. P2-NEW-2: AgentRuntimeConfig 接入
5. 其余 P1/P2 按需排列
