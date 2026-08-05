# Fork-merge 依赖链移除（保留并发调度）

Status: triage:closed
Blocked by: none
Resolved: 2026-08-04

## Question

ParallelScheduler 的并发调度能力（多 instance 并发执行、`ON_RECEIVE` 扇出、连续调度、recovery）是核心设计，保留。

但建立在 **fork-merge 模式**之上的整套机制是不必要的复杂度：asyncio 单线程下 merge segment 同步无 await 不会交错，共享 state 就能安全并发。移除 fork-merge 及其全部依赖链，保留 ParallelScheduler 的并发调度核心。

## Resolution

### 设计哲学

当前持久化层围绕一个从未在生产中成立的假设设计：**node 用 declarative delta（`NodeResult.state_update`）返回状态变更，框架通过 fork-merge + channel + conflict detection 合并到 main_state**。

explore 验证的关键事实：
- **零个生产 node 写 `NodeResult(state_update=...)`** — 所有 14 个生产 node 返回 bare `NodeResult()`，用 imperative `ctx.state.x = y` 写状态
- **`complete_invocation` 对所有生产 node 存的 `state_json` 都是 `{}`**（空字典）— 因为传的是 `result.state_update if result.state_update else {}`，而 `state_update` 永远是 None
- **`rebuild_main_state` 对所有 COMPLETED 记录做 `dict.update({})` — no-op** — 当前 recovery 对不 suspend 的图重建出空状态（pre-existing bug）

fork-merge + channel + declarative delta + SUPERSEDED 两阶段 rebuild + state_factory 是同一棵依赖树上的枝叶。移除根（fork-merge + declarative delta），整棵树自然倒塌。

移除后的设计回归三层持久化本质：**GraphInstanceStore + NodeState(invocation 版本链) + DeliverStore**，加共享 state + imperative 写 + full snapshot 持久化 + 单次 recovery。

### 关键决策

1. **`complete_invocation` 留在 `Node.run()` 内部** — Node.run() 保持自包含生命周期（begin → integrate → execute → submit → complete → finalize + 异常处理）。只改 complete 步骤的传参：从 `result.state_update` 改为 `ctx.state.model_dump(mode="json")`。不移动调用点到 scheduler — 那会引入归属分裂（convergence rule 1 违反）。

2. **SUPERSEDED 状态移除** — suspend 保存 `RUNNING(suspended=True)` + full snapshot，不再标记为 SUPERSEDED。recovery 取 `max(updated_at)` 中的 `{COMPLETED, suspended RUNNING}` — 单次查询，无两阶段 apply。详见 [ticket 26](26-rebuild-main-state-semantics.md)。

3. **`NodeResult` 移除** — 零生产写入者，`after_node` hook 的所有实现（含生产的 `ReactGraphRuntime`）都不读取 result 参数。`node.execute()` 返回 `None`，`after_node` 签名去掉 result 参数。

4. **channel 系统整体移除** — `GraphState` 变成纯 Pydantic BaseModel。`checkpoint()` = `model_dump(mode="json")`，`from_checkpoint()` = `model_validate()`。`LastValue` 是默认 channel，`Annotated[T, LastValue]` 注解不 accomplish 任何功能。`ReducerChannel` 零生产使用。`Codec` / `register_codec` 零生产调用。

5. **`state_factory` 移除** — ReAct 用 imperative `build_react_graph().compile()` 直接创建 `ReActTurnState()`，不经过 factory。factory 只服务 declarative GraphSpec 路径。`GraphSpec.state_schema` 改为引用 state class 直接。

6. **`DispatchStore` 移除** — 它是纯调度事件审计日志，不参与调度决策、不参与 recovery、不被 coordinator 引用。`_handle_dispatch` 同时做两件事：记录到 DispatchStore（审计）和 route_deliver 到 DeliverStore（真正投递状态机）。DeliverStore 三层持久化已覆盖全部 recovery 需求。

7. **`GraphContext.fork()` 保留原样** — scheduler 停止调用它（fork 路径删除），但 node 作者仍可用 `ctx.fork(state=...)` 做子任务隔离。只删 scheduler 的 fork 调用，不改 fork() 本身。

8. **per-node invocation 串行化：保留 coordinator 现有机制** — coordinator 的 `begin_invocation` 已有 crash-on-prior-RUNNING 安全网。生产图用 LinearScheduler（顺序执行，无并发），ON_RECEIVE 只在测试/示例中。不为测试-only 场景增加 scheduler gate。详见 [ticket 31](31-state-machine-cas-thread-safety.md)。

