# Column Projection — SQLite adapter field extraction abstraction

## Status

Accepted (2026-07-19).

## Context

The split store ABCs (`MessageStore`, `KVStore`, `CursorStore`,
`ArchiveStore`) and the runtime-state ABCs (`InboxMQ`, `TurnStateStore`,
`SessionStore`, `TodoStore`, `ApprovalAuditStore`, etc.) all expose
**domain objects** in their contracts: `dict[str, Any]` for messages,
`InboxMessage` for inbox, `TurnSnapshot` for turn state, `SessionInfo` for
sessions, `list[TodoItem]` for todos. The file backend
(`DefaultScopedStorage`, `LocalFileInboxMQ`, `JsonFileTodoStore`) stores
these objects **wholesale** as JSON (`json.dumps(message)` → one line in
`messages.jsonl`). The SQLite backend must store them **split across table
columns plus a residual JSON column**, because certain fields need
`WHERE`/`ORDER BY`/`CHECK`/index access that a single JSON blob cannot
provide.

Before this ADR, the split was done **imperatively in each adapter**:

```python
# SqliteMessageStore.append_message (before)
role = str(message.get("role", "user"))
await conn.execute(
    "INSERT INTO memory_session_messages "
    "(scope_key, scope, seq, role, message_json, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?)",
    (self._scope_json, self._scope_json, seq, role,
     json.dumps(message, ensure_ascii=False), time.time()),
)
```

Problems with this approach:

1. **Hard-coded field lists scattered across adapters.** Adding a new
   extracted column (e.g. `message_id` to `memory_session_messages`) meant
   editing the `INSERT` column list, the `VALUES` placeholder count, the
   `SELECT` column list, the row-to-dict reconstruction, and the
   `WHERE`-clause field extraction — five sites per adapter, with no
   compile-time check that they stay in sync.

2. **`if isinstance(content, list): json.dumps(...)` branches multiplied.**
   `content` can be `str` or `list[dict]` (multimodal, though sanitized to
   `str` before persistence by `Base64SanitizeTransformer`). The type-test
   logic was inlined at every write site, with no reusable encoding.

3. **No symmetry between write and read.** The write path extracted
   fields into columns; the read path had a different, manually-maintained
   reconstruction that re-loaded fields from columns and merged them back
   into the JSON dict. The two paths could drift.

4. **Deep-module seam unclear.** The ABC contract is `dict[str, Any]`;
   the file backend treats it as opaque; the SQLite backend peeks inside.
   Where exactly the seam lives — which fields are "extracted" vs "residual
   JSON" — was implicit in each adapter's SQL strings, not declared.

## Decision

### 1. New abstraction: `ColumnProjection`

`modex_agent/persistence/column_projection.py` defines a declarative
field-mapping abstraction:

```python
@dataclass(frozen=True)
class ColumnField:
    column: str                       # DB column name
    dict_keys: tuple[str, ...]        # candidate dict keys (first hit wins)
    codec: ColumnCodec | None = None  # optional non-identity encoding

@dataclass(frozen=True)
class ColumnCodec(ABC):
    """Field value conversion between dict and DB column(s).
    
    A single codec may fan out into multiple columns (e.g. ContentCodec
    writes both `content` and `is_content_json`)."""
    @abstractmethod
    def encode(self, column: str, value: Any) -> dict[str, Any]: ...
    @abstractmethod
    def decode(self, columns: dict[str, Any]) -> Any: ...

@dataclass(frozen=True)
class IdentityCodec(ColumnCodec):
    """Default: pass-through, no transformation."""
    def encode(self, column, value): return {column: value}
    def decode(self, columns): return columns[self._column]

@dataclass(frozen=True)
class ContentCodec(ColumnCodec):
    """`content` field: str passes through; list[dict] encoded as JSON string.
    Writes companion `is_content_json` flag column."""
    def encode(self, column, value):
        if isinstance(value, list):
            return {column: json.dumps(value, ensure_ascii=False),
                    "is_content_json": 1}
        return {column: value, "is_content_json": 0}
    def decode(self, columns):
        v = columns[self._column]
        if v is None: return None
        if columns.get("is_content_json"): return json.loads(v)
        return v

@dataclass(frozen=True)
class ColumnProjection:
    fields: tuple[ColumnField, ...]
    json_column: str = "message_json"
    
    def split(self, data: dict) -> tuple[dict[str, Any], str]:
        """dict → (column values, residual JSON string)."""
    
    def assemble(self, columns: dict[str, Any], json_str: str) -> dict[str, Any]:
        """(column values, residual JSON string) → dict."""
```

### 2. Adapter usage is declarative

```python
# SqliteMessageStore
_MESSAGE_PROJECTION = ColumnProjection(
    fields=(
        ColumnField(column="message_id", dict_keys=("message_id", "id")),
        ColumnField(column="role", dict_keys=("role",)),
        ColumnField(column="content", dict_keys=("content",),
                    codec=ContentCodec()),
        ColumnField(column="token_count", dict_keys=("token_count",)),
    ),
    json_column="message_json",
)

async def append_message(self, message: dict) -> StorageRevision:
    cols, residual = _MESSAGE_PROJECTION.split(message)
    await conn.execute(
        "INSERT INTO memory_session_messages "
        "(scope_key, seq, message_id, role, content, is_content_json, "
        " token_count, message_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (self._scope_key, seq,
         cols.get("message_id"), cols.get("role"),
         cols.get("content"), cols.get("is_content_json", 0),
         cols.get("token_count"), residual, now_ms()),
    )
```

