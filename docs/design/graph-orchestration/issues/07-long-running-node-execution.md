# 长时 node 的执行模型

Status: triage:closed
Assignee: sisyphus
Started: 2026-08-02
Resolved: 2026-08-02

## Question

一个"agent 完整调用"可能持续几分钟到几小时(尤其 ExternalAgent 是外部进程)。modex_graph 的 `execute` 是图 loop 内的一次函数调用(可能 async)。

**张力 B**:一个 node 阻塞几分钟,整个图就卡住了(LinearScheduler);ParallelScheduler 下其他 node 可以并行,但这个 node 仍然占用一个 instance slot。

**本 ticket 聚焦**:execute 的阻塞语义 + ExternalAgent 特殊考虑 + 超时与取消。**恢复/幂等/多节点并行恢复交给 ticket 10**(图生命周期管理)。

需要决策:

1. **node.execute 的阻塞语义** — 
   - **方案 A:阻塞等待**:execute 内部 `await agent.run()` 直到 agent 完成。简单,但图 loop 被阻塞。
   - **方案 B:异步启动 + 完成回调**:execute 启动 agent 后立即返回(返回一个"pending" NodeResult),agent 完成后通过某种机制(dispatch?)通知图。图不阻塞,但需要新的"异步 node 完成"机制。
   - **方案 C:GraphInterrupt + 外部恢复**:execute 启动 agent 后 raise GraphInterrupt(携带 agent handle),图挂起。agent 完成后,外部调用 `engine.resume()` 恢复。类似 openclaw 的 detached-task-runtime。**依赖 ticket 10 的 CheckpointStore resume 接通。**
   - **方案 D:Task fan-out**:execute 启动 agent 作为 Task(goto=[Task(node="agent_result_collector")]),agent 完成后 dispatch 到结果收集节点。利用 ParallelScheduler 的 continuous scheduling。

2. **与 ParallelScheduler 的结合** — 如果用 ParallelScheduler,长时 node 不阻塞其他 node。但:
   - instance 的 fork 持有 state 快照,长时 node 期间 state 被冻结。
   - max_iterations 计数:长时 node 算 1 次 iteration 还是多次?
   - checkpoint:async checkpoint 在长时 node 期间如何触发?

3. **ExternalAgent 作为 node 的特殊考虑** — ExternalAgent 是外部进程(OpenCode 等):
   - execute 如何启动外部进程?如何等待?
   - 进程输出如何流回?
   - 进程崩溃如何处理?(崩溃恢复的幂等性交给 ticket 10)
   - 与 ADR-0022(External coding agent integration)+ ADR-0027(External coding agent as subagent)的关系?

4. **超时与取消** — 长时 node 需要超时机制。如何配置?超时后如何取消 agent?(取消后的状态恢复交给 ticket 10)

## Context

- grilling 对齐:node = 通用概念,可能是 ExternalAgent
- ADR-0033 D3:sync/async dual mode,execute 可以是 async def
- ADR-0034 D2:ParallelScheduler continuous scheduling,asyncio.wait(FIRST_COMPLETED)
- ADR-0034 D7:multi-instance model,fork-based state isolation
- ADR-0022:External coding agent integration(外部进程作为 agent)
- ADR-0027:External coding agent as subagent
- openclaw:detached-task-runtime(分离任务运行时,后台执行)
- openclaw:tryRecoverTaskBeforeMarkLost(任务恢复钩子)
- dify:节点执行有超时机制

## Resolution criteria

明确以下决策:
- node.execute 的阻塞语义方案(A/B/C/D 选一,或组合)
- 与 ParallelScheduler 的结合方式
- ExternalAgent 作为 node 的执行路径
- 超时与取消机制
- 是否需要先接通 CheckpointStore resume(如果方案 C)

## Resolution

### 1. 阻塞语义:复用现有阻塞 await(方案 A)

AgentNode.execute 内部复用现有 TurnRunner/agent.run 的阻塞 await 路径。调度进程不退出,execute 阻塞在 `await agent.run()` 上直到 agent 完成。ParallelScheduler 下其他 node 可以并行(asyncio)。

### 2. deliver/submit 投递模型(核心设计加强)

