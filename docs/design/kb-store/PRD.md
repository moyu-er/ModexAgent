# KB Store — 知识库存储与检索

Status: implemented (docs synced to v3 code)

## Destination

为 bot_project 业务层提供可拔插的知识库 (KB) 能力:支持 CRUD + 全文搜索,可按多维隔离(taskId / sessionId / category 等)过滤,不传过滤维度时全局搜索。KB 作为 tool 体系提供给 agent(像 SendFileToUserTool / ExperienceTool 一样注册),同时提供 `modexctl kb` CLI 命令供外部 agent 使用。

**持久化与检索完全解耦**: `KbPersistence(ABC)` 负责数据存储 + CRUD,`KbRetriever(ABC)` 负责搜索 + 排序,通过 `KbProvider` 门面组合。两者可独立替换 — 换检索策略不需要重写 CRUD,换存储不需要改检索。

**不是**: 向量检索/RAG(当前不做,但 ABC 预留);框架级抽象(KB 是 bot 业务功能,不引入 modex_agent ABC);graph 机制依赖(KB 与 graph 体系零依赖,仅业务层碰巧用 graphInstanceId 作 taskId 值)。

## 背景

### 与 static-graph-scheduling ticket 02 的关系

ticket 02 (`docs/design/static-graph-scheduling/issues/02-taskid-shared-context-mechanism.md`) 原设计定位 kb 为 "per-task 私有 KV(get/set upsert),agent 不感知 taskId"。本设计在此基础上升级:

- **搜索能力**: 不只 get 单 key,而是跨条目 search(FTS5 全文搜索)
- **多维可选过滤**: task_id 是其一,可扩展 session_id / category 等
- **不传过滤 = 全局搜索**: 非 "task 私有",而是 "可按 task 隔离,也可全局"
- **ABC 可拔插后端**: 持久化和检索各自可替换。当前 FTS5,未来可换向量库/BM25/ReAct

### 架构定位

KB 是 **纯 bot_project 业务层功能**,与 graph 体系零依赖:
- KB 功能本身不依赖 graph 调度机制
- taskId 是业务层选择的隔离维度,值碰巧是 `str(graphInstanceId)`,但功能本身不依赖 graph
- 不镜像 modex_graph 的 DeliverStore(那是 graph 体系的持久化)
- KbPersistence / KbRetriever ABC 在 bot 层,不引入框架层 ABC

### 已有基础设施

| 组件 | 位置 | 状态 |
|------|------|------|
| `MODEX_TASK_ID` env 注入 | `src/modex_agent/agents/external/env_builder.py:90-91` | 已实现,但 `spec.task_id` 永远 None(框架层未接线) |
| `ExternalEnvSpec.task_id` 字段 | `src/modex_agent/agents/external/types.py:174` | 已定义,默认 None |
| `default_id_generator()` Snowflake | `src/modex_graph/id_generator.py:205` | 进程级单例,64-bit int ID |
| `now_ms()` epoch ms | `src/modex_agent/utils/time.py:20` | ADR-0029 统一时间戳 |
| `ConnectionManager` async aiosqlite | `src/modex_agent/persistence/connection.py:61` | workspace 共享连接,WAL + anyio.Lock |
| `BotWorkspaceMigrationRunner` | `examples/bot_project/bot/persistence/migration.py:21` | namespace `bot_project_workspace`,自动发现 `.sql` |
| `TranscriptStore(ABC)` 模式 | `examples/bot_project/bot/webui/transcript_store.py:41` | ABC + concrete + resolver 本地模式参照 |
| `modexctl` CLI 框架 | `examples/bot_project/bot/cli/modexctl/app.py:59` | Typer build_app + build_<name>_command 闭包模式 |

### 参考项目

`F:\tool\pythonProject\references\hermes-agent` 的知识库构建方式(通过 subagent 深度探索):