9. **crash 安全性：可接受** — 无 fork 后，crashed instance 的 partial writes 在共享 state 中。但进程 crash 时内存丢失，recovery 从持久化记录重建。最新 COMPLETED snapshot 是恢复点。`crash_invocation` 保存 `{}`（丢弃 partial state — 当前行为，保持不变）。

## 完整移除清单

### Fork-merge 核心

| 移除项 | 文件:行 | 理由 |
|--------|---------|------|
| fork + deep copy state 隔离 | `parallel.py:208-216`（`need_fork` / `instance.forked_state` / `model_copy(deep=True)`） | 共享 state——asyncio 单线程，同步段不交错 |
| merge segment（conflict detection + apply_state_update + advance + complete） | `parallel.py:492-511` | 没有 fork 就不需要 merge 回 main_state |
| fast path vs fork path 分支 | `parallel.py:452-462` | 没有 fork 就没有两条路径 |
| `_execute_instance` 的 `fork` 参数 | `parallel.py:407`（`fork: bool = False`） | 调用点 `:218` 去掉 `fork=need_fork` |
| `NodeInstance.forked_state` 字段 | `instance.py:60,72,80` | fork 隔离机制移除 |
| `NodeInstance.fork_version` 字段 | `instance.py:61,73,81` | conflict detector 的 generation key，无 conflict detection 后无用 |
| `ParallelScheduler.__init__` 的 `conflict_detector` 参数 | `parallel.py:93` | WriteConflictDetector 移除 |
| `ParallelScheduler.__init__` 的 `dispatch_store` 参数 | `parallel.py:92` | DispatchStore 移除 |

### Conflict detection

| 移除项 | 文件 | 理由 |
|--------|------|------|
| `WriteConflictDetector` ABC | `conflict_detector.py:64` | 没有 fork 就没有并发写冲突 |
| `GenerationWriteTracker` | `conflict_detector.py:112` | 同上 |
| `_Generation` | `conflict_detector.py:42` | 同上 |
| `conflict_detector.py` 整个文件 | — | 全部符号无外部消费者 |
| `InvalidUpdateError` | `exceptions.py:84` | 零 raiser 后成死代码（仅被 `LastValue.update` 和 `WriteConflictDetector.commit` raise，两者都移除）。需确认无外部 catch |

### Declarative delta + NodeResult

| 移除项 | 文件:行 | 理由 |
|--------|---------|------|
| `NodeResult` 类型 | `result.py:32-53` | 零生产写入 `state_update`；`after_node` 零实现读取 result。`node.execute()` 返回 `None` |
| `NodeResult.state_update` 字段 | `result.py:46` | declarative delta 载体，生产零使用 |
| `apply_state_update` | `state/state.py:163` | 没有 delta merge。调用点：`parallel.py:502,504,511`（merge segment，删除）、`linear.py:111`（删除） |
| `apply_concurrent_updates` | `state/state.py:183` | 零生产调用（已是死代码） |
| `linear.py:110-111`（`result.state_update` 读取 + `apply_state_update` 调用） | `linear.py:110-111` | LinearScheduler 的 declarative delta 应用路径，删除 |

### Channel 系统

| 移除项 | 文件 | 理由 |
|--------|------|------|
| `BaseChannel` / `LastValue` / `ReducerChannel` | `state/channel.py` | `LastValue` 是默认 channel，注解不 accomplish 功能；`ReducerChannel` 零生产使用 |
| `Codec` / `register_codec` / `encode_value` / `decode_value` | `state/channel.py` | `register_codec` 零生产调用；`model_dump(mode="json")` 替代序列化 |
| `_channels` PrivateAttr | `state/state.py:110` | channel dict 容器 |
| `_setup_channels` | `state/state.py:113` | model_validator 构建 channel dict |
| `_sync_fields_to_channels` | `state/state.py:142` | Pydantic 字段 → channel 同步（checkpoint 用） |
| `_sync_channels_to_fields` | `state/state.py:153` | channel → Pydantic 字段同步（apply_state_update 用） |
| `_find_channel_marker` | `state/state.py:49` | `_setup_channels` 的 helper |
| `channel.py` 整个文件 | — | 全部符号无外部消费者（`state_factory.py` 依赖一并移除） |

