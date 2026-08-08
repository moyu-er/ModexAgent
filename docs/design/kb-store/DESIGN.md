# KB Store — Schema 与组件设计

Status: implemented (docs synced to v3 code)

> 配套文档: `PRD.md`(需求与决策)、本文件(schema 与组件技术设计)

## 0. 设计演进记录

**v2** — 持久化与检索解耦。原 v1 设计的单一 `KbStore(ABC)` 将 CRUD 和 search 耦合在一个类中,切换检索后端(FTS5→向量)意味着重写全部 CRUD。v2 拆为 `KbPersistence(ABC)` + `KbRetriever(ABC)` 两个正交 ABC,通过 `KbProvider` 门面组合,各自独立替换。同时修正了 v1 中 `ConnectionManager` API 的错误用法。

**v3 (当前)** — review 修复与收敛。多轮 code review 后的收敛性修复,不改架构方向,只消除实现层分歧:

1. **formatting.py** — 消费者面向的结构化文本格式化(5 个 format 函数 + 1 个截断 helper)。KbTool 和 REST route 都直接调用 formatting 函数,返回 `str`。CLI 是透明 passthrough(echo REST route 返回的文本,自身不做格式化)。消除了 v2 中每个消费者各自 `json.dumps` 的分歧。
2. **sqlite_utils.py** — 共享过滤 helper 提取。`build_filter_clauses(filter, *, alias="")` 取代了 `SqliteKbPersistence._build_filter_clauses` 静态方法。SqliteKbPersistence(alias="")和 Fts5Retriever(alias="e.")共用同一函数。
3. **KbAction(StrEnum) + KbControlRequest(BaseModel)** — 共享请求模型。KbAction 是 5 个 action 的 StrEnum;KbControlRequest 带 `Field(ge=1, le=100)` 限制 limit 范围。CLI 和 REST route 共用此模型(CLI 构造 + model_dump,REST route model_validate)。
4. **UNIQUE(task_id, session_id, key)** — 从 v2 的 `(task_id, key)` 扩展为三元组。session_id 成为 upsert 语义的一部分(同一 task + session 下 key 唯一)。ON CONFLICT 子句同步更新。
5. **build_sqlite_kb_persistence 改为 sync** — 不再是 `async def`。`build_default_kb_provider` 仍为 `async`(调用方 resources.py 用 `await`,保持兼容),但内部无 await。
6. **assert 替换为 RuntimeError** — `SqliteKbPersistence.upsert` 中 `assert row is not None` 改为 `if row is None: raise RuntimeError(...)`。
7. **env providers 返回 raw os.environ.get()** — 不再做 `tid if tid else None` 转换。`os.environ.get` 在 env 不存在时返回 None,存在但为空串时返回 ""。保留三态语义(env 不存在 = None = 全局;env 存在但空 = "" = 公共;env 有值 = 隔离)。
8. **CLI by_task 严格校验** — 只接受 true/false/yes/no/on/off(大小写不敏感),其他值报 EXIT_USAGE。v2 只做 truthy 判断。
9. **KbEntryView / KbSearchResultView 删除** — 这两个 view model 在 review 中加入又移除,从未发布。projection(过滤内部字段)改由 formatting.py 在格式化时完成。

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│  消费者                                                              │
│    Agent (图调度内 / 普通对话 / 外部 agent via modexctl)             │
│      kb(action="search", query="...")                                │
│        → tool 内部从 env 拿 taskId (agent 不感知, 可选隔离)          │
│        → tool 持有 KbProvider 引用, 直接调 provider.search()         │
│    modexctl kb search "..." (--by-task true 默认)                    │
│        → REST POST /api/control/kb → route handler → KbProvider     │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│  KbProvider  [bot/kb/provider.py]                                   │
│    门面: 组合 persistence + retriever, 对外暴露统一接口               │
│    search()      → 委托 retriever                                   │
│    get/set/delete/list → 委托 persistence                           │
└────────────┬─────────────────────────┬──────────────────────────────┘
             │                         │
┌────────────▼──────────────┐ ┌────────▼──────────────────────────────┐
│  KbPersistence(ABC)       │ │  KbRetriever(ABC)                     │
│  [bot/kb/persistence.py]  │ │  [bot/kb/retriever.py]                │
│    abstract:              │ │    abstract:                          │
│      upsert / get         │ │      search(query, filter, limit)     │
│      delete / list_keys   │ │        -> list[KbSearchResult]        │
│                          │ │    检索策略由具体后端决定:              │
│  拥有:                   │ │      FTS5 / 向量 / BM25 / 混合 / ReAct │
│    ConnectionManager      │ └───────────────────────────────────────┘
│    kb_entries 表 + DDL    │
│    FTS5 虚拟表 + triggers │ 实现 (当前 + 未来):
│    (索引同步是存储层关注点)│ │  Fts5Retriever(conn)           ← v1 默认
│                          │ │    FTS5 MATCH + BM25 rank + sanitize
│ 实现:                    │ │  VectorRetriever(conn, embed_fn) ← 未来
│  SqliteKbPersistence(conn)│ │    kb_embeddings 表 + cosine (应用层)
│    async aiosqlite        │ │  HybridRetriever(fts, vector)   ← 未来
│    Snowflake PK + epoch ms│ │    FTS5 召回 → 向量 rerank
└──────────────────────────┘ └───────────────────────────────────────┘

装配关系 (bot/workspace/wiring/resources.py):
  persistence = SqliteKbPersistence(conn)    # 共享 workspace 连接
  retriever   = Fts5Retriever(conn)           # 共享同一连接
  provider    = KbProvider(persistence, retriever)
```

### 1.1 为什么拆成两个 ABC

| | 持久化 (Persistence) | 检索 (Retrieval) |
|---|---|---|
| **变化原因** | 换存储技术 (SQLite→Postgres) | 换搜索策略 (FTS5→向量→混合) |
| **变化频率** | 极低 (存储层一旦定下很少动) | 中高 (检索策略是迭代重点) |
| **关注点** | 数据存在哪、怎么写、索引怎么同步 | 怎么找、怎么排序、怎么打分 |

耦合在一起意味着换检索策略 = 重写 CRUD。拆开后:
- 换检索: `KbProvider(sqlite_persistence, fts)` → `KbProvider(sqlite_persistence, vector)`,persistence 不动
- 混合检索: `HybridRetriever(Fts5Retriever(...), VectorRetriever(...))` 自然组合,无需碰 persistence
- 参考验证: hermes-agent 的 `MemoryStore`(持久化) + `FactRetriever`(检索) 已是非正式拆分,retriever 以构造函数依赖拿 store。但 `MemoryStore.search_facts` 泄露了搜索逻辑进持久化类,导致循环导入。正式拆 ABC 正是把这件事做干净。

### 1.2 共享连接,各自查询

Persistence 和 Retriever 共享同一个 `ConnectionManager`(workspace 级单例)。两者各自执行自己的 SQL:
- Persistence: INSERT/UPDATE/DELETE/SELECT (CRUD + FTS5 triggers 自动同步)
- Retriever: SELECT + FTS5 MATCH (搜索查询)

`ConnectionManager` 内部 `anyio.Lock` 序列化所有操作,两者不会并发冲突。Retriever 不通过 Persistence 的接口拿数据 — 它直接查表,因为搜索查询(SQL/FTS5 语法)是检索策略特定的,persistence 不应该暴露检索特定的接口。

## 2. Schema 设计

### 2.1 Schema 归属

| Schema | 归属 | 文件 | 说明 |
|--------|------|------|------|
| `kb_entries` 表 + 索引 | **Persistence** | `001_webui_transcript.sql` (modified) | 数据存储层 |
| `kb_entries_fts` 虚拟表 + triggers | **Persistence** | `001_webui_transcript.sql` (modified) | 索引同步在写入时触发 — 存储层关注点 |
| `kb_embeddings` 表 + 索引 | **VectorRetriever** | `003_kb_embeddings.sql` (未来) | 派生数据,检索策略特定,仅启用向量后端时创建 |

### 2.2 主表 — `kb_entries`

```sql
-- Migration: 001_webui_transcript.sql (modified — 追加 KB schema)
-- Namespace: bot_project_workspace (BotWorkspaceMigrationRunner 自动发现)
-- 不含事务控制 (BotWorkspaceMigrationRunner 在 transaction 中执行)