**Holographic MemoryStore** (plugins/memory/holographic/store.py):
- FTS5 外部内容模式 + triggers + entity resolution + HRR 向量
- `search_facts(category=None)` → 不加 WHERE → 全局搜索(三态过滤灵感来源)
- **缺陷**: `MemoryStore.search_facts` 泄露搜索逻辑进持久化类,延迟导入 `FactRetriever._sanitize_fts_query` 导致循环导入 — 本设计从 ABC 层面杜绝此问题

**FactRetriever** (plugins/memory/holographic/retrieval.py):
- 混合检索: FTS5 候选 → Jaccard rerank → HRR sim → trust weighting
- `_sanitize_fts_query`: stopword drop + OR-join + phrase-literal
- 以构造函数依赖拿 store: `FactRetriever(store=self._store)` — 非正式拆分,本设计正式化为 ABC

**关键模式采纳**: FTS5 外部内容 + triggers、三态过滤(None=全局)、查询净化管线、trigram CJK 兼容、store/retriever 拆分(正式化为双 ABC)。

**未采纳**: 双 FTS5 表(KB 用单 trigram 表)、CJK 路由逻辑(trigram 原生支持)、自愈 4 级、plugin 注册机制。

## 设计决策

### D1. Snowflake 唯一主键

每张表用 `default_id_generator().generate()` 生成 64-bit int PK,非复合主键。符合项目统一模式(DeliverStore / NodeStateStore 等都用这个)。

### D2. 三态过滤语义

每个过滤维度独立三态:
- `None` = 不过滤该维度(跨所有值搜索 = 全局)
- `""` = 只查公共/无范围条目(该字段 = '' 的行)
- `"具体值"` = 只查该隔离值的条目

新增维度只需加字段,后端实现只关注自己认识的维度,不认识的忽略(向前兼容)。

**被 KbPersistence 和 KbRetriever 共用** — 两个 ABC 都用相同的过滤逻辑(get/delete/list_keys 和 search 都支持三态过滤)。

### D3. 隔离维度与 CLI 校验

**两层隔离**:

1. **Workspace 数据源隔离**(与 deliver/send 一致): CLI 通过 `ctx.workspace_root` 指定 workspace,REST route 路由到该 workspace 的 `KbProvider`(不同 workspace = 不同 state.db = 不同 KB 数据源)。这不是 KB 内部过滤,是数据源级别的隔离。CLI 校验与 deliver 一致: `_missing_comm_env_key()` + `ctx.validate_history()`。

2. **KB 内部隔离**(KbFilter 三态): `task_id` 和 `session_id` 两个维度,都是可选的隔离维度。不传入的维度(None)= 无该维度隔离。

| 维度 | CLI 来源 | Tool 来源 | None (无隔离) | 有值 (隔离) |
|------|---------|-----------|---------------|------------|
| `task_id` | `MODEX_TASK_ID` env (图调度=graphInstanceId) | `MODEX_TASK_ID` env | 常规场景 (无 env) | 图调度场景 |
| `session_id` | `ctx.session_id` (comm env 保证有值) | `MODEX_SESSION_ID` env | Tool 无 env 时 | 有 session 上下文时 |

**`--by-task` 参数**: 接受 `true`/`false` 值,**默认 `true`**:
- `--by-task true`(默认): 从 `MODEX_TASK_ID` env 读 taskId;env 无值(常规场景)则无 task 隔离
- `--by-task false`: 显式无 task 隔离(跨 task 搜索)

**注册门控**: KB 命令的注册门控用 comm env keys(能连 bot 后端),**不依赖** MODEX_TASK_ID — 因为 KB 可全局查询。

### D4. KB 作为 tool 体系

KB 不只是 modexctl CLI,同时作为 tool 注册到 agent(像 SendFileToUserTool / ExperienceTool):
- `KbTool(Tool)` 注册在 `_build_tools`(`_assembly_helpers.py`)
- agent 调 `kb(action="search", query="...")`,tool 内部从 env 拿 taskId 和 sessionId(agent 不感知)
- modexctl CLI 供外部 agent 使用(通过 REST POST)
- **Description 面向使用方(agent)**: tool 和 CLI 的 help/description 不暴露内部实现(upsert/FTS5/三态/KbFilter),而是告诉 agent 何时用、怎么用

