# 分布式持久化与 Node 生命周期统一调度

Status: ready-for-implementation
Date: 2026-08-03
Supersedes: issue 10 §1.1 (CheckpointStore load_latest 接通) + issue 10 §类别2新增 (Node 级状态抽象)
Related: issue 04 (GraphInstance), issue 07 (deliver/submit), issue 10 (lifecycle), ADR-0034 D19

## 1. 背景:实现检视暴露的设计缺口

图调度编排系统完成 30 commit 实现后,检视发现持久化层面存在设计分歧:

- **CheckpointData 单一 blob** — `ParallelScheduler._build_checkpoint_data()` 把 `main_state` / `pending_dispatches` / `completed_instances` / `activated_sources` / `instance_seq` / `iteration_count` 序列化为一个 JSON blob,通过 `CheckpointStore.save/load_latest` 持久化。graph 状态是"一个快照",无法 per-node 查询。
- **NodeState ABC 零 caller** — `NodeState` + `NodeStateStore` 已定义但无框架 caller(hypothetical seam, rule 6)。
- **LinearScheduler 无 checkpoint** — 与 ParallelScheduler 的持久化能力分歧。
- **scheduler 拥有持久化逻辑** — `_build_checkpoint_data` / `_schedule_checkpoint` / `_restore_from_checkpoint` 全在 ParallelScheduler 内,scheduler 既管调度又管持久化。

## 2. 核心设计:分布式持久化

### 2.1 原则

1. **graph 状态 = graph metadata + 各 node 状态拼装**。不再用单一 JSON blob 维护全局状态。
2. **每个 node 拥有自己的持久化**。node 有多次调用(图有环),每次调用是一个 invocation,形成版本链。
3. **scheduler 无感知 checkpoint_store**。GraphInstance 提供/配置持久化策略,scheduler 只管调度。
4. **持久化策略是 GraphInstance 级决策**。GraphInstance 选择默认策略(sql/noop/in-memory),node 可覆盖自己的策略。
5. **统一接口**。三种实现(Null / Memory / SQLite)走同一 ABC,memory/noop 简化但 CRUD 接口一致。
6. **持久化与内存对象统一设计**。GraphInstance 是逻辑内存对象,其生命周期自然管理下游所有内存对象。in-memory 实现随 GraphInstance 对象 GC 自然消失;SQLite 实现数据持久化,跨进程存活。这不是"显式清理 vs 不清理"的差异,而是"对象生命周期 vs 数据生命周期"的统一。
7. **非持久化允许恢复时数据丢失**。in-memory 策略只保证**单次执行流程内**的数据流转,不保证 crash recovery。crash 后 GraphInstance 内存对象消失,in-memory 数据丢失——这是持久化策略的语义,不是缺陷。SQLite 策略才提供 crash recovery。
8. **链式处理/版本链/状态机是持久化策略自定义的**。in-memory 可以不做版本链(简单 dict,单次流程够用);SQLite 做完整版本链。ABC 接口统一,实现策略不同。

### 2.2 持久化分层

| 层 | 内容 | 存储位置 | 谁拥有 | 生命周期 |
|----|------|----------|--------|----------|
| Graph metadata | instance_id, status, instance_seq, iteration_count, activated_sources, pending_dispatches | graph_instances 表 | coordinator(per-GraphInstance) | 随 GraphInstance 对象 GC(in-memory) / 持久化(SQLite) |
| Node invocation | invocation_id, node_name, version, parent_version, status, state_json | node_states 表(统一表) | 各 node 的 NodeState | 随 GraphInstance 对象 GC(in-memory) / 持久化(SQLite) |
| Deliver(收到的内容) | deliver_id, graph_instance_id, target_node, source_node, source_invocation_id, content, status(accumulated/consumed) | deliver_store(per-node) | 各 node 持有 deliver_store 引用 | 随 GraphInstance 对象 GC(in-memory) / 持久化(SQLite) |

**关键变化**: DeliverStore 从 graph 级统一管理改为 **per-node** 管理。每个 node 持有自己的 deliver_store 引用(表示"我收到的内容"),可以复用 graph 提供的默认实现,也可以独立自定义策略。

**移除**: `CheckpointData` 单一 blob(被分布式持久化完全替代)。`main_state` 从各 node 的 state_update 累积重建。`completed_instances` 从各 node 的 COMPLETED 版本推导。

### 2.3 Deliver 路由与消费

**投递(生产侧)**: Node A 在 execute 中调 `deliver(content, next_node=B, ctx)`:
```
deliver(content, next_node, ctx)
  → coordinator 路由: 找到 target node B 的 deliver_store
  → B.deliver_store.accumulate(
      graph_instance_id, target_node=B, source_node=A,
      source_invocation_id=A 的 invocation_id,
      content=content
    )
```

**消费(消费侧)**: Node B 的 `run()` 在 integrate 之前,从自己的 deliver_store 查询未消费的 delivers:
```
B.run() → integrate 阶段:
  pending_delivers = B.deliver_store.query_consumable(graph_instance_id, target_node=B)
  未消费的 delivers → 按 source_invocation_id 做消费幂等
  → 标记为 consumed(通过 B 的当前 invocation_id)
  → 整合为 IntegratedInput
  → B.execute(ctx, integrated_input)
```

**消费幂等**: deliver 的消费幂等是**持久化策略自定义的**能力,不同策略实现不同:

**InMemory 策略**(单次流程内,不持久化):
- 简单 dict 记录已消费的 deliver_id
- 同一次流程内不重复消费（node 重新进入时已消费的记录不在了）
- `mark_consumed` 标记为 consumed
- `promote_consumed`（invocation COMPLETED 时）= 清理已消费记录（删除，等价于"不需要了"）
- crash 后内存对象消失,数据丢失,不存在恢复场景——不需要三态

**SQLite 策略**(持久化,支持 crash recovery):
- deliver 消费状态三态,与 invocation 状态机绑定:

| deliver 消费状态 | 含义 | 恢复时行为 |
|-----------------|------|-----------|
| **PENDING** | 已投递,未被任何 invocation 消费 | 纳入消费 |
| **CONSUMED_PENDING** | 被某 invocation 消费,但该 invocation 未 COMPLETED(crash/suspend/cancel) | 重新纳入消费(上次没完成) |
| **CONSUMED_COMPLETED** | 被某 invocation 消费,且该 invocation 已 COMPLETED | 跳过(已处理完成) |

- `mark_consumed` 标记为 CONSUMED_PENDING
- invocation COMPLETED 时 `promote_consumed` 升级为 CONSUMED_COMPLETED
- crash/suspend/cancel 时保持 CONSUMED_PENDING——恢复时新 invocation 重新消费

**node 重新进入时屏蔽(SQLite)**: node B 第二次被 dispatch(图有环),新 invocation 查 deliver_store:
- 上次 invocation CONSUMED_COMPLETED 的 delivers → 跳过(已处理)
- 新投递的 PENDING delivers → 纳入消费

**crash 恢复时重消费(SQLite)**: node B 的 invocation crash(status=CRASHED),恢复时新 invocation:
- crash 的 invocation 消费的 delivers 是 CONSUMED_PENDING → 重新纳入消费
- 新 invocation mark_consumed → CONSUMED_PENDING(新)
- 新 invocation 完成 → 升级为 CONSUMED_COMPLETED

**node 自定义恢复的扩展性**: 消费逻辑在 coordinator(§4.4 `collect_consumable_delivers` / `mark_delivers_consumed` / `promote_delivers`),委托 DeliverStore。未来如 node 需自定义消费(如"不重新传递 CONSUMED_PENDING,node 有自己的内部恢复方案"),出现第二个实现时再抽 ABC(rule 6)。当前默认实现基于消费状态 + invocation 状态机。

**per-node deliver_store 配置**: graph 提供默认 deliver_store 工厂(Null/Memory/SQLite),node 默认复用 graph 的默认实现。node 也可以在 `NodeSpec.config` 或 Node 属性中声明自己的 deliver_store 策略。coordinator 在 `register_node` 时为每个 node 创建 deliver_store 引用。

### 2.4 Graph 状态查询

查询 graph 状态 = 收集 graph metadata + 各 node 的版本列表:

```python
graph_instance.get_state(
    node_status_filter: set[InvocationStatus] | None = None,  # 默认 {COMPLETED}
) -> GraphStateSnapshot:
    """返回 graph metadata + 各 node 的版本列表(按 filter 过滤)。"""
```

返回结构:
```python
class GraphStateSnapshot:
    metadata: GraphMetadata  # instance_id, status, instance_seq, iteration_count
    nodes: dict[str, list[NodeInvocationRecord]]  # node_name → 版本列表(按 version DESC)
```

索引需求:
- `node_states(graph_instance_id, node_name, status)` — 按状态过滤查询
- `node_states(graph_instance_id, node_name, version DESC)` — 按版本排序查询
- `node_states(graph_instance_id, status)` — 跨 node 按状态查询(如查所有 RUNNING)

前端查询接口(未来实现,设计预留):
- `GET /api/graph/{instance_id}/state` — 默认返回 COMPLETED 历史
- `GET /api/graph/{instance_id}/state?status=running,completed` — 指定状态过滤
- `GET /api/graph/{instance_id}/nodes/{node_name}/history` — 单 node 版本历史

## 3. Node 生命周期统一调度

### 3.1 设计原则

框架抽象层统一调度 node 的生命周期(start/canceled/crashed/complete),让 node 自定义负担缩小。node 只实现 `execute` 逻辑 + 幂等设计,框架负责:

- invocation 创建与版本链维护
- 状态机转换(PENDING → RUNNING → COMPLETED / CANCELED / CRASHED)
- 持久化(通过 NodeState 接口)
- finally 处理 crash 情况
- 新建任务(complete 之后再进入节点调度 = 新 invocation)

### 3.2 Node.run() 新流程(概要)

```
Node.run(ctx, *, graph):
  coordinator = ctx.coordinator   ← always present(Null/Memory/SQLite)
  1. coordinator.begin_invocation(node_name)   ← F8: parent_version 内部计算
     → 创建新 invocation,version = max(已有版本)+1
     → status = PENDING → RUNNING
     → 持久化(通过 node 的 NodeState.save_invocation)
  
  2. try:
       integrate(从 deliver_store 消费) → IntegratedInput
       execute(ctx, integrated_input) → NodeResult
       collect delivers → submit(ctx) → ctx.dispatch → coordinator.route_deliver
       coordinator.complete_invocation(invocation_id, result_state)
         → status = COMPLETED (不可变)
         → 调 promote_delivers(C3: 升级消费状态)
         → 持久化最终状态
     except GraphBubbleUp:
       coordinator.cancel_invocation(invocation_id)
         → status = CANCELED
     except GraphInterrupt:
       coordinator.suspend_invocation(invocation_id, state_snapshot)
         → status 保持 RUNNING,持久化 state_snapshot
     except Exception:
       coordinator.crash_invocation(invocation_id)
         → status = CRASHED
     finally:
       coordinator.finalize_invocation(invocation_id)
         → 确保持久化状态一致(即使 crash 也有 CRASHED 记录)
   
  3. return NodeResult (for compatibility)
```

### 3.3 Node.run() 统一流程详解

框架的 `Node.run()` 是 node 生命周期的唯一入口。它统一处理状态转移、持久化、版本链、deliver/submit、undelivered retry、异常分类。node 子类只实现 `execute(ctx, integrated_input) -> NodeResult`。

#### 3.3.1 统一模式(coordinator always present)

**coordinator 总是存在**(C1: 删除退化模式,rule 15)。无持久化需求时用 Null 策略。两种执行路径(GraphOrchestrator + ReActAgent)都用同一 Node.run() 代码路径,差异在 coordinator 策略(Memory/SQLite vs Null)+ 状态持有机制(coordinator vs AgentContext)。详见 §3.3.4 正交层说明。

- **GraphOrchestrator 路径**(Memory/SQLite coordinator): scheduler 传入 coordinator(via ctx),run() 走完整生命周期调度。每次调用有 invocation_id + version,持久化到 NodeState。crash 后可恢复。
- **ReActAgent 路径**(Null coordinator): run() 走同一生命周期调度,但 coordinator 是 no-op。状态由 AgentContext 持有(跨 turn)。

#### 3.3.2 统一完整时序(coordinator always present)

**设计补强(C1/C4)**: 删除 `coordinator=None` 退化模式(rule 15)。coordinator **总是存在**,挂在 `ctx.coordinator` 上。无持久化需求时用 Null 策略(NullNodeState / NullGraphMetadataStore / NullDeliverStore)— 这是正当策略实现(原则 5),不是 backward-compat shim。

**输入模型收敛(C4)**: `upstream_payloads` 参数**移除**。integrate **总是**从 deliver_store 读取(通过 coordinator.collect_consumable_delivers)。scheduler 的 dispatch handler 调 `coordinator.route_deliver()` 生产 delivers 到 target node 的 deliver_store,node 的 integrate 从 deliver_store 消费。单一输入模型,单一代码路径。

