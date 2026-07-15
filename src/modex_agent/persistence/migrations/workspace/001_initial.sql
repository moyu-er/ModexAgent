-- Workspace DB initial schema (T06)
-- Source of truth: docs/design/hybrid-persistence/SCHEMA-DESIGN.md
-- schema_migrations is created by MigrationRunner._ensure_table(); not duplicated here.

-- Sessions
CREATE TABLE sessions (
    session_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT    NOT NULL UNIQUE,
    scope              TEXT    NOT NULL CHECK (json_valid(scope)),
    pool               TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    agent_id           TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.agent_id')) STORED,
    session_prefix     TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.session_prefix')) STORED,
    invocation_id      TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.invocation_id')) STORED,
    parent_session_id  TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.parent_session_id')) STORED,
    parent_session_pk  INTEGER REFERENCES sessions(session_pk) ON DELETE SET NULL,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    metadata_json      TEXT    CHECK (metadata_json IS NULL OR json_valid(metadata_json))
);

CREATE INDEX idx_sessions_pool_prefix ON sessions (pool, session_prefix) WHERE pool IS NOT NULL;
CREATE INDEX idx_sessions_prefix      ON sessions (session_prefix);
CREATE INDEX idx_sessions_parent      ON sessions (parent_session_id);
CREATE INDEX idx_sessions_pool_agent  ON sessions (pool, agent_id) WHERE pool IS NOT NULL;

-- Pool Routing
CREATE TABLE pool_routing (
    session_prefix  TEXT    PRIMARY KEY,
    pool_name       TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_routing_pool ON pool_routing (pool);

-- Inbox Topics
CREATE TABLE inbox_topics (
    topic_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_scope_key TEXT    NOT NULL COLLATE BINARY CHECK (json_valid(owner_scope_key)),
    scope_key       TEXT    NOT NULL COLLATE BINARY,
    session_id      TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    state           TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'active', 'idle', 'expired')),
    created_at      REAL    NOT NULL,
    last_active     REAL    NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0,
    consumer_task   TEXT,
    UNIQUE (scope_key),
    UNIQUE (topic_id, owner_scope_key, scope_key),
    UNIQUE (owner_scope_key, scope_key),
    UNIQUE (owner_scope_key, scope_key, session_id),
    CHECK (scope = scope_key),
    CHECK (COALESCE(json_type(scope, '$.session_id') = 'text', 0)),
    CHECK (json_extract(scope, '$.session_id') = session_id)
);

CREATE INDEX idx_topics_state       ON inbox_topics (state) WHERE state = 'pending';
CREATE INDEX idx_topics_last_active ON inbox_topics (last_active) WHERE state = 'idle';
CREATE INDEX idx_topics_owner       ON inbox_topics (owner_scope_key);
CREATE INDEX idx_topics_session     ON inbox_topics (session_id);
CREATE INDEX idx_topics_pool        ON inbox_topics (pool) WHERE pool IS NOT NULL;

-- Inbox Messages
CREATE TABLE inbox_messages (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id                    INTEGER NOT NULL,
    owner_scope_key             TEXT    NOT NULL COLLATE BINARY,
    scope_key                   TEXT    NOT NULL COLLATE BINARY,
    session_id                  TEXT    NOT NULL,
    scope                       TEXT    NOT NULL CHECK (json_valid(scope)),
    pool                        TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    agent_id                    TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.agent_id')) STORED,
    session_prefix              TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.session_prefix')) STORED,
    invocation_id               TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.invocation_id')) STORED,
    parent_session_id           TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.parent_session_id')) STORED,
    message_id                  TEXT    NOT NULL,
    message_type                TEXT    NOT NULL
                                CHECK (message_type IN (
                                    'task_request', 'agent_message', 'agent_result',
                                    'subagent_result', 'external_input'
                                )),
    source_name                 TEXT    NOT NULL,
    source_kind                 TEXT    NOT NULL DEFAULT 'agent',
    content                     TEXT    NOT NULL,
    payload_json                TEXT    CHECK (payload_json IS NULL OR json_valid(payload_json)),
    envelope_session_id         TEXT,
    envelope_agent_session_id   TEXT,
    state                       TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (state IN ('pending', 'consumed', 'expired')),
    seq                         INTEGER NOT NULL,
    created_at                  REAL    NOT NULL,
    consumed_at                 REAL,
    UNIQUE (scope_key, message_id),
    FOREIGN KEY (topic_id, owner_scope_key, scope_key)
        REFERENCES inbox_topics(topic_id, owner_scope_key, scope_key) ON DELETE CASCADE,
    CHECK (scope = scope_key),
    CHECK (COALESCE(json_type(scope, '$.session_id') = 'text', 0)),
    CHECK (json_extract(scope, '$.session_id') = session_id)
);