### D5. FTS5 外部内容模式 + trigram

- **外部内容模式**(`content=kb_entries`): 不重复存储 value,节省空间
- **trigram tokenizer**(`tokenize='trigram'`): 单一表兼容 CJK + 英文,避免双表复杂度
- **triggers 用 `'delete'` 特殊命令**: FTS5 外部内容模式标准同步方式

### D6. 持久化与检索解耦(双 ABC + Provider 门面)

**核心架构决策**。KB 拆为两个正交 ABC:

```
KbPersistence(ABC)              KbRetriever(ABC)
  upsert / get / delete           search(query, filter, limit)
  list_keys                         -> list[KbSearchResult]
  (拥有 ConnectionManager,
   kb_entries 表, FTS5 DDL        (共享 ConnectionManager,
   + triggers)                     直接执行搜索查询)

KbProvider(门面)
  组合 persistence + retriever
  search() → 委托 retriever
  get/set/delete/list → 委托 persistence
```

**拆分理由**:

| | 持久化 | 检索 |
|---|---|---|
| 变化原因 | 换存储技术 (SQLite→Postgres) | 换搜索策略 (FTS5→向量→混合) |
| 变化频率 | 极低 | 中高 |
| 关注点 | 数据存在哪、怎么写、索引怎么同步 | 怎么找、怎么排序、怎么打分 |

耦合在一起意味着换检索策略 = 重写 CRUD。拆开后:
- 换检索: `KbProvider(sqlite_persistence, fts)` → `KbProvider(sqlite_persistence, vector)`,persistence 不动
- 混合检索: `HybridRetriever(Fts5Retriever(...), VectorRetriever(...))` 自然组合
- Hermes 验证: `MemoryStore` + `FactRetriever` 已是非正式拆分,但 `MemoryStore.search_facts` 泄露搜索逻辑导致循环导入。正式拆 ABC 把这件事做干净。

**Provider 门面**: 消费者(KbTool / CLI / REST route)只依赖 `KbProvider`,不直接接触 persistence 或 retriever。内部拆分是可替换内核,消费者不感知。

**Schema 归属**:
- `kb_entries` 表 + FTS5 虚拟表 + triggers → **Persistence**(索引同步是写入的副作用)
- `kb_embeddings` 表 → **VectorRetriever**(派生数据,检索策略特定)

**ConnectionManager 共享**: persistence 和 retriever 共享同一个 `ConnectionManager`(workspace 级单例)。两者各自执行自己的 SQL。Retriever 不通过 persistence 接口拿数据 — 搜索 SQL/FTS5 语法是检索策略特定的。

### D7. KbTool 命名(不是 TaskKbTool)

KB 是通用知识库工具,task 隔离只是运行时可选的过滤维度,不是工具的定义特征。非 graph 场景(普通对话、外部 agent)同样使用 KB。工具名 `kb`,tool name `kb`。

### D8. delete action

原 ticket 02 设计 "不做 delete"。本设计支持 delete — 用户要求 "灵活 CRUD"。

### D9. 共享 workspace async aiosqlite

KB 用 workspace 的 `ConnectionManager`(async aiosqlite, WAL, `anyio.Lock` 序列化),像 TranscriptStore 那样。**不用** sync sqlite3(graph 子系统用自己的 sync 连接,KB 不镜像)。

### D10. 响应格式化收敛(formatting.py)

v3 收敛决策。消费者面向的结构化文本格式化集中在 `formatting.py`(5 个 format 函数 + 1 个截断 helper)。KbTool 和 REST route 都直接调用 formatting 函数,返回 `str`(不再是 `json.dumps` / `model_dump_json`)。CLI 是透明 passthrough,echo REST route 返回的文本,自身不做格式化。