CREATE TABLE IF NOT EXISTS kb_entries (
    -- Snowflake 64-bit int PK (default_id_generator().generate())
    -- INTEGER PRIMARY KEY 在 SQLite 中是 rowid 别名, 64-bit
    entry_id     INTEGER PRIMARY KEY,

    -- 业务字段
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,

    -- 多维隔离字段 ("" = 公共/无范围; "具体值" = 按值隔离)
    -- task_id: 业务层用 str(graphInstanceId) 作值; "" = 全局公共知识
    -- session_id: 未来维度; "" = 无 session 范围
    task_id      TEXT NOT NULL DEFAULT '',
    session_id   TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    tags         TEXT NOT NULL DEFAULT '',

    -- 时间戳: epoch ms (ADR-0029, now_ms())
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,

    -- upsert 语义: 同一 task_id + session_id 下 key 唯一 (INSERT ... ON CONFLICT)
    -- 注意: 不同 task_id 或 session_id 可以有相同 key (隔离)
    UNIQUE (task_id, session_id, key)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_kb_entries_task
    ON kb_entries (task_id);

CREATE INDEX IF NOT EXISTS idx_kb_entries_category
    ON kb_entries (category);

-- UNIQUE(task_id, session_id, key) 自动创建索引, 覆盖三元组查询
-- tags 不索引 (逗号分隔文本, 走 FTS5 搜索)
```

**设计决策**:

- **Snowflake PK**: `entry_id INTEGER PRIMARY KEY` — `default_id_generator().generate()` 生成。应用侧生成,不用 AUTOINCREMENT(Snowflake 跨进程单调)。
- **UNIQUE(task_id, session_id, key)**: 保证 upsert 语义(`ON CONFLICT(task_id, session_id, key) DO UPDATE`)。不是 PK — 允许不同 task 或 session 有相同 key。session_id 是 upsert 语义的一部分(v3 变更)。
- **隔离字段默认 `''`**: 空字符串表示"公共/无范围",不用 NULL — 避免 SQLite NULL 在 UNIQUE 约束中的歧义(多个 NULL 不冲突,破坏 upsert)。
- **epoch ms**: 遵循 ADR-0029, `now_ms()`。

### 2.3 FTS5 全文检索 — `kb_entries_fts`

外部内容模式(参考 hermes holographic store.py `_SCHEMA`)。

```sql
-- FTS5 外部内容虚拟表
-- content=kb_entries: FTS5 引用 kb_entries 内容, 不自己存
-- content_rowid=entry_id: FTS5 rowid 对应 kb_entries.entry_id (Snowflake int)
-- tokenize='trigram': 兼容 CJK 子串搜索
CREATE VIRTUAL TABLE IF NOT EXISTS kb_entries_fts USING fts5(
    value,
    tags,
    content=kb_entries,
    content_rowid=entry_id,
    tokenize='trigram'
);

-- FTS5 同步 triggers (外部内容模式必须用 'delete' 特殊命令)
CREATE TRIGGER IF NOT EXISTS kb_fts_insert AFTER INSERT ON kb_entries BEGIN
    INSERT INTO kb_entries_fts(rowid, value, tags)
        VALUES (new.entry_id, new.value, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS kb_fts_delete AFTER DELETE ON kb_entries BEGIN
    INSERT INTO kb_entries_fts(kb_entries_fts, rowid, value, tags)
        VALUES ('delete', old.entry_id, old.value, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS kb_fts_update AFTER UPDATE ON kb_entries BEGIN
    INSERT INTO kb_entries_fts(kb_entries_fts, rowid, value, tags)
        VALUES ('delete', old.entry_id, old.value, old.tags);
    INSERT INTO kb_entries_fts(rowid, value, tags)
        VALUES (new.entry_id, new.value, new.tags);
END;
```

**FTS5 模式选择理由**:

- **外部内容模式**(`content=kb_entries`):不重复存储 value。KB value 可能很大(任务知识、代码库结构描述),重复存储不可取。
- **trigram tokenizer**:单一表兼容 CJK + 英文。hermes_state.py 用双表(unicode61 + trigram)因为 unicode61 对英文精度更高,但双表增加复杂度(双 triggers、双查询路径、CJK 路由逻辑)。KB 场景 trigram 单表精度够用,复杂度更低。
- **triggers 用 `'delete'` 命令**: FTS5 外部内容模式标准同步方式。UPDATE trigger 做 delete-then-insert(FTS5 无 in-place update)。
- **归属 Persistence**: triggers 在 INSERT/UPDATE/DELETE 时触发,是写入的副作用 — 属于存储层关注点。Retriever 不维护 triggers,只读取 FTS 索引。

### 2.4 完整迁移文件 — `001_webui_transcript.sql` (modified)

> **注意**: 修改现有 `001_webui_transcript.sql`, 不创建 `002_kb.sql` (用户要求)。KB 表 DDL 追加到现有迁移文件末尾。

```sql
-- 001_webui_transcript.sql (modified — 追加 KB schema)
-- KB knowledge base: entries table + FTS5 full-text search
-- Namespace: bot_project_workspace
-- 归属: KbPersistence (SqliteKbPersistence)

-- ── Main table ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS kb_entries (
    entry_id     INTEGER PRIMARY KEY,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    task_id      TEXT NOT NULL DEFAULT '',
    session_id   TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    tags         TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    UNIQUE (task_id, session_id, key)
);

-- ── Indexes ─────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_kb_entries_task
    ON kb_entries (task_id);

CREATE INDEX IF NOT EXISTS idx_kb_entries_category
    ON kb_entries (category);

-- ── FTS5 external content table ────────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS kb_entries_fts USING fts5(
    value,
    tags,
    content=kb_entries,
    content_rowid=entry_id,
    tokenize='trigram'
);

-- ── FTS5 sync triggers ─────────────────────────────────────
CREATE TRIGGER IF NOT EXISTS kb_fts_insert AFTER INSERT ON kb_entries BEGIN
    INSERT INTO kb_entries_fts(rowid, value, tags)
        VALUES (new.entry_id, new.value, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS kb_fts_delete AFTER DELETE ON kb_entries BEGIN
    INSERT INTO kb_entries_fts(kb_entries_fts, rowid, value, tags)
        VALUES ('delete', old.entry_id, old.value, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS kb_fts_update AFTER UPDATE ON kb_entries BEGIN
    INSERT INTO kb_entries_fts(kb_entries_fts, rowid, value, tags)
        VALUES ('delete', old.entry_id, old.value, old.tags);
    INSERT INTO kb_entries_fts(rowid, value, tags)
        VALUES (new.entry_id, new.value, new.tags);
END;
```

### 2.5 向量扩展表 — `kb_embeddings`(未来)

检索策略特定 schema,归属 `VectorRetriever`。仅启用向量后端时创建。

```sql
-- 未来迁移: 003_kb_embeddings.sql (仅在启用 VectorRetriever 时创建)
-- 归属: VectorRetriever (检索层)
-- 独立于 kb_entries_fts, 两者可共存 (FTS5 + 向量混合检索)

CREATE TABLE IF NOT EXISTS kb_embeddings (
    embedding_id  INTEGER PRIMARY KEY,     -- Snowflake
    entry_id      INTEGER NOT NULL
        REFERENCES kb_entries(entry_id) ON DELETE CASCADE,
    embedding     BLOB NOT NULL,           -- float32 向量 bytes
    model         TEXT NOT NULL,
    dim           INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,        -- epoch ms
    UNIQUE(entry_id, model)
);

CREATE INDEX IF NOT EXISTS idx_kb_embeddings_entry
    ON kb_embeddings (entry_id);
CREATE INDEX IF NOT EXISTS idx_kb_embeddings_model
    ON kb_embeddings (model);
```

### 2.6 验收清单

- [x] 每张表有 Snowflake 唯一主键(`entry_id INTEGER PRIMARY KEY`)
- [x] FTS5 虚拟表有 rowid 对应主表 PK(`content_rowid=entry_id`)
- [x] UNIQUE 约束保证 upsert 语义(`UNIQUE(task_id, session_id, key)`)
- [x] 索引覆盖所有查询路径(task_id, category, task_id+session_id+key)
- [x] 时间戳用 epoch ms(ADR-0029)
- [x] 隔离字段默认 `''`(避免 NULL 在 UNIQUE 中的歧义)
- [x] FTS5 外部内容模式(不重复存储 value)
- [x] trigram tokenizer(CJK 兼容)
- [x] FTS5 triggers 用 `'delete'` 特殊命令同步
- [x] 迁移文件不含事务控制(runner 在 transaction 中执行)
- [x] `CREATE ... IF NOT EXISTS` 幂等
- [x] Schema 归属明确: 主表+FTS5 属 Persistence, embeddings 属 Retriever

## 3. Pydantic Models

共享模型,两个 ABC 和消费者(KbTool / CLI / REST route)都用。`bot/kb/models.py`。包含 KbAction(StrEnum)、KbFilter、KbEntry、KbUpsertRequest、KbSearchResult、KbControlRequest。

```python
# bot/kb/models.py
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class KbAction(StrEnum):
    """KB action enum — KbTool / CLI / REST route 共用。"""
    SEARCH = "search"
    GET = "get"
    SET = "set"
    DELETE = "delete"
    LIST = "list"


class KbFilter(BaseModel):
    """可扩展的多维过滤模型。

    三态语义 (每个维度独立):
      - None  = 不过滤该维度 (跨所有值搜索 = 全局)
      - ""    = 只查公共/无范围条目 (该字段 = '' 的行)
      - "值"  = 只查该隔离值的条目

    新增维度只需加字段。后端实现只关注自己认识的维度,
    不认识的维度忽略 (向前兼容)。

    被 KbPersistence (get/delete/list_keys) 和 KbRetriever (search) 共用。
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str | None = None
    session_id: str | None = None       # 未来维度, 当前不用
    category: str | None = None
    # 未来扩展: workspace_id, agent_name, ...


class KbEntry(BaseModel):
    """KB 条目。读取结果模型。

    所有隔离维度有具体值 ("" = 公共/无范围)。
    entry_id 由 persistence 内部生成 (Snowflake), agent 不传。
    UNIQUE(task_id, session_id, key) 保证 upsert 语义 (v3)。
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: int              # Snowflake PK
    key: str
    value: str
    task_id: str = ""          # "" = 公共知识
    session_id: str = ""       # "" = 无 session 范围
    category: str = ""         # "" = 无分类
    tags: str = ""             # "" = 无标签
    created_at: int            # epoch ms (ADR-0029)
    updated_at: int            # epoch ms


class KbUpsertRequest(BaseModel):
    """写入请求。entry_id 由 persistence 生成, 调用者不传。"""
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: str
    task_id: str = ""          # "" = 写入公共知识
    session_id: str = ""
    category: str = ""
    tags: str = ""


class KbSearchResult(BaseModel):
    """搜索结果条目 (带相关性分数)。

    score 语义由 retriever 后端定义:
      - Fts5Retriever: FTS5 BM25 rank (abs)
      - VectorRetriever: cosine similarity [0, 1]
      - HybridRetriever: 加权综合分
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: KbEntry
    score: float = 0.0


class KbControlRequest(BaseModel):
    """共享请求模型 — CLI 和 REST route 共用。

    CLI 构造 + model_dump(mode="json") 发 HTTP;
    REST route model_validate 从 HTTP body 解析。
    响应格式化由 formatting.py 完成 (不在此模型)。
    limit 用 Field(ge=1, le=100) 限定范围 (v3)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: KbAction
    query_or_key: str | None = None
    value: str | None = None
    filter: KbFilter = KbFilter()
    limit: int = Field(default=20, ge=1, le=100)
```

## 4. KbPersistence(ABC)

`bot/kb/persistence.py`。负责数据存储 + CRUD + FTS5 索引同步。

Mirror pattern: `TranscriptStore(ABC)` at `bot/webui/transcript_store.py:41`。

```python
# bot/kb/persistence.py
from __future__ import annotations
from abc import ABC, abstractmethod


class KbPersistence(ABC):
    """知识库持久化抽象。

    负责数据存储和 CRUD。FTS5 索引同步 (triggers) 是存储层的副作用 —
    persistence 拥有 triggers, retriever 只读取 FTS 索引。

    所有方法接受 KbFilter 进行多维隔离过滤。
    filter 维度为 None 时该维度全局搜索。

    后端可替换:
      - SqliteKbPersistence: FTS5 + async aiosqlite (当前)
      - (未来) PostgresKbPersistence, InMemoryKbPersistence, ...

    Mirror pattern: TranscriptStore(ABC) at
    bot/webui/transcript_store.py:41
    """

    @abstractmethod
    async def upsert(self, request: KbUpsertRequest) -> KbEntry:
        """写入/更新条目 (upsert 语义)。

        ON CONFLICT(task_id, session_id, key) DO UPDATE。
        返回更新后的 KbEntry (含 entry_id)。
        FTS5 triggers 自动同步索引 (无需手动维护)。
        """
        ...

    @abstractmethod
    async def get(self, key: str, filter: KbFilter) -> KbEntry | None:
        """精确读取。filter 限制搜索范围。"""
        ...

    @abstractmethod
    async def delete(self, key: str, filter: KbFilter) -> bool:
        """删除条目。返回是否删除成功。"""
        ...

    @abstractmethod
    async def list_keys(
        self,
        filter: KbFilter,
        prefix: str | None = None,
    ) -> list[str]:
        """列出键。prefix 做前缀匹配 (可选)。"""
        ...
```

**注意**: KbPersistence **没有** `search` 方法。搜索是检索层的职责。hermes 的 `MemoryStore.search_facts` 泄露搜索逻辑进持久化类,导致循环导入 — 本设计从 ABC 层面杜绝此问题。

## 5. KbRetriever(ABC)

`bot/kb/retriever.py`。负责搜索和排序。

```python
# bot/kb/retriever.py
from __future__ import annotations
from abc import ABC, abstractmethod


class KbRetriever(ABC):
    """知识库检索抽象。

    负责搜索。检索策略由具体后端决定:
      - Fts5Retriever: FTS5 MATCH + BM25 ranking (当前)
      - VectorRetriever: embedding + cosine similarity (未来)
      - HybridRetriever: FTS5 召回 + 向量 rerank (未来)
      - ReActRetriever: LLM agent 自主检索决策 (未来)

    Retriever 共享 persistence 的 ConnectionManager, 直接执行搜索查询。
    不通过 persistence 接口拿数据 — 搜索 SQL/FTS5 语法是检索策略特定的。

    所有方法接受 KbFilter 进行多维隔离过滤。
    """

    @abstractmethod
    async def search(
        self,
        query: str,
        filter: KbFilter,
        limit: int = 20,
    ) -> list[KbSearchResult]:
        """搜索。后端自主决定检索策略和评分算法。

        返回带 score 的结果列表, 按 score 降序。
        score 语义由后端定义 (BM25 rank / cosine / 加权综合)。
        """
        ...
```

## 6. KbProvider(门面)

`bot/kb/provider.py`。组合 persistence + retriever, 对消费者暴露统一接口。

```python
# bot/kb/provider.py
from __future__ import annotations


class KbProvider:
    """知识库门面。组合 KbPersistence + KbRetriever。

    消费者 (KbTool / CLI / REST route) 只依赖 KbProvider,
    不直接接触 persistence 或 retriever。

    装配点组合:
      provider = KbProvider(
          persistence=SqliteKbPersistence(conn),
          retriever=Fts5Retriever(conn),
      )

    切换检索策略 (persistence 不动):
      provider = KbProvider(
          persistence=same_sqlite_persistence,
          retriever=HybridRetriever(Fts5Retriever(conn), VectorRetriever(conn, embed_fn)),
      )
    """

    def __init__(
        self,
        persistence: KbPersistence,
        retriever: KbRetriever,
    ) -> None:
        self._persistence = persistence
        self._retriever = retriever

    async def upsert(self, request: KbUpsertRequest) -> KbEntry:
        return await self._persistence.upsert(request)

    async def get(self, key: str, filter: KbFilter) -> KbEntry | None:
        return await self._persistence.get(key, filter)

    async def delete(self, key: str, filter: KbFilter) -> bool:
        return await self._persistence.delete(key, filter)

    async def list_keys(
        self, filter: KbFilter, prefix: str | None = None,
    ) -> list[str]:
        return await self._persistence.list_keys(filter, prefix)

    async def search(
        self, query: str, filter: KbFilter, limit: int = 20,
    ) -> list[KbSearchResult]:
        return await self._retriever.search(query, filter, limit)
```

**为什么需要 Provider**: 消费者只需要一个依赖。Provider 是稳定门面 — 内部拆分是可替换内核,消费者不感知。两个 ABC 在装配点组合,运行时不可变。

## 7. SqliteKbPersistence 实现

`bot/kb/sqlite_persistence.py`。

**ConnectionManager API 修正**: v1 设计错误地假设 `connection.execute()` 返回 cursor。实际 API:
- `connection.query_one(sql, params) -> Row | None`
- `connection.query_all(sql, params) -> list[Row]`
- `connection.transaction(immediate=True) -> AsyncContextManager[Transaction]`
- `Transaction` 同样有 `execute()`, `query_one()`, `query_all()`

```python
# bot/kb/sqlite_persistence.py
from __future__ import annotations

from bot.kb.models import KbEntry, KbFilter, KbUpsertRequest
from bot.kb.persistence import KbPersistence
from bot.kb.sqlite_utils import build_filter_clauses
from modex_agent.persistence.connection import ConnectionManager
from modex_agent.utils.time import now_ms
from modex_graph.id_generator import default_id_generator


class SqliteKbPersistence(KbPersistence):
    """SQLite FTS5 持久化后端。

    共享 workspace async aiosqlite ConnectionManager。
    Snowflake PK (default_id_generator) + epoch ms (now_ms)。
    FTS5 外部内容模式 + trigram tokenizer。
    FTS5 triggers 自动同步索引 (写入时触发)。

    Mirror: SqliteTranscriptStore(ConnectionManager) pattern
    at bot/webui/sqlite_transcript_store.py
    """

    def __init__(self, connection: ConnectionManager) -> None:
        self._conn = connection

    # ── CRUD (过滤 WHERE 构建由 sqlite_utils.build_filter_clauses 提供) ──

    async def upsert(self, request: KbUpsertRequest) -> KbEntry:
        ts = now_ms()
        entry_id = default_id_generator().generate()

        async with self._conn.transaction(immediate=True) as tx:
            await tx.execute(
                """
                INSERT INTO kb_entries
                    (entry_id, key, value, task_id, session_id, category, tags,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, session_id, key) DO UPDATE SET
                    value = excluded.value,
                    category = excluded.category,
                    tags = excluded.tags,
                    updated_at = excluded.updated_at
                """,
                (entry_id, request.key, request.value,
                 request.task_id, request.session_id, request.category,
                 request.tags, ts, ts),
            )
            # FTS5 triggers 自动同步 (无需手动维护)

            # 读取实际写入的 entry (ON CONFLICT 时 entry_id 可能是旧的)
            row = await tx.query_one(
                "SELECT entry_id, key, value, task_id, session_id, "
                "category, tags, created_at, updated_at "
                "FROM kb_entries "
                "WHERE task_id = ? AND session_id = ? AND key = ?",
                (request.task_id, request.session_id, request.key),
            )

        if row is None:
            raise RuntimeError("upsert succeeded but row not found")
        return KbEntry(**dict(row))

    async def get(self, key: str, filter: KbFilter) -> KbEntry | None:
        clauses, params = build_filter_clauses(filter)
        clauses.insert(0, "key = ?")
        params.insert(0, key)

        sql = (
            "SELECT entry_id, key, value, task_id, session_id, "
            "category, tags, created_at, updated_at "
            f"FROM kb_entries WHERE {' AND '.join(clauses)} LIMIT 1"
        )
        row = await self._conn.query_one(sql, tuple(params))
        return KbEntry(**dict(row)) if row else None

    async def delete(self, key: str, filter: KbFilter) -> bool:
        clauses, params = build_filter_clauses(filter)
        clauses.insert(0, "key = ?")
        params.insert(0, key)

        sql = f"DELETE FROM kb_entries WHERE {' AND '.join(clauses)}"
        # query_value 返回 changes() — rowcount
        # 但 ConnectionManager 没有直接暴露 rowcount, 用 query_one 查 affected
        # 更简洁: 用 transaction + execute, 然后 query_value("SELECT changes()")
        async with self._conn.transaction(immediate=True) as tx:
            await tx.execute(sql, tuple(params))
            row = await tx.query_one("SELECT changes()")
        return row is not None and int(row[0]) > 0

    async def list_keys(
        self, filter: KbFilter, prefix: str | None = None,
    ) -> list[str]:
        clauses, params = build_filter_clauses(filter)
        if prefix is not None:
            escaped = (prefix
                       .replace("\\", "\\\\")
                       .replace("%", "\\%")
                       .replace("_", "\\_"))
            clauses.append("key LIKE ? ESCAPE '\\'")
            params.append(escaped + "%")

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT key FROM kb_entries {where_sql} ORDER BY key"
        rows = await self._conn.query_all(sql, tuple(params))
        return [r[0] for r in rows]
```

## 7.5 sqlite_utils.py — 共享过滤 helper

`bot/kb/sqlite_utils.py`。v3 收敛:三态过滤 WHERE 构建从 `SqliteKbPersistence._build_filter_clauses` 静态方法提取为独立模块函数,供 persistence 和 retriever 共用。

```python
# bot/kb/sqlite_utils.py
from __future__ import annotations

from bot.kb.models import KbFilter


def build_filter_clauses(
    filter: KbFilter,
    *,
    alias: str = "",
) -> tuple[list[str], list[str]]:
    """Build three-state filter WHERE clauses.

    None = skip (global); "" = public-only; "value" = isolated.
    alias: optional table alias prefix (e.g. "e." for JOINs).
    """
    clauses: list[str] = []
    params: list[str] = []
    if filter.task_id is not None:
        clauses.append(f"{alias}task_id = ?")
        params.append(filter.task_id)
    if filter.session_id is not None:
        clauses.append(f"{alias}session_id = ?")
        params.append(filter.session_id)
    if filter.category is not None:
        clauses.append(f"{alias}category = ?")
        params.append(filter.category)
    return clauses, params
```

**使用方**:
- `SqliteKbPersistence`: `build_filter_clauses(filter)` (alias 默认 "", 直接列名)
- `Fts5Retriever`: `build_filter_clauses(filter, alias="e.")` (JOIN 查询需要表别名前缀)

**收敛理由**: v2 中 persistence 有 `_build_filter_clauses` 静态方法,retriever 内联重复了相同的过滤逻辑。v3 提取为共享函数,消除重复,未来 VectorRetriever 也可复用。

## 8. Fts5Retriever 实现

`bot/kb/fts5_retriever.py`。负责 FTS5 全文搜索 + BM25 排序 + 查询净化。

```python
# bot/kb/fts5_retriever.py
from __future__ import annotations

from modex_agent.persistence.connection import ConnectionManager

from bot.kb.fts_utils import sanitize_fts_query
from bot.kb.models import KbEntry, KbFilter, KbSearchResult
from bot.kb.retriever import KbRetriever
from bot.kb.sqlite_utils import build_filter_clauses


class Fts5Retriever(KbRetriever):
    """FTS5 检索后端。

    共享 workspace async aiosqlite ConnectionManager (和 persistence 同一个)。
    FTS5 外部内容模式 + trigram tokenizer + BM25 ranking。
    CJK 兼容: trigram tokenizer 原生支持 CJK 子串匹配。

    直接执行 FTS5 MATCH 查询 — 不通过 persistence 接口。
    三态过滤 WHERE 构建复用 sqlite_utils.build_filter_clauses (alias="e.")。
    """

    def __init__(self, connection: ConnectionManager) -> None:
        self._conn = connection

    async def search(
        self,
        query: str,
        filter: KbFilter,
        limit: int = 20,
    ) -> list[KbSearchResult]:
        sanitized = sanitize_fts_query(query)
        if not sanitized:
            return []

        # 三态过滤 WHERE (共享 sqlite_utils.build_filter_clauses, alias="e." 用于 JOIN)
        where_clauses, filter_params = build_filter_clauses(filter, alias="e.")
        where_clauses.insert(0, "kb_entries_fts MATCH ?")
        params: list[str | int] = [sanitized]
        params.extend(filter_params)
        params.append(limit)

        sql = f"""
            SELECT e.entry_id, e.key, e.value, e.task_id, e.session_id,
                   e.category, e.tags, e.created_at, e.updated_at,
                   kb_entries_fts.rank as fts_rank
            FROM kb_entries_fts
            JOIN kb_entries e ON e.entry_id = kb_entries_fts.rowid
            WHERE {' AND '.join(where_clauses)}
            ORDER BY kb_entries_fts.rank
            LIMIT ?
        """
        rows = await self._conn.query_all(sql, tuple(params))

        results: list[KbSearchResult] = []
        for row in rows:
            d = dict(row)
            rank = d.pop("fts_rank", 0.0)
            entry = KbEntry(**d)
            # FTS5 rank 是负值 (lower = better), 转为正值分数
            score = abs(float(rank)) if rank else 0.0
            results.append(KbSearchResult(entry=entry, score=score))
        return results
```

## 9. FTS5 查询净化

`bot/kb/fts_utils.py`。参考 hermes `FactRetriever._sanitize_fts_query`。

```python
# bot/kb/fts_utils.py
"""FTS5 查询净化工具。

参考 hermes FactRetriever._sanitize_fts_query (retrieval.py:564-619)。

归属检索层 (不是持久化层) — FTS5 查询语法是搜索策略特定的。
hermes 的 MemoryStore.search_facts 延迟导入此函数导致循环导入,
本设计将 sanitize 放在检索层的独立模块中, 从 ABC 层面杜绝此问题。

trigram tokenizer 原生支持 CJK, 无需 hermes_state.py 的双表 + CJK 路由逻辑。
"""

import re

_MAX_QUERY_CHARS = 2048
_FTS_SPECIAL = '"()*^:-+'

# 参考 hermes _FTS_STOPWORDS
_FTS_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "he", "in", "is", "it", "its", "of", "on", "or",
    "that", "the", "this", "to", "was", "were", "will", "with",
    # CJK 无 stopwords — trigram 原生支持
})


def sanitize_fts_query(query: str) -> str:
    """将自然语言查询转为 FTS5 安全的 OR 表达式。

    步骤:
    1. 截断长度 (2048 字符)
    2. 分词 (按空格; CJK 连续字符作为整体 token)
    3. 剥离 FTS5 特殊字符
    4. 过滤短 token (< 2 字符) + 英文 stopwords
    5. 引号包裹每个 token (处理 FTS5 特殊字符)
    6. OR-join (提高召回率, 避免 AND-token 零召回)

    CJK 处理: trigram tokenizer 原生支持 CJK 子串匹配,
    不需要分词 — 连续 CJK 字符作为整体 token 传入 FTS5,
    trigram 会自动切分为 3-gram。
    """
    if not query or not query.strip():
        return ""
    query = query[:_MAX_QUERY_CHARS]

    tokens: list[str] = []
    for raw in query.split():
        cleaned = raw.strip(".,;:!?\"'()[]{}#@<>")
        cleaned = cleaned.translate(str.maketrans("", "", _FTS_SPECIAL))
        if len(cleaned) < 2:
            continue
        if cleaned.lower() in _FTS_STOPWORDS:
            continue
        tokens.append(f'"{cleaned}"')

    return " OR ".join(tokens) if tokens else ""
```

> **v3 note**: `fts_utils.py` 在 v3 review 中未改动。查询净化逻辑稳定,不受 persistence/retriever 收敛影响。

## 10. Builder Resolvers

`bot/kb/builder.py`。两个 builder + 一个 provider 组装函数。

Mirror: `build_database_transcript_store(connection)` at `bot/persistence/transcript.py:23`。

```python
# bot/kb/builder.py
from __future__ import annotations

from modex_agent.persistence.connection import ConnectionManager

from bot.kb.persistence import KbPersistence
from bot.kb.retriever import KbRetriever
from bot.kb.provider import KbProvider
from bot.kb.sqlite_persistence import SqliteKbPersistence
from bot.kb.fts5_retriever import Fts5Retriever


def build_sqlite_kb_persistence(
    connection: ConnectionManager,
) -> KbPersistence:
    """构建 SQLite 持久化后端 (sync, v3)。

    schema 由 BotWorkspaceMigrationRunner 自动应用 (001_webui_transcript.sql, modified)。
    Mirror: build_database_transcript_store(connection) at
    bot/persistence/transcript.py:23
    """
    return SqliteKbPersistence(connection)


def build_fts5_retriever(connection: ConnectionManager) -> KbRetriever:
    """构建 FTS5 检索后端。共享 persistence 的连接。"""
    return Fts5Retriever(connection)


def build_kb_provider(
    persistence: KbPersistence,
    retriever: KbRetriever,
) -> KbProvider:
    """组合 persistence + retriever 为 KbProvider 门面。"""
    return KbProvider(persistence=persistence, retriever=retriever)


async def build_default_kb_provider(
    connection: ConnectionManager,
) -> KbProvider:
    """默认组合: SqliteKbPersistence + Fts5Retriever (v3 converged)。

    仍为 async 以保持调用方 (resources.py) 的 await 兼容。
    内部无 await (build_sqlite_kb_persistence 已改为 sync)。
    装配点调用此函数即可。未来切换检索策略时,
    装配点改为 build_kb_provider(sqlite_persistence, hybrid_retriever)。
    """
    persistence = build_sqlite_kb_persistence(connection)
    retriever = build_fts5_retriever(connection)
    return build_kb_provider(persistence, retriever)
```

## 11. KbTool

`bot/tools/kb.py`。agent 调用的 KB tool。

命名: `KbTool`(不是 `TaskKbTool`)。KB 是通用知识库工具,task 隔离只是运行时可选的过滤维度,不是工具的定义特征。非 graph 场景(普通对话、外部 agent)同样使用。

**Description 面向使用方(agent)**: 不暴露内部实现细节(upsert / FTS5 / 三态过滤 / KbFilter),而是告诉 agent 何时用、怎么用。

Mirror: `SendFileToUserTool(Tool)` at `bot/tools/custom.py:29`。

```python
# bot/tools/kb.py
from __future__ import annotations

from collections.abc import Callable
from typing import assert_never

from bot.kb.formatting import (
    format_delete_confirmation,
    format_entry,
    format_key_list,
    format_search_results,
    format_upsert_confirmation,
)
from bot.kb.models import (
    KbAction,
    KbFilter,
    KbUpsertRequest,
)
from bot.kb.provider import KbProvider
from modex_agent.core.tool_manager import Tool, ToolConfig


class KbTool(Tool):
    """Agent 调用的 KB tool。action=get/set/search/delete/list。

    tool 内部从 env 拿 taskId 和 sessionId (agent 不感知这些值)。
    两个维度都是可选的运行时过滤 — 非 graph 场景也可以用 KB
    (普通对话、外部 agent),此时 task_id/session_id 为 None = 全局。

    隔离维度来源:
      - task_id: MODEX_TASK_ID env (图调度场景 = graphInstanceId;
        常规场景无此 env → None = 全局)
      - session_id: MODEX_SESSION_ID env (有则按 session 隔离;
        无则 None = 全局)

    响应格式化由 formatting.py 完成 (返回 str, 不用 json.dumps)。
    Mirror: SendFileToUserTool(Tool) at bot/tools/custom.py:29
    """

    def __init__(
        self,
        provider: KbProvider,
        task_id_provider: Callable[[], str | None],
        session_id_provider: Callable[[], str | None],
    ) -> None:
        self._provider = provider
        self._task_id_provider = task_id_provider
        self._session_id_provider = session_id_provider
        super().__init__(
            name="kb",
            description=(
                "Save and look up knowledge that persists across conversations. "
                "Use when you need to remember something for later, "
                "find what was previously saved, or search across stored notes.\n\n"
                "Actions:\n"
                "  set(key, value) — Save knowledge under a short key for later retrieval\n"
                "  get(key) — Retrieve a specific piece of knowledge by its key\n"
                "  search(query) — Find knowledge by searching its content\n"
                "  delete(key) — Remove knowledge by key\n"
                "  list(prefix?) — Browse saved keys, optionally filtered by prefix"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["set", "get", "search", "delete", "list"],
                        "description": "What to do with the knowledge base",
                    },
                    "key": {
                        "type": "string",
                        "description": "A short identifier for the knowledge (e.g. 'deploy-steps')",
                    },
                    "value": {
                        "type": "string",
                        "description": "The knowledge content to store",
                    },
                    "query": {
                        "type": "string",
                        "description": "Natural language search terms",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional: filter by category (e.g. 'project', 'config')",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 20)",
                    },
                    "prefix": {
                        "type": "string",
                        "description": "Optional: only list keys starting with this prefix",
                    },
                },
                "required": ["action"],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs) -> str:
        try:
            action = KbAction(kwargs["action"])
        except ValueError:
            return '{"error": "unknown action"}'

        task_id = self._task_id_provider()
        session_id = self._session_id_provider()
        filter = KbFilter(
            task_id=task_id,
            session_id=session_id,
            category=kwargs.get("category"),
        )

        match action:
            case KbAction.SEARCH:
                results = await self._provider.search(
                    kwargs["query"], filter, kwargs.get("limit", 20)
                )
                return format_search_results(results)
            case KbAction.GET:
                entry = await self._provider.get(kwargs["key"], filter)
                return format_entry(entry, kwargs["key"])
            case KbAction.SET:
                request = KbUpsertRequest(
                    key=kwargs["key"],
                    value=kwargs["value"],
                    task_id=task_id or "",
                    session_id=session_id or "",
                    category=kwargs.get("category", ""),
                )
                entry = await self._provider.upsert(request)
                return format_upsert_confirmation(entry)
            case KbAction.DELETE:
                deleted = await self._provider.delete(kwargs["key"], filter)
                return format_delete_confirmation(deleted, kwargs["key"])
            case KbAction.LIST:
                keys = await self._provider.list_keys(filter, kwargs.get("prefix"))
                return format_key_list(keys)
            case unreachable:
                assert_never(unreachable)
```

## 11.5 formatting.py — 共享响应格式化

`bot/kb/formatting.py`。v3 收敛:消费者面向的结构化文本格式化。KbTool 和 REST route 都直接调用 formatting 函数,返回 `str`。CLI 是透明 passthrough(echo REST route 返回的文本,自身不做格式化)。

**5 个 format 函数 + 1 个截断 helper**:

```python
# bot/kb/formatting.py
from bot.kb.models import KbEntry, KbSearchResult

_MAX_PREVIEW_LINES = 3
_MAX_PREVIEW_CHARS = 200


def format_search_results(results: list[KbSearchResult]) -> str:
    """搜索结果 → 编号列表, 每条含 key/category/score + 截断预览。"""

def format_entry(entry: KbEntry | None, key: str) -> str:
    """单条 entry → key + category + tags + 完整 value。None → "Not found"。"""

def format_upsert_confirmation(entry: KbEntry) -> str:
    """写入确认 → "Saved: <key> (category: <cat>)"。"""

def format_delete_confirmation(deleted: bool, key: str) -> str:
    """删除确认 → "Deleted: <key>" 或 "Not found: <key>"。"""

def format_key_list(keys: list[str]) -> str:
    """键列表 → "N key(s):" + 逐行 "- <key>"。"""

def _truncate_value(value: str) -> str:
    """搜索预览截断: 最多 3 行 / 200 字符, 超出则截断并提示 'use get'。"""
```

**设计要点**:

- **Projection**: 内部字段(entry_id, task_id, session_id, created_at, updated_at)从不出现在格式化输出中。agent 和用户只看到 key, value, category, tags, score。
- **搜索预览截断**: `format_search_results` 用 `_truncate_value` 将每条结果的 value 截断为最多 3 行 / 200 字符,超出则截断并提示 "use 'get' to see full content"。
- **消费者调用方式**:
  - KbTool: `execute()` 内直接调 `format_*` 函数,返回 `str`(不再是 `json.dumps`)
  - REST route: handler 内直接调 `format_*` 函数,`result` 是 `str`,`Response: {"result": "formatted text"}`
  - CLI: 透明 passthrough,`typer.echo(result.get("result", ""))`,自身不做格式化

**收敛理由**: v2 中每个消费者各自 `json.dumps` / `model_dump_json`,格式不一致且内部字段泄露。v3 提取 formatting.py 为单一格式化源,保证 projection 一致性和消费者面向的文本输出。

## 12. modexctl kb CLI

`bot/cli/modexctl/commands/kb.py`。闭包命令模式。

**两层隔离**:
1. **Workspace 数据源隔离**(与 deliver/send 一致): CLI 通过 `ctx.workspace_root` 指定 workspace,REST route 路由到该 workspace 的 `KbProvider`(不同 workspace = 不同 state.db = 不同 KB 数据源)。这不是 KB 内部过滤,是数据源级别的隔离。
2. **KB 内部隔离**(KbFilter): `task_id` 和 `session_id` 两个维度,三态语义。

**隔离维度来源**:
- `task_id`: `MODEX_TASK_ID` env(图调度场景 = graphInstanceId;常规场景无此 env → None = 无 task 隔离)。`--by-task` 控制是否使用,默认 `true`。
- `session_id`: `ctx.session_id`(modexctl context 始终携带,来自 `MODEX_SESSION_ID` comm env key)。始终作为隔离维度传入 — modexctl 运行在特定 session 上下文中,KB 操作默认 scope 到该 session。

**Help text 面向使用方**: 不暴露内部实现(FTS5 / 三态 / KbFilter),告诉用户怎么用、何时用。

Mirror: `build_deliver_command(ctx)` at `bot/cli/modexctl/commands/deliver.py:76`。

```python
# bot/cli/modexctl/commands/kb.py
from __future__ import annotations
import os
from collections.abc import Callable
from typing import Annotated, Any

import httpx
import typer

from bot.cli.modexctl.app import EXIT_ROUTING, EXIT_USAGE
from bot.cli.modexctl.context import (
    ModexCtlContext,
    _echo_context_error,
    _missing_comm_env_key,
)
from bot.cli.modexctl.http_client import ControlClientError, get_control_origin
from bot.kb.models import KbAction, KbControlRequest, KbFilter


def _fetch_kb(
    request: KbControlRequest,
    workspace: str,
) -> dict[str, Any]:
    """POST KB request to the shared REST route.

    Mirror: _fetch_deliver in deliver.py
    """
    origin = get_control_origin()
    url = f"{origin}/api/control/kb"
    params = {"workspace": workspace} if workspace else None

    try:
        with httpx.Client(
            timeout=httpx.Timeout(connect=1.0, read=10.0, write=10.0, pool=1.0)
        ) as client:
            response = client.post(
                url,
                json=request.model_dump(mode="json"),
                params=params,
            )
    except httpx.RequestError as exc:
        raise ControlClientError(
            f"Failed to connect to control server at {url}: {exc}"
        ) from exc

    if response.status_code != 200:
        try:
            body = response.json()
            detail = body.get("error", response.text[:200])
        except ValueError:
            detail = response.text[:200]
        raise ControlClientError(
            f"Control server returned HTTP {response.status_code}: {detail}",
            status=response.status_code,
        )

    try:
        return response.json()
    except ValueError as exc:
        raise ControlClientError(
            f"Control server returned non-JSON body: {exc}"
        ) from exc


def build_kb_command(ctx: ModexCtlContext) -> Callable[..., None]:
    """KB 命令。注册门控 = comm env keys (和 deliver/send 一样)。

    不要求 MODEX_TASK_ID — KB 可全局查询。
    --by-task 是运行时行为开关, 不是注册门控。
    session_id 始终从 ctx 取 (comm env 保证有值)。
    workspace 始终从 ctx 取 (数据源隔离, 不是 KB 内部隔离)。
    """

    def _kb(
        action: Annotated[str, typer.Argument(
            help="What to do: search, get, set, delete, or list"
        )],
        query_or_key: Annotated[str | None, typer.Argument(
            help="Search query (for search) or key (for get/set/delete)"
        )] = None,
        value: Annotated[str | None, typer.Option(
            "--value", "-v", help="Content to store (for set)"
        )] = None,

        # --by-task 接受 true/false, 默认 true
        by_task: Annotated[str, typer.Option(
            "--by-task",
            help="Scope to current task (true/false, default: true). "
                 "Set false to search all knowledge regardless of task.",
        )] = "true",

        category: Annotated[str | None, typer.Option(
            "--category", help="Filter by category"
        )] = None,
        limit: Annotated[int, typer.Option(
            "--limit", "-n", help="Maximum results (default 20)"
        )] = 20,
    ) -> None:
        """Search, store, and manage persistent knowledge."""

        # 1. 验证 comm env (能连 bot) — 和 deliver 命令一样
        missing = _missing_comm_env_key()
        if missing is not None:
            _echo_context_error(missing)
            raise typer.Exit(code=EXIT_USAGE)

        # 2. 验证 session context (workspace + control_origin) — 和 deliver 一样
        missing_ctx = ctx.validate_history()
        if missing_ctx is not None:
            _echo_context_error(missing_ctx)
            raise typer.Exit(code=EXIT_USAGE)

        # 3. 解析 action (KbAction StrEnum, 严格校验)
        try:
            kb_action = KbAction(action)
        except ValueError:
            typer.echo(f"error: invalid action '{action}'", err=True)
            raise typer.Exit(code=EXIT_USAGE) from None

        # 4. 解析 --by-task (严格: true/false/yes/no/on/off, 否则 EXIT_USAGE)
        normalized = by_task.lower()
        if normalized in ("true", "1", "yes", "on"):
            by_task_enabled = True
        elif normalized in ("false", "0", "no", "off"):
            by_task_enabled = False
        else:
            typer.echo(
                f"error: invalid --by-task value '{by_task}'. Use true or false.",
                err=True,
            )
            raise typer.Exit(code=EXIT_USAGE)

        # 5. task_id (raw os.environ.get, 保留三态: None=全局, ""=公共, "值"=隔离)
        task_id = os.environ.get("MODEX_TASK_ID") if by_task_enabled else None

        # 6. 构建 filter (KbFilter, task_id 和 session_id 都是隔离维度)
        filter = KbFilter(
            task_id=task_id,
            session_id=ctx.session_id,
            category=category,
        )

        # 7. REST POST 到 bot (workspace = 数据源隔离)
        assert ctx.workspace_root is not None
        request = KbControlRequest(
            action=kb_action,
            query_or_key=query_or_key,
            value=value,
            filter=filter,
            limit=limit,
        )
        try:
            result = _fetch_kb(request, ctx.workspace_root)
        except ControlClientError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=EXIT_ROUTING) from exc

        typer.echo(result.get("result", ""))

    return _kb
```

**CLI 使用示例**:

```bash
# 搜索 (默认按当前 task + session 隔离)
modexctl kb search "部署流程"

# 显式跨 task 搜索 (--by-task false)
modexctl kb search "部署流程" --by-task false

# 常规场景 (无 MODEX_TASK_ID env) → 自动无 task 隔离, 仅按 session 隔离
modexctl kb search "部署流程"

# 写入 (自动按当前 task + session 隔离)
modexctl kb set "架构约束" --value "..." --category project

# 精确读取
modexctl kb get "架构约束"

# 列出键
modexctl kb list
```

## 13. REST Route

```
POST /api/control/kb?workspace=<path>
Body: KbControlRequest (JSON)
  {
    "action": "search" | "get" | "set" | "delete" | "list",   # KbAction StrEnum
    "query_or_key": str | null,
    "value": str | null,
    "filter": {                                                # KbFilter
        "task_id": str | null,
        "session_id": str | null,
        "category": str | null
    },
    "limit": int                                               # Field(ge=1, le=100)
  }
Response: 200 OK + {"result": "<formatted text>"}
```

Route handler 在 `bot/webui/routes/kb_routes.py` 中。`body = KbControlRequest.model_validate(await request.json())` 解析请求,持有 workspace 的 `KbProvider` 引用,直接调 provider 方法。每个 case 调 formatting 函数格式化结果,`result` 是 `str`(不是 dict/list)。

```python
# bot/webui/routes/kb_routes.py
from bot.kb.formatting import (
    format_delete_confirmation,
    format_entry,
    format_key_list,
    format_search_results,
    format_upsert_confirmation,
)
from bot.kb.models import KbAction, KbControlRequest, KbUpsertRequest


async def handle_kb_post(request: web.Request) -> web.Response:
    workspace = request.query.get("workspace")
    # ... resolve provider from workspace ...

    body = KbControlRequest.model_validate(await request.json())

    match body.action:
        case KbAction.SEARCH:
            results = await provider.search(body.query_or_key, body.filter, body.limit)
            result = format_search_results(results)
        case KbAction.GET:
            entry = await provider.get(body.query_or_key, body.filter)
            result = format_entry(entry, body.query_or_key)
        case KbAction.SET:
            entry = await provider.upsert(KbUpsertRequest(...))
            result = format_upsert_confirmation(entry)
        case KbAction.DELETE:
            deleted = await provider.delete(body.query_or_key, body.filter)
            result = format_delete_confirmation(deleted, body.query_or_key)
        case KbAction.LIST:
            keys = await provider.list_keys(body.filter, body.query_or_key)
            result = format_key_list(keys)
        case unreachable:
            assert_never(unreachable)

    return web.json_response({"result": result})
```

**workspace 参数** (`?workspace=<path>` query param): 决定路由到哪个 workspace 的 KbProvider — 这是数据源隔离,不是 KB 内部过滤。不同 workspace 有不同的 state.db 和 KbProvider 实例。

## 14. 两层隔离模型

KB 有两层隔离,必须区分:

### 14.1 Workspace 数据源隔离 (与 deliver/send 一致)

每个 workspace 有独立的 `state.db` → 独立的 `ConnectionManager` → 独立的 `KbProvider`。CLI 通过 `?workspace=<path>` query param 指定 workspace,REST route 路由到该 workspace 的 KbProvider。

这不是 KB 内部过滤 — 是物理数据源隔离。workspace A 的 KB 数据对 workspace B 完全不可见(不同的数据库文件)。

**实现**: `_assemble_resources` 为每个 workspace 构建独立的 `KbProvider`(§15.1)。CLI 传 workspace 参数(§12)。REST route 按 workspace 解析 KbProvider(§13)。

### 14.2 KB 内部隔离 (KbFilter 三态)

在同一个 workspace 的 KB 内,通过 `KbFilter` 的三态语义过滤:

| 维度 | 来源 | None (全局) | "" (公共) | "具体值" (隔离) |
|------|------|-------------|-----------|----------------|
| `task_id` | `MODEX_TASK_ID` env (图调度 = graphInstanceId) | 无 task 隔离 (常规场景) | 只查公共知识 | 只查该 task 的知识 |
| `session_id` | `ctx.session_id` (CLI) / `MODEX_SESSION_ID` env (Tool) | 无 session 隔离 | 只查公共知识 | 只查该 session 的知识 |
| `category` | CLI `--category` / Tool `category` param | 无 category 过滤 | 只查无分类 | 只查该 category |

**关键**: 不传入的维度 (None) = 无该维度隔离。当前 `task_id` 和 `session_id` 都作为隔离维度使用。

## 15. Workspace 装配接线

### 15.1 resources.py — persistence + retriever + provider 构建

`bot/workspace/wiring/resources.py` — `_assemble_resources` 中,在 persistence 打开之后(transcript_store 接线之后)。

**每个 workspace 构建独立的 KbProvider** — 数据源隔离。不同 workspace 的 KB 互不可见。

Mirror: `build_database_transcript_store(persistence.connection)` at `resources.py:165`。

```python
# bot/workspace/wiring/resources.py — _assemble_resources 中
# 在 workspace_transcript_store 构建之后

kb_provider: KbProvider | None = None
if persistence is not None:
    from bot.kb.builder import build_default_kb_provider
    kb_provider = await build_default_kb_provider(persistence.connection)

resources = PoolWorkspaceResources(
    # ... existing fields ...
    kb_provider=kb_provider,  # NEW — 每个 workspace 独立
)
```

`PoolWorkspaceResources` (`bot/workspace/handle.py`) 加 `kb_provider: KbProvider | None` 字段。

### 15.2 _assembly_helpers.py — KbTool 注册

`bot/service/_assembly_helpers.py` — `_build_tools` 中,在 ExperienceTool 注册之后。

KbTool 注入两个 provider: `task_id_provider` (读 `MODEX_TASK_ID`) 和 `session_id_provider` (读 `MODEX_SESSION_ID`)。两者都从 env 读,有值=隔离,无值=None=全局。

```python
# 在 ExperienceTool 注册之后

# KB tool — kb_provider 作为关键字参数传入 _build_tools (不携带在 PoolData 上)
if kb_provider is not None:
    from bot.tools.kb import KbTool
    task_id_provider = _make_task_id_provider()
    session_id_provider = _make_session_id_provider()
    tm.register(KbTool(kb_provider, task_id_provider, session_id_provider))
    logger.info("Pool '%s': kb tool registered", pool_name)


def _make_task_id_provider() -> Callable[[], str | None]:
    """从 env 拿 taskId。图调度时 env 已注入 (graphInstanceId)。

    MODEX_TASK_ID 由 env_builder.py:90-91 注入 (当 spec.task_id 有值时)。
    当前 spec.task_id 永远 None (框架层未接线), 所以这里拿 None = 全局。
    后续框架层接线后自动生效, 业务层零改动。

    非 graph 场景 (普通对话) env 无 MODEX_TASK_ID → None = 无 task 隔离。

    v3: 返回 raw os.environ.get() (不做 tid if tid else None 转换)。
    保留三态: env 不存在 = None = 全局; env 存在但空 = "" = 公共; env 有值 = 隔离。
    """
    def _provider() -> str | None:
        return os.environ.get("MODEX_TASK_ID")  # raw: None=全局, ""=公共, "值"=隔离
    return _provider


def _make_session_id_provider() -> Callable[[], str | None]:
    """从 env 拿 sessionId。

    MODEX_SESSION_ID 在 agent 进程中可能存在 (图调度注入 / 外部 agent 注入)。
    有值 = 按 session 隔离; 无值 (None) = 无 session 隔离 (全局)。

    v3: 返回 raw os.environ.get() (保留三态)。
    """
    def _provider() -> str | None:
        return os.environ.get("MODEX_SESSION_ID")  # raw: None=全局, ""=公共, "值"=隔离
    return _provider
```

**注意**: `kb_provider` 作为关键字参数传入 `_build_tools` (不携带在 `PoolData` 上)。`_build_tools` 签名见 `bot/service/_assembly_helpers.py`;`kb_provider` 由 `PoolWorkspaceResources` 构建并透传。

### 15.3 app.py — 注册 CLI 命令

`bot/cli/modexctl/app.py` — `build_app` 中添加。Help text 面向使用方。

```python
# KB 命令: 注册门控 = comm env (和 deliver/send 一样)
app.command(name="kb", help="Search, store, and manage persistent knowledge.")(
    build_kb_command(ctx)
)
```

## 16. 目录结构

```
examples/bot_project/bot/
├── kb/                              # NEW — KB feature package
│   ├── __init__.py
│   ├── models.py                    # KbAction, KbFilter, KbEntry, KbUpsertRequest, KbSearchResult, KbControlRequest
│   ├── persistence.py               # KbPersistence(ABC)
│   ├── retriever.py                 # KbRetriever(ABC)
│   ├── provider.py                  # KbProvider (门面)
│   ├── sqlite_persistence.py        # SqliteKbPersistence(ConnectionManager)
│   ├── sqlite_utils.py              # build_filter_clauses (共享过滤 helper)
│   ├── fts5_retriever.py            # Fts5Retriever(ConnectionManager)
│   ├── fts_utils.py                 # sanitize_fts_query
│   ├── formatting.py                # format_search_results / format_entry / ... (共享响应格式化)
│   └── builder.py                   # build_sqlite_kb_persistence / build_fts5_retriever / build_default_kb_provider
├── tools/
│   ├── custom.py                    # (existing) SendFileToUserTool
│   └── kb.py                        # NEW — KbTool(Tool)
├── cli/modexctl/
│   ├── app.py                       # (modified) 注册 kb 命令
│   └── commands/
│       └── kb.py                    # NEW — build_kb_command(ctx)
├── persistence/
│   └── migrations/workspace/
│       └── 001_webui_transcript.sql  # MODIFIED — 追加 kb_entries + FTS5 + triggers
└── webui/routes/
    └── kb_routes.py                  # NEW — POST /api/control/kb route handler
```

**未来向量后端**:
```
bot/kb/
├── vector_retriever.py              # FUTURE — VectorRetriever(KbRetriever)
├── hybrid_retriever.py              # FUTURE — HybridRetriever(KbRetriever)
bot/persistence/migrations/workspace/
└── 003_kb_embeddings.sql            # FUTURE — kb_embeddings 表 (VectorRetriever 的 schema)
```

## 17. 实现顺序

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

## 18. ConnectionManager API 参照

本设计严格遵循 `ConnectionManager` 实际 API(`src/modex_agent/persistence/connection.py:61`):

```python
class ConnectionManager:
    # 非事务操作 (自动加锁, 返回结果)
    async def execute(self, sql: str, parameters=()) -> None
    async def query_all(self, sql: str, parameters=()) -> list[Row]
    async def query_one(self, sql: str, parameters=()) -> Row | None
    async def query_value(self, sql, value_type, parameters=()) -> ValueT

    # 事务 (手动控制, 返回 Transaction 句柄)
    @asynccontextmanager
    async def transaction(self, *, immediate=False) -> AsyncIterator[Transaction]:
        # yield Transaction
        # 异常时自动 rollback, 正常时自动 commit

class Transaction:
    async def execute(self, sql: str, parameters=()) -> None
    async def executemany(self, sql, parameters) -> None
    async def query_all(self, sql, parameters=()) -> list[Row]
    async def query_one(self, sql, parameters=()) -> Row | None
    async def query_value(self, sql, value_type, parameters=()) -> ValueT
```

**关键**: `connection.execute()` 返回 `None`(不是 cursor)。读取必须用 `query_one()` / `query_all()`。事务内用 `Transaction` 句柄的 `query_one()` / `query_all()`。

`Row` 是 `sqlite3.Row`,可通过 `dict(row)` 转为字典。

## 19. 向量检索后端设计(未来)

`VectorRetriever` — 检索层,不碰 persistence 的 CRUD。

```python
# bot/kb/vector_retriever.py (未来)
"""向量检索后端。使用 kb_embeddings 表 + cosine similarity。

依赖: numpy + embedding 模型 (OpenAI / 本地模型)

检索管线:
1. query → embedding (调 embedding 模型)
2. cosine similarity(query_vec, entry_vec) 遍历 kb_embeddings
3. score 加权 (可选)
4. 返回 top-N

共享 persistence 的 ConnectionManager — 直接查 kb_embeddings 表。
kb_embeddings schema 由 003_kb_embeddings.sql 创建 (VectorRetriever 的 schema)。

embedding 写入: VectorRetriever 可选实现 ensure_embedding(entry) 方法,
在 upsert 后由 persistence 调用 (或由 provider 协调)。
当前设计: embedding 写入是检索层的附加操作, 不在 persistence ABC 中。
"""

class VectorRetriever(KbRetriever):
    """向量检索后端。共享 kb_entries 主表, 独立 kb_embeddings 向量表。"""

    def __init__(
        self,
        connection: ConnectionManager,
        embed_fn: Callable[[str], list[float]],  # embedding 函数注入
        model_name: str = "default",
        dim: int = 1536,
    ) -> None:
        self._conn = connection
        self._embed = embed_fn
        self._model = model_name
        self._dim = dim

    async def search(self, query, filter, limit=20) -> list[KbSearchResult]:
        import numpy as np
        query_vec = self._embed(query)
        query_arr = np.array(query_vec, dtype=np.float32)

        # 三态过滤 WHERE (同 Fts5Retriever 模式)
        where_clauses = ["emb.model = ?"]
        params: list = [self._model]
        if filter.task_id is not None:
            where_clauses.append("e.task_id = ?")
            params.append(filter.task_id)
        if filter.category is not None:
            where_clauses.append("e.category = ?")
            params.append(filter.category)

        sql = f"""
            SELECT e.entry_id, e.key, e.value, e.task_id, e.session_id,
                   e.category, e.tags, e.created_at, e.updated_at,
                   emb.embedding
            FROM kb_embeddings emb
            JOIN kb_entries e ON e.entry_id = emb.entry_id
            WHERE {' AND '.join(where_clauses)}
        """
        rows = await self._conn.query_all(sql, tuple(params))

        # cosine similarity (应用层)
        scored = []
        for row in rows:
            d = dict(row)
            emb_bytes = d.pop("embedding")
            entry_vec = np.frombuffer(emb_bytes, dtype=np.float32)
            cos_sim = float(
                np.dot(query_arr, entry_vec) /
                (np.linalg.norm(query_arr) * np.linalg.norm(entry_vec))
            )
            scored.append(KbSearchResult(entry=KbEntry(**d), score=cos_sim))

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:limit]
```

## 20. 混合检索后端设计(未来)

`HybridRetriever` — 组合 FTS5 召回 + 向量 rerank。解耦架构的核心价值。

```python
# bot/kb/hybrid_retriever.py (未来)
"""混合检索: FTS5 召回 + 向量 rerank。

解耦架构的核心价值 — 组合两个 retriever, 不碰 persistence。
参考 hermes FactRetriever.search() 权重: fts=0.4, jaccard=0.3, hrr=0.3。
"""

