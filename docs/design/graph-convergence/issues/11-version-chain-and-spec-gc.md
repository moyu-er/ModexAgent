# 11 — 版本链与 spec GC 最小落地

Status: closed
Labels: wayfinder:task
Assignee: GYT
Blocked-by: (无 — frontier)

## Question

两个 ADR-0040 显式 deferred 的清理项,按"收敛优先、最小落地"原则定稿并实现:

1. **孤儿 spec GC**:spec 不可变化后,历史 spec 行(无 instance 引用且非同名最新)只增不减。定稿回收机制 — 候选:启动时 sweep(与 `GraphSpecLoader` 的 stale 删除对称)、按需(保存新版本时清理无引用旧版)、或明确"当前规模不做,仅记录判断阈值"。若实现,注意 instance 的 `spec_id` 引用检查需跨 `GraphInstanceStore` 查询。
2. **版本链查询优化**(`superseded` 字段 + 部分索引):ADR-0040 定的 revisit 条件是"千级版本/实例"。先核实当前量级与查询路径(`load_latest`/`query_versions` 的索引使用),若远未触发则**明确不做**,在 ADR-0040 deferred 段落记录判断依据;若触发则按 ADR 预案实现。

产出:两项各自"做了/明确不做+依据"的定稿,做了的部分含实现与测试。

## Comments

**决议(2026-08-15 关闭):两项均明确不做,触发条件记录。**

**明确不做(避免误导)**:
1. **版本链查询优化(superseded 字段+部分索引)**:不做。依据:版本数=崩溃重试次数(个位量级);07 退役 state_json/suspended 列后 node_states 行缩为纯生命周期元组,查询负载结构性下降 — 判断依据比 ADR-0040 当时的"千级版本"更远离触发。触发条件:单实例版本链达百级以上时重估(ADR-0040 deferred 段落补记归 13 票)。
2. **孤儿 spec GC**:不做。依据:累积速率=人工编辑次数(人类尺度);GraphSpecLoader 已对称处理磁盘↔store 删除;pre-release 无生产数据;GC 错删(跨 loader 引用/审计链断裂)代价 > 文本存储代价。触发条件:spec 行数千级,或首次对外发布前(届时启动 sweep 与 on-save 清理二选一)。
