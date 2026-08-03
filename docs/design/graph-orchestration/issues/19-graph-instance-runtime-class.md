# 19 — GraphInstance 演进为运行时 class

**What to build:** GraphInstance 从 frozen Pydantic 数据记录演进为普通 class 持有 coordinator + GraphMetadata(可序列化值对象)+ 可扩展字段。GraphInstanceStore → GraphMetadataStore 演进。~33 callers 更新。这是 wide refactor(expand-contract: GraphMetadata 已在 ticket 12 定义为 expand,此处 contract — GraphInstance 演进为持有它的运行时 class)。

**Blocked by:** 17 — CheckpointData 移除 + scheduler 集成(依赖 scheduler 接入 coordinator,Node.run 新签名)

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §7.2, §13.1, §13.2, C2

## 交付内容

### GraphInstance 演进(C2)
- 从 `class GraphInstance(BaseModel)` with `ConfigDict(frozen=True, extra="forbid")` → 普通 class
- 持有: `metadata: GraphMetadata`(可序列化值对象)+ `coordinator: GraphPersistenceCoordinator` + 可扩展字段
- 方法: `get_state() -> GraphStateSnapshot`(委托 coordinator.get_graph_state)
- 方法: `load_for_recovery() -> RecoveryContext`(委托 coordinator.load_for_recovery)
- 方法: `update_status(status: GraphInstanceStatus)`(委托 metadata + GraphMetadataStore.update_status, A7)
- 属性: `graph_instance_id` / `status`(委托 metadata)

### GraphInstanceStore → GraphMetadataStore 演进
- store 存 `GraphMetadata`(可序列化),不存运行时 `GraphInstance`
- `InMemoryGraphInstanceStore` → `InMemoryGraphMetadataStore`(dict)
- `SqliteGraphInstanceStore` → `SqliteGraphMetadataStore`(graph_instances 表,同 schema)
- GraphInstance 在内存中由 GraphOrchestrator 构造(metadata + coordinator),不序列化

### callers 更新(~33 处)
- 大多访问 `graph_instance.graph_instance_id` / `graph_instance.status` → 委托 metadata,不改
- 直接构造 GraphInstance 的地方(GraphOrchestrator / GraphRecoveryService / 测试)→ 改为构造 GraphMetadata + coordinator → GraphInstance(metadata, coordinator)

## Acceptance criteria

- [ ] GraphInstance 是普通 class(非 frozen Pydantic),持有 metadata + coordinator
- [ ] GraphInstance.graph_instance_id / .status 委托 metadata(属性)
- [ ] GraphInstance.get_state() / load_for_recovery() / update_status() 方法定义完整
- [ ] GraphInstanceStore 演进为 GraphMetadataStore(存 GraphMetadata,不存 GraphInstance)
- [ ] InMemoryGraphMetadataStore + SqliteGraphMetadataStore 实现完整
- [ ] 所有 callers 更新(GraphOrchestrator / GraphRecoveryService / 测试)
- [ ] 访问 graph_instance_id / status 的 callers 不需改(委托 metadata)
- [ ] mypy clean
- [ ] 现有测试更新后全绿