**收敛理由**: v2 中每个消费者各自 `json.dumps`,格式不一致且内部字段(entry_id, task_id, timestamps)泄露给 agent。formatting.py 统一 projection,保证内部字段从不出现在输出中。搜索预览截断(3 行 / 200 字符)也在 formatting.py 统一实现。

**View models 取舍**: review 中曾加入 `KbEntryView` / `KbSearchResultView` 做 projection,但最终移除。projection 改由 formatting.py 在格式化时完成,不需要额外的 view model 层。

### D11. UNIQUE(task_id, session_id, key) — session_id 进入 upsert 语义

v3 变更。UNIQUE 约束从 v2 的 `(task_id, key)` 扩展为 `(task_id, session_id, key)`。session_id 成为 upsert 语义的一部分:同一 task + session 下 key 唯一;不同 task 或 session 可以有相同 key。`ON CONFLICT(task_id, session_id, key) DO UPDATE` 子句同步更新。

**理由**: session_id 作为隔离维度已在 v2 引入,但 v2 的 UNIQUE 约束未包含它,导致同一 task 下不同 session 无法存储相同 key。v3 修正此不一致。

## 关键设计约束

- **KB = 纯 bot_project 业务层**: 不引入 modex_agent 框架层 ABC
- **不镜像 modex_graph 模式**: DeliverStore / sync sqlite3 是 graph 体系,不属于 KB 参照
- **收敛规则**: 不新增并行路径,KB 融入现有 tool / CLI / workspace 体系
- **ABCs before implementations**: KbPersistence(ABC) + KbRetriever(ABC) + KbProvider 先,SqliteKbPersistence + Fts5Retriever 后
- **frozen Pydantic**: 所有 model `frozen=True, extra="forbid"`
- **Snowflake PK + epoch ms**: 遵循项目统一 ID 和时间戳模式
- **ADR-0029**: 时间戳用 epoch ms
- **ConnectionManager API**: `query_one()` / `query_all()` / `transaction()`,不是 `execute().fetchone()`

## 关键代码文件索引

| 文件 | 作用 |
|------|------|
| `bot/kb/models.py` | KbAction, KbFilter, KbEntry, KbUpsertRequest, KbSearchResult, KbControlRequest |
| `bot/kb/persistence.py` | KbPersistence(ABC) — CRUD 抽象 |
| `bot/kb/retriever.py` | KbRetriever(ABC) — search 抽象 |
| `bot/kb/provider.py` | KbProvider — 组合门面 |
| `bot/kb/sqlite_persistence.py` | SqliteKbPersistence(ConnectionManager) — FTS5 持久化后端 |
| `bot/kb/sqlite_utils.py` | build_filter_clauses — 共享过滤 helper (v3) |
| `bot/kb/fts5_retriever.py` | Fts5Retriever(ConnectionManager) — FTS5 检索后端 |
| `bot/kb/fts_utils.py` | sanitize_fts_query |
| `bot/kb/formatting.py` | format_search_results / format_entry / ... — 共享响应格式化 (v3) |
| `bot/kb/builder.py` | build_sqlite_kb_persistence / build_fts5_retriever / build_default_kb_provider |
| `bot/tools/kb.py` | KbTool(Tool) — agent tool |
| `bot/cli/modexctl/commands/kb.py` | build_kb_command(ctx) — CLI 命令 |
| `bot/persistence/migrations/workspace/001_webui_transcript.sql` (modified) | Schema DDL |
| `bot/webui/routes/kb_routes.py` | POST /api/control/kb REST route |
| `src/modex_graph/id_generator.py:205` | default_id_generator() — Snowflake 单例 |
| `src/modex_agent/utils/time.py:20` | now_ms() — epoch ms |
| `src/modex_agent/persistence/connection.py:61` | ConnectionManager — async aiosqlite |
| `bot/webui/transcript_store.py:41` | TranscriptStore(ABC) — 本地 ABC 模式参照 |
| `bot/persistence/transcript.py:23` | build_database_transcript_store — resolver 模式参照 |
| `bot/persistence/migration.py:21` | BotWorkspaceMigrationRunner — 迁移 runner |
| `bot/cli/modexctl/app.py:59` | build_app — CLI 注册模式参照 |
| `bot/cli/modexctl/commands/deliver.py:76` | build_deliver_command — 命令闭包模式参照 |
| `bot/service/_assembly_helpers.py:236` | _build_tools — tool 注册枢纽 |
| `bot/tools/custom.py:29` | SendFileToUserTool — business tool 模式参照 |

