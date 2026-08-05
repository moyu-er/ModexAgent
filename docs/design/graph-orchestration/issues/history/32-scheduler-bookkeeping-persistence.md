# Scheduler bookkeeping 持久化

Status: triage:closed (resolved 2026-08-04)
Blocked by: none

## Question

ticket 22 让 `GraphInstanceStore` 能存 `bookkeeping_json`。但 NOBODY 写这些字段。scheduler 在内存里更新 `_iteration_count` / `_instance_seq` / `_activated_sources` / `_pending_dispatches`（`parallel.py:519, 379, 604-607`），但从不持久化。

ticket 33 保留 ParallelScheduler 的并发调度核心，所以 bookkeeping 字段全部保留：`instance_seq` / `iteration_count` / `activated_sources` / `pending_dispatches`。

### ticket 33 影响确认（2026-08-04 决议后）

ticket 33 的决议**不影响本 ticket**：

- **DispatchStore 移除**：DispatchStore 是调度事件审计日志（记录"谁 dispatch 给了谁"），与 bookkeeping 字段无关。`_activated_sources` / `_pending_dispatches` 是 ParallelScheduler 内部内存状态（`_handle_dispatch` 直接管理），不依赖 DispatchStore。
- **SUPERSEDED 移除**：SUPERSEDED 是 `InvocationStatus` 的值，与 bookkeeping 字段无关。
- **fork-merge 移除**：fork / merge segment 是 `_execute_instance` 内部逻辑，不影响 bookkeeping 字段的值或持久化需求。
- **channel / state_factory 移除**：与 scheduler bookkeeping 无关。

## Resolution（2026-08-04 grilling 裁决）

### 裁决：零持久化——三层体系已足够

Scheduler bookkeeping 四字段不包含任何新信息——它们是三层持久化体系已有数据的**运行时视图**，不是事实源：

| 字段 | 事实源 | 恢复方式 |
|------|--------|----------|
| `pending_dispatches` | deliver 层 PENDING 记录（每个进 pending 队列的 dispatch 必留 deliver 记录——唯一例外是 dispatch→END，而 END 两边都不进） | recovery 时 scan PENDING 记录，按 target/source 分组重建 |
| `activated_sources` | 与 `pending_dispatches` 的 keys 恒等冗余（同点写入 `parallel.py:604-607`，同点弹出 `701-702`） | 重建 pending 时白得 |
| `iteration_count` | node 层 COMPLETED invocation 计数（一实例完成 = 一条 COMPLETED 记录，1:1） | 派生。微秒级 crash 窗口 off-by-one，方向保守（安全网提前一轮触发）——对安全网而言保守即正确方向 |
| `instance_seq` | 无事实源，也无人需要（`instance_id` 是纯内存临时量；ticket 33 删 DispatchStore 后连审计消费者都没了） | 重置为 0 |

「谁写、何时写、写频率」问题整体消失——**没有第四类需要持久化的信息**。持久化 `pending_dispatches` 等于把 deliver store 的内容复制第二份——ticket 33 删 DispatchStore 时裁决过的双份事实反模式。

### 完备性压力测试（边界 case，explore 验证）

- **END 投递**：deliver 层与 pending 队列两边都不进，无信息缺口 ✅
- **外部 deliver**（`GraphControlService` 的 `__external__`）：走 `route_deliver`，正常落 deliver 层 ✅
- **crash 于「`_try_fire` 清空队列」与「`begin_invocation`」之间**（孤儿 PENDING）：重建逻辑从 PENDING 记录恢复队列 + `_recheck_pending` 点火——**顺带修复现存 bug**（今天这个窗口的 delivers 会被孤儿化：node 无 invocation 记录 → status 驱动路径 `record is None → continue` 跳过）
- **ON_RECEIVE target 的 PENDING 记录**：不进 pending 队列，重建时直接触发点火——与 [ticket 31](31-state-machine-cas-thread-safety.md) 的 ON_RECEIVE TODO 联动（fog）
- **LinearScheduler**：无此四字段，[ticket 24](24-linearscheduler-recovery-contract.md) 的 recovery 不依赖 bookkeeping ✅

### Recovery 重建（唯一新增代码，实现侧）

`_restore_from_recovery` 新增约 15 行：scan 全部 PENDING deliver 记录 → 按 trigger mode 过滤（ON_ALL_PREDS 进 pending 队列；ON_RECEIVE 直接点火）→ 分组重建 → `_recheck_pending`。同时 `_redispatch_from_recovery` 的 COMPLETED+delivers 捷径被「重建 + recheck」覆盖而删除——顺带修复其绕过 ON_ALL_PREDS 门控的现存 bug；status 驱动 re-dispatch 只管 CRASHED / suspended-RUNNING。

**重建必须无条件执行**（2026-08-04 一致性审计补齐）：不能挂在 `has_prior_state`（`any(node_states)`)门上——「只有 PENDING delivers、无任何 invocation 记录」正是「crash 于 `_try_fire` 与 `begin_invocation` 之间」的孤儿场景，此时 `node_states` 为空、门会跳过重建，delivers 被孤儿化。恢复入口的语义改为：restore/rebuild 逻辑总是运行，空数据即自然退化为 fresh start。

### 连带后果（级联修订）

1. **ticket 22**：`bookkeeping_json` 列作废——最终 schema = 纯列存储；`GraphMetadata` 修剪 4 个 bookkeeping 字段
2. **ticket 28**：执行计划吸收本 ticket 的修剪 + recovery 重建工作项（并补齐 22/24/29/31 的实现项——原 14 步只覆盖 33/23）
3. `GraphStateSnapshot`（前端查询，fog）：bookkeeping 字段如需展示，查询时按需派生，不经持久化
4. `graph_orchestrator.py` 构造 `GraphMetadata` 时的 4 字段初始化随之删除

## Resolution criteria

- ✅ 哪些字段持久化，哪些重建 — **零持久化**：pending/activated 重建，iteration_count 派生，instance_seq 重置
- ✅ 持久化机制（coordinator 接口） — 不需要，无写入路径
- ✅ 持久化时机 — 不需要
- ✅ 与 ticket 22 的 `bookkeeping_json` schema 一致 — **作废该列**（级联修订 ticket 22）