**消费逻辑归属(I10)**: 移除 DeliverConsumer ABC(rule 6: 只有一个实现是 hypothetical seam)。消费逻辑(collect/mark/promote)作为 coordinator 方法。DeliverStore ABC 保留(三实现 — 真实 seam)。

```
Node.run(ctx, *, graph):
  coordinator = ctx.coordinator   ← always present(Null/Memory/SQLite 策略)
  │
  ┌─ 1. begin_invocation (with self-cleanup on failure, I17)
  │    invocation = coordinator.begin_invocation(node_name)   ← F8: parent_version 内部计算
  │      → parent_version = load_latest_completed version(内部)
  │      → version = max(所有已有版本号) + 1          ← I18: 不是 load_latest_completed+1
  │      → 如果存在 suspended=True 的 RUNNING invocation: 标记为 SUPERSEDED(I4, F4: 用 suspended 字段判断)
  │      → 如果存在 orphan PENDING/RUNNING(suspended=False): 标记为 CRASHED(安全网)
  │      → invocation_id = snowflake_id_generator.generate()
  │      → status = PENDING, save_invocation(PENDING)
  │      → status = RUNNING, save_invocation(RUNNING)
  │      → return InvocationContext(invocation_id, node_name, version, parent_version)
  │    ── begin 内部 try/except: 失败时自清理 PENDING 记录(I17)──
  │
  ├─ 2. integrate (framework, always from deliver_store)
  │    ── 检查是否为 resume from suspend(I16)──
  │    prev = coordinator.load_latest_invocation(node_name)
  │    if prev and prev.status == SUPERSEDED and prev.state_json:
  │      ── resume from suspend: 用 state_snapshot 作为 integrated input,跳过 re-consume ──
  │      integrated = input_integrator.integrate_from_snapshot(prev.state_json)
  │      ── prev 的 CONSUMED_PENDING delivers 保持,等当前 invocation complete 时 promote ──
  │    else:
  │      ── 正常消费: 从 deliver_store 查询可消费的 delivers ──
  │      consumable = coordinator.collect_consumable_delivers(
  │          node_name=self.name, invocation_id=invocation.invocation_id
  │        )                                          ← I10: coordinator 方法,非 deliver_consumer
  │      ── 返回 PENDING + CONSUMED_PENDING(上次未完成的)的 delivers ──
  │      ── CONSUMED_COMPLETED 的不返回(已处理完成) ──
  │      ── 标记为已消费(当前 invocation) ──
  │      coordinator.mark_delivers_consumed(
  │          node_name=self.name,
  │          deliver_ids=[d.deliver_id for d in consumable],
  │          invocation_id=invocation.invocation_id,
  │        )                                          ← I10: coordinator 方法
  │      ── 标记为 CONSUMED_PENDING(invocation 完成后升级) ──
  │      ── 整合为 IntegratedInput ──
  │      integrated = input_integrator.integrate(
  │          [IntegratedPayload(source=d.source_node, content=d.content) for d in consumable]
  │        )
  │
  ├─ 3. try: execute + submit (with undelivered retry)
  │    retry_count = 0
  │    while True:
  │      reset _pending_delivers
  │      raw_result = self.execute(ctx, integrated)    ← node 自定义逻辑
  │      result = await if awaitable
  │      delivers = self._collect_delivers(ctx)
  │      if delivers: break
  │      if retry_count >= max_retry:
  │        raise RoutingError("no delivers after max_retry")
  │      retry_count += 1
  │      integrated = integrate([error_feedback] + integrated_payloads)  ← 重试带错误反馈
  │    ── submit (framework, after delivers collected) ──
  │    self.submit(ctx)  →  _submit(ctx)  →  ctx.dispatch(target, {"delivered": payload})
  │      → scheduler dispatch handler 调 coordinator.route_deliver 生产到下游 deliver_store
  │    ── I7: 不再有 "mark as SUBMITTED" — 消费状态机用 CONSUMED,不用 SUBMITTED ──
  │
  │    coordinator.complete_invocation(invocation, state_update=result.state_update)
  │      → status = COMPLETED (不可变)
  │      → save_invocation(COMPLETED, state_json=result.state_update or {})
  │      → C3/F3: 调 coordinator.promote_delivers(node_name, invocation_id)
  │        → F3: promote 该 node 的所有 CONSUMED_PENDING delivers(不限当前 invocation)
  │        → InMemory: 删除已消费记录; SQLite: CONSUMED_PENDING → CONSUMED_COMPLETED
  │      → F2: save COMPLETED + promote_delivers 在 SQLite 中包事务(原子,防 crash-between)
  │      → return
  │
  ├─ 4. except GraphBubbleUp:
  │    coordinator.cancel_invocation(invocation)
  │      → status = CANCELED
  │      → save_invocation(CANCELED, state_json={})
  │      → re-raise (传播到 scheduler,不吞)
  │
  ├─ 5. except GraphInterrupt:
  │    ── GraphInterrupt 不走 crash/cancel 路径 ──
  │    ── 它是 HITL suspend,不是 crash ──
  │    state_snapshot = ctx.state.checkpoint()  ← 提取当前 state(含 imperative mutations)
  │    │    ── ⚠️ 必须直接调 ctx.state.checkpoint(),不能用 state_schema().fields 迭代 ──
  │    │    ── checkpoint() 迭代 _channels(含继承字段如 resume_target)──
  │    │    ── state_schema() 故意跳过继承字段(state_factory.py:344),会导致 resume_target 丢失 ──
  │    coordinator.suspend_invocation(invocation, state_snapshot)
  │      → status 保持 RUNNING(未完成,未取消)
  │      → suspended = True(F4: 显式标记,区别于 orphan/crash RUNNING)
  │      → save_invocation(RUNNING, state_json=state_snapshot, suspended=True)
  │      → re-raise (传播到 scheduler/engine,由上层处理 PAUSED)
  │
  ├─ 6. except Exception:
  │    coordinator.crash_invocation(invocation)
  │      → status = CRASHED
  │      → save_invocation(CRASHED, state_json={})
  │      → re-raise (传播到 scheduler)
  │
  └─ 7. finally:
       coordinator.finalize_invocation(invocation)
         → 确保持久化状态一致
          → 如果 status 仍为 PENDING 且 suspended=False(crash 在 begin/execute 之间):
            标记为 CRASHED(安全网)
          → suspended=True 的 RUNNING 不动(F4: 用 suspended 字段判断,不标记为 CRASHED)
          → SUPERSEDED 的不动(已被新 invocation 取代)
         → 更新 graph metadata(instance_seq, iteration_count)
```

#### 3.3.3 关键设计决策

**GraphInterrupt 不走 crash/cancel 路径**: `GraphInterrupt` 是 HITL 暂停(approval 等),不是 crash 也不是 cancel。它走 `suspend_invocation` — status 保持 RUNNING(表示"未完成,待恢复")。`Node.run()` 从 `ctx.state.checkpoint()` 提取 state snapshot(含 execute 期间的 imperative mutations 如 `resume_target`)传给 coordinator 持久化。恢复时 coordinator 把 suspended 的 state snapshot apply 到 main_state,使重新 dispatch 的 node 能看到之前设的 imperative state。

**⚠️ state snapshot 提取陷阱(缺口C)**: 必须直接调 `ctx.state.checkpoint()`,**不能用** `state_schema().fields` 迭代构建 snapshot。原因:`GraphState._setup_channels`(`state.py:112-140`)从 `type(self).model_fields.items()` 填充 `_channels`(包含继承字段如 `resume_target`),`checkpoint()` 迭代 `_channels` → 捕获 `resume_target`。但 `SimpleStateFactory.state_schema()`(`state_factory.py:344,364-370`)**故意跳过**继承自 GraphState 的字段(如 `resume_target`)— 这是 schema 描述(用于 DynamicStateFactory),不影响 checkpoint 路径。如果用 schema 迭代构建 snapshot,`resume_target` 会丢失 → resume 后 StartNode 路由错误。

**SUPERSEDED 状态(I4)**: 恢复时新建 invocation(v5),`begin_invocation` 先标记 v4(suspended RUNNING)为 **SUPERSEDED**。维持"每次最多一个非 COMPLETED 行"不变式。SUPERSEDED 是终态(不可变),recovery 时跳过(像 COMPLETED),但 state_snapshot 仍 apply(§5 step 2)。v5 的 parent_version 指向最后 COMPLETED 版本(不是 v4)。

**resume 时跳过 re-consume(I16)**: resume 时 integrate 检查前一 invocation(v4,SUPERSEDED):
- **有 state_snapshot**(suspended → SUPERSEDED):用 state_snapshot 作为 integrated input,**跳过** query_consumable + mark_consumed。v4 的 CONSUMED_PENDING delivers 保持,等 v5 complete 时 promote_consumed。
- **无 state_snapshot**(crash before integrate):正常路径,query_consumable 返回 CONSUMED_PENDING → re-consume → mark_consumed(新 invocation_id)。

这避免 double-effect:deliver 已消费(state_snapshot 含 integrated input)+ state_snapshot 也 apply → 重复。跳过 re-consume,state_snapshot 是唯一 integrated input 源。

**finally 安全网排除 suspend**: finally 检查 status 时,suspended 的 RUNNING 不被标记为 CRASHED。只有 PENDING(从未进入 RUNNING)才标记为 CRASHED。SUPERSEDED 也不动(已被新 invocation 取代)。coordinator 通过 invocation 的 suspended 标记区分。

**undelivered retry 在 coordinator 生命周期内**: retry 循环在 `try` 块内,是 execute 的一部分。retry 不创建新 invocation — 同一个 invocation 内多次 execute 尝试。只有 retry 耗尽 raise RoutingError 时才走 `except Exception → crash_invocation`。

**deliver/submit 在 complete 之前**: execute 返回后、complete 之前,框架做 collect delivers + submit。submit → ctx.dispatch → scheduler dispatch handler → coordinator.route_deliver 生产到下游 deliver_store。complete 时调 promote_consumed(C3),确保 COMPLETED 记录反映"deliver 已 submit + 消费已升级"的最终状态。

**complete 的 state_json = NodeResult.state_update**: complete 时把 `result.state_update` 存入 `state_json`。恢复时 coordinator 按 `invocation_id`(全局时间序,I5)遍历各 node 的 COMPLETED 记录,把 `state_update` apply 到 fresh state,重建 `main_state`。

**begin_invocation 自清理(I17)**: `begin_invocation` 内部做 try/except,失败时自清理已创建的 PENDING 记录,不留 orphan。避免 begin 在 try 外导致 finalize 无 invocation 变量的问题。

#### 3.3.4 两种执行路径(正交层,非分歧)

**设计补强(C1/缺口A)**: 删除 `coordinator=None` 退化模式(rule 15 违规)。coordinator **总是存在**。两种执行路径不是"同一关注点的分歧",而是**两个正交层**:

| 层 | 关注 | 机制 | 路径 |
|----|------|------|------|
| **Node invocation 持久化**(coordinator) | per-node 版本链 + state_json + deliver 消费 | coordinator + NodeState + DeliverStore | GraphOrchestrator(长生命周期图,跨 _execute) |
| **Agent turn 状态持久化**(AgentContext) | ReActTurnState + resume_target + tool 批次 | AgentContext + AgentPool | ReActAgent(per-turn,跨 turn) |

**收敛点在机制层**: 两条路径都 always pass coordinator(Null for ReActAgent,Memory/SQLite for GraphOrchestrator),都用同一 `Node.run()` 代码路径(begin → integrate from deliver_store → execute → complete)。分歧在"谁持有跨 suspend/resume 的状态"(coordinator vs AgentContext),这是合法的因为它们是不同关注点。

**GraphOrchestrator 路径(Memory/SQLite coordinator)**:
- 长生命周期 GraphInstance,多个 _execute 调用(crash recovery, resume from pause)
- coordinator 持有状态,跨 _execute 存活(C2: coordinator 生命周期 = GraphInstance 生命周期)
- GraphInterrupt suspend: coordinator.suspend_invocation 持久化 state snapshot
- Resume: 同一 GraphInstance + 同一 coordinator → load_for_recovery → state snapshot 可用

**ReActAgent 路径(Null coordinator)**:
- per-turn GraphEngine 构造(agent.py:272-287),无 GraphInstance
- AgentContext(含 ReActTurnState)由 AgentPool 持有,跨 turn 存活(**不 GC**)
- GraphInterrupt suspend: coordinator.suspend_invocation 是 no-op(Null)— **AgentContext 是状态载体**
- Resume: NEW GraphEngine,但 AgentContext 复用 → StartNode 读 `state.resume_target`(state.py:105, GraphState 基类字段)→ 路由到 TOOL → _resume_suspended_batch
- **完全独立于 coordinator** — AgentContext 的状态管理是 active 机制,Null coordinator 是 structural pass-through

