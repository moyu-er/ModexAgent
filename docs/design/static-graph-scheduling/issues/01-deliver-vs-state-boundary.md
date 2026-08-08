# 01 — Deliver 机制设计（节点间通信通道）

Status: closed
Labels: wayfinder:resolved
Blocking: 02-taskid-shared-context-mechanism, 05-agent-node-redesign, 06-graph-deliver-tool

## Question

**deliver 机制作为 agent 节点间通信的唯一通道，如何设计？**

修正（2026-08-07）：原 ticket 标题"Deliver vs State 职责边界"基于错误理解。state（GraphState）是图控制状态（resume_target、图级阶段），不是 agent 间共享数据总线。agent 上下文由 agent 自己通过 modexctl kb 按 taskId 构建（ticket 02）。**deliver 是 agent 节点间通信的唯一通道**——结果、交接、触发、中间产物全走 deliver。

> **调度统一性原则**: graph 的正常调度、暂停恢复、崩溃恢复,全部依赖同一套 node/deliver 机制。不存在独立的"恢复引擎"——恢复 = "找到该继续的 node → 加入协程池 → 走正常调度路径"。node 通过 load_latest 幂等恢复 (读取最新 invocation 状态),deliver 消费通过 mark_consumed + promote 幂等防重复。**版本链收敛**: invocation version 连续递增,不区分正常调用和恢复调用,不重置、不标记恢复来源。详见 `src/modex_graph/AGENTS.md`。

### 上下文（基于实际实现）

**Node.run() 完整生命周期**（`src/modex_graph/node.py`）：

```
load_latest(resume 检查) → begin_invocation → integrate(collect_consumable_delivers + mark_consumed) → execute(undelivered 检测重试) → submit → complete_invocation + promote_delivers → finalize
```

- Resume from suspend：用 state snapshot 作 integrated input，不重消费 delivers。suspend 后到达的新 PENDING delivers 会被消费。
- Undelivered 检测：execute() 无 deliver → 注入错误反馈重试（max_retry=3）→ RoutingError。

**Deliver 在 execute 期间是纯内存**（`_deliver` docstring）：

> "The `deliver_store`/`graph_instance_id` persistence branch is removed — delivers are always in-memory during execute. Persistence routing happens via the coordinator's `route_deliver` in the dispatch handler."

`deliver()` 累积到 `_pending_delivers`（内存 list）。`submit()` 按 next_node 分组，调 `ctx.dispatch()`。dispatch handler（scheduler 注册）调 `coordinator.route_deliver()` 持久化。

**ctx.dispatch 是即时的**（`src/modex_graph/context.py:246`）：

```python
def dispatch(self, target, state_update):
    self._dispatch_handler(self._current_instance or "", target, state_update)
```

ParallelScheduler `_handle_dispatch`（`src/modex_graph/scheduler/parallel.py:437`）：
1. 校验 target 在 outgoing edges
2. 调 `coordinator.route_deliver(target, content, source, inv_id)` → **立即持久化**
3. 解析 trigger：ON_RECEIVE 无 in-flight → 立即创建+READY；有 in-flight → FIFO。ON_ALL_PREDS → pending queue → 所有 activated sources dispatch + reachability 清除后触发。

**GraphState 是图控制状态**（`src/modex_graph/state/state.py`）：只有 `resume_target` + `checkpoint()`/`from_checkpoint()`。ReActTurnState 的字段（current_node/iteration/llm_response/tool_batches/approval/result）是一个 ReAct agent 的 turn 内部状态——LLM/Tool/End 节点共享因为它们是同一个 agent 的 turn 的组成部分，不是不同 agent 在交换数据。

**GraphPersistenceCoordinator**（`src/modex_graph/persistence/persistence_coordinator.py`）：
- `route_deliver(target, content, source, inv_id)` → `deliver_store.accumulate(...)` → 持久化
- `collect_consumable_delivers(node, inv_id)` → `query_consumable(gid, node)` → PENDING + CONSUMED_PENDING (SQLite)
- `mark_delivers_consumed(node, ids, inv_id)` → `mark_consumed(ids, inv_id)`
- `promote_delivers(node, inv_id)` → promote ALL CONSUMED_PENDING

**AgentNode 当前线性模型**（`src/modex_agent/agents/agent_node.py`）：

