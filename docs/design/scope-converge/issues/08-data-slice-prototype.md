# 08 原型：数据层纵向切片

Labels: wayfinder:prototype
Status: closed (2026-08-18 — 不需要 prototype)
blocked-by: 01
spec: SPEC.md §10

## Question

做一个最小切片，让 01 的决定变成看得见、可反驳的实物：一个插件登记的带类型变量，真实流过一个 turn——写入（校验）→ 读取 → 注入系统提示 → 持久化 → 会话恢复后还在。

产出用 /prototype 技能做的廉价切片，用来确认或推翻 01 选的形态，再推广到 02/03 的整体设计。

## Comments

### 依赖 (2026-08-18)

完整设计见 `docs/design/scope-converge/SPEC.md` §10 数据层。08 验证 01 的 KVStore + 类型命名空间层决议，独立于整体装配架构。prototype 通过后，DATA_NAMESPACE 槽位（02 决议）与图 state schema 类型共用（SPEC §8.3 原则 4）均依赖此验证。

### Resolution (2026-08-18 — 不需要 prototype)

**08 不需要单独做 prototype。** 理由：

- KVStore 是已有基础设施（MemoryStoreBundle 成员，FILE/SQLite 双后端，RecordScope 解析，get/set/list_keys/delete 全有），已在生产中跑（session 簿记 + Core Memory 三文件），不需要验证它能工作。
- 类型命名空间层是薄新增（`dict[str, type[BaseModel]]` 注册表 + 写校验 + `resolve_bundle` 一等访问面），技术风险几乎为零。
- 01 决议明确"v1 只做变量面"，系统提示注入不是 01 范围——缩小后 08 = 验证 KVStore get/set + 薄注册表。
- 实现时用 TDD 验证即可，不需要前置 prototype。