CREATE INDEX idx_messages_topic_state_seq ON inbox_messages (topic_id, state, seq);
CREATE INDEX idx_messages_owner_session   ON inbox_messages (owner_scope_key, session_id);
CREATE INDEX idx_messages_scope_session   ON inbox_messages (scope_key, session_id);
CREATE INDEX idx_messages_pool_session    ON inbox_messages (pool, session_id) WHERE pool IS NOT NULL;
CREATE INDEX idx_messages_pool_agent      ON inbox_messages (pool, agent_id) WHERE pool IS NOT NULL;
CREATE INDEX idx_messages_parent          ON inbox_messages (parent_session_id);

-- Inbox Delivered IDs (internal to InboxMQ)
CREATE TABLE inbox_delivered_ids (
    owner_scope_key TEXT    NOT NULL COLLATE BINARY,
    scope_key       TEXT    NOT NULL COLLATE BINARY,
    session_id      TEXT    NOT NULL,
    message_id      TEXT    NOT NULL,
    delivered_at    REAL    NOT NULL,
    PRIMARY KEY (scope_key, message_id),
    FOREIGN KEY (owner_scope_key, scope_key, session_id)
        REFERENCES inbox_topics(owner_scope_key, scope_key, session_id) ON DELETE CASCADE
);

-- Inbox Dead Letter
CREATE TABLE inbox_dead_letter (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_scope_key     TEXT    NOT NULL COLLATE BINARY,
    scope_key           TEXT    NOT NULL COLLATE BINARY,
    session_id          TEXT    NOT NULL,
    scope               TEXT    NOT NULL CHECK (json_valid(scope)),
    pool                TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    message_id          TEXT    NOT NULL,
    message_type        TEXT    NOT NULL,
    source_name         TEXT    NOT NULL,
    content             TEXT    NOT NULL,
    payload_json        TEXT,
    expired_reason      TEXT    NOT NULL,
    expired_at          REAL    NOT NULL,
    original_created_at REAL    NOT NULL,
    UNIQUE (scope_key, message_id),
    FOREIGN KEY (owner_scope_key, scope_key)
        REFERENCES inbox_topics(owner_scope_key, scope_key) ON DELETE CASCADE,
    CHECK (scope = scope_key),
    CHECK (COALESCE(json_type(scope, '$.session_id') = 'text', 0)),
    CHECK (json_extract(scope, '$.session_id') = session_id)
);

CREATE INDEX idx_dead_letter_owner_session ON inbox_dead_letter (owner_scope_key, session_id);
CREATE INDEX idx_dead_letter_pool_session ON inbox_dead_letter (pool, session_id) WHERE pool IS NOT NULL;

-- Turn Snapshots
CREATE TABLE turn_snapshots (
    snapshot_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT    NOT NULL,
    agent_id          TEXT    NOT NULL,
    turn_id           TEXT    NOT NULL,
    scope             TEXT    NOT NULL CHECK (json_valid(scope)),
    pool              TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    session_prefix    TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.session_prefix')) STORED,
    parent_session_id TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.parent_session_id')) STORED,
    agent_kind        TEXT    NOT NULL DEFAULT 'react',
    phase             TEXT    NOT NULL
                      CHECK (phase IN ('running', 'suspended', 'completed', 'cancelled', 'error')),
    reason            TEXT,
    created_at        REAL    NOT NULL,
    updated_at        REAL    NOT NULL,
    schema_version    INTEGER NOT NULL DEFAULT 1,
    payload_json      TEXT    NOT NULL CHECK (json_valid(payload_json)),
    UNIQUE (session_id, agent_id, turn_id)
);

CREATE UNIQUE INDEX idx_turn_active_unique
    ON turn_snapshots (agent_id, session_id)
    WHERE phase IN ('running', 'suspended');

CREATE INDEX idx_turn_session      ON turn_snapshots (session_id);
CREATE INDEX idx_turn_pool_session ON turn_snapshots (pool, session_id) WHERE pool IS NOT NULL;
CREATE INDEX idx_turn_parent       ON turn_snapshots (parent_session_id);
CREATE INDEX idx_turn_phase        ON turn_snapshots (phase) WHERE phase IN ('running', 'suspended');
CREATE INDEX idx_turn_created      ON turn_snapshots (created_at);

