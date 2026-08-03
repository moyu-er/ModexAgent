# React 图适配新架构

Status: triage:closed
Assignee: sisyphus
Resolved: 2026-08-02
Note: 设计已在 ticket 07/09 完成。本 ticket 是实现任务(框架修改的连带必做项),不需要 grilling。实现阶段执行适配清单即可。

## Question

deliver/submit 投递模型(ticket 07)完全替代了 transition/command/state_update-as-payload/manual-dispatch/_compile_routing。React 图(build_react_graph)当前使用 transition/Command/静态边 reason 做路由,必须适配新架构。

这是实现任务,非决策任务。设计已在 ticket 07 中完成,本 ticket 是执行。

## 适配清单

### 节点 execute 适配

4 个节点的 execute 需要将 transition/Command 改为 deliver/submit:

| 节点 | 原方式 | 新方式 |
|------|--------|--------|
| StartNode | `NodeResult(transition=ReActReason.NORMAL_START)` | deliver(content, next_node="llm") |
| LLMNode | `NodeResult(transition=ReActReason.HAS_TOOLS)` | deliver(content, next_node="tool") |
| LLMNode | `NodeResult(transition=ReActReason.NO_TOOLS)` | deliver(content, next_node="end") |
| LLMNode | `NodeResult(transition=ReActReason.MAX_ITERATIONS)` | deliver(content, next_node="end") |
| LLMNode | `NodeResult(transition=ReActReason.LLM_ERROR)` | deliver(content, next_node="end") |
| ToolNode | `NodeResult(transition=ReActReason.TOOLS_DONE)` | deliver(content, next_node="llm") |
| ToolNode | `NodeResult(transition=ReActReason.TURN_CANCELLED)` | deliver(content, next_node="end") |
| EndNode | `NodeResult(transition=None)` → default edge | deliver(content, next_node=END) 或不 deliver(silent skip,END 收尾) |
| Approval resume | `Command(goto=state.resume_target)` | deliver(content, next_node=state.resume_target) |

### build_react_graph 适配

- 静态边的 `reason` 参数不再需要(deliver 显式指定 next_node)
- 边仍定义拓扑(哪些节点可以连),但不再用 reason 做条件匹配
- 入口边 `GraphNode.START → ReActNode.START` 保留
- 默认边 `ReActNode.END → GraphNode.END` 保留

### _compile_routing → _submit

LinearScheduler 的路由逻辑从 _compile_routing 改为 _submit:
- _compile_routing(transition/command 匹配静态边)→ _submit(按 deliver 累积的 next_node 分组派发)
- LinearScheduler 简化版:单路径,deliver 直接转 submit

### GraphInterrupt 保留

- `ctx.interrupt(requests)` 保留(approval 机制不变)
- GraphInterrupt 仍用于 approval 暂停/恢复
- resume_target 保留(恢复时路由到指定节点)

### ReactGraphRuntime 适配

- 适配 _execute/_deliver/_submit 调用
- ReactGraphRuntime 的 hook(before_node/after_node/dispatch_hook/drain_control/capture_snapshot/emit)保留

## React 简化(LinearScheduler 优势)

- **不需要 deliver_states 表**:linear 单路径,deliver 直接转 submit
- **不需要 InputIntegrator**:linear 单上游
- **不需要 graph_instance 复杂恢复**:linear 从 START 重跑或从 resume_target 恢复(已有机制)
- **不需要 ParallelScheduler 的 activated_sources/pending_dispatches/multi-instance**

## 验收标准

- React 图的所有测试通过(tests/unit/agents/react/)
- transition/command/静态边 reason 不再使用
- deliver/submit 正常工作
- approval 机制(GraphInterrupt + resume_target)不受影响
