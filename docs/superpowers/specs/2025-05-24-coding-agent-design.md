# Coding Agent 体系设计文档

## 1. 设计目标

在 `examples/bot_project/` 中新增一个与 `main` 同等级、完全独立的 Coding Agent 体系,支持用户通过 `/coding` 和 `/main` 命令切换。

## 2. 核心设计决策

| 决策              | 选择                                                                          |
| ----------------- | ----------------------------------------------------------------------------- |
| **体系关系**      | `main` 和 `coding` 完全独立,各自有独立的 AgentPool、session、memory、context |
| **切换命令**      | `/coding` 切换到 coding 体系,`/main` 切换回 main 体系                        |
| **切换行为**      | 切换不中断任何 agent 的执行,只改变后续用户消息的接收方                       |
| **配置格式**      | `config/coding_config.yml`,与 `bot_config.yml` 同格式                        |
| **LLM 配置**      | 共享 `.env`,各自 YAML 引用 `${VAR}`                                          |
| **system prompt** | `agents/*.md` 文件,YAML 中通过 `system_prompt_file` 引用                     |
| **skills**        | `skills/coding/`(coding 体系专属)                                           |
| **subagent 兜底** | `SubagentAutoSendHook`,parent 指向 `coding`                                  |
| **输出方式**      | coding 直接回复用户(不经过 main)                                            |
| **AST/LSP 工具**  | 本次不实现,记为 TODO                                                         |

## 3. 架构

```
User (QQ)
    │
    ▼
┌────────────────────────┐
│ SessionAgentRouter     │  ← 拦截 /coding /main,按 session 路由
│ (新增)                 │
└────────┬───────────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌────────┐  ┌──────────┐
│  main  │  │  coding  │   ← 各自独立的 AgentPool,完全隔离
│  pool  │  │   pool   │
└───┬────┘  └────┬─────┘
    │            │
    ▼            ▼
┌────────┐  ┌──────────┐
│office- │  │ reviewer │   ← subagent(只与父 agent 通信)
│expert  │  │ planner  │
│query-  │  │          │
│12306   │  └──────────┘
└────────┘

共享层:InMemoryMessageBroker、LLM Provider、.env、输出通道
```

## 4. 通信规则

### 4.1 Star Topology(保持框架约束)

- `coding`(NORMAL)可以发送消息给 `reviewer`(SUBAGENT)
- `reviewer`(SUBAGENT)只能发送消息给 `coding`(其父 NORMAL agent)
- `reviewer` **不能**直接看到 `main`
- `coding` 可以发送消息给 `main`(通过 `send_to_agent_async`,invocation_id=null)

### 4.2 典型通信流程

**代码审查流程:**

```
coding: send_to_agent_async(target="reviewer", content="请审查 src/auth.py 的修改", invocation_id="")
  → reviewer 收到消息,执行审查
  → reviewer: send_to_agent_async(target="coding", content="审查结果...", invocation_id=null)
  → coding 收到 inbox 消息,继续处理
```

**最终返回用户:**

```
coding 完成所有工作后,直接通过 output_adapter 回复用户
(不经过 main,因为 coding 自己就是直接面向用户的 agent)
```

### 4.3 SubagentAutoSendHook 兜底

如果 `reviewer` 达到 `max_steps` 忘记回复,Hook 自动将最后输出转发给 `coding`:

```python
SubagentAutoSendHook(
    agent_bus=self.agent_bus,
    self_name="reviewer",
    parent_name="coding",  # ← 指向 coding
)
```

## 5. Agent 通知 Hook 体系

### 5.1 设计目标

当 agent 因为**达到 max_iterations**或**漏发通信消息**而退出时,需要向其"调用方"发送结构化通知:

| Agent 类型                   | 调用方             | 通知方式                         |
| ---------------------------- | ------------------ | -------------------------------- |
| NORMAL(main/coding)        | 用户               | 通过 `output_adapter` 直接发送   |
| SUBAGENT(reviewer/planner) | 父 agent(coding) | 通过 `agent_bus` 发送 inbox 消息 |

**不硬编码 parent_name**:每个 hook 实例在构造时接收 `parent_name`,由初始化代码根据 agent 体系决定。

### 5.2 XML 消息格式

统一的 XML 结构,包含 `type`、`reason`、`details`、`truncated_content` 四个字段:

**场景一:迭代达到限制退出**

```xml
<agent_notification type="max_iterations_exceeded">
  <reason>迭代次数达到上限而退出</reason>
  <details>agent 'reviewer' 已达到最大迭代次数 (max_iterations=80)</details>
  <truncated_content>最后一次 assistant 输出的截断内容,最长 2000 字符...</truncated_content>
</agent_notification>
```

**场景二:subagent 漏发通信消息**

