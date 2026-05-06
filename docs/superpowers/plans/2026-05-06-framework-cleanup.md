# Framework 组件清理与重组实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除冗余的 extensions/ 目录和旧 memory/session ABC，将 intervention 合并到 control/，将错位组件移到正确位置，清理无用 multi_agent 组件。

**Architecture:** 不新增抽象层，仅做文件移动、删除和导入更新。旧系统（MemoryStore/SessionStore）已被新系统完全替代，直接删除不保留兼容。 intervention 的任务监督功能吸收为 control/task_supervision.py。

**Tech Stack:** Python, pytest, ruff, git mv

---

## 文件映射

### 删除的文件

| 文件 | 原因 |
|------|------|
| `framework/core/memory.py` | 旧 `MemoryStore`/`MemoryEntry` ABC，零生产使用 |
| `framework/core/session.py` | 旧 `SessionStore`/`Session` ABC，零生产使用 |
| `framework/extensions/` 整个目录 | 全部基于旧 ABC，零生产导入 |
| `framework/multi_agent/governance.py` | 与 `memory/context_governance.py` 功能重复 |
| `framework/multi_agent/commands.py` | `SystemCommandInterceptor` 与 control 系统重复 |
| `framework/multi_agent/assembly_kit.py` | 零生产使用 |
| `framework/multi_agent/toolset.py` | 零使用 |
| `framework/multi_agent/rpc_broker.py` | 零生产使用 |
| `framework/multi_agent/discovery.py` | 零使用 |
| `framework/multi_agent/intervention.py` | 合并到 control/task_supervision.py |
| `framework/multi_agent/policy_registry.py` | 合并到 control/policy_registry.py |
| `framework/multi_agent/agent_skill_manager.py` | 移到 core/skills/filter.py |
| `framework/multi_agent/filtered_tool_manager.py` | 移到 tools/filter.py |
| `framework/multi_agent/sanitizer.py` | 移到 utils/sanitizer.py |
| `framework/multi_agent/context_builder.py` | 移到 utils/context_builder.py |
| `framework/multi_agent/deduplicator.py` | 移到 utils/deduplicator.py |

### 新增/修改的文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `framework/providers/__init__.py` | 创建 | 导出 LiteLLMProvider |
| `framework/providers/litellm_provider.py` | 移动 | 从 extensions/llm/ 移入 |
| `framework/control/task_supervision.py` | 创建 | 从 intervention.py 合并，类重命名 |
| `framework/control/policy_registry.py` | 创建 | 从 multi_agent/policy_registry.py 合并，类重命名 |
| `framework/core/skills/filter.py` | 创建 | 从 agent_skill_manager.py 移入，类重命名 |
| `framework/tools/filter.py` | 创建 | 从 filtered_tool_manager.py 移入 |
| `framework/utils/sanitizer.py` | 创建 | 从 sanitizer.py 移入 |
| `framework/utils/context_builder.py` | 创建 | 从 context_builder.py 移入 |
| `framework/utils/deduplicator.py` | 创建 | 从 deduplicator.py 移入 |
| `framework/__init__.py` | 修改 | 移除 MemoryStore/MemoryEntry/SessionStore/Session 导出 |
| `framework/core/__init__.py` | 修改 | 同上 |
| `framework/multi_agent/__init__.py` | 修改 | 移除已删除/移动的导出 |
| `framework/control/__init__.py` | 修改 | 添加 task_supervision 导出 |
| `framework/multi_agent/factory.py` | 修改 | 更新导入路径和类名 |
| `framework/multi_agent/subagent_manager.py` | 修改 | 更新 intervention 导入 |
| `framework/multi_agent/coordinator.py` | 修改 | 更新 TaskInterventionPolicy 类型引用 |
| `framework/pipeline/pipeline.py` | 修改 | 更新 multi_agent 组件导入 |
| `framework/session/agent_session.py` | 修改 | 更新 deduplicator/context_builder 导入 |
| `framework/hook/builtin/dynamic_tool_filter.py` | 修改 | 更新 FilteredToolManager 导入 |
| `examples/bot_project/bot/service/core.py` | 修改 | 更新 LiteLLMProvider 导入 |

---

## Task 1: 删除旧 Memory/Session ABC

**Files:**
- Delete: `framework/core/memory.py`
- Delete: `framework/core/session.py`
- Modify: `framework/core/__init__.py`
- Modify: `framework/__init__.py`

- [ ] **Step 1: 删除旧 ABC 文件**

```bash
git rm framework/core/memory.py framework/core/session.py
```

- [ ] **Step 2: 更新 framework/core/__init__.py**

删除以下两行：
```python
from .memory import MemoryEntry, MemoryStore
from .session import Session, SessionStore
```

从 `__all__` 列表移除：
```python
"MemoryStore",
"MemoryEntry",
"SessionStore",
"Session",
```

- [ ] **Step 3: 更新 framework/__init__.py**

删除以下两行：
```python
from .core.memory import MemoryEntry, MemoryStore
from .core.session import Session, SessionStore
```