## 参考实现模式来源

| 模式 | 来源 | 参考方式 |
|------|------|---------|
| FTS5 外部内容 + triggers | hermes holographic store.py `_SCHEMA` | 镜像 trigger 结构 |
| FTS5 查询净化 | hermes FactRetriever._sanitize_fts_query (retrieval.py:564-619) | 采用 stopword drop + OR-join + phrase-literal |
| 三态过滤(None=全局) | hermes holographic store.py search_facts(category=None) | 灵感来源,扩展为三态 |
| Store/Retriever 拆分 | hermes MemoryStore + FactRetriever (非正式拆分) | 正式化为双 ABC,杜绝循环导入 |
| ABC + concrete + resolver | bot TranscriptStore pattern | 本地模式镜像 |
| Tool 注册 | bot SendFileToUserTool + ExperienceTool | 本地模式镜像 |
| CLI 闭包命令 | bot deliver/send 命令 | 本地模式镜像 |
| Snowflake PK + epoch ms | bot DeliverStore / NodeStateStore | 项目统一模式 |
| namespaced migration | bot BotWorkspaceMigrationRunner | 本地迁移模式 |
| ConnectionManager API | bot/persistence/connection.py:61 | query_one/query_all/transaction |

## Out of scope

- **向量检索 / embedding / RAG**: 当前不做,ABC 预留未来 VectorRetriever
- **混合检索(FTS5 + 向量 rerank)**: 未来 HybridRetriever 可实现
- **框架级 TaskKvStore ABC**: KB 是 bot 业务功能,不引入 modex_agent 抽象
- **框架层 taskId 注入接线**: AgentNode context_factory 接线是未来工作,不阻塞 KB 实现(当前 task_id_provider 从 env 读,框架层接线后自动生效)
- **多模型 embedding 共存**: kb_embeddings 表设计支持,但当前不实现
- **PostgresKbPersistence**: 未来存储后端替换

## 实现顺序

| 步骤 | 文件 | 内容 | 依赖 |
|------|------|------|------|
| 1 | `bot/kb/models.py` | KbAction + KbFilter + KbEntry + KbUpsertRequest + KbSearchResult + KbControlRequest | 无 |
| 2 | `bot/kb/persistence.py` | KbPersistence(ABC) | 步骤 1 |
| 3 | `bot/kb/retriever.py` | KbRetriever(ABC) | 步骤 1 |
| 4 | `bot/kb/provider.py` | KbProvider(门面) | 步骤 2,3 |
| 5 | `bot/persistence/migrations/workspace/001_webui_transcript.sql` (modified) | Schema DDL | 无 |
| 6 | `bot/kb/fts_utils.py` | sanitize_fts_query | 无 |
| 7 | `bot/kb/sqlite_utils.py` | build_filter_clauses (共享过滤 helper) | 步骤 1 |
| 8 | `bot/kb/sqlite_persistence.py` | SqliteKbPersistence(ConnectionManager) | 步骤 1,2,5,7 |
| 9 | `bot/kb/fts5_retriever.py` | Fts5Retriever(ConnectionManager) | 步骤 1,3,6,7 |
| 10 | `bot/kb/builder.py` | build_default_kb_provider + 三个 builder | 步骤 4,8,9 |
| 11 | `bot/kb/formatting.py` | format_search_results / format_entry / ... (共享响应格式化) | 步骤 1 |
| 12 | `bot/tools/kb.py` | KbTool(Tool) | 步骤 1,4,11 |
| 13 | `resources.py` + `handle.py` + `pool_data.py` | 装配接线 | 步骤 10 |
| 14 | `_assembly_helpers.py` | KbTool 注册 | 步骤 12,13 |
| 15 | `bot/cli/modexctl/commands/kb.py` | CLI 命令 | 步骤 1 |
| 16 | `app.py` | 注册 kb 命令 | 步骤 15 |
| 17 | `bot/webui/routes/kb_routes.py` | REST route | 步骤 10,11 |
| 18 | (未来) `bot/kb/vector_retriever.py` + `003_kb_embeddings.sql` | 向量后端 | 步骤 1-10 |