**Null coordinator 行为**:
- `begin_invocation()`: 创建 InvocationContext(in-memory,提供 invocation_id + version 原语)
- `complete_invocation()`: no-op(无持久化)
- `suspend_invocation()`: no-op(AgentContext 持有状态)
- `collect_consumable_delivers()`: NullDeliverStore 返回 in-memory queue(功能等价原 upstream_payloads 参数,已移除)
- `route_deliver()`: accumulate 到 in-memory deliver_store

**rule 15 合规论证**: rule 15 收敛**同一关注点**的分歧路径。这里 coordinator(node invocation 持久化)和 AgentContext(agent turn 状态持久化)是不同关注点,不同语义,不同机制。收敛在 Node.run() 代码路径(always coordinator),不是在状态持有机制。这不是分歧,是正交分层。

**向后兼容性**: 现有测试、现有 scheduler 调用**需要更新** — 不再有 `coordinator=None` 兼容。所有 Node.run() 调用必须通过 `ctx.coordinator` 提供 coordinator(Null/Memory/SQLite)。这是 rule 15 的要求:不保留 backward-compat shim。

#### 3.3.5 node 子类的负担

node 子类只需实现:
- `execute(ctx, integrated_input) -> NodeResult` — 业务逻辑,调 `self.deliver()` 累积投递
- 可选: 自定义 `NodeState`(如果需要特殊持久化策略)
- 可选: 幂等设计(execute 被重新调用时如何处理 — 框架提供 invocation_id + version 原语)

框架负责:
- invocation 创建 + 版本链 + 持久化
- 状态机转换(PENDING → RUNNING → COMPLETED/CANCELED/CRASHED)
- integrate(input)
- undelivered retry
- collect delivers + submit(submit → ctx.dispatch → coordinator.route_deliver 生产到下游 deliver_store)
- finally 安全网
- GraphBubbleUp / GraphInterrupt / Exception 分类处理

### 3.4 版本链规则

- 每次 `begin_invocation` 创建新版本,`parent_version` 指向上一个 COMPLETED
- COMPLETED 记录不可变
- **version = max(所有已有版本号) + 1**(I18)— 不是 `load_latest_completed + 1`,避免 CRASHED 版本号相同导致 UNIQUE 冲突
- **node 每次最多一个非 COMPLETED 数据行** — 新 invocation 开始前(`begin_invocation` 内部):
  - 如果有 suspended=True 的 RUNNING 行:标记为 **SUPERSEDED**(I4, F4: 用 suspended 字段判断)
  - 如果有 orphan PENDING/RUNNING(suspended=False)行:标记为 CRASHED(安全网)
  - CANCELED/CRASHED/SUPERSEDED 都是终态,不算"非 COMPLETED 活跃行"
- 恢复时:查各 node 的最新版本 → 如果是 COMPLETED/SUPERSEDED,跳过;如果是 CRASHED 或 orphan PENDING/RUNNING(suspended=False),重新 dispatch(新建 invocation,parent_version 指向最后一个 COMPLETED);如果是 suspended=True 的 RUNNING,resume(新建 invocation,v4 标记 SUPERSEDED,v5 用 state_snapshot)

### 3.5 状态机(I22 拆分 + I4 SUPERSEDED)

**设计补强(I22)**: 当前 `NodeInstanceStatus` 混两个维度 — scheduler 调度状态(DORMANT/READY)+ invocation 状态(PENDING/RUNNING/COMPLETED)。拆分为两个独立 enum(rule 1: 精确类型,关注点分离)。

**SchedulerInstanceStatus**(scheduler 调度状态 — 实例是否 ready 待执行):

```
DORMANT → READY → RUNNING → COMPLETED
```

- **DORMANT**: 实例已创建,未 ready
- **READY**: 在 ready queue 中,等待执行
- **RUNNING**: 当前正在执行
- **COMPLETED**: scheduler 完成调度(实例不再活跃)

**InvocationStatus**(invocation 版本链状态 — 持久化到 node_states 表):

```
PENDING → RUNNING → COMPLETED (终态,不可变)
                 ↘ CANCELED (GraphBubbleUp 取消,终态)
                 ↘ CRASHED (异常,可恢复,终态)
RUNNING → SUPERSEDED (suspended 后被新 invocation 取代,终态)
RUNNING (GraphInterrupt suspend — 保持 RUNNING,待恢复)
```

- **PENDING**: invocation 已创建,未开始执行
- **RUNNING**: execute 正在执行 / GraphInterrupt suspend 后待恢复
- **COMPLETED**: execute 正常完成,deliver 已 submit,消费已 promote,状态不可变
- **CANCELED**: 被 GraphBubbleUp 取消中断(如用户 stop),终态
- **CRASHED**: execute 抛异常(可被故障恢复重新 dispatch),终态
- **SUPERSEDED**(I4 新增): suspended(RUNNING)后被新 invocation 取代,终态。state_snapshot 仍 apply(§5),但不算活跃行。语义:"被延续取代,非中止"

**状态机设计原则**: 保持简洁(6 个状态)+ 保留扩展空间(未来可加 GARBAGED 清理状态等)。不过度设计。

**使用位置**:
- `NodeInstance`(scheduler 内部): `status: SchedulerInstanceStatus` — 追踪 `_ready` set 成员
- `NodeInvocationRecord`(持久化): `status: InvocationStatus` — 追踪版本链状态
- `node_states` 表 `status` 列: 存 InvocationStatus 值
- coordinator 的 `begin/complete/cancel/suspend/crash_invocation`: 用 InvocationStatus

## 4. 持久化接口

### 4.1 NodeState ABC(演进现有)

```python
class NodeState(ABC):
    """Per-node persistence interface. Node owns its state persistence.
    
    框架的 coordinator 在生命周期事件点调用这些方法。node 自定义实现
    (SimpleNodeState / AgentNodeState / 无状态)决定具体存储策略。
    """

    @abstractmethod
    def save_invocation(
        self,
        graph_instance_id: int,
        node_name: str,
        invocation_id: int,
        version: int,
        parent_version: int | None,
        status: InvocationStatus,
        state: dict[str, Any],
    ) -> None:
        """保存一次 invocation 的状态(创建或更新)。"""
        ...

    @abstractmethod
    def load_invocation(
        self, graph_instance_id: int, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None:
        """加载指定 invocation。"""
        ...

    @abstractmethod
    def load_latest(
        self, graph_instance_id: int, node_name: str
    ) -> NodeInvocationRecord | None:
        """加载 node 最新版本的 invocation(不论状态)。"""
        ...

    @abstractmethod
    def query_versions(
        self,
        graph_instance_id: int,
        node_name: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]:
        """查询 node 的版本列表(按 version DESC)。默认不过滤(返回全部状态)。"""
        ...

    @abstractmethod
    def load_latest_completed(
        self, graph_instance_id: int, node_name: str
    ) -> NodeInvocationRecord | None:
        """加载 node 最后一个 COMPLETED invocation。用于恢复时判断节点是否已完成。"""
        ...
```

### 4.2 NodeInvocationRecord

```python
class NodeInvocationRecord(BaseModel):
    """一次 node 调用的持久化记录。Frozen value object (rules 10-16)。"""
    
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    invocation_id: int          # 全局唯一(Snowflake)
    graph_instance_id: int      # FK → graph_instances
    node_name: str
    version: int                # 该 node 的版本号(从 0 开始)
    parent_version: int | None  # 上一个 COMPLETED 版本(版本链)
    status: InvocationStatus  # PENDING/RUNNING/COMPLETED/CANCELED/CRASHED/SUPERSEDED
    state_json: dict[str, Any]  # node 自定义状态内容(NodeResult.state_update / suspend snapshot)
    suspended: bool = False     # F4: 显式标记 suspended RUNNING(区别于 orphan RUNNING/crash RUNNING)
    created_at: int             # epoch ms
    updated_at: int             # epoch ms
```

**F4 suspended 字段**: 显式区分 suspended RUNNING(GraphInterrupt suspend,待恢复)vs orphan RUNNING(crash,需 re-dispatch)。begin_invocation 检查 `suspended` 决定标记 SUPERSEDED 还是 CRASHED;finalize 跳过 `suspended=True` 的 RUNNING;recovery 识别 `suspended=True` 的 RUNNING 做 resume。不依赖 state_json 非空的隐式判断(边界情况:空 state_snapshot 会导致误判)。

### 4.3 GraphMetadata

```python
class GraphMetadata(BaseModel):
    """Graph instance 级元数据(scheduler bookkeeping + 状态机)。"""
    
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    graph_instance_id: int
    spec_id: int
    parent_instance_id: int | None
    parent_node: str | None
    status: GraphInstanceStatus  # running/paused/stopped/crashed/completed/failed
    instance_seq: int            # 全局 invocation 序号
    iteration_count: int         # 迭代计数(max_iterations 安全网)
    activated_sources: dict[str, list[str]]  # ON_ALL_PREDS bookkeeping
    pending_dispatches: dict[str, dict[str, list[dict[str, Any] | None]]]  # 待处理队列
```

### 4.4 GraphPersistenceCoordinator

**设计补强(C3/I8/I9/I10/I20)**: 移除 `get_deliver_consumer`(I10: DeliverConsumer ABC 移除,消费逻辑作为 coordinator 方法)。加消费方法(collect/mark/promote)。加 `rebuild_main_state`(I9)。`route_deliver` 检查 END target(I20)。

```python
class GraphPersistenceCoordinator:
    """统一调度 node 生命周期事件 + 持久化路由。
    
    scheduler 无感知 checkpoint_store。coordinator 持有 graph metadata
    store + 各 node 的 NodeState 引用,在生命周期事件点调用对应方法。
    """

    def __init__(
        self,
        graph_instance_id: int,
        graph_metadata_store: GraphMetadataStore,
        default_node_state_factory: NodeStateFactory,
        default_deliver_store_factory: DeliverStoreFactory,  # F11: required(非 Optional),NullDeliverStoreFactory 作为默认
    ) -> None:
        self._graph_instance_id = graph_instance_id
        self._metadata_store = graph_metadata_store
        self._default_node_state_factory = default_node_state_factory
        self._default_deliver_store_factory = default_deliver_store_factory
        self._node_states: dict[str, NodeState] = {}  # node_name → NodeState
        self._deliver_stores: dict[str, DeliverStore] = {}  # node_name → DeliverStore

    def register_node(
        self, node_name: str, node_state: NodeState | None = None,
        deliver_store: DeliverStore | None = None,
    ) -> None:
        """注册 node 的持久化策略。None = 用默认策略。"""
        state = node_state if node_state is not None else self._default_node_state_factory.create()
        self._node_states[node_name] = state
        ds = deliver_store if deliver_store is not None else self._default_deliver_store_factory.create()
        self._deliver_stores[node_name] = ds

    def get_deliver_store(self, node_name: str) -> DeliverStore | None:
        """获取 node 的 deliver_store(用于外部 deliver 路由查询)。"""
        return self._deliver_stores.get(node_name)

    def route_deliver(
        self, target_node: str, content: Any, source_node: str, source_invocation_id: int
    ) -> int | None:
        """路由 deliver 到 target node 的 deliver_store。返回 deliver_id。
        
        I20: target == GraphNode.END 时跳过(END 无 deliver_store),返回 None。
        """
        if target_node == GraphNode.END:   # I20: END 无 deliver_store
            return None
        store = self._deliver_stores.get(target_node)
        if store is None:
            raise RoutingError(f"Node {target_node!r} has no deliver_store registered.")
        return store.accumulate(
            graph_instance_id=self._graph_instance_id,
            target_node=target_node,
            source_node=source_node,
            source_invocation_id=source_invocation_id,
            content=content,
        )

    # ── 消费逻辑(I10: 从 DeliverConsumer ABC 移入 coordinator)──

    def collect_consumable_delivers(
        self, node_name: str, invocation_id: int
    ) -> list[DeliverRecord]:
        """收集本次 invocation 需要消费的 delivers。委托 deliver_store.query_consumable。"""
        store = self._deliver_stores.get(node_name)
        if store is None:
            return []
        return store.query_consumable(self._graph_instance_id, node_name)

    def mark_delivers_consumed(
        self, node_name: str, deliver_ids: list[int], invocation_id: int
    ) -> None:
        """标记 delivers 为已消费。委托 deliver_store.mark_consumed。"""
        store = self._deliver_stores.get(node_name)
        if store is not None:
            store.mark_consumed(deliver_ids, invocation_id)

    def promote_delivers(self, node_name: str, invocation_id: int) -> None:
        """invocation COMPLETED 时升级消费状态。委托 deliver_store.promote_consumed。
        
        C3: complete_invocation 内部调此方法,确保 CONSUMED_PENDING → CONSUMED_COMPLETED。
        F3: 升级该 node 的所有 CONSUMED_PENDING delivers(不限 invocation_id 匹配的)——
            修复 I16 resume 时 v4 的 delivers(consumed_by=v4)不被 v5 complete promote 的问题。
            实现:promote_consumed 改为按 node_name 升级所有 CONSUMED_PENDING(不按 invocation_id 过滤)。
        """
        store = self._deliver_stores.get(node_name)
        if store is not None:
            store.promote_consumed(invocation_id)  # F3: 实现时改为 promote 所有 CONSUMED_PENDING for this node

    # ── 生命周期 ──

    def begin_invocation(self, node_name: str) -> InvocationContext:
        """创建新 invocation,PENDING → RUNNING,持久化。
        
        F8: parent_version 内部计算(从 load_latest_completed),不作为参数。
        I17: 内部 try/except,失败时自清理 PENDING 记录。
        I18: version = max(所有已有版本号) + 1。
        I4/F4: 如果存在 suspended=True 的 RUNNING invocation,先标记为 SUPERSEDED。
        F1: SUPERSEDED 标记 + 新 invocation 创建在 SQLite 中包事务(原子)。
        """
        ...

    def complete_invocation(self, invocation: InvocationContext, state: dict[str, Any]) -> None:
        """RUNNING → COMPLETED,持久化(不可变)。
        
        C3: 内部调 promote_delivers(node_name, invocation_id) 升级消费状态。
        F2: save COMPLETED + promote_delivers 在 SQLite 中包事务(原子)。
        F3: promote_delivers 升级该 node 的所有 CONSUMED_PENDING delivers(不限当前 invocation 的)——
            修复 I16 resume 时 v4 的 delivers(consumed_by=v4)不被 v5 promote 的问题。
        """
        ...

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        """RUNNING → CANCELED,持久化。re-raise GraphBubbleUp。"""
        ...

    def suspend_invocation(self, invocation: InvocationContext, state_snapshot: dict[str, Any]) -> None:
        """GraphInterrupt — status 保持 RUNNING(未完成),持久化 state snapshot。re-raise。
        
        state_snapshot 由 Node.run() 从 ctx.state.checkpoint() 提取(⚠️ 缺口C: 必须直接调
        checkpoint(),不能用 state_schema().fields 迭代 — 否则 resume_target 丢失)。
        """
        ...

    def crash_invocation(self, invocation: InvocationContext) -> None:
        """RUNNING → CRASHED,持久化。re-raise。"""
        ...

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        """安全网: 确保持久化状态一致。orphan PENDING → CRASHED;suspended RUNNING 不动;SUPERSEDED 不动。"""
        ...

    def load_latest_invocation(self, node_name: str) -> NodeInvocationRecord | None:
        """加载 node 最新 invocation(用于 resume 判断 I16)。"""
        ...

    # ── 状态查询与恢复 ──

    def get_graph_state(
        self, node_status_filter: set[InvocationStatus] | None = None
    ) -> GraphStateSnapshot:
        """收集 graph metadata + 各 node 版本列表。"""
        ...

    def load_for_recovery(self) -> RecoveryContext:
        """恢复时:加载 graph metadata + 各 node 最新状态 + 重建 main_state(I9)。
        
        I9: 返回 RecoveryContext 包含 rebuilt_main_state,scheduler 直接使用。
        I5: 重建按 invocation_id(全局时间序)排序,不是 per-node version。
        """
        ...
```