```xml
<agent_notification type="missed_communication">
  <reason>subagent 未通过通信工具发送消息</reason>
  <details>agent 'reviewer' 已完成但未调用 send_to_agent_async,内容已自动转发给 'coding'</details>
  <truncated_content>被自动转发的内容,最长 2000 字符...</truncated_content>
</agent_notification>
```

### 5.3 AgentNotificationService - 统一输出抽象层

```python
class AgentNotificationService:
    """Unified notification service.
    
    Hook 完全无感知：调用方不需要知道自己是 NORMAL 还是 SUBAGENT。
    Service 内部根据 ctx.session_meta.comm_kind 自动路由：
    - NORMAL → output_adapter (用户)
    - SUBAGENT → agent_bus inbox (父 agent)
    """
    
    def __init__(
        self,
        output_adapter: OutputAdapter,
        agent_bus: AgentMessageBus,
        session_strategy: DefaultSessionIdStrategy,
        parent_map: dict[str, str] | None = None,  # agent_name -> parent_name
    ):
        self._output_adapter = output_adapter
        self._agent_bus = agent_bus
        self._session_strategy = session_strategy
        self._parent_map = parent_map or {}
    
    async def notify(
        self,
        ctx: AgentContext,
        notification_type: str,
        reason: str,
        details: str,
        content: str | None = None,
    ) -> None:
        """Hook 调用的唯一入口。自动根据 comm_kind 路由。"""
        xml = self._build_xml(notification_type, reason, details, content)
        
        if (ctx.session_meta 
                and ctx.session_meta.comm_kind == AgentCommKind.SUBAGENT):
            parent_name = self._parent_map.get(ctx.session_meta.agent_name)
            await self._notify_parent(ctx, xml, parent_name)
        else:
            await self._notify_user(ctx, xml)
    
    def _build_xml(
        self, notification_type: str, reason: str, 
        details: str, content: str | None,
    ) -> str:
        lines = [
            f'<agent_notification type="{notification_type}">',
            f"  <reason>{self._escape_xml(reason)}</reason>",
            f"  <details>{self._escape_xml(details)}</details>",
        ]
        if content:
            lines.append(
                f"  <truncated_content>"
                f"{self._escape_xml(content)}"
                f"</truncated_content>"
            )
        lines.append("</agent_notification>")
        return "\n".join(lines)
    
    async def _notify_user(self, ctx: AgentContext, xml: str) -> None:
        from framework.core.types import OutputMessage
        await self._output_adapter.send(
            OutputMessage(content=xml), ctx.session_id,
        )
    
    async def _notify_parent(
        self, ctx: AgentContext, xml: str, parent_name: str | None,
    ) -> None:
        if not parent_name:
            logger.warning(
                "No parent mapped for subagent '%s', dropping notification",
                ctx.session_meta.agent_name if ctx.session_meta else "unknown",
            )
            return
        
        session_id = ctx.session_id or ""
        parts = self._session_strategy.parse(session_id)
        inbox_key = self._session_strategy.format(
            conversation_id=parts.conversation_id,
            agent_name=parent_name,
        )
        
        from framework.multi_agent.address import AgentAddress
        from framework.multi_agent.envelope import AgentMessageEnvelope
        
        envelope = AgentMessageEnvelope(
            payload={"content": xml, "message_type": "agent_notification"},
            source=AgentAddress(name=ctx.session_meta.agent_name),
            target=AgentAddress(name=parent_name),
            message_type="agent_notification",
            conversation_id=parts.conversation_id,
            agent_session_id=inbox_key,
        )
        await self._agent_bus.send(inbox_key, envelope)
```

### 5.4 MaxIterationNotifyHook

在 `after_turn` 中检查 `result.stop_reason == "max_iterations"`,发送 XML 通知:

```python
class MaxIterationNotifyHook:
    """Hook 完全无感知：不区分 NORMAL/SUBAGENT。
    
    所有 agent（main/coding/reviewer/planner）共用同一个实例。
    路由逻辑封装在 AgentNotificationService 内部。
    """
    
    def __init__(
        self,
        notification_service: AgentNotificationService,
        content_max_chars: int = 2000,
    ):
        self._svc = notification_service
        self._content_max_chars = content_max_chars
    
    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        if result.stop_reason != "max_iterations":
            return
        
        truncated = None
        if result.content:
            truncated = result.content[:self._content_max_chars]
            if len(result.content) > self._content_max_chars:
                truncated += "\n... (truncated)"
        
        agent_name = ctx.session_meta.agent_name if ctx.session_meta else "unknown"
        await self._svc.notify(
            ctx=ctx,
            notification_type="max_iterations_exceeded",
            reason="迭代次数达到上限而退出",
            details=f"agent '{agent_name}' 已达到最大迭代次数 "
                    f"(max_iterations={ctx.max_iterations})",
            content=truncated,
        )
```