```python
async def execute(self, ctx, integrated_input):
    agent_ctx = self._agent_context_factory(ctx)
    result = await self._agent.run(agent_ctx, emitter)
    self.deliver(result, self._next_node, ctx)  # 累积，execute 结束后 dispatch
```

只调 `self.deliver()` 累积——不直接调 `ctx.dispatch()`。当前不支持 mid-execution deliver。

## Discussion (2026-08-07)

### 修正：deliver 是唯一节点间通信通道

原分析基于 ADR-0033 D4 / ADR-0034 D7 的"state 是共享可变的"机制描述，错误地把 state 当成了 agent 间的数据总线。实际：
- GraphState 是图控制状态（resume_target 等），类似 ReActTurnState 是一个 agent turn 的内部状态
- graph_patterns 用 state 传数据是 FunctionNode 模式（确定性函数共享计算状态），不是 agent 间通信
- agent 间通信走 deliver；agent 自己的上下文走 modexctl kb（ticket 02）

### 无 mid-execution：deliver 在 execute() 中累积，submit() 在结束时统一 dispatch

```
execute() 期间：
  agent 调 deliver tool → node.deliver(content, target) 累积到 _pending_delivers
  （可多次 deliver 到不同 target——选择性地投递给下游）

execute() 结束后：
  submit() → 按 target 分组 → ctx.dispatch() → coordinator.route_deliver() 持久化
  → ParallelScheduler: 按 trigger 模式创建/排队下游实例
```

并行发生在节点之间：A 完成后 B 和 C 并行开始（ParallelScheduler 原生支持）。ON_ALL_PREDS 是核心编排模式——节点 D（上游 B+C）等 B 和 C 都 dispatch 后才 READY。下游通过 `IntegratedInput.payloads` 收到多个 `IntegratedPayload`（每个有 `source_node`），按来源区分处理。

### 三条 deliver 路径（收敛到 coordinator.route_deliver）

> **node_id 对齐** (2026-08-07): deliver target 内部统一用 node_id(持久化层主键)。deliver tool 对 agent 暴露 node_name(局部安全:同一 node 的下游不重名),内部转换为 node_id。IntegratedPayload.source_node 用 node_id(全局可能重名:上游可能来自不同子图的同名 node)。

| 路径 | 机制 | 适用 |
|------|------|------|
| **deliver tool**（native agent） | agent 在 execute() 中调 tool(传 target_name) → tool 内部 name→node_id 转换 → `node.deliver(content, node_id)` 累积到 `_pending_delivers`(临时 in-memory) → submit() 统一 dispatch → `deliver_store.accumulate()`(持久化,策略可选) | 框架内 agent |
| **modexctl deliver**（external agent） | CLI → REST → `GraphControlService._deliver` → `coordinator.route_deliver(source="__external__")` → DeliverStore | 外部 agent，需配 `MODEX_TASK_ID` env |
| **auto-deliver**（execute 结束） | AgentNode.execute() 结束时调 `node.deliver(output, None, ctx)`(None → _resolve_default_target 返回下游 node_id 列表) → submit() dispatch | 兜底，agent 没用 deliver tool 时自动投递最终结果 |

### auto-deliver：SubagentAutoSendHook 模式

参考 `SubagentAutoSendHook`（`src/modex_agent/hook/builtin/subagent_auto_send.py`）：
- `FINALLY_TURN` 钩子，总是触发（success/error/cancel 全路径）
- 从 `result.messages` 提取最后一条 assistant 消息
- 通过 `build_agent_comm_message` 格式化：header（status/stop_reason/issue）+ body（Result: 实际输出）

图调度版 auto-deliver：
- **不需要 hook 也会投递**：AgentNode.execute() 结束时 `self.deliver(output, None, ctx)`(None → `_resolve_default_target` 返回下游 node_id 列表) → submit() dispatch。基础路径 always works。
- **先实现基础版**：直接提取最后 assistant 消息文本 deliver
- **后续完善**：加 SubagentAutoSendHook 式的结构化信封（ResultMeta 头 + 格式化 body）

### deliver tool：动态暴露 + system provider 补强