### 4.5 三种实现

| 实现 | GraphMetadataStore | NodeState | 适用场景 |
|------|-------------------|-----------|----------|
| **Null** | NullGraphMetadataStore (no-op) | NullNodeState (no-op) | LinearScheduler 默认;不需要持久化的图 |
| **Memory** | MemoryGraphMetadataStore (dict) | SimpleNodeState (内存 dict) | 测试;单进程临时图 |
| **SQLite** | SqliteGraphMetadataStore | SqliteNodeState | 生产;需要 crash recovery 的图 |

统一行为:三种实现走同一 ABC 接口。memory/noop 简化但不缺接口。

### 4.6 GraphMetadataStore ABC

```python
class GraphMetadataStore(ABC):
    """Graph instance 级元数据持久化。"""

    @abstractmethod
    def save(self, graph_instance_id: int, metadata: GraphMetadata) -> None:
        """保存/更新 graph metadata(整体覆盖)。"""
        ...

    @abstractmethod
    def load(self, graph_instance_id: int) -> GraphMetadata | None:
        """加载 graph metadata。"""
        ...

    @abstractmethod
    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None:
        """更新 graph 状态(高频操作,单独优化)。"""
        ...
```

### 4.7 NodeStateFactory ABC

```python
class NodeStateFactory(ABC):
    """创建默认 NodeState 实例。GraphInstance 用它为未自定义持久化的 node 提供默认策略。"""

    @abstractmethod
    def create(self) -> NodeState:
        """创建一个新的 NodeState 实例(绑定到特定 graph_instance_id 由 coordinator 负责)。"""
        ...
```

三种实现: `NullNodeStateFactory` / `SimpleNodeStateFactory`(memory) / `SqliteNodeStateFactory`。

### 4.8 InvocationContext

```python
class InvocationContext(BaseModel):
    """begin_invocation 的返回值,携带当前 invocation 的上下文。Frozen value object。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: int          # 全局唯一(Snowflake)
    node_name: str
    version: int                # 该 node 的版本号
    parent_version: int | None  # 上一个 COMPLETED 版本
```

### 4.9 RecoveryContext

**设计补强(I9)**: 加 `rebuilt_main_state` 字段。`load_for_recovery` 内部重建 main_state,scheduler 直接使用,无需额外调 rebuild 方法。

```python
class RecoveryContext(BaseModel):
    """load_for_recovery 的返回值,scheduler 用它重建调度状态。Frozen value object。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: GraphMetadata
    node_states: dict[str, NodeInvocationRecord | None]  # node_name → 最新 invocation(无则 None)
    rebuilt_main_state: dict[str, Any]  # I9: 重建后的 main_state(按 invocation_id 全局排序 apply)
```

### 4.10 scheduler 集成点

scheduler 在以下事件点调用 coordinator(scheduler 不直接调 NodeState):

**coordinator 引用来源**: coordinator 挂在 `GraphContext` 上(`ctx.coordinator`)。scheduler 在 `run_async` 顶部从 ctx 获取,传给 `node.run()`(通过 ctx,不是参数)。GraphContext 由 GraphInstance/GraphOrchestrator 构造时注入 coordinator。**coordinator 总是存在**(C1: 无 None fallback,Null 策略用于无持久化)。

| 事件 | 位置 | 调用 | 作用 |
|------|------|------|------|
| run_async 启动 | `run_async` 顶部(替换 `_init_fresh_state` / `_restore_from_checkpoint`) | `ctx.coordinator.load_for_recovery()` | 恢复或初始化(返回含 rebuilt_main_state 的 RecoveryContext) |
| 节点执行前 | `_execute_instance` / `run_async` 循环内,`node.run()` 调用前 | 传 `coordinator=ctx.coordinator`(通过 ctx)给 `node.run()` | node.run() 内部用 coordinator 调度生命周期 |
| 节点执行后 | `_execute_instance` / `run_async` 循环内,`node.run()` 返回后 | `ctx.coordinator` 更新 metadata(instance_seq, iteration_count, activated_sources, pending_dispatches) | scheduler bookkeeping 持久化 |
| dispatch 时 | scheduler dispatch handler | `ctx.coordinator.route_deliver(target, content, source_node=ctx.current_invocation.node_name, source_invocation_id=ctx.current_invocation.invocation_id)` | F6: source_node/source_invocation_id 从 ctx.current_invocation 获取(scheduler 在 execute 前设置);生产 delivers 到 target node 的 deliver_store(C4: 收敛输入模型)。调度层 ready 判断(pending_dispatches / _ready set)沿用现有 scheduler 机制,本设计不改变 |

**scheduler 不感知 NodeState / GraphMetadataStore / DeliverStore** — 只调 coordinator 的方法。coordinator 内部路由到各 store。

**scheduler ready 判断**: 沿用现有 ParallelScheduler(_ready set + dispatch events + _completed_instances 动态机制)和 LinearScheduler(顺序执行)的实现。本设计只改变 dispatch handler 的数据层(从 upstream_payloads 参数改为 coordinator.route_deliver),不改变调度层的 ready 判断逻辑。自环节点(A→A)作为有效场景在实现时验证(见 §10 待办)。

**register_node 时机(I3/I19)**: 在 **GraphInstance 构造时**(编译后,_execute 前)。orchestrator 遍历 `compiled.nodes` 调 `coordinator.register_node`。GraphSpecCompiler 不改(不持有 coordinator)。动态 node(子图)在子 GraphInstance 构造时注册。

**fork() 传播 coordinator(缺口B)**: GraphContext.fork() 加 `coordinator` 参数,默认继承父 context 的 coordinator(shared)。子图(GraphAsNode)创建自己的 GraphInstance + 自己的 coordinator,子 GraphContext 用子 coordinator(不是父的)。

```python
def fork(self, *, state=None, runtime=None, ..., coordinator=None) -> GraphContext[S]:
    return GraphContext(
        ...,
        coordinator=coordinator if coordinator is not None else self.coordinator,
    )
```

### 4.11 node_states 表 schema 演进

当前 schema(node_state_store.py):
```sql
node_states:
  node_state_id     INTEGER PRIMARY KEY
  graph_instance_id INTEGER
  node_name         TEXT
  version           INTEGER
  state_json        TEXT
  created_at        INTEGER
  updated_at        INTEGER
```

演进为:
```sql
node_states:
  node_state_id     INTEGER PRIMARY KEY          -- = invocation_id (Snowflake)
  graph_instance_id INTEGER NOT NULL
  node_name         TEXT NOT NULL
  version           INTEGER NOT NULL             -- 该 node 的版本号(从 0 开始)
  parent_version    INTEGER                      -- 上一个 COMPLETED 版本(NULL = 首次)
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','completed','canceled','crashed','superseded'))
                                                                  -- I4: 加 'superseded'
  state_json        TEXT NOT NULL DEFAULT '{}'   -- node 自定义状态(NodeResult.state_update / suspend snapshot)
  created_at        INTEGER NOT NULL
  updated_at        INTEGER NOT NULL
  UNIQUE(graph_instance_id, node_name, version)  -- 版本唯一
```

新增字段: `parent_version`, `status`。`state_json` 语义不变(node 自定义内容)。

**Schema 迁移(I13)**: `CREATE TABLE IF NOT EXISTS` 对已存在表是 no-op,不会加新列。需加 `ALTER TABLE` 迁移逻辑:
```sql
-- 迁移脚本(幂等,检查列是否存在再 ADD)
ALTER TABLE node_states ADD COLUMN parent_version INTEGER;
ALTER TABLE node_states ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
-- 加 CHECK 约束(SQLite 不支持直接 ADD CONSTRAINT,需重建表或用应用层校验)
```
或明确要求 fresh DB(开发阶段可接受,生产需迁移脚本)。实现时用 SQLite PRAGMA `table_info` 检查列是否存在,缺则 ALTER。

索引:
```sql
CREATE INDEX IF NOT EXISTS idx_node_states_latest ON node_states (graph_instance_id, node_name, version DESC);
CREATE INDEX IF NOT EXISTS idx_node_states_status ON node_states (graph_instance_id, node_name, status);
CREATE INDEX IF NOT EXISTS idx_node_states_cross  ON node_states (graph_instance_id, status);
CREATE INDEX IF NOT EXISTS idx_node_states_global  ON node_states (graph_instance_id, invocation_id);  -- I5: 全局排序
```

## 5. 恢复流程(新)

**设计补强(I5/I6/I16/I21)**: 全局排序用 invocation_id(Snowflake 时间序);CANCELED 不 re-dispatch;resume 用 state_snapshot 跳过 re-consume;区分故障恢复 vs 手动恢复。

```
1. GraphInstance.load_for_recovery()
   → GraphPersistenceCoordinator.load_for_recovery()
   → 返回 RecoveryContext(metadata + node_states + rebuilt_main_state)
    
2. 加载 graph metadata
   → instance_seq, iteration_count, activated_sources, pending_dispatches
    
3. 各 node 的 NodeState.load_latest()
   → 该 node 最新 invocation 状态
    
4. I21: 区分故障恢复 vs 手动恢复
   ── 故障恢复(recover_crashed): 只捡 status=crashed 的 graph instance ──
   ── 手动恢复(resume): 捡 paused/stopped 的 graph instance ──
   ── 两者共享 _recover_instances 流程,但入口过滤不同 ──
    
5. scheduler 根据 (2) 的 pending/activated + (3) 的 node 状态重建调度状态:
   ── I6/F4: 按最新 invocation 状态决定 ──
   - COMPLETED → 跳过(不重新 dispatch)
   - CRASHED + orphan PENDING/RUNNING(suspended=False) → 重新 dispatch(新建 invocation,parent_version 指向最后 COMPLETED)
   - CANCELED → 跳过(I6: deliberate cancel,不自动 re-dispatch,需显式 resume)
   - SUPERSEDED → 检查是否有后继 invocation:
     ── F1: 有后继 → 跳过(已被新 invocation 取代,终态)──
     ── F1: 无后继(crash 在标记 SUPERSEDED 和创建 v5 之间)→ 重新 dispatch(同 CRASHED 处理)──
   - suspended=True 的 RUNNING → resume: 新建 invocation,v4 标记 SUPERSEDED,v5 用 state_snapshot 跳过 re-consume(I16)
    
6. F2: 自动 promote 遗漏的 CONSUMED_PENDING delivers
   ── 扫描所有 consuming invocation 为 COMPLETED 但 delivers 仍 CONSUMED_PENDING 的记录 ──
   ── 自动 promote_consumed(这些 delivers 的 invocation_id)──
   ── 修复 crash 在 save COMPLETED 和 promote_delivers 之间的状态不一致 ──
    
7. F5: _recheck_pending 推导未完成节点
   ── 沿用现有 scheduler 动态机制(pending_dispatches + _ready set)──
   ── F5 补充: 查询 deliver_store 的 PENDING delivers 给 COMPLETED nodes → 如有 pending deliver 则 re-dispatch ──
   ── 修复 crash 在 route_deliver(dispatch 时)和 pending_dispatches 更新(node-end)之间的不同步 ──
   ── 正常调度循环 ──
```