### 5.5 改进的 SubagentAutoSendHook

当 subagent 漏发消息时,除了自动转发内容,还通过 `AgentNotificationService` 发送 XML 通知:

```python
class SubagentAutoSendHook:
    def __init__(
        self,
        agent_bus: AgentMessageBus,
        self_name: str,
        parent_name: str = "main",
        notification_service: AgentNotificationService | None = None,
    ):
        self._agent_bus = agent_bus
        self._self_name = self_name
        self._parent_name = parent_name
        self._svc = notification_service  # ← 新增
    
    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        # ...existing check logic...
        
        if any(c.tool_name in sent_tools for c in calls):
            return  # 已发送，无需兜底
        
        sanitized = self._sanitize_forward_content(result.content or "")
        
        # 1. 自动转发内容给父 agent（保持原有行为）
        await self._forward_content(ctx, sanitized)
        
        # 2. 发送 XML 通知（新增）
        if self._svc:
            await self._svc.notify(
                ctx=ctx,
                notification_type="missed_communication",
                reason="subagent 未通过通信工具发送消息",
                details=f"agent '{self._self_name}' 已完成但未调用 "
                        f"send_to_agent_async，内容已自动转发给 "
                        f"'{self._parent_name}'",
                content=sanitized[:2000] if sanitized else None,
            )
```

### 5.6 Hook 配置方式

`AgentNotificationService` 由 `BotService` 创建一次,共享给所有 agent。每个 agent 的 `HookRunner` 按需注入:

```python
# BotService.initialize() 中创建共享 service
self.notification_service = AgentNotificationService(
    output_adapter=self.output_adapter,
    agent_bus=self.agent_bus,
    session_strategy=DefaultSessionIdStrategy(main_agent_name=parent_name),
    parent_map={
        "reviewer": "coding",
        "planner": "coding",
        "office-expert": "main",
        "query-12306": "main",
    },  # ← 业务层配置 agent→parent 映射
)

# 所有 agent 共用同一个 MaxIterationNotifyHook 实例
max_iter_hook = MaxIterationNotifyHook(
    notification_service=self.notification_service,
)

# main agent（NORMAL → 通知用户）
main_runner.add(HookSpec(hook=max_iter_hook, on_error=HookErrorPolicy.LOG))

# coding agent（NORMAL → 通知用户）
coding_runner.add(HookSpec(hook=max_iter_hook, on_error=HookErrorPolicy.LOG))

# reviewer subagent（SUBAGENT → 通知 coding）
reviewer_runner.add(HookSpec(hook=max_iter_hook, on_error=HookErrorPolicy.LOG))
reviewer_runner.add(HookSpec(
    hook=SubagentAutoSendHook(
        agent_bus=self.agent_bus,
        self_name="reviewer",
        parent_name="coding",
        notification_service=self.notification_service,
    ),
    on_error=HookErrorPolicy.LOG,
))

# planner subagent（SUBAGENT → 通知 coding）
planner_runner.add(HookSpec(hook=max_iter_hook, on_error=HookErrorPolicy.LOG))
```

**关键点**：
1. `MaxIterationNotifyHook` **完全无感知**：不接收 `parent_name`，不区分 NORMAL/SUBAGENT
2. `AgentNotificationService` 内部通过 `ctx.session_meta.comm_kind` 自动路由
3. `parent_map` 由业务初始化代码配置，框架不硬编码任何 agent 名称
4. 同一个 `MaxIterationNotifyHook` 实例被所有 agent 共享

## 6. 配置文件

### 6.1 `config/coding_config.yml`

```yaml
llm:
  model: "${LLM_MODEL:-openai/MiniMax-M2.5}"
  api_key: "${LLM_API_KEY}"
  base_url: "${LLM_BASE_URL}"
  temperature: 0.7
  max_tokens: 50000

agents:
  - name: coding
    role: main
    system_prompt_file: "agents/coding.md"
    max_steps: 100
    use_terminal: true
    standard_tools: true
    memory:
      short_term:
        max_messages: 500
        max_tokens: 150000
        keep_ratio_for_messages: 0.3
        keep_ratio_for_token: 0.3
      governance:
        lossy_compaction:
          tool_result_head_chars: 2000
          assistant_head_chars: 2000
          agent_head_chars: 5000
          user_head_chars: 5000
    skills:
      roots: ["skills/coding"]
    approval:
      tools:
        shell: { allowed_paths: ["*"] }
        write_file: { allowed_paths: ["*"] }
        edit_file: { allowed_paths: ["*"] }

  - name: reviewer
    role: subagent
    system_prompt_file: "agents/reviewer.md"
    max_steps: 80
    standard_tools: true
    memory:
      short_term: { max_messages: 80, max_tokens: 80000 }
      governance: {}

  - name: planner
    role: subagent
    system_prompt_file: "agents/planner.md"
    max_steps: 50
    standard_tools: true
    memory:
      short_term: { max_messages: 50, max_tokens: 50000 }
      governance: {}

terminal:
  close_on_exit: false

multi_agent:
  session_retention:
    max_sessions_per_subagent: 10
    max_sessions_global: 100
    ttl_seconds: 86400
    cleanup_interval_seconds: 1800
```