从 `__all__` 列表移除：
```python
"MemoryStore",
"MemoryEntry",
"SessionStore",
"Session",
```

- [ ] **Step 4: Commit**

```bash
git add framework/core/__init__.py framework/__init__.py
git commit -m "refactor: remove dead MemoryStore and SessionStore ABCs

Delete framework/core/memory.py and framework/core/session.py.
These old ABCs (MemoryStore, MemoryEntry, SessionStore, Session)
have zero production usage. The new memory system in framework/memory/
uses entirely different abstractions (MemoryStorage, ChatMessage, etc.).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: 删除 extensions/ 目录

**Files:**
- Delete: `framework/extensions/` 整个目录

- [ ] **Step 1: 删除 extensions 目录**

```bash
git rm -r framework/extensions/
```

- [ ] **Step 2: Commit**

```bash
git commit -m "refactor: remove dead extensions/ directory

All files in extensions/ were based on the old MemoryStore/SessionStore
ABCs and have zero production imports. The new memory system
(framework/memory/) and session system (framework/session/) are
entirely separate implementations.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 删除无用 multi_agent 组件

**Files:**
- Delete: `framework/multi_agent/assembly_kit.py`
- Delete: `framework/multi_agent/toolset.py`
- Delete: `framework/multi_agent/rpc_broker.py`
- Delete: `framework/multi_agent/discovery.py`
- Delete: `framework/multi_agent/governance.py`
- Delete: `framework/multi_agent/commands.py`

- [ ] **Step 1: 删除无用组件**

```bash
git rm framework/multi_agent/assembly_kit.py \
  framework/multi_agent/toolset.py \
  framework/multi_agent/rpc_broker.py \
  framework/multi_agent/discovery.py \
  framework/multi_agent/governance.py \
  framework/multi_agent/commands.py
```

- [ ] **Step 2: Commit**

```bash
git commit -m "refactor: remove unused multi_agent components

Remove components with zero production usage:
- assembly_kit.py, toolset.py, rpc_broker.py, discovery.py
- governance.py (functionality duplicated by memory/context_governance.py)
- commands.py (SystemCommandInterceptor duplicates control/ system)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 创建 providers/ 并移动 LiteLLMProvider

**Files:**
- Create: `framework/providers/__init__.py`
- Create: `framework/providers/litellm_provider.py`
- Delete: `framework/extensions/llm/litellm_provider.py` (已在 Task 2 删除)
- Modify: `examples/bot_project/bot/service/core.py`
- Modify: `framework/multi_agent/factory.py`

- [ ] **Step 1: 创建 providers/__init__.py**

```python
"""LLM Provider implementations."""

try:
    from .litellm_provider import LiteLLMProvider
    __all__ = ["LiteLLMProvider"]
except ImportError:
    __all__ = []
```

- [ ] **Step 2: 从 git history 恢复 litellm_provider.py 到 providers/**

```bash
# 从最后一次提交的 extensions/llm/litellm_provider.py 复制内容
# 因为 Task 2 已经删除了 extensions/，需要手动复制
# 使用 git show 获取文件内容
git show HEAD~2:framework/extensions/llm/litellm_provider.py > framework/providers/litellm_provider.py
```

- [ ] **Step 3: 更新 examples/bot_project/bot/service/core.py**

将 line 41:
```python
from framework.extensions.llm.litellm_provider import LiteLLMProvider
```
改为：
```python
from framework.providers.litellm_provider import LiteLLMProvider
```

- [ ] **Step 4: 更新 framework/multi_agent/factory.py**

将 line 20:
```python
    from framework.extensions.llm import LiteLLMProvider
```
改为：
```python
    from framework.providers import LiteLLMProvider
```

- [ ] **Step 5: Commit**

```bash
git add framework/providers/ examples/bot_project/bot/service/core.py framework/multi_agent/factory.py
git commit -m "refactor: move LiteLLMProvider from extensions/ to providers/

LiteLLMProvider is the primary LLM provider used by the framework.
It should not live in a deprecated 'extensions' directory.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: 创建 control/task_supervision.py

**Files:**
- Create: `framework/control/task_supervision.py`
- Delete: `framework/multi_agent/intervention.py`

- [ ] **Step 1: 从 git history 获取 intervention.py 内容并创建新文件**

```bash
git show HEAD~3:framework/multi_agent/intervention.py > /tmp/intervention.py
```

然后基于该内容创建 `framework/control/task_supervision.py`，进行以下重命名：