### state_factory + state_schema

| 移除项 | 文件 | 理由 |
|--------|------|------|
| `StateFactory` ABC | `state_factory.py:201` | `create_state()` = `state_class()`，`restore_state()` = `state_class.model_validate()` — 一层不必要抽象 |
| `SimpleStateFactory` | `state_factory.py:335` | ReAct 用 imperative builder，不经过 factory |
| `DynamicStateFactory` | `state_factory.py:399` | channel-aware class building，channel 移除后无用 |
| `StateRegistry` | `state_factory.py:236` | factory registry，factory 移除后无用 |
| `_resolve_channel` / `_channel_marker_to_string` / `_find_channel_marker` | `state_factory.py` | channel 解析 helper |
| `state_factory.py` 整个文件 | — | 全部符号无外部消费者（`graph_orchestrator.py:415` 改为 `state_class()`） |
| `StateFieldSpec.channel` 字段 | `state_schema.py:52` | channel kind 描述，channel 移除后无用。`StateFieldSpec` 其余字段（name/field_type/default）如保留 declarative GraphSpec 路径则保留 |
| `ReactStateFactory` | `react/state_factory.py` | `SimpleStateFactory(ReActTurnState)` — 直接用 `ReActTurnState` |

### DispatchStore

| 移除项 | 文件 | 理由 |
|--------|------|------|
| `DispatchStore` ABC + `InMemoryDispatchStore` + `SqliteDispatchStore` | `persistence/dispatch_store.py` | 纯审计日志，不参与调度决策/recovery。DeliverStore 覆盖投递状态 |
| `DispatchEvent` | `result.py:56` | DispatchStore 的记录类型，无消费者 |
| `_dispatch_log` property | `parallel.py:134-139` | wrap `DispatchStore.query_all` |
| `query_dispatches_by_target` | `parallel.py:141-150` | wrap `DispatchStore.query_by_target` |
| `_handle_dispatch` 中的 DispatchEvent 创建 + record | `parallel.py:572-578` | 步骤 1（审计），删除后只保留步骤 2（`route_deliver`） |
| **`now_ms()` 提取** | `dispatch_store.py:47` | ⚠️ 被 6 个持久化模块导入（`spec_store` / `instance_store` / `node_state` / `deliver_store` / `node_state_store` / `graph_metadata_store`）。**移除 dispatch_store.py 前必须提取 `now_ms` 到共享位置**（如 `persistence/_time.py`） |

### SUPERSEDED 状态

| 移除项 | 文件 | 理由 |
|--------|------|------|
| `SUPERSEDED` enum 值 | `constants.py`（`InvocationStatus.SUPERSEDED`） | full snapshot 模式下两阶段 rebuild 不必要。suspend 保存 `RUNNING(suspended=True)`，recovery 取 `max(updated_at)` 中的 `{COMPLETED, suspended RUNNING}` |
| `begin_invocation` 中的 SUPERSEDED 标记逻辑 | `persistence_coordinator.py:326-341` | 不再标记 suspended RUNNING 为 SUPERSEDED |
| `rebuild_main_state` 两阶段 apply | `persistence_coordinator.py:596-629` | 改为单次查询 `max(updated_at)`。详见 [ticket 26](26-rebuild-main-state-semantics.md) |

### ReActTurnState 迁移

| 移除项 | 文件 | 理由 |
|--------|------|------|
| `from modex_graph.state import LastValue` | `react/state.py:58` | LastValue 移除 |
| 15 个 `Annotated[T, LastValue]` 注解 | `react/state.py:93-118` | LastValue 是默认 channel，注解无功能。改为普通 `T` |
| `ReActSnapshotPolicy` 中的 `checkpoint()` / `from_checkpoint()` 调用 | `react/state.py:231,249,251,257,269` | 签名不变，实现体变为 `model_dump` / `model_validate`（自动完成） |

### `__init__.py` 导出清理

| 文件 | 清理项 |
|------|--------|
| `modex_graph/__init__.py` | 移除 `BaseChannel` / `LastValue` / `ReducerChannel` / `Codec` / `register_codec` / `JsonValue` / `DispatchStore` / `InMemoryDispatchStore` / `SqliteDispatchStore` / `WriteConflictDetector` / `GenerationWriteTracker` / `NodeResult` / `DispatchEvent` / `InvalidUpdateError` 导出 |
| `state/__init__.py` | 移除 `BaseChannel` / `Codec` / `JsonValue` / `LastValue` / `ReducerChannel` / `register_codec` 导出 |
| `persistence/__init__.py` | 移除 `DispatchStore` / `InMemoryDispatchStore` / `SqliteDispatchStore` 导出 |