### 6.2 `agents/coding.md`

见 [`examples/bot_project/agents/coding.md`](../../../examples/bot_project/agents/coding.md)。

该文件基于 pi 内置 `worker` agent 的 system prompt,增加了 Coding Agent 专属能力和与 subagent 的通信规则。文件末尾包含**多 Agent 通信规则**强制说明,确保 model 明白:subagent 看不到直接输出的文本,必须通过 `send_to_agent_async` 工具调用通信。

### 6.3 `agents/reviewer.md`

见 [`examples/bot_project/agents/reviewer.md`](../../../examples/bot_project/agents/reviewer.md)。

该文件基于 pi 内置 `reviewer` agent 的 system prompt,增加了与 Coding Agent 的通信规则。文件末尾包含**多 Agent 通信规则**强制说明,确保 model 明白:必须通过 `send_to_agent_async(target_agent="coding", ...)` 返回审查结果。

### 6.4 `agents/planner.md`

见 [`examples/bot_project/agents/planner.md`](../../../examples/bot_project/agents/planner.md)。

该文件基于 pi 内置 `planner` agent 的 system prompt,增加了与 Coding Agent 的通信规则。文件末尾包含**多 Agent 通信规则**强制说明,确保 model 明白:必须通过 `send_to_agent_async(target_agent="coding", ...)` 返回计划。

## 7. 代码修改清单

### 7.1 新增文件

| 文件                                      | 说明                                                          |
| ----------------------------------------- | ------------------------------------------------------------- |
| `framework/multi_agent/session_router.py` | SessionAgentRouter - 按 session 路由用户输入到不同 agent pool |
| `framework/hook/notification.py`          | AgentNotificationService + MaxIterationNotifyHook             |
| `framework/ioc/configs/agent.py`          | 新增 `system_prompt_file: str \| None` 字段                   |
| `config/coding_config.yml`                | coding 体系配置                                               |
| `agents/coding.md`                        | coding system prompt                                          |
| `agents/reviewer.md`                      | reviewer system prompt                                        |
| `agents/planner.md`                       | planner system prompt                                         |
| `skills/coding/`                          | coding 体系 skills 目录                                       |

### 7.2 修改文件

| 文件                                           | 修改内容                                                                                                                                            |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `framework/ioc/configs/app.py`                 | `from_yaml` 支持加载 `system_prompt_file` 内容                                                                                                      |
| `framework/hook/builtin/subagent_auto_send.py` | 集成 `AgentNotificationService`,漏发消息时发送 XML 通知                                                                                            |
| `examples/bot_project/bot/service/core.py`     | 1. 加载 `coding_config.yml`;2. 初始化 `coding_pool`;3. 初始化 `SessionAgentRouter`;4. 创建 `AgentNotificationService`;5. 为所有 agent 注入 hook |
| `examples/bot_project/bot/service/builders.py` | 1. 为 coding subagent 注册通信工具;2. 配置 `SubagentAutoSendHook`(parent="coding")+ `MaxIterationNotifyHook`                                     |
| `examples/bot_project/bot_service.py`          | 启动时加载两个配置                                                                                                                                  |

## 8. 消息路由流程

```
用户发送消息
    │
    ▼
InputAdapter.receive()
    │
    ▼
SessionAgentRouter.route(session_id, content)
    │
    ├── /coding ──→ 标记 session 为 "coding"
    │                 发送 "已切换到 Coding Agent" 给用户
    │                 返回 None(不进入任何 pool)
    │
    ├── /main ────→ 标记 session 为 "main"
    │                 发送 "已切换到 Main Agent" 给用户
    │                 返回 None(不进入任何 pool)
    │
    └── 普通消息 ──→ 查询 session_map
                      main  → 转发到 main_pool broker address
                      coding → 转发到 coding_pool broker address
```

**关键:切换命令只改变 session 标记,不中断任何正在执行的 agent。两个 pool 独立运行,互不阻塞。**

## 9. TODO

- [ ] AST 工具(ast_grep_search, ast_grep_replace)
- [ ] LSP 导航工具(lsp_navigation)
- [ ] reviewer 只读工具集(移除 write_file/edit_file/shell 的写入能力)
- [ ] coding 的 `max_steps` 和 token 限制动态调整