**main_state 重建(I5 全局排序)**: 恢复时分两步重建 `main_state`:

1. **COMPLETED 记录**: 按 **`invocation_id`(全局时间序)** 遍历各 node 的 COMPLETED invocation(I5: 不是 per-node version,是全局 invocation_id 排序 — Snowflake 时间序,无碰撞,因果序保持)。把 `state_json`(= `NodeResult.state_update`)逐个 apply 到 fresh state。
   ```sql
   -- I5: 全局排序查询
   SELECT * FROM node_states 
   WHERE graph_instance_id = ? AND status = 'completed'
   ORDER BY invocation_id ASC;
   ```
   - 并行分支(同 timestamp)的 state_updates 独立(无因果依赖),顺序不重要。

2. **suspended SUPERSEDED 记录**: 如果某 node 有 SUPERSEDED invocation(原 suspended RUNNING,被新 invocation 取代),其 `state_json` 包含 suspend 时的 state snapshot(含 imperative mutations 如 `resume_target`)。coordinator 把这个 snapshot **最后** apply 到 main_state(它是最新的状态,suspend 发生在所有 COMPLETED 之后,invocation_id 自然最大)。

这样恢复后的 main_state = 所有 COMPLETED 的 state_update(按 invocation_id 全局序)+ suspended 的 state snapshot(最后 apply)。重新 dispatch suspended node 时,它能看到之前设的 `resume_target` 等 imperative state。

**resume 时跳过 re-consume(I16)**: 新 invocation(v5)的 integrate 检查 v4(SUPERSEDED):
- v4 有 state_snapshot → 用 snapshot 作为 integrated input,跳过 query_consumable + mark_consumed。v4 的 CONSUMED_PENDING delivers 保持,等 v5 complete 时 promote_consumed。
- v4 无 state_snapshot(crash before integrate)→ 正常路径,query_consumable 返回 CONSUMED_PENDING → re-consume。

**移除**: `_restore_from_checkpoint` (ParallelScheduler) — 替换为 coordinator 的 `load_for_recovery`。

**移除**: `_build_checkpoint_data` / `_schedule_checkpoint` (ParallelScheduler) — 替换为 coordinator 在生命周期事件点的增量持久化。

## 6. Node.run() 签名变化

**设计补强(C1/C4)**: 删除 `coordinator` 参数(在 `ctx.coordinator` 上,always present)。删除 `upstream_payloads` 参数(收敛到 deliver_store,通过 coordinator.collect_consumable_delivers)。删除 `enforce_deliver`(always enforce)。

```python
# 当前
async def run(self, ctx, upstream_payloads=None, *, enforce_deliver=True, graph=None) -> NodeResult:

# 新
async def run(self, ctx: GraphContext[S], *, graph: CompiledGraph[S] | None = None) -> NodeResult:
```

- `enforce_deliver` 参数移除 — 框架统一调度,always enforce
- `coordinator` 参数不作为函数参数 — coordinator 在 `ctx.coordinator`(always present,Null/Memory/SQLite)
- `upstream_payloads` 参数移除 — integrate 总是从 deliver_store 读取(C4: 单一输入模型)
- `graph` 保留 — topology 引用

**node 自定义只需实现 `execute(ctx, integrated_input) -> NodeResult`** — 生命周期/持久化/版本链由框架统一调度。

**无 coordinator=None 兼容性(C1)**: 不再有退化模式。所有 Node.run() 调用必须通过 `ctx.coordinator` 提供 coordinator。Null 策略(NullNodeState/NullGraphMetadataStore/NullDeliverStore)是 no-persistence 的正当实现,不是 backward-compat shim。现有测试、现有 scheduler 调用**需要更新** — 不保留 coordinator=None 兼容(rule 15)。

## 7. 对现有实现的影响

### 7.1 移除

| 组件 | 文件 | 原因 |
|------|------|------|
| `CheckpointData` | checkpoint_store.py | 被分布式持久化替代 |
| `ParallelScheduler._build_checkpoint_data` | scheduler/parallel.py | 增量持久化替代 |
| `ParallelScheduler._schedule_checkpoint` | scheduler/parallel.py | 增量持久化替代 |
| `ParallelScheduler._restore_from_checkpoint` | scheduler/parallel.py | coordinator.load_for_recovery 替代 |
| `GraphEngine(checkpoint_store=...)` | engine.py | scheduler 无感知 checkpoint_store;coordinator 持有持久化 |
| `LinearScheduler` 无 checkpoint | scheduler/linear.py | 统一接口;Linear 默认 Null 实现 |
| `GraphAsNode` 特殊 wrapper 机制 | nodes/graph_as_node.py | node 天生可用 GraphEngine,不需特殊 wrapper(§9.1) |
| `FunctionNodeFactory` callable 注册表 | nodes/function_node.py | callable 注入与声明式设计矛盾(§9.2) |
| `DeliverStore` graph 级统一管理 | deliver_store.py | 改为 per-node,node 持有引用(§14) |
| `DeliverRecord.status = SUBMITTED` | deliver_store.py | 改为 `CONSUMED`,语义从"已提交"改为"已消费"(§14.3) |
| `GraphOrchestrator` 共享 stores | graph_orchestrator.py | 改为 per-GraphInstance coordinator 持有(§13.2) |

### 7.2 演进

| 组件 | 变化 |
|------|------|
| `NodeState` ABC | 从 read/snapshot/restore → save_invocation/load/query_versions |
| `NodeStateStore` | 演进为 `GraphPersistenceCoordinator` + `NodeState` 分层 |
| `Node.run()` | 删除 upstream_payloads + coordinator 参数(C1/C4);coordinator 在 ctx.coordinator;框架统一调度生命周期 |
| `NodeInstanceStatus` | 拆分为 `SchedulerInstanceStatus`(DORMANT/READY/RUNNING/COMPLETED)+ `InvocationStatus`(PENDING/RUNNING/COMPLETED/CANCELED/CRASHED/SUPERSEDED)(I22) |
| `GraphContext` | 新增 `coordinator` 属性(always present,GraphInstance/ReActAgent 构造时注入);fork() 传播 coordinator(缺口B);新增 `current_invocation: InvocationContext | None` 字段(F6: scheduler 在 execute 前设置,dispatch handler 读取 source_node + source_invocation_id) |
| `GraphInstance` | **演进为运行时 class**(C2: 从 frozen Pydantic → 普通 class 持有 coordinator + 可序列化字段 + 可扩展其他字段)。提供 get_state / load_for_recovery |
| `ParallelScheduler` | 移除 checkpoint 逻辑,改为在事件点通知 coordinator;dispatch handler 调 coordinator.route_deliver(C4) |
| `GraphOrchestrator` | 加 `_active_instances` 注册表(C2);per-GraphInstance 创建 coordinator |
| `GraphControlService` | `_deliver` 收敛到 `coordinator.route_deliver`(I2: 移除共享 deliver_store) |
| `GraphInstanceStore` | 演进为 `GraphMetadataStore`(存可序列化 metadata,不存运行时 GraphInstance) |

### 7.3 保留

| 组件 | 原因 |
|------|------|
| `DeliverStore` + `deliver_states` 表 | deliver 持久化独立,不变 |
| `DispatchStore` + `dispatch_events` | 审计日志,不变 |
| `GraphSpecStore` + `graph_specs` 表 | 图定义持久化,不变 |
| `GraphInstanceStore` + `graph_instances` 表 | 演进为 graph metadata store |
| `node_states` 表 schema | 统一表,各 node 共用,演进字段(invocation_id, version, parent_version, status) |

## 8. 收敛效果

| 收敛项 | 效果 |
|--------|------|
| Linear vs Parallel checkpoint 分歧 | ✅ 统一接口,差异在默认实现(Null vs Memory) |
| CheckpointData blob vs NodeState | ✅ 完全替代,单一持久化机制 |
| scheduler 拥有持久化逻辑 | ✅ 移除,coordinator 统一路由 |
| NodeState 零 caller | ✅ 激活,coordinator 调用 |
| Node 生命周期分散 | ✅ 框架统一调度(PENDING→RUNNING→COMPLETED/CANCELED/CRASHED) |
| GraphAsNode 特殊 wrapper 机制 | ✅ 移除;node 天生可在 execute 内用 GraphEngine,不需特殊 wrapper |
| FunctionNode callable 注入模式 | ✅ 移除;通用 node 是声明式 config 驱动,不注入 callable |
| DeliverStore graph 级统一管理 | ✅ 改为 per-node,node 持有 deliver_store 引用 |
| deliver 投递与消费不分离 | ✅ 投递(accumulate)+ 消费(mark_consumed + integrate)分离,消费有幂等 |
| in-memory 对象泄漏 | ✅ per-GraphInstance 绑定,随对象 GC 自然管理 |

## 9. 通用 Node 类型重新定位

实现检视暴露两个通用 Node 类型的设计偏差,本设计重新定位:

### 9.1 GraphAsNode — 移除特殊机制

**原设计**(issue 02 P2.8): `GraphAsNode` 是一个 wrapper,持有 `CompiledGraph`,在 execute 内调 `compiled.execute(ctx, ...)` 共享父图 ctx。

**新设计**: GraphAsNode 不应是特殊机制。Node 天生可以在 execute 内部:
1. 创建子图 GraphInstance(分配 graph_instance_id,parent_instance_id 指向当前实例)
2. 编译 GraphSpec → CompiledGraph
3. 创建 GraphEngine 执行子图
4. 处理 state 隔离(fork ctx.state / 创建新 state / 共享,node 自己决定)
5. 捕获或传播 GraphInterrupt(node 自己决定)

框架只提供原语(GraphInstance / GraphEngine / coordinator / NodeState),不规定子图如何执行。当前 `GraphAsNode` wrapper 保留为示范实现,不作为核心设计。

### 9.2 FunctionNode — 移除 callable 注入

**原设计**(issue 02 P2.7): `FunctionNodeFactory` 持有 `dict[str, Callable]` 运行时注册表,config 引用函数名,factory 查注册表获取 callable。

**新设计**: 通用 node 是声明式 config 驱动,不注入 callable。如果需要可配置的逻辑,应该:
1. 用户自定义 Node(继承 Node ABC,实现 execute)— 这是标准模式
2. 或通过 NodeSpec config 声明逻辑参数,node 的 execute 根据 config 执行不同分支

`FunctionNode` + `FunctionNodeFactory` 的 callable 注册表模式与声明式设计矛盾,标记为可移除。当前实现保留为示范,不作为核心设计。

### 9.3 保留的通用 Node 类型

| Node | 保留? | 理由 |
|------|-------|------|
| `DelayNode` | ✅ 保留 | 声明式 config 驱动(`delay_seconds`),无运行时注入,符合设计 |
| `HumanInputNode` | ✅ 保留 | 声明式 config 驱动(`prompt`),GraphInterrupt 交互是框架原语 |
| `GraphAsNode` | 示范保留 | 不作为核心机制;node 天生可用 GraphEngine |
| `FunctionNode` | 示范保留 | 不作为核心模式;callable 注入与声明式设计矛盾 |
| `AgentNode` | ✅ 保留(业务层) | TurnRunner 注入是业务层特殊 case(ADR 支撑) |

## 10. 待定/扩展

- **清理状态不纳入查询**: node 版本链中,旧版本可标记为"清理"状态,查询时排除。扩展设计,暂不实现。
- **图级 MVCC 轮次**: 保留为待办,不在此设计范围。
- **node 幂等设计**: 框架提供 invocation_id + version + parent_version 原语,node 自行实现幂等逻辑(execute 被重新调用时如何处理)。框架不传递"调用原因"信号。
- **前端查询接口**: 设计预留(§2.3),实现待前端集成阶段。
- **LiveGraphEngineController**: pause/stop 控制运行中 engine,需在 coordinator 层集成(不是 scheduler 层)。
- **自环节点(A→A)调度验证**: scheduler ready 判断沿用现有动态机制(ParallelScheduler `_ready` set + `_handle_dispatch` + `_recheck_pending` + reachability BFS;LinearScheduler 顺序执行),不是静态入度。本设计只改变 dispatch handler 的数据层(route_deliver 替代 upstream_payloads 参数),不改变调度层。自环节点作为有效场景,在实现时验证: A 完成 → dispatch 到自己 → deliver_store 投递 → A 再次 ready → integrate 消费自己的 deliver。串行保证: 旧实例 COMPLETED 后才 recheck,新实例才 READY。

