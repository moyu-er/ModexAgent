# 14 — Null/Memory/SQLite 三种持久化实现

**What to build:** 为 NodeState / GraphMetadataStore / DeliverStore 三个 ABC 各实现 Null / Memory / SQLite 三种策略。每种实现走同一 ABC 接口,能力边界不同(Null no-op / Memory 单次流程 / SQLite 持久化 + crash recovery)。包含 SQLite schema 迁移和共享 connection 优化。

**Blocked by:** 12 — 持久化类型定义与 enum 拆分;13 — DeliverStore ABC 演进(依赖新 ABC 接口)

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §4.5, §4.6, §4.7, §4.11, §14.6

## 交付内容

### NodeState 三实现
- `NullNodeState`: 全 no-op(save/load 返回 None / 空列表)
- `SimpleNodeState` 演进: 从当前 read/write/snapshot/restore → 新接口(memory dict 实现)
- `SqliteNodeState` 演进: 新接口 + parent_version/status/suspended 字段 + schema 迁移

### GraphMetadataStore 三实现
- `NullGraphMetadataStore`: 全 no-op
- `MemoryGraphMetadataStore`: dict 实现
- `SqliteGraphMetadataStore`: SQLite 实现(graph_instances 表)

### NodeStateFactory 三实现
- `NullNodeStateFactory` / `SimpleNodeStateFactory` / `SqliteNodeStateFactory`

### DeliverStore 三实现(§14.6)
- `NullDeliverStore`: in-memory queue(无状态机,用于 ReActAgent per-turn)
- `InMemoryDeliverStore`: 二态(PENDING/CONSUMED),promote_consumed = 删除已消费记录
- `SqliteDeliverStore`: 三态(PENDING/CONSUMED_PENDING/CONSUMED_COMPLETED),promote_consumed = 升级状态

### DeliverStoreFactory 三实现(I14)
- `NullDeliverStoreFactory` / `InMemoryDeliverStoreFactory` / `SqliteDeliverStoreFactory`
- `SqliteDeliverStoreFactory` 接受共享 `sqlite3.Connection` 参数(避免 2N connection 增殖)

### SQLite schema 迁移(§4.11, I13)
- node_states 表: ALTER TABLE ADD COLUMN parent_version / status / suspended(幂等,PRAGMA table_info 检查)
- 加索引: idx_node_states_latest / idx_node_states_status / idx_node_states_cross / idx_node_states_global(invocation_id 排序,I5)
- status CHECK 约束含 'superseded'(I4)

## Acceptance criteria

- [ ] NullNodeState + NullGraphMetadataStore + NullDeliverStore 全 no-op(或 in-memory queue for NullDeliverStore)
- [ ] SimpleNodeState 实现新 ABC 接口(save_invocation/load_invocation/load_latest/load_latest_completed/query_versions)
- [ ] SqliteNodeState 实现新 ABC 接口 + parent_version/status/suspended 字段
- [ ] schema 迁移幂等(运行两次不报错,PRAGMA table_info 检查列存在)
- [ ] InMemoryDeliverStore 二态(PENDING/CONSUMED),promote 删除
- [ ] SqliteDeliverStore 三态,promote 升级 CONSUMED_PENDING → CONSUMED_COMPLETED
- [ ] NullDeliverStore in-memory queue,query_consumable 返回所有记录
- [ ] SqliteDeliverStoreFactory 接受共享 connection 参数(I14)
- [ ] 每种实现 CRUD 测试通过
- [ ] round-trip 测试(save → load → compare)
- [ ] Null 确认 no-op
- [ ] schema 迁移幂等测试
- [ ] mypy clean;现有测试全绿