class HybridRetriever(KbRetriever):
    def __init__(
        self,
        candidate: KbRetriever,       # Fts5Retriever (召回)
        reranker: KbRetriever,        # VectorRetriever (精排)
        candidate_factor: int = 3,    # 召回 limit × factor
        weights: dict[str, float] | None = None,
    ) -> None:
        self._candidate = candidate
        self._reranker = reranker
        self._factor = candidate_factor
        self._weights = weights or {"candidate": 0.4, "reranker": 0.6}

    async def search(self, query, filter, limit=20) -> list[KbSearchResult]:
        # 1. FTS5 召回 (扩大 limit)
        candidates = await self._candidate.search(
            query, filter, limit * self._factor
        )
        if not candidates:
            return []

        # 2. 向量 rerank (对候选重新打分)
        reranked = await self._reranker.search(
            query, filter, limit * self._factor
        )

        # 3. 加权合并 (候选 score + rerank score)
        rerank_map = {r.entry.entry_id: r.score for r in reranked}
        merged = []
        for c in candidates:
            rerank_score = rerank_map.get(c.entry.entry_id, 0.0)
            final = (
                c.score * self._weights["candidate"]
                + rerank_score * self._weights["reranker"]
            )
            merged.append(KbSearchResult(entry=c.entry, score=final))

        merged.sort(key=lambda x: x.score, reverse=True)
        return merged[:limit]