## 11. 决策记录

本设计替代以下 issue 中的相关部分:

| 被替代 | 位置 | 替代内容 |
|--------|------|----------|
| issue 10 §1.1 CheckpointStore load_latest 接通 | 本文档 §5 | 恢复流程改为 coordinator.load_for_recovery |
| issue 10 §类别2新增 Node 级状态抽象 | 本文档 §4 | NodeState ABC 演进为完整持久化接口 |
| issue 10 §2.1 多节点并行恢复 | 本文档 §5 | 恢复从各 node 最新状态推导,不再从单一 blob |
| issue 07 §deliver 持久化 "策略由 scheduler/节点选择" | 本文档 §2.1 | 策略由 GraphInstance 级决策,scheduler 无感知 |
| ADR-0034 D19 CheckpointStore ABC | 本文档 §7.1 | CheckpointData 被移除,CheckpointStore ABC 被 coordinator + NodeState 替代 |

issue 10 的其他部分(生命周期状态机 §3.2、外部控制接口 §3.3、恢复两种类型 §3.5、bot 工厂 §3.6、持久化 schema §3.7)不受影响,继续有效。

issue 02 的 GraphAsNode(P2.8)和 FunctionNode(P2.7)重新定位(§9),不作为核心机制。

## 12. 分阶段实现计划

本设计是大工程,分 5 个阶段,每阶段独立可验证、可 commit。

### Phase 1: 持久化接口与类型定义(基础,无行为变更)

**目标**: 定义所有新类型和 ABC,不改现有行为。

**设计补强(C5/I11)**: DeliverStore ABC 演进 + DeliverRecord 演进 + DeliverStoreFactory ABC + NodeStateFactory ABC 移到 Phase 1(原列在 Phase 5,但 Phase 3-4 就需要)。

| 任务 | 文件 | 内容 |
|------|------|------|
| 1.1 `NodeInvocationRecord` | `node_state.py`(或新文件) | frozen Pydantic: invocation_id, graph_instance_id, node_name, version, parent_version, status(InvocationStatus), state_json, created_at, updated_at |
| 1.2 `GraphMetadata` | 新文件 `graph_metadata.py` | frozen Pydantic: graph_instance_id, spec_id, parent_instance_id, parent_node, status, instance_seq, iteration_count, activated_sources, pending_dispatches |
| 1.3 `InvocationContext` / `RecoveryContext`(含 rebuilt_main_state)/ `GraphStateSnapshot` | 同上 | frozen Pydantic value objects |
| 1.4 `NodeState` ABC 演进 | `node_state.py` | 新接口: save_invocation / load_invocation / load_latest / load_latest_completed / query_versions |
| 1.5 `GraphMetadataStore` ABC | 新文件 `graph_metadata_store.py` | save / load / update_status |
| 1.6 `NodeStateFactory` ABC | `node_state.py` 或 `node_factory.py` | create() -> NodeState |
| 1.7 `SchedulerInstanceStatus` + `InvocationStatus` 拆分 | `constants.py` | I22: 拆分 NodeInstanceStatus;I4: InvocationStatus 加 SUPERSEDED |
| 1.8 `DeliverStore` ABC 演进(C5) | `deliver_store.py` | 加 query_consumable / mark_consumed / promote_consumed;accumulate 加 source_node/source_invocation_id |
| 1.9 `DeliverRecord` 演进(C5) | `deliver_store.py` | 加 source_node/source_invocation_id/consumed_by_invocation_id;status 用 `DeliverConsumptionStatus` enum(I12) |
| 1.10 `DeliverConsumptionStatus` enum(I12) | `constants.py` | rule 1: enum 替代 raw str。值: PENDING / CONSUMED_PENDING / CONSUMED_COMPLETED(InMemory 用子集) |
| 1.11 `DeliverStoreFactory` ABC(C5) | `deliver_store.py` | create() -> DeliverStore |
| 1.12 `DeliverStoreFactory` 三实现占位 | 同上 | NullDeliverStoreFactory / InMemoryDeliverStoreFactory / SqliteDeliverStoreFactory(接口定义,实现可在 Phase 2) |
| 1.13 移除 `DeliverConsumer` ABC(I10) | `deliver_store.py` | 不引入 DeliverConsumer ABC;消费逻辑将在 Phase 3 作为 coordinator 方法实现 |

**验证**: 类型定义编译通过,mypy clean,新 ABC 不可直接实例化。现有测试全绿(无行为变更)。

### Phase 2: 三种实现(Null / Memory / SQLite)

**目标**: 实现 ABC 的三种策略,各自有完整测试。

| 任务 | 文件 | 内容 |
|------|------|------|
| 2.1 `NullNodeState` + `NullGraphMetadataStore` + `NullNodeStateFactory` | 新文件或现有 | 全 no-op |
| 2.2 `SimpleNodeState` 演进 | `node_state.py` | 从当前 read/snapshot/restore → 新接口(memory dict 实现) |
| 2.3 `MemoryGraphMetadataStore` | `graph_metadata_store.py` | dict 实现 |
| 2.4 `SimpleNodeStateFactory` | 同上 | 创建 SimpleNodeState |
| 2.5 `SqliteNodeState` 演进 | `node_state_store.py` | 新接口 + parent_version/status 字段 + schema 迁移 |
| 2.6 `SqliteGraphMetadataStore` | `graph_metadata_store.py` | SQLite 实现(graph_instances 表演进) |
| 2.7 `SqliteNodeStateFactory` | 同上 | 创建 SqliteNodeState |

**验证**: 每种实现的 CRUD 测试通过。round-trip 测试(save → load → compare)。Null 确认 no-op。Schema 迁移幂等。

### Phase 3: GraphPersistenceCoordinator

**目标**: coordinator 完整实现,独立可测试(不接入 scheduler)。

| 任务 | 文件 | 内容 |
|------|------|------|
| 3.1 `GraphPersistenceCoordinator` | 新文件 `persistence_coordinator.py` | begin/complete/cancel/suspend/crash/finalize + get_graph_state + load_for_recovery + route_deliver + get_deliver_store |
| 3.2 `register_node` | 同上 | node_name → NodeState + DeliverStore 映射,默认策略 fallback |
| 3.3 `begin_invocation` | 同上 | I18: version = max(已有版本)+1;I4: 标记 suspended RUNNING 为 SUPERSEDED;I17: 内部 try/except 自清理 PENDING |
| 3.4 `complete_invocation` | 同上 | save COMPLETED(不可变);C3: 调 promote_delivers 升级消费状态 |
| 3.5 `cancel/crash/suspend_invocation` | 同上 | save CANCELED/CRASHED/RUNNING(suspend 保存 state_snapshot) |
| 3.6 消费方法(I10: 替代 DeliverConsumer ABC) | 同上 | collect_consumable_delivers / mark_delivers_consumed / promote_delivers — 委托 deliver_store |
| 3.7 `get_graph_state` | 同上 | 收集 metadata + 各 node query_versions |
| 3.8 `load_for_recovery` | 同上 | I9: 加载 metadata + 各 node load_latest + rebuild_main_state → RecoveryContext(含 rebuilt_main_state) |
| 3.9 `rebuild_main_state` | 同上 | I5: 按 invocation_id(全局时间序)遍历 COMPLETED 记录 apply state_update;最后 apply SUPERSEDED 的 state_snapshot |
| 3.10 `load_latest_invocation` | 同上 | 加载 node 最新 invocation(用于 I16 resume 判断) |
| 3.11 `route_deliver` | 同上 | I20: target == END 时跳过;否则路由到 deliver_store.accumulate |
| 3.12 `finalize_invocation` | 同上 | 安全网: orphan PENDING → CRASHED;suspended RUNNING 不动;SUPERSEDED 不动 |

**验证**: coordinator 单元测试(用 mock NodeState + GraphMetadataStore)。生命周期转换测试。版本链测试。恢复测试(含 I16 resume 跳过 re-consume)。

### Phase 4: Node.run() 演进 + scheduler 集成

**目标**: Node.run() 接入 coordinator,scheduler 集成点接线。

| 任务 | 文件 | 内容 |
|------|------|------|
| 4.1 `Node.run()` 签名变化 | `node.py` | C1/C4: 删除 upstream_payloads + coordinator 参数;coordinator 在 ctx.coordinator(always present) |
| 4.2 `Node.run()` 生命周期调度 | `node.py` | 统一流程: begin → integrate(总是从 deliver_store,I16 resume 检查) → try(execute+retry+submit) → complete(含 promote_delivers)/cancel/suspend/crash → finally |
| 4.3 `GraphContext` 新增 coordinator 属性 | `context.py` | always present;fork() 传播 coordinator(缺口B) |
| 4.4 `ParallelScheduler` 集成 | `scheduler/parallel.py` | run_async 顶部调 ctx.coordinator.load_for_recovery;dispatch handler 调 coordinator.route_deliver(C4);移除 _build_checkpoint_data / _schedule_checkpoint / _restore_from_checkpoint |
| 4.5 `LinearScheduler` 集成 | `scheduler/linear.py` | run_async dispatch handler 调 coordinator.route_deliver;传 coordinator via ctx(Null 默认,行为不变) |
| 4.6 `GraphEngine` 回退 | `engine.py` | 移除 checkpoint_store 参数(改由 coordinator 持有) |
| 4.7 `CheckpointData` 移除 | `checkpoint_store.py` | 移除 CheckpointData + 相关方法 |
| 4.8 ReActAgent Null coordinator 接入(缺口A) | `agents/react/agent.py` | actual_turn 创建 NullCoordinator → 注入 ReActGraphContext(coordinator=ctx.coordinator) |
| 4.9 现有测试更新 | `tests/` | 所有 Node.run() 调用更新(无 upstream_payloads,coordinator via ctx);ReActAgent 测试用 Null coordinator |

**验证**: 现有测试全绿(更新后)。ParallelScheduler 恢复测试(用 coordinator + Memory 实现)。LinearScheduler 行为不变(Null 实现)。ReActAgent GraphInterrupt suspend/resume 测试(Null coordinator + AgentContext 状态)。

### Phase 5: GraphInstance 演进 + GraphOrchestrator 接线

**目标**: GraphInstance 演进为运行时 class 持有 coordinator,GraphOrchestrator 创建 coordinator + 管理注册表。

| 任务 | 文件 | 内容 |
|------|------|------|
| 5.1 `GraphInstance` 演进为运行时 class(C2) | `graph_instance.py` | 从 frozen Pydantic → 普通 class;持有 GraphMetadata(可序列化值对象)+ coordinator + 可扩展字段;提供 get_state / load_for_recovery / update_status |
| 5.2 `GraphOrchestrator` 创建 coordinator + 注册表(C2) | `graph_orchestrator.py` | 加 `_active_instances: dict[int, GraphInstance]` 注册表;per-GraphInstance 创建 coordinator(不再共享 stores);根据策略选 Null/Memory/SQLite;_execute 从注册表取 GraphInstance |
| 5.3 `register_node` 在 GraphInstance 构造时(I3/I19) | `graph_orchestrator.py` | orchestrator 遍历 compiled.nodes 调 coordinator.register_node(编译后,_execute 前);GraphSpecCompiler 不改 |
| 5.4 `GraphRecoveryService` 接入 | `graph_recovery.py` | 恢复流程: load GraphMetadata → reconstruct coordinator(SQLite stores)→ create GraphInstance → register → _execute |
| 5.5 移除旧的 checkpoint 接线 | `graph_orchestrator.py` | 移除 checkpoint_store / deliver_store 共享参数,改为 per-instance coordinator |
| 5.6 `GraphControlService` 收敛(I2) | `control/graph_control.py` | `_deliver` 调 `controller.deliver_to_node` → `coordinator.route_deliver`;移除 GraphControlService 的共享 deliver_store(行 137/141/214) |
| 5.7 `GraphInstanceStore` → `GraphMetadataStore` 演进 | `instance_store.py` | store 存 GraphMetadata(可序列化),不存运行时 GraphInstance;InMemory/Sqlite 实现演进 |
| 5.8 DeliverStore per-node 接线 | `deliver_store.py` | DeliverStore 从 graph 级统一管理改为 per-node(coordinator 持有 dict[node_name, DeliverStore]);Phase 1 已定义 ABC,此处接线 |

**验证**: GraphOrchestrator E2E 测试(创建 → 执行 → 恢复)。恢复测试(crash → recover_crashed → 验证状态)。GraphInterrupt suspend/resume 测试(Memory coordinator: 跨 _execute state snapshot 存活)。GraphControlService deliver 收敛测试。

## 13. 生命周期管理

### 13.1 设计哲学:持久化与内存对象统一

