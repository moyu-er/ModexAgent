# 13 — DeliverStore ABC 演进 + DeliverRecord 演进

**What to build:** DeliverStore ABC 从 graph 级统一管理(accumulate/query_pending/mark_submitted)演进为 per-node 消费状态机(accumulate 新签名/query_consumable/mark_consumed/promote_consumed)。DeliverRecord 加 source/source_invocation_id/consumed_by 字段 + status 用 enum。移除 DeliverConsumer ABC(I10)。旧方法保留(expand),待 contract 阶段移除。

**Blocked by:** 12 — 持久化类型定义与 enum 拆分(依赖 DeliverConsumptionStatus enum)

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §14.2, §14.3, §14.4, §14.5

## 交付内容

### DeliverStore ABC 演进(§14.2)
- 新方法:`query_consumable(graph_instance_id, target_node) -> list[DeliverRecord]`
- 新方法:`mark_consumed(deliver_ids, consumed_by_invocation_id) -> None`
- 新方法:`promote_consumed(consumed_by_invocation_id) -> None`
- `accumulate` 新签名:keyword-only,加 `source_node: str` + `source_invocation_id: int` 参数
- 旧方法保留(expand):`query_pending` / `query_by_target` / `mark_submitted` — 待 contract 阶段移除
- `clear(graph_instance_id) -> None`:保留(SQLite 可选批量删除)

### DeliverRecord 演进(§14.4)
- 加 `source_node: str`
- 加 `source_invocation_id: int`
- 加 `consumed_by_invocation_id: int | None`
- `status`: 从 raw str → `DeliverConsumptionStatus` enum(I12)
- 旧字段 `node_name` / `next_node` → 保留(expand),待 contract 阶段移除

### 移除 DeliverConsumer ABC(§14.3, I10)
- 不引入 `DeliverConsumer` ABC + `DefaultDeliverConsumer`
- 消费逻辑将在 ticket 15 作为 coordinator 方法实现
- rule 6 合规:只有一个实现是 hypothetical seam;DeliverStore ABC 保留(三实现 — 真实 seam)

## Acceptance criteria

- [ ] DeliverStore ABC 有 query_consumable / mark_consumed / promote_consumed 三个新 abstract 方法
- [ ] accumulate 新签名是 keyword-only,含 source_node + source_invocation_id
- [ ] 旧方法(query_pending / query_by_target / mark_submitted)仍保留 — 现有实现编译通过
- [ ] DeliverRecord 有 source_node / source_invocation_id / consumed_by_invocation_id 字段
- [ ] DeliverRecord.status 类型是 DeliverConsumptionStatus enum(非 raw str)
- [ ] 无 DeliverConsumer ABC / DefaultDeliverConsumer 类(rule 6)
- [ ] mypy clean
- [ ] 现有 DeliverStore 实现(InMemoryDeliverStore / SqliteDeliverStore)仍编译(旧方法保留)
- [ ] 现有测试全绿
