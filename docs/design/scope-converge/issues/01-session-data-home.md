# 01 会话数据的家：跟参考项目的事件日志，还是加"带类型的变量仓"？

Labels: wayfinder:deliberate
Status: closed (resolved 2026-08-18)

## Question

参考项目里会话数据全是一条 append-only 的事件流水账，插件加新数据类型 = 登记新事件类型，UI / 回放 / 上下文投影都自动认识它。

我们当前是"消息列表 + memory 分层"。两条路：

**A. 跟参考项目**：会话存储改为事件流水账。一步拿到参考项目的全部开放性，但是重写级改动——transcript / 回放 / 压缩 / 治理全部要适配，且会话存储是所有模块的公共依赖，很难按"模块内一次到位"的纪律分步切。

**B. 不跟**：会话存储不动，新建一个"带类型的变量仓"——插件用 Pydantic 模型登记自己的数据类型，按 turn / session / workspace / global 四个作用域读写，持久层复用现有 persistence。开放性拿到大头，改动小一个数量级；代价是 transcript 与变量仓两份数据需要各自消费。

若选 B，还要定：变量仓的作用域边界、持久层落点、写入校验边界，以及 `runtime.state` 和 `TurnCustomKey` 两处旧裸袋如何收编进来（消灭第二个无 schema 袋子的先例）。

## Comments

### Resolution (2026-08-18, Sisyphus)

**决议：收敛现有面——不新建存储，不动会话记忆，不碰 transcript。**

1. **插件持久数据的家 = 现有 KVStore**（`MemoryStoreBundle` 成员，经 `MemoryStoreRegistry.resolve` 按 `RecordScope` 解析，FILE/SQLite 双后端现成）。参考项目取证印证：参考项目插件持久状态走存储域（命名空间 + 类型化设施），不进事件流——与本方案同构。
2. **框架唯一新增：类型命名空间层**——`命名空间 → Pydantic model` 注册表，写入校验，类型化读取。取证发现插件今天拿到 scoped bundle 需拼三个低层件（`register_memory_system_modifier` → `memory_system.store_registry.resolve(...)`），无一等 API——命名空间层必须含 `resolve_bundle` 式一等插件访问面。
3. **作用域沿用现有 RecordScope**（session / user / global）；turn 级临时数据留在 `runtime.state` 不动；`TurnCustomKey` 保持框架内部闭集。
4. **v1 只做变量面**；transcript 是呈现层（已验证 native agent 的 WebUI 历史也读 MessageStore），事件面开放留雾区。

**验证记录**（探索 agent 源码级确认）：
- 上下文链路 VERIFIED：`ReActTurnRunner.execute_turn` → `MemorySystemContextManager.load()`（memory/system.py:346）→ `ScopedMessageHistory`（写穿 `ScopedSessionMemoryManager.add_messages` → `bundle.messages.append_message`，layers/session.py:53-79）→ FILE（`DefaultScopedStorage`，messages.jsonl/kv.json）或 SQLITE（`SqliteMessageStore`，memory_session_messages 表，scope_key=canonical JSON）。
- bot_project 接线 VERIFIED：`build_pool_data`（bot/workspace/pool_data.py:100-115）→ `create_memory(store_registry=...)`；`PersistenceBackend` 默认 SQLITE（persistence/config.py:41）。TranscriptStore 仅 UI 回放 + EXTERNAL agent 兜底（bot/control/facade.py:179-206）。
- KVStore 现状 PARTIAL：真实客户 = session 簿记（.last_write_id/.last_activity）+ Core Memory 三文件内容（layers/core.py）；docstring 的 "plugin data" 零消费者（本决议使其成真），"archive state"/"scope metadata" 说法过时（各有独立存储）。
- RecordScope 键 VERIFIED：SQLite=canonical JSON（BotRecordScope 含 pool/workspace_id/__scope_type__）；FILE=`to_path_segment` 路径段（None→"default"）。