GraphInstance 是逻辑内存对象。`graph_instance_id` 是逻辑标识(持久化 key),不是内存地址。故障恢复时重建内存对象,新对象的 `graph_instance_id` 与旧对象一致——持久化层(SQLite)能关联,但内存对象本身是新的。

**核心原则**: 内存对象的生命周期自然管理下游所有内存对象。不需要显式 `clear()` 清理——对象 GC 时数据自然消失。

| 策略 | 内存对象生命周期 | 数据生命周期 | 清理方式 |
|------|-----------------|-------------|----------|
| Null | 不创建内存对象 | 无数据 | N/A |
| InMemory | 随 GraphInstance 对象 GC | 随对象 GC 消失 | 自然(对象引用释放) |
| SQLite | connection 随 GraphInstance 对象 GC | 持久化(跨进程) | 自然(connection close) + 可选定时清理 |

### 13.2 per-GraphInstance 对象树

**设计补强(C2/I2)**: coordinator 持有者是 **GraphInstance**(不是 _execute 调用栈)。GraphOrchestrator 加 `_active_instances` 注册表管理 GraphInstance 生命周期。GraphControlService 的共享 deliver_store 收敛到 coordinator.route_deliver。

```
GraphOrchestrator
  ├── _active_instances: dict[graph_instance_id, GraphInstance]   ← C2: 注册表
  │     └── GraphInstance (运行时 class, 持有 coordinator + metadata)
  │           ├── metadata: GraphMetadata (frozen Pydantic, 可序列化)
  │           ├── coordinator: GraphPersistenceCoordinator (per-GraphInstance)
  │           │     ├── graph_metadata_store (per-instance)
  │           │     ├── node_states: dict[node_name, NodeState] (per-node)
  │           │     └── deliver_stores: dict[node_name, DeliverStore] (per-node)
  │           └── (可扩展其他运行时字段)
  │
  └── _execute(instance) → 创建运行时对象
        ├── GraphContext (per-run, 持有 coordinator 引用 via ctx.coordinator)
        └── GraphEngine → Scheduler
              └── node.run(ctx, graph)   ← coordinator 从 ctx.coordinator 获取
```

**生命周期链(C2 修正)**:
- **GraphOrchestrator.create_and_run()**: create GraphMetadata → save to store → create coordinator → create GraphInstance(metadata, coordinator) → register in `_active_instances` → `_execute(graph_instance)`
- **_execute()**: retrieve GraphInstance from registry → use `graph_instance.coordinator` → create `GraphContext(coordinator=graph_instance.coordinator)` → GraphEngine → `run_async`
- **GraphInterrupt**: GraphOrchestrator catches → sets GraphInstance status to PAUSED → GraphInstance **stays in registry**(coordinator 不释放)→ Resume: `_execute(graph_instance)` again → SAME coordinator → state snapshot available
- **Crash recovery**: load GraphMetadata from store → reconstruct coordinator(SQLite stores, state recovered from DB)→ create new GraphInstance → register → `_execute`
- **Terminal**: GraphInstance remains in registry for state queries; removed when terminal + no longer needed(natural GC)

**关键修正(vs 原 §13.2)**: coordinator **不随 _execute 调用栈存在**。_execute 是 GraphOrchestrator 的方法调用,使用 GraphInstance(含 coordinator),不拥有它。coordinator 生命周期 = GraphInstance 生命周期 = GraphOrchestrator 注册表管理。这解决 I1(InMemory GraphInterrupt resume:coordinator 跨 _execute 存活,state snapshot 保留)。

**ReActAgent 路径(缺口A)**: 无 GraphInstance。actual_turn 创建 NullCoordinator(per-turn)→ 注入 ReActGraphContext。AgentContext(含 ReActTurnState)由 AgentPool 持有,跨 turn 存活。Null coordinator 是 structural pass-through,AgentContext 是 active 状态机制。两条路径正交(§3.3.4)。

**GraphControlService 收敛(I2/F7)**: 原 `GraphControlService.__init__` 创建共享 deliver_store(graph_control.py:137),`_deliver` 调 `self._deliver_store.accumulate`(line 214)。收敛后:`_deliver` 从 `GraphOrchestrator._active_instances[graph_instance_id].coordinator` 获取 coordinator 引用(F7: 直接获取,不经 controller 中转),调 `coordinator.route_deliver(target_node=node_name, content=content, source_node="__external__", source_invocation_id=0)`。移除 GraphControlService 的共享 deliver_store。外部 DELIVER_TO_NODE 走统一 coordinator 路径。

**GraphInstance 注册表驱逐(F9/F10)**:
- `GraphOrchestrator.unregister_instance(graph_instance_id)`: 调 `graph_instance.coordinator.close()`(F10: 关闭 SQLite connection 等)→ 从 `_active_instances` 移除。
- 触发条件: terminal status(COMPLETED/FAILED/CRASHED)+ 显式应用调用(如应用关闭、清理 hook)。不依赖"natural GC"(dict 强引用阻止 GC)。
- `coordinator.close()`: SQLite 策略调 `connection.close()`;Null/Memory 策略 no-op。实现 `__del__` 作为安全网,但 explicit close() 是主路径。
- Crash recovery: 如果 old GraphInstance 仍在 registry(F9: 无驱逐),先 `unregister_instance(old_gid)`(关闭旧 connection)再注册新的。

### 13.3 InMemory 策略的能力边界

InMemory 策略**只保证单次执行流程内的数据流转**:
- ✅ deliver 投递 → 消费(单次流程内)
- ✅ node 状态(当前执行状态)
- ✅ graph metadata(当前执行状态)
- ❌ crash recovery(内存对象消失,数据丢失)
- ❌ 版本链(可选,in-memory 可做简单 dict 不做版本链)

这是设计语义,不是缺陷。InMemory 用于测试和不需要 crash recovery 的场景(如 LinearScheduler 默认)。

### 13.4 SQLite 策略的能力边界

SQLite 策略**提供完整能力**:
- ✅ deliver 投递 → 消费(跨恢复)
- ✅ node 状态 + 版本链(完整 MVCC)
- ✅ graph metadata(持久化)
- ✅ crash recovery(从持久化数据重建)
- ✅ 消费幂等(跨恢复,通过 invocation_id + 版本链判断)

## 14. DeliverStore 演进

### 14.1 从 graph 级到 per-node

当前 `DeliverStore` 是 graph 级统一管理(一个 store 管所有 node 的 delivers)。新设计改为 per-node:每个 node 持有自己的 `deliver_store` 引用,表示"我收到的内容"。

### 14.2 DeliverStore ABC 演进

```python
class DeliverStore(ABC):
    """Per-node deliver 接收 + 消费 store。node 持有引用。
    
    ABC 接口统一,实现策略不同:
    - Null: in-memory queue,无状态机,用于 ReActAgent(缺口A)
    - InMemory: 简单 dict,单次流程内消费幂等,不持久化,crash 后数据丢失
    - SQLite: 三态消费状态机(PENDING/CONSUMED_PENDING/CONSUMED_COMPLETED),
      与 invocation 状态机绑定,支持 crash recovery
    """

    @abstractmethod
    def accumulate(
        self, *,
        graph_instance_id: int,
        target_node: str,
        source_node: str,
        source_invocation_id: int,
        content: Any,
    ) -> int:
        """投递一条 deliver 到此 store。返回 deliver_id。"""
        ...

    @abstractmethod
    def query_consumable(
        self, graph_instance_id: int, target_node: str,
    ) -> list[DeliverRecord]:
        """查询可消费的 delivers。
        
        Null: 返回 in-memory queue 所有记录。
        InMemory: 返回未消费的(PENDING 等效)。
        SQLite: 返回 PENDING + CONSUMED_PENDING(上次未完成的),排除 CONSUMED_COMPLETED。
        """
        ...

    @abstractmethod
    def mark_consumed(self, deliver_ids: list[int], consumed_by_invocation_id: int) -> None:
        """标记 delivers 为已消费。
        
        Null: 从 queue 移除(或标记)。
        InMemory: 标记为已消费(简单标记,不区分状态)。
        SQLite: 标记为 CONSUMED_PENDING(invocation 完成后升级为 CONSUMED_COMPLETED)。
        """
        ...

    @abstractmethod
    def promote_consumed(self, consumed_by_invocation_id: int) -> None:
        """invocation COMPLETED 时调用(C3: coordinator.complete_invocation 内部调)。
        
        Null: no-op。
        InMemory: 清理该 invocation 消费的记录(删除,等价于"已完成,不需要了")。
        SQLite: 该 invocation 消费的 CONSUMED_PENDING → CONSUMED_COMPLETED(保留历史)。
        """
        ...

    @abstractmethod
    def clear(self, graph_instance_id: int) -> None:
        """清理(Null/InMemory: no-op,对象 GC 自然清理;SQLite: 可选批量删除)。"""
        ...
```

### 14.3 消费逻辑归属(I10: 移除 DeliverConsumer ABC)

**设计补强(I10)**: 移除 `DeliverConsumer` ABC + `DefaultDeliverConsumer`(rule 6: 只有一个实现是 hypothetical seam)。消费逻辑(collect/mark/promote)作为 **coordinator 方法**(§4.4 已定义):
- `coordinator.collect_consumable_delivers(node_name, invocation_id)` → 委托 `deliver_store.query_consumable`
- `coordinator.mark_delivers_consumed(node_name, deliver_ids, invocation_id)` → 委托 `deliver_store.mark_consumed`
- `coordinator.promote_delivers(node_name, invocation_id)` → 委托 `deliver_store.promote_consumed`

**DeliverStore ABC 保留**(三实现 Null/InMemory/SQLite — 真实 seam,rule 6 合规)。消费状态机(二态 vs 三态)在 DeliverStore 实现,不在独立 DeliverConsumer。

**未来扩展**: 如果 node 需要自定义消费逻辑(如"不重新传递 CONSUMED_PENDING,node 有自己的内部恢复方案"),出现第二个实现时再抽 ABC(rule 6: 两个实现才 justify seam)。

### 14.4 DeliverRecord 演进

**设计补强(I12)**: `status` 字段从 raw str → `DeliverConsumptionStatus` enum(rule 1: enum 替代 raw string)。

```python
class DeliverConsumptionStatus(StrEnum):
    """Deliver 消费状态。不同实现用不同子集。"""
    PENDING = "pending"                    # 已投递,未消费(Null/InMemory/SQLite 共用)
    CONSUMED = "consumed"                  # 已消费(InMemory 二态: 终态)
    CONSUMED_PENDING = "consumed_pending"  # 已消费,invocation 未完成(SQLite 三态)
    CONSUMED_COMPLETED = "consumed_completed"  # 已消费,invocation 已完成(SQLite 三态: 终态)

class DeliverRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    
    deliver_id: int              # Snowflake
    graph_instance_id: int
    target_node: str             # 接收方(此 store 的 owner)
    source_node: str             # 投递方
    source_invocation_id: int    # 投递方的 invocation_id
    content: Any                 # 投递内容
    status: DeliverConsumptionStatus  # I12: enum 替代 raw str
    consumed_by_invocation_id: int | None  # 消费方的 invocation_id
    created_at: int
    updated_at: int
```

**实现策略用不同子集**:
- Null: PENDING → CONSUMED(二态,in-memory queue)
- InMemory: PENDING → CONSUMED(二态,promote_consumed = 删除)
- SQLite: PENDING → CONSUMED_PENDING → CONSUMED_COMPLETED(三态,与 invocation 状态机绑定)

enum 统一所有可能值,不同实现用不同子集。rule 1 合规。

### 14.5 DeliverStoreFactory ABC

```python
class DeliverStoreFactory(ABC):
    """创建默认 DeliverStore 实例。graph 提供默认,node 可覆盖。"""

    @abstractmethod
    def create(self) -> DeliverStore:
        ...
```

三种实现: `NullDeliverStoreFactory` / `InMemoryDeliverStoreFactory` / `SqliteDeliverStoreFactory`。

### 14.6 三种 DeliverStore 实现

| 实现 | 消费状态 | promote_consumed | 消费幂等 | crash recovery | 生命周期 |
|------|---------|-----------------|----------|---------------|----------|
| Null | 二态(PENDING/CONSUMED) | no-op(或删除) | ❌ | ❌ | per-turn(ReActAgent) |
| InMemory | 二态(PENDING/CONSUMED) | 删除已消费记录 | ✅(单次流程内) | ❌(数据丢失) | 随 GraphInstance GC |
| SQLite | 三态(PENDING/CONSUMED_PENDING/CONSUMED_COMPLETED) | CONSUMED_PENDING→CONSUMED_COMPLETED | ✅(跨恢复) | ✅ | connection 随 GraphInstance GC,数据持久化 |

**共享 SQLite connection(I14)**: N 个 node × 2 store(NodeState + DeliverStore)= 2N SQLite 连接。应共享 connection per GraphInstance。`DeliverStoreFactory` / `NodeStateFactory` 接受共享 `sqlite3.Connection` 参数(由 coordinator 创建,per-GraphInstance)。避免连接增殖。