## 保留清单

### ParallelScheduler 并发调度核心（不变）

| 保留项 | 说明 |
|--------|------|
| `run_async`（`asyncio.create_task` + `asyncio.wait(FIRST_COMPLETED)`） | 移除 fork 逻辑，所有 instance 共享 `ctx.state` |
| `_ready` set + `_mark_ready` | 不变 |
| `_handle_dispatch`（`ON_RECEIVE` 立即创建 + `ON_ALL_PREDS` 排队） | 删除 DispatchStore record，保留 route_deliver |
| `_recheck_pending` / `_can_reach_active` / `_try_fire_on_all_preds` | 不变 |
| `NodeInstance` + `NodeInstanceStatus` / `SchedulerInstanceStatus` | 移除 `forked_state` / `fork_version` 字段，其余不变 |
| `NodeTrigger` enum | 不变 |
| `_restore_from_recovery` + `_redispatch_from_recovery` | 移除 `_conflict_detector.reset()` 调用，其余不变 |
| `Scheduler` ABC + `SchedulerKind` enum | 不变 |

### LinearScheduler（不变）

| 保留项 | 说明 |
|--------|------|
| `run_async`（顺序执行） | 删除 `:110-111` 的 `state_update` 读取 + `apply_state_update` 调用，其余不变 |

### State 层（简化）

| 保留项 | 简化 |
|--------|------|
| `GraphState`（Pydantic BaseModel） | 移除全部 channel 机制；`checkpoint()` → `self.model_dump(mode="json")`；`from_checkpoint()` → `cls.model_validate(data)` |
| `ctx.state` | 共享——所有 instance 直接读写，imperative 唯一写模式 |
| `resume_target` | 保留在 GraphState 基类（普通 Pydantic 字段） |

### Persistence 层（简化）

| 保留项 | 简化 |
|--------|------|
| `GraphPersistenceCoordinator` | `complete_invocation` 传参从 delta 改为 full snapshot；`rebuild_main_state` 改为单次查询；移除 SUPERSEDED 标记逻辑。**ticket 23 收敛后**：coordinator 退出 lifecycle 路径（6 个 lifecycle 方法移入 NodeStateStore），只保留 deliver 路由 + graph 级 rebuild |
| `Node.run()` 生命周期 | **保持自包含**。`complete` 步骤传参从 `result.state_update` 改为 `ctx.state.model_dump(mode="json")`。`execute()` 返回 `None`（移除 NodeResult）。异常处理不变。**ticket 23 收敛后**：通过 `ctx.node_state_store` 直接调 lifecycle 方法（不再通过 coordinator） |
| GraphInstance + NodeStateStore(invocation 版本链) + Deliver 持久化 | 不变（ticket 23 收敛后 NodeState → NodeStateStore） |

### GraphContext

| 保留项 | 简化 |
|--------|------|
| `GraphContext` | `fork()` 保留原样（node 作者仍可用）。scheduler 停止调用 `fork(state=instance.forked_state)`。`dispatch()` 的 `state_update` 参数保留（是 dispatch payload，不是 NodeResult.state_update — 命名碰撞但不移除）。**ticket 23 收敛后**：新增 `ctx.node_state_store: NodeStateStore` |
| `coordinator` / `current_invocation` / `graph_instance_id` | 不变 |

### 三层持久化（ticket 23 收敛后）

| 保留项 | 说明 |
|--------|------|
| `GraphInstanceStore` | 图实例生命周期（ticket 22 已收敛） |
| `NodeStateStore`（invocation 版本链 + lifecycle） | per-node invocation 版本链 + lifecycle 状态机 + crash recovery。Node.run() 通过 `ctx.node_state_store` 自管理。一个 graph instance 一个 store 实例（不是 per-node）。Null/InMemory/Sqlite 三种持久化策略 |
| `DeliverStore` | per-node 投递积累 + 消费状态机 |

## 移除后的执行模型