-- Approval Audit Log
CREATE TABLE approval_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_uuid       TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    agent_id        TEXT    NOT NULL,
    turn_id         TEXT    NOT NULL,
    tool_name       TEXT    NOT NULL,
    tool_call_id    TEXT,
    decision        TEXT    NOT NULL CHECK (decision IN ('approved', 'denied')),
    deny_reason     TEXT,
    decided_at      REAL    NOT NULL,
    decided_by      TEXT    NOT NULL DEFAULT 'user'
);

CREATE INDEX idx_approval_session ON approval_audit_log (session_id, decided_at);
CREATE INDEX idx_approval_turn    ON approval_audit_log (turn_uuid);
CREATE INDEX idx_approval_pool    ON approval_audit_log (pool, decided_at) WHERE pool IS NOT NULL;

-- Todos
CREATE TABLE todos (
    session_id      TEXT    PRIMARY KEY,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    items_json      TEXT    NOT NULL CHECK (json_valid(items_json)),
    updated_at      REAL    NOT NULL
);

CREATE INDEX idx_todos_pool ON todos (pool) WHERE pool IS NOT NULL;

-- Memory — Session Messages (with state machine)
CREATE TABLE memory_session_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key       TEXT NOT NULL,
    scope           TEXT NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    session_id      TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.session_id')) STORED,
    user_id         TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.user_id')) STORED,
    agent_id        TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.agent_id')) STORED,
    seq             INTEGER NOT NULL,
    role            TEXT    NOT NULL,
    message_json    TEXT    NOT NULL CHECK (json_valid(message_json)),
    created_at      REAL    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'normal'
                    CHECK (state IN ('normal', 'pinned', 'soft_deleted')),
    deleted_at      REAL,
    UNIQUE (scope_key, seq),
    CHECK ((state = 'soft_deleted') = (deleted_at IS NOT NULL))
);

CREATE INDEX idx_memory_session_active
    ON memory_session_messages (scope_key, seq)
    WHERE state IN ('normal', 'pinned');

CREATE INDEX idx_memory_session_ttl
    ON memory_session_messages (deleted_at)
    WHERE state = 'soft_deleted';

CREATE INDEX idx_memory_session_state
    ON memory_session_messages (scope_key, state);

CREATE INDEX idx_memory_session_pool
    ON memory_session_messages (pool) WHERE pool IS NOT NULL;

-- Memory — KV Store
CREATE TABLE memory_kv (
    scope_key       TEXT    NOT NULL,
    key             TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    value_json      TEXT    NOT NULL CHECK (json_valid(value_json)),
    updated_at      REAL    NOT NULL,
    PRIMARY KEY (scope_key, key)
);

CREATE INDEX idx_memory_kv_pool ON memory_kv (pool) WHERE pool IS NOT NULL;

-- Memory — Cursors
CREATE TABLE memory_cursors (
    scope_key       TEXT    NOT NULL,
    cursor_name     TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    cursor_value    INTEGER NOT NULL,
    updated_at      REAL    NOT NULL,
    PRIMARY KEY (scope_key, cursor_name)
);

-- Memory — Revisions
CREATE TABLE memory_revisions (
    scope_key       TEXT    PRIMARY KEY,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    message_count   INTEGER NOT NULL DEFAULT 0,
    version         INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL    NOT NULL
);

-- Memory — Archive State
CREATE TABLE memory_archive_state (
    scope_key       TEXT    PRIMARY KEY,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    next_archive_id INTEGER NOT NULL DEFAULT 1,
    state_json      TEXT    CHECK (state_json IS NULL OR json_valid(state_json)),
    updated_at      REAL    NOT NULL
);

-- Memory — Archive Entries
CREATE TABLE memory_archive_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key       TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    archive_id      INTEGER NOT NULL,
    channel         TEXT    NOT NULL,
    summary         TEXT,
    created_at      REAL    NOT NULL,
    UNIQUE (scope_key, archive_id, channel)
);

CREATE INDEX idx_archive_entries_scope ON memory_archive_entries (scope_key, archive_id);

-- External Provider Session Map
CREATE TABLE external_session_map (
    modex_session_id    TEXT    PRIMARY KEY,
    provider_session_id TEXT    NOT NULL,
    provider_kind       TEXT    NOT NULL CHECK (provider_kind IN ('pi', 'opencode')),
    scope               TEXT    NOT NULL CHECK (json_valid(scope)),
    pool                TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    last_committed_at   REAL    NOT NULL,
    invalidated         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_external_pool ON external_session_map (pool) WHERE pool IS NOT NULL;

-- Workspace Metadata
CREATE TABLE workspace_meta (
    key         TEXT    PRIMARY KEY,
    value_json  TEXT    NOT NULL CHECK (json_valid(value_json)),
    updated_at  REAL    NOT NULL
);