**Tool 动态暴露**（参考 `TaskDispatchTool` + `CommunicationTargetStore`，`src/modex_agent/multi_agent/tools.py`）：
- `CommunicationTargetStore` 持有 targets（name/kind/description）
- `tool.description` 是 property，动态构建：列出可用 downstream 节点名 + 描述
- `get_dynamic_schema()` 把 `target_node` 参数绑 enum 到当前可用 target 名
- `execute()` 校验 target_name → 内部转换为 node_id(局部安全:同一 node 的下游不重名) → 调 `node.deliver(content, node_id, ctx)` 累积

图调度版：`GraphDeliverTargetStore` 从 `_graph_ref.edges_from(self.name)` 提取下游节点名，tool description 动态列出可用 downstream。execute 时 name→node_id 转换(通过 `graph_ref.nodes[name].node_id`)。

**System provider 补强**（参考 `AgentCommunicationSystemPromptProvider`，`src/modex_agent/memory/prompt_pipeline/providers.py`）：
- `SystemPromptProvider` ABC：`_fetch_version()` + `_fetch_content()`，版本缓存
- 复合 provider 有 sub-modules 检查 tool_manager 里的 targets，注入通信上下文

图调度版：`GraphContextSystemPromptProvider`——注入图上下文到 system prompt（当前节点位置、上下游节点、角色职责）。

### IntegratedInput 消费：按 node 分章节 + part_x（AgentNode 业务设计）

> **source_node 语义** (2026-08-07): `IntegratedPayload.source_node` 是 node_id(来自 deliver_store 持久化层)。AgentNode 格式化时需 node_id→name 反查(通过 `graph_ref.nodes` 反查,全局可能重名所以必须用 node_id 作为标识)。非 AgentNode 节点同样消费 node_id。

下游 agent 收到上游 delivers 时，AgentNode 把 `IntegratedInput.payloads` 格式化为**一个** system-reminder，**按 source node 分章节**(source_node 从 node_id 反查为 name 显示)，每个 node 内部按顺序标 `part_x`：

```
<system-reminder>
Message from node 'research':
  part_1: 研究发现3个子问题...
  part_2: 补充发现一个关键约束...

Message from node 'planner':
  part_1: 任务分解完成，分为3个子任务...
</system-reminder>
```

上游可能多次 deliver，同一 source node 的多条 payload 归到同一章节内，按顺序标 `part_N`。

**归属**：`IntegratedInput`（payloads 带 `source_node`）是 modex_graph 的机制——框架层提供数据结构。格式化为 system-reminder 的排版和章节化是 **AgentNode 的业务设计**，放 modex_agent，不是 modex_graph 的强制要求。非 AgentNode 的节点（FunctionNode 等）可以按自己的方式消费 `IntegratedInput`。

参考 SubagentAutoSendHook 的消费侧——parent agent 通过 InboxFlushHook 把 inbox 消息作为 `role=SYSTEM_REMINDER` 注入 conversation history。图调度版：AgentNode.execute() 收到 `integrated_input`，格式化后注入 agent 的 conversation history。

### agent 感知上下游

图调度中需要引入（非图调度的常规使用不受影响）：
- **下游感知**：deliver tool 动态描述列出可用 downstream targets（从图拓扑提取）
- **上游感知**：IntegratedInput 的 system-reminder 格式化让 agent 知道谁投递了什么
- **全貌**（是否需要完整图视图）：后续再说
- 当前 `examples/bot_project/` 没有这套机制，图调度需要引入

### 清理项

| # | 问题 | 处置 |
|---|------|------|
| C1 | `NodeInstance.upstream_payloads` 死数据 | 保留清理——存了没人读，节点通过 coordinator 拿上游数据 |
| C3 | `deliver()` 的 ctx 参数 vestigial | 保留清理——`_deliver` 不用 ctx，旧设计残留 |
| C5 | `SchedulerInstanceStatus` 重复枚举 | 保留清理——和 `NodeInstanceStatus` 重复 |

`_submit_result` 留着（submit 处理 deliver 的结果记录）。`ctx.dispatch()` 留着 public（submit 内部用）。

### 待确认

- auto-deliver 的内容结构（从 AgentResult 提取什么）→ ticket 05
- GraphDeliverTargetStore 的具体设计（放框架还是业务）→ ticket 06

## Comments

<!-- 讨论记录追加于此 -->