```python
"""Task supervision — runtime monitoring and policy enforcement for async tasks.

Absorbed from the former multi_agent/intervention.py module.
Provides timeout monitoring, heartbeat emission, and policy-based
task cancellation. This is part of the control/ runtime plane.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from framework.multi_agent.coordinator import TaskCoordinator, TaskRecord

T = TypeVar("T")
logger = logging.getLogger(__name__)


class SupervisionAction(Enum):
    """监督动作枚举。"""

    PASS = "pass"
    CANCEL = "cancel"
    PAUSE = "pause"
    NOTIFY = "notify"
    THROTTLE = "throttle"
    REDIRECT = "redirect"


@dataclass
class SupervisionResult:
    """监督结果。"""

    action: SupervisionAction = SupervisionAction.PASS
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskSupervisionPolicy(ABC):
    """任务监督策略抽象基类。"""

    policy_type: str = ""

    @abstractmethod
    async def check(self, task_record: TaskRecord) -> SupervisionResult:
        """检查策略条件。"""
        ...

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> TaskSupervisionPolicy:
        """从配置创建策略实例（子类应覆盖）。"""
        return cls(**config)


class NoOpSupervisionPolicy(TaskSupervisionPolicy):
    """显式关闭所有监督的占位策略。"""

    policy_type = "no_op"

    async def check(self, task_record: TaskRecord) -> SupervisionResult:
        return SupervisionResult(action=SupervisionAction.PASS, reason="No-op policy")


class TimeoutSupervisionPolicy(TaskSupervisionPolicy):
    """超时取消策略。"""

    policy_type = "timeout_cancellation"

    def __init__(self, deadline: float):
        self.deadline = deadline

    async def check(self, task_record: TaskRecord) -> SupervisionResult:
        if time.time() > self.deadline:
            return SupervisionResult(
                action=SupervisionAction.CANCEL,
                reason=f"Task {task_record.task_id} exceeded deadline {datetime.fromtimestamp(self.deadline).isoformat()}",
            )
        return SupervisionResult(action=SupervisionAction.PASS, reason="Within deadline")

    @classmethod
    def from_duration(cls, seconds: float = 180.0) -> TimeoutSupervisionPolicy:
        return cls(deadline=time.time() + seconds)


class TaskSupervisor:
    """任务监督器，不侵入 Agent 层。"""

    def __init__(
        self,
        coordinator: TaskCoordinator,
        check_interval: float = 5.0,
        emit_heartbeat: bool = True,
    ):
        self._coordinator = coordinator
        self._check_interval = check_interval
        self._emit_heartbeat = emit_heartbeat

    async def supervise(self, task_id: str, coro: Coroutine[Any, Any, T]) -> T:
        """包裹任意协程，在开始前和运行中持续执行策略检查。"""
        from framework.multi_agent.event_bus import TaskEvent, TaskEventType

        try:
            record = await self._coordinator.get_task_record(task_id)
        except Exception:
            logger.warning("TaskCoordinator unavailable during pre-check, proceeding without policy check")
            record = None

        if record:
            for result in await record.check_all():
                if result.action == SupervisionAction.CANCEL:
                    raise asyncio.CancelledError(result.reason)

        try:
            await self._coordinator.update_task_status(task_id, "running")
        except Exception:
            logger.warning("Failed to update task status to running, proceeding")

        main_task = asyncio.create_task(coro)
        monitor_task = asyncio.create_task(self._monitor(task_id, main_task))

        bus = self._coordinator.event_bus
        if bus:
            try:
                await bus.emit(TaskEvent(task_id=task_id, event_type=TaskEventType.STARTED))
            except Exception:
                logger.exception("EventBus emit STARTED failed")

        try:
            result = await main_task
            if bus:
                try:
                    await bus.emit(
                        TaskEvent(
                            task_id=task_id,
                            event_type=TaskEventType.COMPLETED,
                            payload={"stop_reason": getattr(result, "stop_reason", None)},
                        )
                    )
                except Exception:
                    logger.exception("EventBus emit COMPLETED failed")
            return result
        except asyncio.CancelledError:
            if bus:
                try:
                    await bus.emit(TaskEvent(task_id=task_id, event_type=TaskEventType.CANCELLED))
                except Exception:
                    logger.exception("EventBus emit CANCELLED failed")
            with contextlib.suppress(Exception):
                await self._coordinator.update_task_status(
                    task_id, "cancelled", {"reason": "policy_or_external_cancel"}
                )
            raise
        except Exception as exc:
            if bus:
                try:
                    await bus.emit(
                        TaskEvent(
                            task_id=task_id,
                            event_type=TaskEventType.FAILED,
                            payload={"error": str(exc), "error_type": type(exc).__name__},
                        )
                    )
                except Exception:
                    logger.exception("EventBus emit FAILED failed")
            raise
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task

    async def _monitor(self, task_id: str, main_task: asyncio.Task) -> None:
        from framework.multi_agent.event_bus import TaskEvent, TaskEventType

        change_event = self._coordinator.on_policy_change(task_id)
        bus = self._coordinator.event_bus
        while not main_task.done():
            try:
                await asyncio.wait_for(change_event.wait(), timeout=self._check_interval)
                change_event = self._coordinator.on_policy_change(task_id)
            except TimeoutError:
                pass
            except Exception:
                logger.exception("Error waiting for policy change event")

            try:
                record = await self._coordinator.get_task_record(task_id)
            except Exception:
                logger.warning("TaskCoordinator unavailable during monitor, skipping check")
                continue

            if not record:
                continue

            triggered_results: list[SupervisionResult] = []
            for result in await record.check_all():
                if result.action == SupervisionAction.CANCEL and not main_task.done():
                    main_task.cancel()
                    return
                if result.action == SupervisionAction.NOTIFY:
                    triggered_results.append(result)
                elif result.action not in (SupervisionAction.PASS, SupervisionAction.CANCEL):
                    logger.debug("Unhandled supervision action: %s", result.action)

            if bus:
                if triggered_results:
                    try:
                        await bus.emit(
                            TaskEvent(
                                task_id=task_id,
                                event_type=TaskEventType.POLICY_TRIGGERED,
                                payload={
                                    "results": [
                                        {"action": r.action.value, "reason": r.reason, "metadata": r.metadata}
                                        for r in triggered_results
                                    ]
                                },
                            )
                        )
                    except Exception:
                        logger.exception("EventBus emit POLICY_TRIGGERED failed")
                if self._emit_heartbeat:
                    try:
                        await bus.emit(
                            TaskEvent(
                                task_id=task_id,
                                event_type=TaskEventType.HEARTBEAT,
                                payload={
                                    "status": record.status,
                                    "elapsed_seconds": time.time() - (record.started_at or record.created_at),
                                },
                            )
                        )
                    except Exception:
                        logger.exception("EventBus emit HEARTBEAT failed")
```