**替代 transition/command/state_update-as-payload/manual-dispatch/_compile_routing,成为唯一的投递机制。**

#### 拆分:deliver(累积)→ submit(投递)

```
node._execute(ctx) 框架调用:
  1. InputIntegrator.integrate(上游 submits) → 整合多上游输入为 IntegratedInput
  2. node.execute(ctx) → node 自定义逻辑(execute 执行期间 deliver 可被多次调用,通过 cli 或 node 内部逻辑)
     deliver(content, next_node?) → 累积
     deliver(content, next_node?) → 继续累积
  3. execute 完成 → node._submit(ctx) 框架自动调用:
     累积的 results 按 next_node 分组
     → 每组打包成一个整合消息(IntegratedPayload)
     → dispatch 到对应下游节点
```

**三层方法拆分**(与 _execute/execute 一致):
- `_deliver`(框架固定):累积 deliver 到节点内部,持久化(deliver 表)
- `deliver`(node 自定义,可覆盖):实际累积逻辑(默认:append 到 pending list)
- `_submit`(框架固定):execute 完成后自动调用,按 next_node 分组派发
- `submit`(node 自定义,可覆盖):实际派发逻辑(默认:按 next_node 分组,每组整合)

**deliver 参数**:
- `content: Any` — 投递内容
- `next_node: str | None = None` — 目标节点。不填→走默认(有默认边就用默认);无默认→看是否有下游节点,有就全部累积(deliver 中说明)

**next_node 规则**:
- 显式指定 → 累积到指定 next_node
- 不填 + 有默认边 → 走默认
- 不填 + 无默认 + 有下游 → 全部下游累积
- 不填 + 无默认 + 无下游 → 累积到 END(终态收尾)

#### 下游输入整合(InputIntegrator ABC)

每个节点可能有多个上游,各自 submit 一个 IntegratedPayload。node 内部做整合:

```python
class InputIntegrator(ABC):
    @abstractmethod
    def integrate(self, inputs: list[IntegratedPayload]) -> IntegratedInput: ...
```

- `IntegratedPayload`:结构体,包含上游的全部内容 + 元数据(source_node, content, metadata)
- `IntegratedInput`:整合后的单一输入,用于 execute。内部维护原有结构体全部内容,整合到一个结构体且配置元数据
- 框架给默认通用实现(如:拼接所有 IntegratedPayload 为 list / 合并 dict / 取最后一个)
- node 可自定义实现

**触发时机**:InputIntegrator.integrate() 在 _execute 中 execute 之前调用。所有已激活上游 submit 后触发(不可达上游不 submit,不等待,复用 activated_sources + _can_reach_active 机制判断)。

**ParallelScheduler 下游触发条件**:
```
上游 _submit 派发 → 下游收到 IntegratedPayload → _recheck_pending:
  ON_ALL_PREDS: 所有已激活上游(activated_sources)都 submit
                + 不可达上游(_can_reach_active=False)不等待 → READY → 加入执行池
  ON_RECEIVE:  任意上游 submit → READY → 加入执行池
```
**注意**:不能用静态入度判断"所有上游是否 submit"。上游可能因路由选择(deliver 指定其他 next_node)永远不会被调度,此时该上游不可达(_can_reach_active=False),下游的等待集合应减少,不等待这些上游。deliver/submit 替代后,`activated_sources` 改为记录"实际 submit 过的上游",`_can_reach_active` 逻辑不变(BFS 基于图拓扑 + 当前活跃实例)。

**即时调度**:submit 触发 _recheck_pending 后,能执行的节点立即加入 parallel 执行池,不分批。与现有 ParallelScheduler 的 `asyncio.wait(FIRST_COMPLETED)` 机制一致。

**注意**:上游和下游是多对多关系。每个 node 只处理它从多个上游收到的内容然后整合。

#### deliver 持久化

**持久化抽象**:deliver 持久化是 ABC + 通用实现。ParallelScheduler 场景用 deliver_states 表(SQLite);LinearScheduler 场景可用内存对象(不写表),因为 crash 后重跑 execute,deliver 重新累积。持久化策略由 scheduler/节点选择,框架提供 ABC。

