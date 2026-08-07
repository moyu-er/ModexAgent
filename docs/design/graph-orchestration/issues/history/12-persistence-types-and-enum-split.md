# 12 — 持久化类型定义与 enum 拆分

**What to build:** 分布式持久化的全部基础类型和 ABC 定义 — 数据记录、值对象、枚举、持久化接口。这是整个增量实现的地基:所有下游 ticket 依赖这些类型。纯类型定义,不改现有行为,现有测试全绿。

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §4.1-§4.9, §3.5, §14.4

## 交付内容

### 数据记录(frozen Pydantic)
- `NodeInvocationRecord`(§4.2): invocation_id, graph_instance_id, node_name, version, parent_version, status(InvocationStatus), state_json, **suspended: bool = False**(F4: 显式标记 suspended RUNNING), created_at, updated_at
- `GraphMetadata`(§4.3): graph_instance_id, spec_id, parent_instance_id, parent_node, status, instance_seq, iteration_count, activated_sources, pending_dispatches
- `DeliverRecord`(§14.4): deliver_id, graph_instance_id, target_node, source_node, source_invocation_id, content, status(DeliverConsumptionStatus), consumed_by_invocation_id, created_at, updated_at

### 值对象(frozen Pydantic)
- `InvocationContext`(§4.8): invocation_id, node_name, version, parent_version
- `RecoveryContext`(§4.9): metadata, node_states, **rebuilt_main_state: dict[str, Any]**(I9)
- `GraphStateSnapshot`(§2.4): metadata, nodes

### 枚举(I22 拆分 + I4 + I12)
- `SchedulerInstanceStatus`: DORMANT, READY, RUNNING, COMPLETED
- `InvocationStatus`: PENDING, RUNNING, COMPLETED, CANCELED, CRASHED, **SUPERSEDED**(I4)
- `DeliverConsumptionStatus`: PENDING, CONSUMED, CONSUMED_PENDING, CONSUMED_COMPLETED(I12)
- 移除 `NodeInstanceStatus`(拆分为上述两个 enum)

### ABC 接口(演进,新接口与旧共存 — expand 阶段)
- `NodeState` ABC(§4.1): save_invocation / load_invocation / load_latest / load_latest_completed / query_versions
- `GraphMetadataStore` ABC(§4.6): save / load / update_status
- `NodeStateFactory` ABC(§4.7): create() -> NodeState
- `DeliverStoreFactory` ABC(§14.5): create() -> DeliverStore(**F11: required 类型,非 Optional**)

## Acceptance criteria

- [ ] 所有类型定义为 frozen Pydantic(`ConfigDict(frozen=True, extra="forbid")`)或 StrEnum
- [ ] NodeInvocationRecord 含 `suspended: bool = False` 字段(F4)
- [ ] RecoveryContext 含 `rebuilt_main_state: dict[str, Any]` 字段(I9)
- [ ] InvocationStatus 含 SUPERSEDED 值(I4)
- [ ] SchedulerInstanceStatus 与 InvocationStatus 是独立 enum(I22 拆分)
- [ ] DeliverConsumptionStatus 是 StrEnum,含 4 个值(I12)
- [ ] NodeState ABC 新接口定义完整(save_invocation / load_invocation / load_latest / load_latest_completed / query_versions)
- [ ] DeliverStoreFactory ABC 的 create() 返回 DeliverStore(非 Optional)
- [ ] 旧 NodeInstanceStatus enum 保留(expand,待 contract 阶段移除)
- [ ] mypy clean(`mypy src/modex_graph`)
- [ ] 新 ABC 不可直接实例化(abstract)
- [ ] 现有测试全绿(`pytest tests/unit/ -v`),无行为变更