- [ ] **Step 2: 删除原 intervention.py**

```bash
git rm framework/multi_agent/intervention.py
```

- [ ] **Step 3: Commit**

```bash
git add framework/control/task_supervision.py
git commit -m "refactor: merge intervention into control/task_supervision.py

Move TaskSupervisor, TimeoutCancellationPolicy, and related types
from multi_agent/intervention.py to control/task_supervision.py.
Rename classes for clarity:
- TaskInterventionPolicy → TaskSupervisionPolicy
- InterventionAction → SupervisionAction
- InterventionResult → SupervisionResult
- TimeoutCancellationPolicy → TimeoutSupervisionPolicy
- NoOpInterventionPolicy → NoOpSupervisionPolicy

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: 创建 control/policy_registry.py

**Files:**
- Create: `framework/control/policy_registry.py`
- Delete: `framework/multi_agent/policy_registry.py`

- [ ] **Step 1: 从 git history 获取 policy_registry.py 并创建新文件**

```bash
git show HEAD~4:framework/multi_agent/policy_registry.py > /tmp/policy_registry.py
```

然后基于该内容创建 `framework/control/policy_registry.py`，进行重命名：

```python
"""Task supervision policy registry.

Absorbed from the former multi_agent/policy_registry.py module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .task_supervision import TaskSupervisionPolicy


class SupervisionPolicyRegistry:
    """监督策略注册表。"""

    _registry: dict[str, type[TaskSupervisionPolicy]] = {}

    @classmethod
    def register(cls, policy_type: str, policy_class: type[TaskSupervisionPolicy]) -> None:
        cls._registry[policy_type] = policy_class

    @classmethod
    def get(cls, policy_type: str) -> type[TaskSupervisionPolicy]:
        if policy_type not in cls._registry:
            raise ValueError(f"Unknown supervision policy type: {policy_type}")
        return cls._registry[policy_type]


class SupervisionPolicySpec:
    """监督策略配置规格。"""

    def __init__(self, policy_type: str, config: dict[str, Any] | None = None):
        self.policy_type = policy_type
        self.config = config or {}

    def to_policy(self) -> TaskSupervisionPolicy:
        policy_class = SupervisionPolicyRegistry.get(self.policy_type)
        return policy_class.from_config(self.config)
```

- [ ] **Step 2: 删除原 policy_registry.py**

```bash
git rm framework/multi_agent/policy_registry.py
```

- [ ] **Step 3: Commit**

```bash
git add framework/control/policy_registry.py
git commit -m "refactor: move policy_registry from multi_agent to control/

Rename:
- PolicyRegistry → SupervisionPolicyRegistry
- TaskInterventionPolicySpec → SupervisionPolicySpec

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: 移动通用组件到正确位置

**Files:**
- Create: `framework/core/skills/filter.py`
- Create: `framework/tools/filter.py`
- Create: `framework/utils/sanitizer.py`
- Create: `framework/utils/context_builder.py`
- Create: `framework/utils/deduplicator.py`
- Delete: `framework/multi_agent/agent_skill_manager.py`
- Delete: `framework/multi_agent/filtered_tool_manager.py`
- Delete: `framework/multi_agent/sanitizer.py`
- Delete: `framework/multi_agent/context_builder.py`
- Delete: `framework/multi_agent/deduplicator.py`

- [ ] **Step 1: 创建 core/skills/filter.py**

```python
"""Skill filtering utilities."""

from __future__ import annotations

from framework.core.skills import ResolutionContext, SkillManager


class SkillWhitelistFilter:
    """基于白名单过滤的 SkillManager 包装器。"""

    def __init__(self, base: SkillManager, allowed_skills: list[str] | None = None):
        self._base = base
        self._allowed = set(allowed_skills) if allowed_skills is not None else None

    def is_skill_allowed(self, name: str) -> bool:
        if self._allowed is None:
            return True
        return name in self._allowed

    async def build_prompt(self, ctx: ResolutionContext | None = None) -> str:
        if self._allowed is None:
            return await self._base.build_prompt(ctx)
        skills = await self.list_skills(ctx)
        builder = getattr(self._base, "_builder", None)
        if builder is None:
            from framework.core.skills import ProgressiveBuilder
            builder = ProgressiveBuilder()
        return await builder.build(skills, ctx)

    async def list_skills(self, ctx: ResolutionContext | None = None):
        skills = await self._base.list_skills(ctx)
        if self._allowed is None:
            return skills
        return [s for s in skills if s.name in self._allowed]
```

- [ ] **Step 2: 创建 tools/filter.py**

```bash
git show HEAD~5:framework/multi_agent/filtered_tool_manager.py > framework/tools/filter.py
```

- [ ] **Step 3: 创建 utils/sanitizer.py, utils/context_builder.py, utils/deduplicator.py**

```bash
git show HEAD~5:framework/multi_agent/sanitizer.py > framework/utils/sanitizer.py
git show HEAD~5:framework/multi_agent/context_builder.py > framework/utils/context_builder.py
git show HEAD~5:framework/multi_agent/deduplicator.py > framework/utils/deduplicator.py
```

- [ ] **Step 4: 更新 utils/context_builder.py 的导入**

原文件从 `multi_agent.governance` TYPE_CHECKING 导入 `ContextGovernancePolicy`。由于 governance.py 已被删除，需要修改 `MultiAgentContextBuilder` 以接受 `Callable` 替代：

```python
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from framework.multi_agent.descriptor import AgentDescriptor
    from framework.multi_agent.envelope import AgentMessageEnvelope


class MultiAgentContextBuilder:
    """按 agent_session_id 构建运行时消息上下文。"""

    def __init__(
        self,
        governance: Callable[[list[dict[str, Any]], AgentDescriptor], list[dict[str, Any]]] | None = None,
    ):
        self._governance = governance

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_envelope: AgentMessageEnvelope,
        agent_descriptor: AgentDescriptor,
        session_summary: str | None = None,
    ) -> list[dict[str, Any]]:
        """构建注入当前消息和治理策略后的完整 messages 列表。"""
        messages = self._build_system_block(agent_descriptor, session_summary)
        for msg in history:
            messages.append(self._normalize_message(msg))
        messages.append(self._envelope_to_message(current_envelope))
        if self._governance is not None:
            messages = self._governance(messages, agent_descriptor)
        return messages

    # ... rest of methods unchanged
```

实际上更简单：检查生产代码是否实例化 `MultiAgentContextBuilder`。根据审计，只有测试代码实例化它。生产代码（pipeline.py, agent_session.py）只检查 `self._context_builder is not None`，且 `MultiAgentContextBuilder` 只在 `multi_agent/__init__.py` 中被导入作为可选项。

所以最简单的做法：保持 `MultiAgentContextBuilder` 构造函数接受 `Any` 类型（因为生产代码从未实例化它）：

```python
class MultiAgentContextBuilder:
    def __init__(self, governance: Any | None = None):
        self._governance = governance
    # ...
```

- [ ] **Step 5: 删除原文件**

```bash
git rm framework/multi_agent/agent_skill_manager.py \
  framework/multi_agent/filtered_tool_manager.py \
  framework/multi_agent/sanitizer.py \
  framework/multi_agent/context_builder.py \
  framework/multi_agent/deduplicator.py
```

- [ ] **Step 6: Commit**

```bash
git add framework/core/skills/filter.py framework/tools/filter.py framework/utils/sanitizer.py framework/utils/context_builder.py framework/utils/deduplicator.py
git commit -m "refactor: relocate misplaced components from multi_agent/

Move general-purpose components out of multi_agent/:
- agent_skill_manager.py → core/skills/filter.py (SkillWhitelistFilter)
- filtered_tool_manager.py → tools/filter.py
- sanitizer.py → utils/sanitizer.py
- context_builder.py → utils/context_builder.py
- deduplicator.py → utils/deduplicator.py

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: 更新 multi_agent/__init__.py 导出

**Files:**
- Modify: `framework/multi_agent/__init__.py`

- [ ] **Step 1: 移除已删除/移动组件的导入和导出**

从 `framework/multi_agent/__init__.py` 中删除以下导入：

```python
from framework.multi_agent.agent_skill_manager import AgentSkillManager
from framework.multi_agent.commands import CommandInterceptor, SystemCommandInterceptor
from framework.multi_agent.context_builder import MultiAgentContextBuilder
from framework.multi_agent.deduplicator import MessageDeduplicator
from framework.multi_agent.filtered_tool_manager import FilteredToolManager
from framework.multi_agent.governance import ContextGovernancePolicy, FullGovernance, NoOpGovernance
from framework.multi_agent.intervention import (
    InterventionAction,
    InterventionResult,
    NoOpInterventionPolicy,
    TaskInterventionPolicy,
    TaskSupervisor,
    TimeoutCancellationPolicy,
)
from framework.multi_agent.policy_registry import PolicyRegistry, TaskInterventionPolicySpec
from framework.multi_agent.sanitizer import ContentSanitizer
```

以及 `__all__` 中对应的导出字符串。

保留的导出项（仅多 Agent 特有组件）：
```python
__all__ = [
    "AgentAddress",
    "AgentDescriptor",
    "AgentDirectory",
    "AgentMessageBus",
    "LocalAgentMessageBus",
    "AgentFactory",
    "AgentInstance",
    "AgentLLMConfig",
    "AgentMessageEnvelope",
    "AgentMessageRouter",
    "AgentPool",
    "AgentRegistry",
    "AgentState",
    "CommandInterceptor",       # 如果保留（但 commands.py 已删除）
    "CompositeTaskEventReporter",
    "ContextGovernanceConfig",
    "DefaultAgentFactory",
    "DefaultMeshRouter",
    "ExecutionStrategy",
    "InMemoryTaskCoordinator",
    "LocalAgentMessageBus",
    "LoggingTaskEventReporter",
    "NullTaskCoordinator",
    "PeerAgentValidator",
    "RouteResult",
    "RPCTimeoutError",          # 如果 rpc_broker.py 已删除则移除
    "RPCBroker",                # 同上
    "ReActStrategy",
    "SendMessageAsyncTool",
    "SendMessageTool",
    "SingleTurnStrategy",
    "SubagentManager",
    "SystemCommandInterceptor", # 如果 commands.py 已删除则移除
    "TaskCoordinationConfig",
    "TaskCoordinator",
    "TaskEvent",
    "TaskEventBus",
    "TaskEventReporter",
    "TaskEventType",
    "TaskRecord",
    "format_peer_session_id",
    "format_pool_session_id",
    "is_peer_session_id",
    "parse_peer_session_id",
    "parse_pool_session_id",
    "reverse_peer_session_id",
]
```

实际上需要精确清理。根据已删除的文件，从 `__all__` 中移除：
- `AgentSkillManager`
- `CommandInterceptor` (commands.py 已删除)
- `ContentSanitizer`
- `ContextGovernancePolicy`
- `FilteredToolManager`
- `FullGovernance`
- `InterventionAction`
- `InterventionResult`
- `MessageDeduplicator`
- `MultiAgentContextBuilder`
- `NoOpGovernance`
- `NoOpInterventionPolicy`
- `PolicyRegistry`
- `RPCBroker` (rpc_broker.py 已删除)
- `RPCTimeoutError` (rpc_broker.py 已删除)
- `SystemCommandInterceptor` (commands.py 已删除)
- `TaskInterventionPolicy`
- `TaskInterventionPolicySpec`
- `TaskSupervisor`
- `TimeoutCancellationPolicy`

- [ ] **Step 2: Commit**

```bash
git add framework/multi_agent/__init__.py
git commit -m "refactor: clean up multi_agent/__init__.py exports

Remove exports for deleted/moved components.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: 更新生产代码导入 — multi_agent 内部

**Files:**
- Modify: `framework/multi_agent/factory.py`
- Modify: `framework/multi_agent/subagent_manager.py`
- Modify: `framework/multi_agent/coordinator.py`

- [ ] **Step 1: 更新 factory.py**

line 27: `from .agent_skill_manager import AgentSkillManager` → `from framework.core.skills.filter import SkillWhitelistFilter`
line 29: `from .filtered_tool_manager import FilteredToolManager` → `from framework.tools.filter import FilteredToolManager`
line 53: `skill_manager: AgentSkillManager | None = None` → `skill_manager: SkillWhitelistFilter | None = None`
line 72: `skill_manager: AgentSkillManager | None = None` → `skill_manager: SkillWhitelistFilter | None = None`
line 182: `skill_mgr = AgentSkillManager(` → `skill_mgr = SkillWhitelistFilter(`

- [ ] **Step 2: 更新 subagent_manager.py**

line 19: `from .intervention import TaskSupervisor, TimeoutCancellationPolicy` → `from framework.control.task_supervision import TaskSupervisor, TimeoutSupervisionPolicy`
line 228: `TimeoutCancellationPolicy.from_duration(` → `TimeoutSupervisionPolicy.from_duration(`

- [ ] **Step 3: 更新 coordinator.py**

line 13: `from .intervention import TaskInterventionPolicy` → `from framework.control.task_supervision import TaskSupervisionPolicy`
line 32: `policies: list[TaskInterventionPolicy]` → `policies: list[TaskSupervisionPolicy]`
line 74: `async def bind_policy(self, task_id: str, policy: TaskInterventionPolicy)` → `async def bind_policy(self, task_id: str, policy: TaskSupervisionPolicy)`
line 78: `async def replace_policies(self, task_id: str, policies: list[TaskInterventionPolicy])` → `async def replace_policies(self, task_id: str, policies: list[TaskSupervisionPolicy])`
line 122: `async def bind_policy(self, task_id: str, policy: TaskInterventionPolicy)` → `async def bind_policy(self, task_id: str, policy: TaskSupervisionPolicy)`
line 126: `async def replace_policies(self, task_id: str, policies: list[TaskInterventionPolicy])` → `async def replace_policies(self, task_id: str, policies: list[TaskSupervisionPolicy])`
line 194: `async def bind_policy(self, task_id: str, policy: TaskInterventionPolicy)` → `async def bind_policy(self, task_id: str, policy: TaskSupervisionPolicy)`
line 214: `async def replace_policies(self, task_id: str, policies: list[TaskInterventionPolicy])` → `async def replace_policies(self, task_id: str, policies: list[TaskSupervisionPolicy])`

- [ ] **Step 4: Commit**

```bash
git add framework/multi_agent/factory.py framework/multi_agent/subagent_manager.py framework/multi_agent/coordinator.py
git commit -m "refactor: update multi_agent internal imports after cleanup

Update factory, subagent_manager, and coordinator to use new paths:
- framework.core.skills.filter.SkillWhitelistFilter
- framework.tools.filter.FilteredToolManager
- framework.control.task_supervision.TaskSupervisor/TimeoutSupervisionPolicy

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: 更新生产代码导入 — pipeline 和 session

**Files:**
- Modify: `framework/pipeline/pipeline.py`
- Modify: `framework/session/agent_session.py`
- Modify: `framework/hook/builtin/dynamic_tool_filter.py`

- [ ] **Step 1: 更新 pipeline/pipeline.py**

line 35-41:
```python
from ..multi_agent import (
    AgentDescriptor,
    AgentMessageRouter,
    MessageDeduplicator,
    MultiAgentContextBuilder,
    SubagentManager,
)
```
改为：
```python
from ..multi_agent import (
    AgentDescriptor,
    AgentMessageRouter,
    SubagentManager,
)
from ..utils.context_builder import MultiAgentContextBuilder
from ..utils.deduplicator import MessageDeduplicator
```

- [ ] **Step 2: 更新 session/agent_session.py**

找到并更新 deduplicator 和 context_builder 的导入。由于 agent_session.py 只接受这些参数为 `Any` 类型，没有显式导入类型，可能不需要修改导入。但需要确认 `__init__.py` 导出变更后，如果有运行时导入则需要更新。

实际上 agent_session.py 没有显式从 multi_agent 导入 `MessageDeduplicator` 或 `MultiAgentContextBuilder`，它只接受 `Any` 类型参数。所以不需要修改 agent_session.py 的导入。

但 grep 确认 agent_session.py 的 docstring 提到了这些类型名，不需要改。

- [ ] **Step 3: 更新 hook/builtin/dynamic_tool_filter.py**

line 62: `from framework.multi_agent.filtered_tool_manager import FilteredToolManager` → `from framework.tools.filter import FilteredToolManager`

- [ ] **Step 4: Commit**

```bash
git add framework/pipeline/pipeline.py framework/hook/builtin/dynamic_tool_filter.py
git commit -m "refactor: update pipeline and hook imports after component relocation

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: 更新 control/__init__.py

**Files:**
- Modify: `framework/control/__init__.py`

- [ ] **Step 1: 添加新组件的导出**

在 `framework/control/__init__.py` 中添加：

```python
from .task_supervision import (
    NoOpSupervisionPolicy,
    SupervisionAction,
    SupervisionResult,
    TaskSupervisionPolicy,
    TaskSupervisor,
    TimeoutSupervisionPolicy,
)
from .policy_registry import SupervisionPolicyRegistry, SupervisionPolicySpec
```

- [ ] **Step 2: Commit**

```bash
git add framework/control/__init__.py
git commit -m "refactor: add task_supervision exports to control/__init__.py

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 12: 运行冒烟测试

- [ ] **Step 1: 运行 ruff 检查导入错误**

```bash
ruff check framework/ --select=E,W,F
```

Expected: 只报告与新移动/删除文件相关的 import 错误。修复任何发现的问题。

- [ ] **Step 2: 运行 pytest 快速测试**

```bash
pytest tests/unit/agents/react/test_agent.py tests/unit/core/test_context.py -v
```

Expected: PASS（验证核心框架未被破坏）

- [ ] **Step 3: 如有错误，修复并提交**

---

## Task 13: 清理和迁移测试文件

**Files:**
- Delete: `tests/unit/extensions/` 整个目录
- Delete: `tests/unit/multi_agent/test_assembly_kit.py`
- Modify: `tests/unit/multi_agent/test_core_runtime.py`
- Modify: `tests/unit/multi_agent/test_governance_security.py`
- Modify: `tests/integration/multi_agent/test_multi_agent_communication_and_intervention.py`
- Create: `tests/unit/providers/` (从 extensions/llm/ 移入)
- Create: `tests/unit/control/test_task_supervision.py`
- Create: `tests/unit/core/skills/test_filter.py`
- Create: `tests/unit/tools/test_filter.py`
- Create: `tests/unit/utils/test_sanitizer.py`
- Create: `tests/unit/utils/test_context_builder.py`
- Create: `tests/unit/utils/test_deduplicator.py`

- [ ] **Step 1: 删除旧测试目录和文件**

```bash
git rm -r tests/unit/extensions/
git rm tests/unit/multi_agent/test_assembly_kit.py
```

- [ ] **Step 2: 移动 providers 测试**

```bash
# 从 git history 恢复 extensions/llm 测试到 providers/
git show HEAD~10:tests/unit/extensions/llm/test_litellm_provider_error.py > tests/unit/providers/test_litellm_provider_error.py 2>/dev/null || true
git show HEAD~10:tests/unit/extensions/llm/test_litellm_provider_reasoning.py > tests/unit/providers/test_litellm_provider_reasoning.py 2>/dev/null || true
git show HEAD~10:tests/unit/extensions/llm/test_litellm_provider_retry.py > tests/unit/providers/test_litellm_provider_retry.py 2>/dev/null || true
git show HEAD~10:tests/unit/extensions/llm/test_think_tag_extractor.py > tests/unit/providers/test_think_tag_extractor.py 2>/dev/null || true
```

注意：需要批量替换这些测试文件中的导入路径：
- `from framework.extensions.llm.litellm_provider` → `from framework.providers.litellm_provider`
- `from framework.extensions.llm` → `from framework.providers`

- [ ] **Step 3: 更新 test_core_runtime.py**

将 intervention/policy_registry 相关导入改为从 control/ 导入：
- `from framework.multi_agent.intervention import ...` → `from framework.control.task_supervision import ...`
- `from framework.multi_agent.policy_registry import ...` → `from framework.control.policy_registry import ...`
- `InterventionAction` → `SupervisionAction`
- `InterventionResult` → `SupervisionResult`
- `TaskInterventionPolicy` → `TaskSupervisionPolicy`
- `TaskInterventionPolicySpec` → `SupervisionPolicySpec`
- `TaskSupervisor` 保持名称
- `TimeoutCancellationPolicy` → `TimeoutSupervisionPolicy`

- [ ] **Step 4: 更新 test_governance_security.py**

该文件测试了 governance, commands, sanitizer。由于 governance.py 和 commands.py 已删除，需要：
- 删除 governance 相关测试（`TestNoOpGovernance`, `TestFullGovernance`）
- 删除 commands 相关测试（`TestSystemCommandInterceptor`）
- 保留 sanitizer 测试并移动

最佳做法：拆分该测试文件。sanitizer 测试移到 `tests/unit/utils/test_sanitizer.py`。

- [ ] **Step 5: 更新 integration 测试**

`tests/integration/multi_agent/test_multi_agent_communication_and_intervention.py`：
- 更新 intervention 导入到 control/task_supervision
- 更新类名引用

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "refactor: update and migrate test files after cleanup

- Delete tests/unit/extensions/ (old dead code tests)
- Delete tests/unit/multi_agent/test_assembly_kit.py
- Move provider tests to tests/unit/providers/
- Update intervention tests to use control/task_supervision
- Split test_governance_security.py, move sanitizer tests

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 14: 最终验证

- [ ] **Step 1: 运行 ruff 全量检查**

```bash
ruff check framework/
```

Expected: 无 import 错误

- [ ] **Step 2: 运行核心单元测试**

```bash
pytest tests/unit/ -x --ignore=tests/unit/extensions -q
```

Expected: 全部通过（或仅有与已删除组件相关的预期失败）

- [ ] **Step 3: 运行 bot_project 测试**

```bash
pytest examples/bot_project/tests/ -x -q
```

Expected: 全部通过

- [ ] **Step 4: 修复任何剩余问题并提交**

```bash
git commit -m "fix: resolve remaining import and test issues after framework cleanup"
```

---

## 计划自审

**Spec coverage check:**
- ✅ 删除 extensions/ 目录 — Task 2
- ✅ 删除 core/memory.py 和 core/session.py — Task 1
- ✅ 删除无用 multi_agent 组件 — Task 3
- ✅ 移动 litellm_provider 到 providers/ — Task 4
- ✅ intervention → control/task_supervision.py 合并 — Task 5
- ✅ policy_registry → control/policy_registry.py 合并 — Task 6
- ✅ agent_skill_manager → core/skills/filter.py — Task 7
- ✅ filtered_tool_manager → tools/filter.py — Task 7
- ✅ sanitizer → utils/sanitizer.py — Task 7
- ✅ context_builder → utils/context_builder.py — Task 7
- ✅ deduplicator → utils/deduplicator.py — Task 7
- ✅ 删除 governance.py — Task 3
- ✅ 删除 commands.py — Task 3
- ✅ 更新所有生产代码导入 — Tasks 8-11
- ✅ 测试迁移 — Task 13
- ✅ 最终验证 — Task 14

**Placeholder scan:**
- ✅ 无 TBD/TODO
- ✅ 所有步骤包含实际命令或代码
- ✅ 文件路径精确

**Type consistency:**
- ✅ `SkillWhitelistFilter` 在 Task 7 创建，Task 9 中使用 — 一致
- ✅ `TimeoutSupervisionPolicy` 在 Task 5 创建，Task 9 中使用 — 一致
- ✅ `TaskSupervisionPolicy` 在 Task 5 创建，Task 9 中使用 — 一致