- deliver 累积的内容需要持久化(故障恢复)
- 统一 SQLite 表(deliver_states),与 node_states / graph_instances 同 schema 风格
- Snowflake ID
- modexctl 的 deliver 正确路由到相应 node 的 deliver

```sql
deliver_states:
  deliver_id          BIGINT (PK, Snowflake)
  graph_instance_id   BIGINT (FK → graph_instances)
  node_name           TEXT (当前累积的节点)
  next_node           TEXT (目标下游节点)
  content_json        TEXT (JSON, 投递内容)
  status              TEXT (accumulated/submitted)
  created_at          INTEGER (timestamp ms)
  updated_at          INTEGER (timestamp ms)
```

### 3. 与 ParallelScheduler 的结合

- 长时 node 阻塞 await,不阻塞其他 node(ParallelScheduler 并行)
- instance slot 占用直到 execute 返回
- max_iterations:长时 node 算 1 次 iteration(execute 一次调用)
- checkpoint:execute 期间不触发 checkpoint(execute 返回后才 checkpoint)。deliver 的累积通过 deliver_states 表持久化(不等 checkpoint)

### 4. ExternalAgent 作为 node

- AgentNode.execute 内部调 TurnRunner.process_locked(阻塞 await)
- agent 执行过程中可通过 cli deliver 累积结果(modexctl deliver → REST → deliver_states 表 → node._deliver 读取)
- agent 继续执行,自然收尾(不中断)
- execute 返回 → _submit 派发
- 进程崩溃:故障恢复(ticket 10)从 checkpoint + deliver_states 恢复

### 5. 超时与取消

- 超时配置:NodeSpec.config 中声明 timeout(如 `timeout_seconds: 300`)
- 超时后:框架触发异常(ticket 10 类别 3 的异常控制链),node._execute 全部退出
- 取消后的状态:故障恢复(ticket 10),从 checkpoint + deliver_states 恢复
- 外部 stop/pause:走异常控制链(ticket 10 类别 3)

### 6. transition/command 的替代

**完全替代**。transition/command/state_update-as-payload/manual-dispatch/_compile_routing 全部被 deliver/submit 取代。

| 原机制 | 替代 |
|--------|------|
| NodeResult.transition | deliver 的 next_node 参数 |
| NodeResult.command.goto | deliver 的 next_node 参数(显式指定) |
| NodeResult.state_update (as dispatch payload) | deliver 的 content |
| NodeResult.state_update (as graph state) | 保留,仍用于图级状态更新(main_state 字段更新,checkpoint 用) |
| ctx.dispatch (manual) | node._deliver |
| _compile_routing | node._submit |
| static edge reason (transition matching) | 不再需要(deliver 显式指定 next_node)。静态边仍定义拓扑(哪些节点可以连),但不再用 reason 做条件匹配 |

**state_update 分离**:state_update 不再作为 dispatch payload,只用于图级状态更新(main_state 字段更新)。deliver content 是下游输入。两者职责分离。

### 7. 对其他 ticket 的影响(需统一修正)

| Ticket | 修正 |
|--------|------|
| **02** | 双输入模型修正:上游 submit → 下游 InputIntegrator 整合 → execute。输出修正:deliver 累积 → _submit 派发(不再用 NodeResult.transition/command 做 dispatch) |
| **03** | "未投递"重新定义:execute 返回但无任何 deliver 累积 → 可能是 silent skip(旁挂/END)或需要错误反馈重跑。ticket 03 原定义的"情况 E(transition 不匹配)"不再存在(transition 被替代) |
| **04** | node._execute 扩展:调 _deliver(累积,可被 agent 通过 cli 触发)→ execute → _submit(框架自动,按 next_node 分组派发) |
| **08** | NodeSpec 可增加 timeout 配置。EdgeSpec 的 reason 字段废弃(transition 被替代) |
| **11** | deliver 持久化(deliver_states 表)。deliver 路由(modexctl deliver → REST → deliver_states)。外部控制 deliver 的含义修正:不是"外部投递触发异常中断",而是"外部投递累积到当前节点" |
| **PRD** | Not yet specified 中补充"transition/command 迁移" |