```
run_async(ctx):
    recovery = ctx.coordinator.load_for_recovery()
    if has_prior_state: _restore_from_recovery(ctx, recovery)
    else: _init_fresh_state(ctx)
    
    while _ready or running:
        for instance in _ready:
            task = asyncio.create_task(_execute_instance(instance, ctx))  # 无 fork
            running[task] = instance
        
        done = await asyncio.wait(running, FIRST_COMPLETED)
        for task in done:
            task.result()  # 异常传播
    
    return ctx.state

_execute_instance(instance, ctx):
    ctx.set_current_instance(instance_id)
    await ctx.runtime.before_node(ctx, node_name)
    await node.run(ctx, graph=self.graph)  # 共享 ctx.state，无 fork，无 merge segment
    await ctx.runtime.after_node(ctx, node_name)  # 无 result 参数
    instance.status = COMPLETED
    _recheck_pending()
```

**关键变化**：
- `_execute_instance` 不再 fork state——所有 instance 共享 `ctx.state`
- `node.run()` 返回后没有 merge segment——state 已经在共享对象上更新
- `before_node` / `after_node` hook 看到的是共享 state（含其他 instance 的更新）
- `complete_invocation` 在 `Node.run()` 内部调用（**不移动到 scheduler**），存 `ctx.state.model_dump(mode="json")`（full snapshot）
- `after_node` 不再接收 result 参数（`node.execute()` 返回 `None`）

## Node.run() 生命周期（保持自包含）

**注意**：ticket 23 决议 lifecycle 方法移入 `NodeStateStore`。以下伪代码反映 ticket 23 收敛后的调用方式（通过 `ctx.node_state_store`，不通过 coordinator）：

```
Node.run(ctx, graph):
    store = ctx.node_state_store
    
    # begin
    invocation = store.begin_invocation(self.name)
    ctx.current_invocation = invocation
    
    try:
        # integrate
        delivers = ctx.coordinator.collect_consumable_delivers(...)
        ctx.coordinator.mark_delivers_consumed(...)
        integrated = self.input_integrator.integrate(delivers)
        
        # execute (retry loop)
        await self.execute(ctx, integrated)  # 返回 None，imperative 写 ctx.state
        
        # submit
        self.submit(ctx)  # ctx.dispatch(target, state_update={"delivered": ...})
        
        # complete — full snapshot + deliver promotion 解耦
        store.complete_invocation(invocation, ctx.state.model_dump(mode="json"))
        ctx.coordinator.promote_delivers(self.name, invocation.invocation_id)
    
    except GraphInterrupt:
        store.suspend_invocation(invocation, ctx.state.model_dump(mode="json"))
        raise
    except GraphBubbleUp:
        store.cancel_invocation(invocation)
        raise
    except Exception:
        store.crash_invocation(invocation)  # 保存 {}，丢弃 partial state
        raise
    finally:
        store.finalize_invocation(invocation)  # 安全网：orphan → CRASHED
```

## 设计契约

移除 fork 后，并发 instance 共享 `ctx.state`。契约：

1. **imperative 唯一写模式** — `ctx.state.x = y`，无 declarative delta
2. **写不相交约定** — 并发 instance 应写不同字段或用 append-only 集合（如 `list.append()`）。同一字段的并发 read-modify-write 是 node 作者的设计错误，框架不自动检测
3. **asyncio 单线程安全** — 同步段（无 `await`）不交错。`await execute()` 期间其他 instance 可运行，但 state 写入在 `await` 前后是原子的。跨 `await` 的中间状态对其他 instance 可见——node 作者需注意多步修改的原子性
4. **full snapshot 持久化** — `complete_invocation` 存 `ctx.state.model_dump(mode="json")`，`suspend_invocation` 存同一格式。recovery 取 `max(updated_at)` 中的 `{COMPLETED, suspended RUNNING}`
5. **crash 丢弃 partial state** — `crash_invocation` 保存 `{}`。进程 crash 后 recovery 从最新 COMPLETED/suspended snapshot 重建，crashed instance 的 partial writes 正确丢弃
6. **`ctx.dispatch()` 的 `state_update` 参数保留** — 它是 dispatch payload（投递内容 `{"delivered": ...}`），不是 `NodeResult.state_update`（declarative delta）。命名碰撞但不移除

## 对测试的影响

### 完全删除