```python
class SqliteDeliverStoreFactory:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection  # 共享 per-GraphInstance
    
    def create(self) -> DeliverStore:
        return SqliteDeliverStore(self._connection)
```

---

## 15. 设计补强总结(22 项检视问题 + 4 缺口闭环)

本设计文档经过实现检视 → grilling 设计讨论 → 设计检视(22 项问题)→ 设计补强(本轮)的完整闭环。本节是增量补强的权威索引。

### 15.1 Critical(5 项)— 逻辑断裂 / rule 违规

| # | 问题 | 补强方案 | 修订位置 |
|---|------|----------|----------|
| C1 | coordinator=None 是 rule 15 违规 | 删除退化模式;coordinator 总是在 ctx.coordinator;Null 策略是 no-persistence 正当实现 | §3.3.2, §3.3.4, §6 |
| C2 | coordinator 持有者矛盾(§7.2 vs §13.2) | GraphInstance 演进为运行时 class 持有 coordinator;GraphOrchestrator 加 _active_instances 注册表;coordinator 生命周期 = GraphInstance 生命周期(不是 _execute 调用栈) | §7.2, §13.2 |
| C3 | promote_consumed 从未在 complete_invocation 中调用 | complete_invocation 内部调 coordinator.promote_delivers → deliver_store.promote_consumed | §3.3.2 step 3, §4.4 |
| C4 | deliver vs submit 路由歧义(双重 dispatch) | 收敛: integrate 总是从 deliver_store;upstream_payloads 参数移除;scheduler dispatch handler 调 coordinator.route_deliver 生产到下游 deliver_store | §3.3.2, §3.3.4, §6, §4.10 |
| C5 | DeliverStore ABC 演进在 Phase 5 但 Phase 3-4 需要 | DeliverStore ABC 演进 + DeliverRecord 演进 + DeliverStoreFactory ABC + DeliverConsumptionStatus enum 移到 Phase 1 | §12 Phase 1 |

### 15.2 Important(17 项)— 设计缺口 / 收敛问题

| # | 问题 | 补强方案 | 修订位置 |
|---|------|----------|----------|
| I1 | InMemory GraphInterrupt resume 断裂 | C2 修复后 coordinator 跨 _execute 存活;ReActAgent 路径 AgentContext 持有状态(缺口A) | §13.2, §3.3.4 |
| I2 | GraphControlService 共享 deliver_store 未收敛 | _deliver 调 controller.deliver_to_node → coordinator.route_deliver;移除共享 deliver_store | §13.2, §7.2, §12 Phase 5.6 |
| I3 | register_node 时机矛盾(编译时 vs 构造时) | GraphInstance 构造时(编译后,_execute 前);orchestrator 遍历 compiled.nodes 调 register_node | §4.10, §12 Phase 5.3 |
| I4 | suspended invocation 被取代时终态未定义 | 新增 SUPERSEDED 状态(终态,不可变);begin_invocation 先标记 v4 为 SUPERSEDED 再创建 v5 | §3.3.3, §3.4, §3.5, §4.4 |
| I5 | cross-node main_state 重建顺序非确定 | 用 invocation_id(Snowflake 时间序)全局排序;无碰撞,因果序保持 | §3.3.3, §5, §4.11 索引 |
| I6 | CANCELED invocation 不应被恢复时 re-dispatch | CANCELED → 跳过(deliberate cancel,需显式 resume);CRASHED + orphan PENDING/RUNNING → re-dispatch | §5 step 5 |
| I7 | §3.3.2 step 3 残留 "mark as SUBMITTED" | 删除 SUBMITTED 引用;消费状态机用 CONSUMED | §3.3.2 step 3 |
| I8 | coordinator 缺少 deliver_consumer 访问器 | 移除 DeliverConsumer(I10);coordinator 直接提供消费方法 | §4.4 |
| I9 | coordinator 缺少 rebuild_main_state 方法 | load_for_recovery 返回 RecoveryContext 含 rebuilt_main_state;rebuild_main_state 内部按 invocation_id 排序 | §4.4, §4.9, §5 |
| I10 | DeliverConsumer 是 hypothetical seam(rule 6) | 移除 DeliverConsumer ABC;消费逻辑作为 coordinator 方法;DeliverStore ABC 保留(三实现) | §3.3.2, §4.4, §14.3, §12 Phase 1.13 |
| I11 | Phase 1 缺少 6 个类型定义 | Phase 1 加 tasks 1.8-1.13(DeliverStore/Record/Factory/enum/移除 DeliverConsumer) | §12 Phase 1 |
| I12 | DeliverRecord.status: str 违反 rule 1 | 定义 DeliverConsumptionStatus enum;不同实现用不同子集 | §14.4, §12 Phase 1.10 |
| I13 | Schema 迁移: CREATE TABLE IF NOT EXISTS 不加列 | 加 ALTER TABLE ADD COLUMN 迁移逻辑(PRAGMA table_info 检查) | §4.11 |
| I14 | per-node SQLite connection 增殖 | DeliverStoreFactory/NodeStateFactory 接受共享 connection 参数(per-GraphInstance) | §14.6 |
| I15 | DeliverConsumer 持有者歧义(Node 属性 vs ctx) | I10 解决:移除 DeliverConsumer,消费逻辑在 coordinator | §4.4 |
| I16 | suspend 后 mark_consumed → 恢复时 double-effect | resume 时检查前一 invocation state_snapshot:有则用 snapshot 跳过 re-consume;无则正常 re-consume | §3.3.2 step 2, §3.3.3, §5 |
| I17 | begin_invocation 失败留 orphan PENDING | begin_invocation 内部 try/except,失败时自清理 PENDING 记录 | §3.3.2 step 1, §4.4 |
| I18 | version 冲突(load_latest_completed+1 可能 UNIQUE 冲突) | version = max(所有已有版本号) + 1 | §3.3.2 step 1, §3.4, §4.4 |
| I19 | GraphSpecCompiler 无 coordinator 访问权 | register_node 在 GraphInstance 构造时(orchestrator 遍历 compiled.nodes);compiler 不改 | §4.10, §12 Phase 5.3 |
| I20 | deliver to END 未处理 | route_deliver 检查 target == GraphNode.END 时跳过(返回 None) | §4.4 |
| I21 | 恢复不区分故障恢复 vs 手动恢复 | §5 step 4: 故障恢复只捡 crashed;手动恢复捡 paused/stopped;入口过滤不同 | §5 step 4 |
| I22 | NodeInstanceStatus 混两个维度 | 拆分 SchedulerInstanceStatus(DORMANT/READY/RUNNING/COMPLETED)+ InvocationStatus(PENDING/RUNNING/COMPLETED/CANCELED/CRASHED/SUPERSEDED) | §3.5, §7.2, §12 Phase 1.7 |

### 15.3 新发现缺口(4 项)

| # | 缺口 | 补强方案 | 修订位置 |
|---|------|----------|----------|
| 缺口A | ReActAgent 直接 GraphEngine 路径 — coordinator 注入未覆盖 | ReActAgent 用 Null coordinator(正交层论证:coordinator 管 node invocation 持久化,AgentContext 管 agent turn 状态,不同关注点) | §3.3.4, §13.2, §12 Phase 4.8 |
| 缺口B | GraphContext.fork() 的 coordinator 传播 | fork() 加 coordinator 参数,默认继承父;子图创建自己的 GraphInstance + coordinator | §4.10, §7.2 |
| 缺口C | ToolNode resume_target + suspend_invocation 交互 | ⚠️ 必须直接调 ctx.state.checkpoint(),不能用 state_schema().fields 迭代(后者跳过继承字段 resume_target) | §3.3.3, §3.3.2 step 5 |
| 缺口D | GraphInstance ID 与 graph_instance_id 复用关系 | 验证: GraphRecoveryService 从 instance_store 拿 GraphInstance 记录(含 graph_instance_id),coordinator 创建时用此 ID 绑定持久化层。链路通,非决策 | §13.2 生命周期链 |

### 15.4 核心架构决策

1. **GraphInstance 演进为运行时 class**(C2): 从 frozen Pydantic → 普通 class 持有 coordinator + GraphMetadata(可序列化值对象)+ 可扩展字段。GraphOrchestrator 加 _active_instances 注册表管理生命周期。
2. **coordinator always present**(C1): 删除 coordinator=None 退化模式。Null 策略是 no-persistence 正当实现。
3. **输入模型收敛**(C4): integrate 总是从 deliver_store。upstream_payloads 参数移除。单一输入模型,单一代码路径。
4. **两条路径正交**(缺口A): GraphOrchestrator(Memory/SQLite coordinator,跨 _execute)+ ReActAgent(Null coordinator,AgentContext 跨 turn)。不是分歧,是不同关注点的正交分层。
5. **SUPERSEDED 状态**(I4): suspended invocation 被新 invocation 取代时标记为 SUPERSEDED(终态)。状态机保持简洁(6 状态)+ 保留扩展空间。
6. **全局排序 invocation_id**(I5): Snowflake 时间序,无碰撞,因果序保持。无需新增字段。
7. **消费逻辑在 coordinator**(I10): 移除 DeliverConsumer ABC。DeliverStore ABC 保留(三实现 — 真实 seam)。

### 15.5 Design-Closure 检视补强(11 项 critical finding)

经 5 维度并行 closure 追踪(data-flow / state-machine / interface / lifecycle / convergence),发现并修复 11 项 critical finding + 2 项 stale 残留:

| # | 维度 | 问题 | 修复方案 | 修订位置 |
|---|------|------|----------|----------|
| F1 | SM | Crash between SUPERSEDED marking 和新 invocation 创建 → graph stuck | Recovery 补充: SUPERSEDED 无后继的 node → re-dispatch;SQLite 包事务 | §5 step 5, §4.4 begin_invocation |
| F2 | SM | Crash between save COMPLETED 和 promote_delivers → double-effect | SQLite 包事务;Recovery 自动 promote COMPLETED invocation 的 CONSUMED_PENDING delivers | §3.3.2 step 3, §4.4, §5 step 6 |
| F3 | SM | v4 的 CONSUMED_PENDING delivers 不被 v5 complete promote → 孤儿 | promote_delivers 升级该 node 的所有 CONSUMED_PENDING(不限 invocation_id) | §3.3.2 step 3, §4.4 promote_delivers |
| F4 | SM | suspended RUNNING 标记机制未定义(NodeInvocationRecord 无 suspended 字段) | 加 `suspended: bool = False` 字段;suspend_invocation 设 True;begin/finalize/recovery 检查 | §4.2, §3.3.2 step 1/5/7, §3.4, §5 |
| F5 | DF | pending_dispatches vs deliver_store 在 crash 时不同步 → deliver 孤儿 | Recovery _recheck_pending 补充查询 deliver_store 的 PENDING delivers 给 COMPLETED nodes | §5 step 7 |
| F6 | IF | route_deliver 的 source_node/source_invocation_id 在 dispatch handler 不可用 | GraphContext 加 current_invocation 字段;dispatch handler 从 ctx 读取 | §4.10, §7.2 |
| F7 | IF | GraphControlService._deliver 收敛路径未接线(无 coordinator 引用) | 直接从 _active_instances[gid].coordinator 获取,调 route_deliver(source="__external__") | §13.2 |
| F8 | IF | begin_invocation 的 parent_version 参数来源未追踪 | 移除参数,内部从 load_latest_completed 计算 | §4.4, §3.3.2 step 1 |
| F9 | LC | GraphInstance 注册表无驱逐机制 → 资源泄漏 | unregister_instance(gid) + 触发条件(terminal + 显式调用) | §13.2 |
| F10 | LC | SQLite connection 无显式 close() 路径 | coordinator.close() 调 connection.close();从 unregister_instance 调用 | §13.2 |
| F11 | CV | default_deliver_store_factory=None 是 rule 15 "fall back if None" 违规 | 改 required(非 Optional),NullDeliverStoreFactory 作为默认 | §4.4 |
| R1 | CV | §3.3.4 "deliver_consumer" stale 残留(DeliverConsumer 已移除) | 改为 "deliver_store" | §3.3.4 |
| R2 | CV | §3.3.4 "等价当前 upstream_payloads" stale 残留(已移除) | 改为 "功能等价原 upstream_payloads(已移除)" | §3.3.4 |

### 15.6 依赖图与实现顺序

```
C2 (基础) ──┬──→ C1 (always coordinator) ──→ C4 (always deliver_store) ──→ 缺口A (Null for ReActAgent)
            ├──→ I1 (coordinator 跨 _execute) [自动]
            └──→ 缺口B (fork 传播 coordinator)

I4 (SUPERSEDED) ──→ I16 (resume 用 snapshot,跳过 re-consume)

I5 (invocation_id 排序) [独立]
I10 (移除 DeliverConsumer) [独立]
I22 (拆分 enum) [独立, I4 的 SUPERSEDED 放入 InvocationStatus]
```

**关键路径**: C2 → C1+C4 → 缺口A(顺序依赖)
**可并行**: I4/I16(一组)+ I5/I10/I22(独立)