## 开始实现

### 环境确认

```bash
cd F:\tool\pythonProject\ModexAgent-kb
git branch --show-current  # 应为 feature/kb-store
git status                 # 应有 docs/design/kb-store/ 未跟踪文件
```

### 必读文档(按顺序)

1. **`docs/design/kb-store/PRD.md`** — 本文件(需求与决策)
2. **`docs/design/kb-store/DESIGN.md`** — 完整代码级设计
3. (可选) 主项目 `docs/design/static-graph-scheduling/issues/02-taskid-shared-context-mechanism.md` — 原始 ticket 02 设计(理解演进背景)
4. (可选) 主项目 `AGENTS.md` + `examples/bot_project/AGENTS.md` — 项目规范

### 每步完成后

- 运行 `lsp_diagnostics` 检查类型
- 运行相关测试
- 更新 todo

### 待确认的开放问题

1. **REST route 位置**: `bot/webui/routes/kb_routes.py` 新文件,还是加入已有 control routes? — 设计倾向新文件
2. **PoolData 携带 kb_provider**: `PoolData` 加 `kb_provider` 字段,从 `PoolWorkspaceResources` 传入 — 需要修改 `pool_data.py`
3. **FTS5 查询净化复杂度**: 当前简化版(OR-join + stopword drop),是否需要 hermes 的完整管线? — 设计用简化版,实现时可调整
4. **框架层 taskId 注入接线**: AgentNode context_factory 接线是未来工作,当前 task_id_provider 从 env 读 — 本期不做

## 设计演进

### v1 → v2 关键变更

| 维度 | v1 (原设计) | v2 |
|------|------------|----------|
| **ABC 架构** | 单一 `KbStore(ABC)` 耦合 CRUD + search | 双 ABC: `KbPersistence` + `KbRetriever`,通过 `KbProvider` 门面组合 |
| **检索后端切换** | 必须重写全部 CRUD | persistence 不动,只换 retriever |
| **混合检索** | 不可能 (两个 KbStore.search 无法组合) | `HybridRetriever(Fts5Retriever, VectorRetriever)` 自然组合 |
| **工具命名** | `TaskKbTool` | `KbTool` (KB 是通用工具,task 隔离是可选运行时维度) |
| **ConnectionManager API** | 错误假设 `execute().fetchone()` | 修正为 `query_one()` / `query_all()` / `transaction()` |
| **隔离维度** | 仅 task_id | task_id + session_id (两者都是可选隔离维度) |
| **两层隔离** | 未区分 | workspace 数据源隔离 ≠ KB 内部隔离 (KbFilter) |
| **Description** | 暴露实现细节 (upsert/FTS5) | 面向使用方 (agent),告诉何时用、怎么用 |
| **文件结构** | `store.py` + `sqlite_store.py` | `persistence.py` + `retriever.py` + `provider.py` + `sqlite_persistence.py` + `fts5_retriever.py` |

### v2 → v3 关键变更 (review 修复 + 收敛)