| 测试文件 | 理由 |
|---------|------|
| `test_parallel_fork.py` | 全部测试 fork-merge 行为 |
| `test_conflict_detector.py` | 全部测试 WriteConflictDetector / GenerationWriteTracker |
| `test_dispatch_store.py` | 全部测试 DispatchStore ABC + 实现 |
| `test_channels.py`（大部分） | 测试 BaseChannel / LastValue / ReducerChannel / Codec |

### 改写（~30+ 站点）

| 改写类型 | 影响 | 方式 |
|---------|------|------|
| `NodeResult(state_update={...})` → imperative 写 | `test_parallel_e2e.py` / `test_parallel_errors.py` / `test_parallel_trigger.py` / `test_routing.py` / `test_node_factory.py` / `test_node_run_lifecycle.py` / `helpers.py` / `test_map_reduce.py` | 改为 `ctx.state.x = y; return None` |
| `complete_invocation(inv, {delta})` → full snapshot | `test_persistence_coordinator.py`（22 站点）/ `test_distributed_persistence_e2e.py`（10 站点） | 改为传 full snapshot dict |
| `ctx.fork(state=...)` → 去掉 state 参数 | `test_scheduler.py`（3 站点）/ `test_parallel_scheduler.py`（2 站点）/ `test_node_run_lifecycle.py`（4 站点） | 改为 `ctx.fork()` |
| `Annotated[T, LastValue]` → 普通 `T` | `helpers.py`（CounterState）/ `test_state_factory.py` / `test_parallel_e2e.py` 等 | 去掉注解 |
| `apply_state_update(...)` / `apply_concurrent_updates(...)` 调用 | `test_state_factory.py` / `test_channels.py` / `test_parallel_fork.py` | 删除或改为 imperative |
| `assert result.state_update == {...}` → `assert ctx.state.x == ...` | `test_node_run_lifecycle.py:139,143` 等 | 改为断言 ctx.state |

### 设计差距（测试揭示，新契约需覆盖）

| 测试 | 揭示的差距 | 新契约的覆盖 |
|------|-----------|-------------|
| `test_parallel_fork.py::TestForkIsolationMultiWrite::test_two_concurrent_writes_different_lastvalue_fields_succeeds` | 并发写不同字段应成功 | 写不相交约定契约 #2 |
| `test_parallel_errors.py::test_exception_does_not_corrupt_main_state` | fork 保护 main_state 不被失败 batch 部分修改 | 无 fork 后 partial writes 泄漏到共享 state——契约 #5 crash 丢弃 + 契约 #3 文档化 |

## 对 GraphMetadata bookkeeping 字段的影响

ticket 22 已确认 4 字段全保留（ParallelScheduler 保留）。ticket 33 不改变 bookkeeping 字段。

| 字段 | 来源 | 是否保留 |
|------|------|---------|
| `instance_seq` | ParallelScheduler 的 instance 序号 | 保留 |
| `iteration_count` | 两个 scheduler 都用（max_iterations 安全网） | 保留 |
| `activated_sources` | ParallelScheduler ON_ALL_PREDS bookkeeping | 保留 |
| `pending_dispatches` | ParallelScheduler ON_ALL_PREDS bookkeeping | 保留 |

## 风险

1. **`complete_invocation` 传参变更**：从 `result.state_update`（delta，生产 node 为 `{}`）改为 `ctx.state.model_dump()`（full snapshot）。COMPLETED 记录的 `state_json` 从 `{}` 变为完整状态——存储增长从 O(N * delta) 到 O(N * S)。对 agent 框架（N 通常小，S 可大但对话常被压缩）可接受。如未来需压缩，可加 snapshot 频率控制。

2. **`now_ms()` 提取**：dispatch_store.py 删除前必须先提取 `now_ms` 到共享位置，否则 6 个持久化模块 import 断裂。

3. **`state_factory` 移除影响 GraphSpec 路径**：`GraphSpec.state_schema: StateSchema | str` 需改为 `state_class: type[GraphState] | str`。`GraphSpecCompiler._resolve_state_factory` 需重写。`GraphOrchestrator._create_state` 改为 `state_class()`。ReAct 不受影响（用 imperative builder）。

4. **测试迁移量大**：~30+ 测试站点需从 declarative 改为 imperative。这是机械式工作但量不小。

5. **`InvalidUpdateError` 移除需确认**：移除 `LastValue` 和 `WriteConflictDetector` 后，`InvalidUpdateError` 零 raiser。需确认没有外部代码 catch 这个异常。