Adding a new extracted column is **one line** in the projection tuple.
The `INSERT`/`SELECT`/`WHERE` sites follow automatically because they
read from the projection.

### 3. Residual JSON excludes extracted fields

`split()` removes every `dict_key` (all candidates) from the dict before
serializing the residual. `assemble()` re-injects the extracted values
under the **first** `dict_key` only. This means:

- The residual JSON column never contains duplicate data already in
  extracted columns.
- On read-back, the dict is reconstructed with the canonical key name
  (the first candidate), unifying legacy `id` and `message_id` into
  `message_id`.

### 4. File backend does not use `ColumnProjection`

`DefaultScopedStorage.append_message(message: dict)` continues to do
`json.dumps(message)` wholesale. The file backend's storage shape is a
single JSON line per message; there are no "columns" to split into. The
deep-module seam is: **ABC contract is `dict`; file backend stores it
wholesale; SQLite backend splits it via `ColumnProjection`.**

This means the file backend's `messages.jsonl` contains the **full**
dict (including `message_id`, `role`, `content`, `token_count`), while
the SQLite backend's `message_json` column contains only the **residual**
(after extraction). The two backends have different storage shapes but
the same ABC contract — exactly the deep-module property we want.

### 5. Scope of application

`ColumnProjection` is applied to tables where the JSON column carries a
**dict with extractable fields**:

| Table | Projection | Extracted columns |
|-------|-----------|-------------------|
| `memory_session_messages` | `_MESSAGE_PROJECTION` | `message_id`, `role`, `content`, `is_content_json`, `token_count` |
| `inbox_messages` | `_INBOX_PROJECTION` | `message_id`, `message_type`, `session_id` |

Tables where the JSON column carries an **opaque serialized object** (not
a field-extractable dict) do **not** use `ColumnProjection`:

| Table | JSON column | Why not |
|-------|------------|---------|
| `turn_snapshots` | `payload_json` | Serialized `TurnSnapshot` via `RuntimeStateCodec`; fields like `phase`/`agent_kind` are already explicit columns, the payload is opaque |
| `approval_audit_log` | (none) | All fields are explicit columns; no JSON column |
| `memory_kv` | `value_json` | Value is arbitrary JSON, no fields to extract |
| `memory_archive_state` | `state_json` | Opaque state dict |
| `sessions` | `metadata_json` | `SessionInfo.metadata` (Pydantic `extra='allow'`); opaque |
| `workspaces` | `metadata_json` | Opaque extension metadata |
| `todos` | `items_json` | Serialized `list[TodoItem]`; opaque |
| `bot_webui_transcript_events` | `payload_json` | Serialized `ServerEvent`; opaque |

### 6. `ContentCodec` and the multimodal edge case

`ChatMessage.content` is `str | list[dict] | None`. The framework's
`Base64SanitizeTransformer` (`memory/content_transform.py`) runs before
persistence and replaces `image_url` blocks with `data:` URIs by text
placeholders, collapsing most `list[dict]` content to `str`. The only
`list[dict]` that survives sanitization is `image_url` with `http(s)`
URLs (not sanitized, kept as-is) — a rare edge case in v1 (mechanism A
is dormant per ADR-0013 §9).

`ContentCodec` handles both: `str` → stored as-is with
`is_content_json=0`; `list[dict]` → `json.dumps` with
`is_content_json=1`. On read-back, `is_content_json` determines whether
to `json.loads` the column back to a list. This preserves the original
type through the round-trip without inflating the common case.

## Consequences

- **Adding an extracted column is a one-line change** to the projection
  tuple, not a five-site scatter. The `INSERT`/`SELECT` SQL still needs
  updating (the column list is not generated dynamically — that would
  require string-interpolating column names, which we reject for SQL
  injection safety), but the field-extraction logic is centralized.

- **File backend and SQLite backend diverge in storage shape, not in
  ABC contract.** `messages.jsonl` has the full dict; `message_json` has
  the residual. A `FILE` → `SQLITE` backend switch (via
  `PersistenceConfig.backend`) changes the storage shape but not the
  data the ABC returns — `load_messages()` returns the same `dict` either
  way.

- **`ColumnProjection` is unit-testable in isolation.** `split`/`assemble`
  round-trip property tests cover all codec combinations without needing
  a database. Adapter integration tests then verify the SQL wiring.

- **`is_content_json` is a companion column, not an independent field.**
  It is always written and read alongside `content` via `ContentCodec`.
  It never appears in a `ColumnField` of its own — that would decouple it
  from `content` and reintroduce the sync problem.

- **Future codec extensions** (e.g. `TokenCountCodec` that defaults to
  `NULL` when absent, or a `RoleCodec` that validates against
  `MessageRole`) plug in without touching `ColumnProjection`'s core
  logic.

- **Not applied to opaque-payload tables.** `turn_snapshots.payload_json`
  stays a `json.dumps(snapshot_payload)` wholesale; the explicit columns
  (`phase`, `agent_kind`, `reason`, `schema_version`) are written
  directly by the adapter because they come from `TurnSnapshot`'s typed
  fields, not from a dict's string keys. Forcing `ColumnProjection` here
  would add ceremony without benefit.