```

## 21. 关键注意事项

1. **不要镜像 modex_graph 模式** — DeliverStore / sync sqlite3 是 graph 体系,KB 不参照
2. **用 async aiosqlite** — 共享 workspace ConnectionManager,不用 sync sqlite3
3. **ConnectionManager API** — `query_one()` / `query_all()` / `transaction()`,不是 `execute().fetchone()`
4. **Snowflake PK** — `default_id_generator().generate()`,不用 AUTOINCREMENT
5. **epoch ms** — `now_ms()`,不用 `CURRENT_TIMESTAMP`
6. **隔离字段默认 `''`** — 不用 NULL(避免 SQLite UNIQUE NULL 歧义)
7. **frozen Pydantic** — 所有 model `frozen=True, extra="forbid"`(项目规范)
8. **ABCs before implementations** — KbPersistence(ABC) + KbRetriever(ABC) 先,SqliteKbPersistence + Fts5Retriever 后
9. **迁移文件不含事务控制** — BotWorkspaceMigrationRunner 在 transaction 中执行
10. **`--by-task` 严格校验** — 接受 true/false/yes/no/on/off(大小写不敏感),其他值报 EXIT_USAGE。默认 "true"(v3: 从宽松 truthy 判断收紧)
11. **KB 命令注册门控 = comm env keys** — 不依赖 MODEX_TASK_ID
12. **KbTool 不是 TaskKbTool** — KB 是通用知识库工具,task 隔离是可选运行时维度
13. **Persistence 无 search 方法** — 搜索是检索层职责,从 ABC 层面杜绝 hermes 的循环导入问题
14. **Retriever 共享连接,不通过 persistence 接口** — 搜索 SQL/FTS5 语法是检索策略特定的
15. **FTS5 triggers 属 persistence** — 索引同步是写入的副作用,存储层关注点
16. **两层隔离必须区分** — workspace 数据源隔离 (不同 state.db) ≠ KB 内部隔离 (KbFilter 三态)。详见 §14
17. **task_id 和 session_id 都是隔离维度** — task_id 来自 MODEX_TASK_ID env (图调度=graphInstanceId, 常规场景无),session_id 来自 ctx.session_id (CLI) / MODEX_SESSION_ID env (Tool)。不传入的维度 = 无该维度隔离
18. **Description 面向使用方** — KbTool 和 CLI 的 help/description 面向 agent/用户,不暴露内部实现 (upsert/FTS5/三态/KbFilter)。告诉使用方何时用、怎么用
19. **modexctl 校验与 deliver 一致** — `_missing_comm_env_key()` + `ctx.validate_history()`,workspace 从 `ctx.workspace_root` 取 (数据源隔离)

**v3 review 修复**:

20. **formatting.py 是响应格式化单一来源** — KbTool 和 REST route 都直接调用 formatting.py 的 format 函数,返回 `str`。不在消费者中各自 `json.dumps` 或 `model_dump_json`。projection(过滤内部字段)在 formatting.py 统一完成。
21. **CLI 是透明 passthrough** — CLI 不做格式化,只 `typer.echo(result.get("result", ""))`。格式化在 REST route 端完成,CLI 透传文本。
22. **UNIQUE(task_id, session_id, key)** — session_id 是 upsert 语义的一部分(v3 变更)。同一 task + session 下 key 唯一;不同 task 或 session 可以有相同 key。ON CONFLICT 子句同步更新。
23. **build_sqlite_kb_persistence 是 sync** — v3 从 `async def` 改为 `def`。`build_default_kb_provider` 仍为 `async`(调用方 resources.py 用 `await`),但内部无 await。
24. **env providers 返回 raw os.environ.get()** — 不做 `tid if tid else None` 转换。保留三态:env 不存在 = None = 全局;env 存在但空 = "" = 公共;env 有值 = 隔离。
25. **KbControlRequest.limit bounded [1, 100]** — `Field(ge=1, le=100)` 限定 limit 范围,防止过大请求。
26. **assert 替换为 RuntimeError** — `SqliteKbPersistence.upsert` 中 `assert row is not None` 改为 `if row is None: raise RuntimeError(...)`。assert 在 Python -O 模式下会被移除,不适合做运行时校验。
