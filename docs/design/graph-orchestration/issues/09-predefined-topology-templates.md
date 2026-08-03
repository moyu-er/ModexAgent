# 预定义拓扑模板

Status: triage:closed
Assignee: sisyphus
Started: 2026-08-02
Resolved: 2026-08-02

## Question

用户之前选了"分层:预定义+自定义"——v1 支持预定义拓扑模式(star/supervisor/swarm/map-reduce),v2 开放任意合法图。

需要决策:

1. **v1 提供哪些预定义拓扑?** — 
   - Star(主 agent 调度子 agent,现有 multi_agent 的模式)
   - Supervisor(一个 supervisor 节点分发任务给多个 worker)
   - Swarm(多 agent 并行处理,结果聚合)
   - Map-Reduce(split → fan-out → reduce,已有 graph_patterns/map_reduce.py)
   - Pipeline(线性链,A→B→C→END)
   - Conditional(if/else,已有 graph_patterns/conditional.py)
   - 其他?

2. **拓扑模板的形态** — 
   - 生成 GraphSpec(声明式配置)?
   - 生成 Graph(编程式构建)?
   - 作为 Graph 子类(如 `class StarGraph(Graph[S])`)?
   - 作为 builder 函数(如 `build_star_graph(...) -> Graph`)?

3. **模板参数化** — 每个模板需要什么参数?
   - Star:主节点 + 子节点列表 + 委派策略
   - Supervisor:supervisor 节点 + worker 节点列表 + 分发策略
   - Map-Reduce:map 函数 + worker 节点 + reduce 函数
   - 参数如何表达?Pydantic config model per topology?

4. **与现有 graph_patterns 的关系** — `examples/graph_patterns/` 已有 conditional/retry/map_reduce 三个模式。它们是"示例"还是"模板"?如果做预定义拓扑,是否将它们提升为框架级模板?

5. **与现有 multi_agent star topology 的关系** — 现有 `multi_agent/` 是 star topology 的完整实现(subagent_validator + AgentCommunicationService + send_to_agent)。如果提供 Star 拓扑模板,它与现有 multi_agent 是什么关系?收敛?共存?

## Context

- 用户之前:"分层:预定义+自定义"
- 02 决议确认:graph_patterns 的 conditional/retry/map_reduce 提升为 modex_graph 通用实现(图调度系统是第二个消费者,满足 ADR-0007)。它们对应的 Node 类型(ConditionNode/RetryNode/MapReduceNode)已在 02 决议的通用 Node 清单中。
- ADR-0033 D9.1:Preset graphs deferred,Graph 子类化是合法模式
- 现有 `examples/graph_patterns/`:conditional.py, retry.py, map_reduce.py(将提升为框架级)
- 现有 `multi_agent/`:star topology 完整实现
- graph engineering 概念:cycgraph 内置 Supervisor/Swarm/Map-Reduce/Self-Annealing 模式

## Resolution criteria

明确以下决策:
- v1 预定义拓扑清单
- 拓扑模板形态(GraphSpec 生成 / Graph 子类 / builder 函数)
- 模板参数化方式
- 与现有 graph_patterns 的关系(提升 / 保持示例 / 替代)
- 与现有 multi_agent star topology 的关系(收敛 / 共存 / 独立)

## Resolution

### 1. 预定义拓扑模板:推迟实现

预定义拓扑模板(Pipeline/Star/Supervisor/Swarm)的 ROI 当前太低,推迟到后续。除非实现过程中发现需要这些图,否则不做。

### 2. deliver/submit 让大部分模式可直接构造

deliver/submit 分解后,很多原来需要专门通用实现的模式可以直接用通用 node + deliver/submit 构造:

| 模式 | 原方案 | deliver/submit 后 | 是否需要单独实现 |
|------|--------|-------------------|----------------|
| **Map-Reduce** | MapReduceNode(内部封装 fan-out+fan-in) | split 节点 deliver 到多个 worker → worker deliver 到 reduce → InputIntegrator 整合 | ❌ 不需要 |
| **Conditional** | ConditionNode(内部 if/else) | 判断节点 deliver(content, next_node="branch_a/b") | ❌ 不需要 |
| **Retry** | RetryNode(内部自循环) | 节点 deliver 到自己 + 计数判断 | ⚠️ 边界,可用 FunctionNode+状态字段实现 |
| **Pipeline** | 专门模板 | A→B→C→END,静态边+deliver(走默认) | ❌ 不需要 |

**结论**:02 决议中提升的 ConditionNode/RetryNode/MapReduceNode 可能不需要单独通用实现——deliver/submit 让它们可以用通用 node 直接构造。实现阶段如发现需要再补。

### 3. 首要任务:React 图适配新架构

当前框架调整完成后(deliver/submit 替代 transition/command/_compile_routing),React 图(build_react_graph)必须相应适配。这是当前首要任务。

**React 是 LinearScheduler,可大幅简化**:
- 不需要 deliver_states 表(linear 单路径,deliver 直接转 submit)
- 不需要 InputIntegrator(linear 单上游)
- 不需要 graph_instance 的复杂恢复(linear 从 START 重跑或从 resume_target 恢复,已有机制)

**适配点**:
- `NodeResult(transition=ReActReason.HAS_TOOLS)` → deliver(content, next_node="tool")
- `NodeResult(transition=ReActReason.NO_TOOLS)` → deliver(content, next_node="end")
- `Command(goto=...)` → deliver(content, next_node=state.resume_target)
- 静态边的 `reason` → 不再需要(deliver 显式指定 next_node)
- `_compile_routing` → `_submit`(linear 简化版)
- 4 个节点(StartNode/LLMNode/ToolNode/EndNode)的 execute 适配
- ReactGraphRuntime 适配 _execute/_deliver/_submit 调用

**新建 ticket 11**:React 图适配新架构(实现任务,非决策)。