| 维度 | v2 | v3 (当前) |
|------|----------|----------|
| **响应格式** | 每个消费者各自 `json.dumps` / `model_dump_json` | `formatting.py` 共享结构化文本格式化;返回 `str`;CLI 透明 passthrough |
| **过滤 helper** | `SqliteKbPersistence._build_filter_clauses` 静态方法;retriever 内联重复 | `sqlite_utils.build_filter_clauses(filter, *, alias="")` 共享函数 |
| **UNIQUE 约束** | `(task_id, key)` | `(task_id, session_id, key)` — session_id 进入 upsert 语义 |
| **View models** | (无) | `KbEntryView` / `KbSearchResultView` 加入又移除;projection 改由 formatting.py 完成 |
| **Builder** | `async def build_sqlite_kb_persistence` | `def build_sqlite_kb_persistence` (sync);`build_default_kb_provider` 仍 async 但无 await |
| **Env provider** | `tid if tid else None` (丢失空串三态) | raw `os.environ.get()` (保留三态: None/""/"值") |
| **Action dispatch** | `if action == "search": ...` 字符串比较 | `KbAction(StrEnum)` + `match/case` + `assert_never` |
| **共享请求模型** | (无,CLI 手动构造 dict) | `KbControlRequest(BaseModel)` + `Field(ge=1, le=100)` on limit |
| **by_task 校验** | 宽松 truthy (`"true", "1", "yes", "on"`) | 严格 true/false/yes/no/on/off,其他报 EXIT_USAGE |
| **upsert 校验** | `assert row is not None` | `if row is None: raise RuntimeError(...)` |
| **新增文件** | — | `sqlite_utils.py`, `formatting.py` |

### 会话历史要点(设计演进)

以下是设计过程中用户提出的关键修正:

1. **初始设计**: 镜像 DeliverStore 三档模式 → **用户纠正**: KB 是纯业务层,不镜像 graph 体系
2. **task_id 三态**: 初始二态 → **用户补充**: 需要 `""` 中间态(只查公共),扩展为三态
3. **可扩展过滤**: 初始只做 task_id → **用户要求**: 必须可扩展(sessionId 等)
4. **--by-task**: 初始 bool flag(默认 off) → 默认 true → 接受 true/false 值(不是 flag)
5. **ABC 可拔插**: 初始固定 FTS5 → **用户要求**: ABC 可拔插(FTS5/向量/BM25/ReAct)
6. **hermes 深度探索**: 用户要求用 subagent 探索 hermes-agent 知识库构建方式
7. **KB 作为 tool**: 用户确认 KB 不只是 CLI,同时作为 tool 注册到 agent
8. **Snowflake PK**: 用户要求每表有 Snowflake 唯一主键(非复合主键)
9. **环境变量门控**: 用户纠正 KB 命令门控用 comm env keys(不依赖 MODEX_TASK_ID)
10. **worktree 隔离**: 用户要求在 `F:\tool\pythonProject` 下创建 worktree
11. **v2 解耦**: 用户要求持久化和检索完全解耦,分别可以灵活组合 → 拆为 `KbPersistence(ABC)` + `KbRetriever(ABC)` + `KbProvider` 门面
12. **v2 命名**: 用户指出 `TaskKbTool` 命名不当 — KB 不一定依赖 task → 改名 `KbTool`
13. **v2 隔离维度**: 用户确认 task_id 和 session_id 都应作为隔离维度,workspace 数据源隔离 ≠ KB 内部隔离
14. **v2 Description**: 用户要求 tool/CLI 的 description 面向使用方(agent),不暴露内部实现

## Suggested Skills

实现阶段应加载以下 skills:

| Skill | 何时用 | 原因 |
|-------|--------|------|
| `tdd` / `test-driven-development` | 写 SqliteKbPersistence / Fts5Retriever 时 | 先写测试再实现,验证 CRUD + FTS5 + 三态过滤 |
| `verification-planning` | 开始实现前 | 规划验证路径(单元测试 + 集成测试) |
| `database-schema-designer` | 调整 schema 时 | 索引/约束/迁移最佳实践 |
| `simplify` | 实现完成后 | 简化代码,确保无过度设计 |
| `code-review` / `requesting-code-review` | 实现完成后 | 按项目标准 review |
