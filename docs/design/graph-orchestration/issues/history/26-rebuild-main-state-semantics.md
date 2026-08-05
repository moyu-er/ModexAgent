# rebuild_main_state 简化

Status: triage:closed
Blocked by: none (was: [Fork-merge 依赖链移除](33-fork-merge-removal.md))
Resolved: 2026-08-04

## Question

ticket 33 移除 fork-merge 依赖链（channel / declarative delta / conflict detection），保留 ParallelScheduler 并发调度。

**之前的三个 loss**：
1. Reducer 语义丢失 — `dict.update` 不 fold
2. Imperative mutations 丢失 — `state_json` 存 delta，不含 imperative writes
3. Commit order 不确定 — `invocation_id` 是 begin-time 序

## Resolution

### 三个 loss 全部消失

- **loss 1（Reducer 语义）**：`ReducerChannel` 移除，不存在 reducer。imperative 模式下 `ctx.state.items.append(x)` 直接操作 list 字段。
- **loss 2（Imperative mutations）**：`complete_invocation` 存 `ctx.state.model_dump(mode="json")`（full snapshot），捕获所有 imperative 写。
- **loss 3（Commit order）**：见下方论证。

### commit order 论证

ParallelScheduler 下：instance A `begin_invocation`（invocation_id=X）→ `await execute()` → instance B `begin_invocation`（invocation_id=Y, Y>X）→ `await execute()` → B 的 execute 先返回 → B `complete_invocation`（updated_at=T_B）→ A 的 execute 返回 → A `complete_invocation`（updated_at=T_A, T_A > T_B）。

`invocation_id` 序（X<Y）与 commit 序（B 先 A 后）不一致。**但** A 的 snapshot 在 A complete 时拍摄（T_A > T_B），包含 B 的结果（共享 state）。所以 A 的 snapshot 是最新的。

**排序用 `updated_at`（完成时间），不是 `invocation_id`（开始时间）**。因为完成顺序才反映 state 的累积顺序。`updated_at` 是 `save_invocation` 时记录的 epoch ms（`now_ms()`），在 `complete_invocation` / `suspend_invocation` 调用时写入。

### SUPERSEDED 移除

**之前为什么需要 SUPERSEDED**：COMPLETED 记录存 `state_update` delta（对生产 node 是 `{}`），SUPERSEDED 记录存 `ctx.state.checkpoint()`（full snapshot 含 imperative 写如 `resume_target`）。两阶段 apply（COMPLETED 先 → SUPERSEDED 后）确保恢复时 `resume_target` 可见。

**为什么现在不需要**：`complete_invocation` 存 full snapshot 后，每条 COMPLETED 记录都是完整状态。suspend 保存 `RUNNING(suspended=True)` + full snapshot（不再标记 SUPERSEDED）。recovery 取 `max(updated_at)` 中的 `{COMPLETED, suspended RUNNING}` — 单次查询，最新 snapshot 包含一切。

### 目标设计

#### `complete_invocation` 的 `state_json` 变化

从 `state_json = NodeResult.state_update`（delta，生产 node 为 `{}`）改为 `state_json = ctx.state.model_dump(mode="json")`（full snapshot）。

`complete_invocation` 调用点在 `Node.run()` 内部（node.py:311-313），保持自包含生命周期。只改传参，不移动调用点。

#### `rebuild_main_state` 逻辑

```
1. 查询所有 node 的 {COMPLETED, suspended RUNNING} 记录
2. 按 updated_at 降序排序（完成时间序，不是 invocation_id 开始序）
3. 取第一条（最新）的 state_json
4. 返回 dict
```

单次查询，无两阶段 apply，无 SUPERSEDED。

#### edge case 处理

| 场景 | 最新记录 | rebuild 结果 | 正确性 |
|------|---------|-------------|--------|
| 正常完成 | COMPLETED（full snapshot） | 该 snapshot | ✅ 包含所有历史 |
| suspend 后未 resume | RUNNING(suspended=True)（full snapshot） | 该 snapshot | ✅ 含 resume_target |
| suspend 后 resume 完成 | COMPLETED（full snapshot，含 resume 期间的所有写） | 该 snapshot | ✅ |
| suspend 后 resume 中 crash | resume 的 CRASHED 记录被跳过，取旧的 suspended RUNNING | 旧 snapshot | ✅ 从 suspend 点恢复 |
| 正常完成后 crash | CRASHED 被跳过，取最后 COMPLETED | 最后 COMPLETED snapshot | ✅ 丢弃 partial writes |
| 无任何 COMPLETED/suspended | 空 | `{}` | ✅ fresh start |

#### `updated_at` 碰撞处理

asyncio 单线程下 `save_invocation` 调用是串行的（同步方法，无 await），`now_ms()` 返回值应唯一。如极端情况碰撞，加 `invocation_id` 作为 tiebreaker（`ORDER BY updated_at DESC, invocation_id DESC LIMIT 1`）。

### 对 recovery 流程的影响

`load_for_recovery` 返回 `RecoveryContext`：
- `rebuilt_main_state`：从 `rebuild_main_state()` 获取（现在是单条最新 snapshot）
- `node_states`：每个 node 的最新 invocation 记录（用于 `_redispatch_from_recovery` 调度决策）
- `_redispatch_from_recovery`：COMPLETED 检查 pending delivers，suspended RUNNING → resume re-dispatch，CRASHED → fresh re-dispatch

> **2026-08-04 修订（ticket 32 闭环后）**：「COMPLETED 检查 pending delivers」捷径已被删除——recovery 改为「从 deliver 层 PENDING 记录重建 pending 队列 + `_recheck_pending` 正常门控点火」（见 [ticket 32](32-scheduler-bookkeeping-persistence.md)）；status 驱动 re-dispatch 只管 CRASHED / suspended-RUNNING。

### suspend 不再标记 SUPERSEDED

`begin_invocation` 在发现 prior RUNNING(suspended=True) 时：
- **之前**：标记为 SUPERSEDED（保存 SUPERSEDED + same state_json + suspended=True），然后创建新 invocation
- **之后**：不标记，直接创建新 invocation。旧记录保持 RUNNING(suspended=True)

`Node.run()` 的 resume 检查（`store.load_latest(self.name)` 在 `begin_invocation` 之前调用，ticket 23 收敛后经 `ctx.node_state_store`）不受影响 — 它读取最新的 suspended 记录作为 integrate input，这个时序不变。

`finalize_invocation` 的 SUPERSEDED 跳过逻辑（`:561`）变为：suspended RUNNING 不被 finalize 触碰（已有逻辑 `:549` 跳过 suspended）。

## Resolution criteria

- ✅ `complete_invocation` 存 full snapshot 的接口变化（传参从 `result.state_update` 改为 `ctx.state.model_dump(mode="json")`，签名不变）
- ✅ `rebuild_main_state` 简化后的逻辑（单次查询 `max(updated_at)` 中的 `{COMPLETED, suspended RUNNING}`）
- ✅ 共享 state 下 `updated_at` 序等价于 commit 序的论证
- ✅ SUPERSEDED 移除的论证（full snapshot 后两阶段 apply 不必要）
- ✅ 对 recovery 流程的影响（`load_for_recovery` + `_redispatch_from_recovery` 逻辑不变，`rebuild_main_state` 简化）
