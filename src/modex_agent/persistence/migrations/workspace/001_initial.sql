-- Workspace DB initial schema.
-- NOTE: Modifying 001 requires DB rebuild for existing deployments.
-- Target schema per ADR-0028 (RecordScope base/subclass split, pool removal),
-- ADR-0029 (epoch-ms timestamps + updated_at triggers), and
-- ADR-0031 (scope/scope_key merge, dead-table drops, inbox_topics minimization,
-- inbox_messages simplification).
-- schema_migrations is created by MigrationRunner._ensure_table(); not duplicated here.
-- No transaction-control statements: the runner wraps the whole migration in a
-- single BEGIN IMMEDIATE transaction.

-- ---------------------------------------------------------------------------
-- 1. memory_session_messages
--    ColumnProjection-driven: message_id/role/content+is_content_json/token_count
--    extracted to columns; message_json holds the residual dict.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_session_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key       TEXT    NOT NULL,
    seq             INTEGER NOT NULL,
    message_id      TEXT,
    role            TEXT    NOT NULL,
    content         TEXT,
    is_content_json INTEGER NOT NULL DEFAULT 0 CHECK (is_content_json IN (0, 1)),
    token_count     INTEGER,
    message_json    TEXT    NOT NULL CHECK (json_valid(message_json)),
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    state           TEXT    NOT NULL DEFAULT 'normal'
                    CHECK (state IN ('normal', 'pinned', 'soft_deleted', 'superseded')),
    UNIQUE (scope_key, seq)
);

CREATE INDEX IF NOT EXISTS idx_memory_session_active
    ON memory_session_messages (scope_key, seq)
    WHERE state IN ('normal', 'pinned');

CREATE INDEX IF NOT EXISTS idx_memory_session_ttl
    ON memory_session_messages (updated_at)
    WHERE state IN ('soft_deleted', 'superseded');

CREATE INDEX IF NOT EXISTS idx_memory_session_state
    ON memory_session_messages (scope_key, state);

CREATE INDEX IF NOT EXISTS idx_memory_session_msg_id
    ON memory_session_messages (scope_key, message_id)
    WHERE message_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_memory_session_messages_auto_updated_at
AFTER UPDATE ON memory_session_messages
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE memory_session_messages SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 2. inbox_topics — FK anchor only (no state machine).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbox_topics (
    topic_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_scope_key TEXT    NOT NULL COLLATE BINARY,
    scope_key       TEXT    NOT NULL COLLATE BINARY,
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    UNIQUE (scope_key),
    UNIQUE (topic_id, owner_scope_key, scope_key)
);

CREATE INDEX IF NOT EXISTS idx_topics_owner ON inbox_topics (owner_scope_key);

CREATE TRIGGER IF NOT EXISTS trg_inbox_topics_auto_updated_at
AFTER UPDATE ON inbox_topics
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE inbox_topics SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 3. inbox_messages — payload_json stores the full InboxMessage dict.
--    ColumnProjection extracts message_id/message_type/session_id for WHERE/UNIQUE.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbox_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id        INTEGER NOT NULL,
    owner_scope_key TEXT    NOT NULL COLLATE BINARY,
    scope_key       TEXT    NOT NULL COLLATE BINARY,
    session_id      TEXT    NOT NULL,
    message_id      TEXT    NOT NULL,
    message_type    TEXT    NOT NULL
                    CHECK (message_type IN (
                        'task_request', 'agent_message', 'agent_result',
                        'subagent_result', 'external_input'
                    )),
    payload_json    TEXT    NOT NULL CHECK (json_valid(payload_json)),
    state           TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'consumed', 'expired')),
    seq             INTEGER NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    consumed_at     INTEGER,
    UNIQUE (scope_key, message_id),
    FOREIGN KEY (topic_id, owner_scope_key, scope_key)
        REFERENCES inbox_topics(topic_id, owner_scope_key, scope_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_scope_state_seq
    ON inbox_messages (scope_key, state, seq);

CREATE INDEX IF NOT EXISTS idx_messages_owner_pending
    ON inbox_messages (owner_scope_key, created_at)
    WHERE state = 'pending';

CREATE TRIGGER IF NOT EXISTS trg_inbox_messages_auto_updated_at
AFTER UPDATE ON inbox_messages
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE inbox_messages SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 4. inbox_delivered_ids — append-only dedup tracker (no session_id, no trigger).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbox_delivered_ids (
    scope_key       TEXT    NOT NULL COLLATE BINARY,
    message_id      TEXT    NOT NULL,
    owner_scope_key TEXT    NOT NULL COLLATE BINARY,
    delivered_at    INTEGER NOT NULL,
    PRIMARY KEY (scope_key, message_id),
    FOREIGN KEY (scope_key) REFERENCES inbox_topics(scope_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_delivered_owner
    ON inbox_delivered_ids (owner_scope_key, delivered_at);

-- ---------------------------------------------------------------------------
-- 5. turn_snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS turn_snapshots (
    snapshot_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT    NOT NULL,
    agent_id          TEXT    NOT NULL,
    turn_id           TEXT    NOT NULL,
    scope_key         TEXT    NOT NULL,
    agent_kind        TEXT    NOT NULL DEFAULT 'react',
    phase             TEXT    NOT NULL
                      CHECK (phase IN ('running', 'suspended', 'completed', 'cancelled', 'error')),
    reason            TEXT,
    created_at        INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at        INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    schema_version    INTEGER NOT NULL DEFAULT 1,
    payload_json      TEXT    NOT NULL CHECK (json_valid(payload_json)),
    UNIQUE (session_id, agent_id, turn_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_active_unique
    ON turn_snapshots (agent_id, session_id)
    WHERE phase IN ('running', 'suspended');

CREATE INDEX IF NOT EXISTS idx_turn_session ON turn_snapshots (session_id);
CREATE INDEX IF NOT EXISTS idx_turn_phase   ON turn_snapshots (phase) WHERE phase IN ('running', 'suspended');
CREATE INDEX IF NOT EXISTS idx_turn_created ON turn_snapshots (created_at);

CREATE TRIGGER IF NOT EXISTS trg_turn_snapshots_auto_updated_at
AFTER UPDATE ON turn_snapshots
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE turn_snapshots SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 6. sessions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    session_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT    NOT NULL UNIQUE,
    scope_key          TEXT    NOT NULL,
    session_prefix     TEXT    GENERATED ALWAYS AS (json_extract(scope_key, '$.session_prefix')) STORED,
    agent_id           TEXT    GENERATED ALWAYS AS (json_extract(scope_key, '$.agent_id')) STORED,
    parent_session_id  TEXT    GENERATED ALWAYS AS (json_extract(scope_key, '$.parent_session_id')) STORED,
    created_at         INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at         INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    metadata_json      TEXT    CHECK (metadata_json IS NULL OR json_valid(metadata_json))
);

CREATE INDEX IF NOT EXISTS idx_sessions_prefix ON sessions (session_prefix);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions (parent_session_id);

CREATE TRIGGER IF NOT EXISTS trg_sessions_auto_updated_at
AFTER UPDATE ON sessions
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE sessions SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 7. todos
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS todos (
    session_id      TEXT    PRIMARY KEY,
    scope_key       TEXT    NOT NULL,
    items_json      TEXT    NOT NULL CHECK (json_valid(items_json)),
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE TRIGGER IF NOT EXISTS trg_todos_auto_updated_at
AFTER UPDATE ON todos
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE todos SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 8. approval_audit_log — append-only (no updated_at, no trigger).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS approval_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_uuid       TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    scope_key       TEXT    NOT NULL,
    agent_id        TEXT    NOT NULL,
    turn_id         TEXT    NOT NULL,
    tool_name       TEXT    NOT NULL,
    tool_call_id    TEXT,
    decision        TEXT    NOT NULL CHECK (decision IN ('approved', 'denied')),
    deny_reason     TEXT,
    decided_at      INTEGER NOT NULL,
    decided_by      TEXT    NOT NULL DEFAULT 'user'
);

CREATE INDEX IF NOT EXISTS idx_approval_session ON approval_audit_log (session_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_approval_turn    ON approval_audit_log (turn_uuid);

-- ---------------------------------------------------------------------------
-- 9. memory_kv
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_kv (
    scope_key       TEXT    NOT NULL,
    key             TEXT    NOT NULL,
    value_json      TEXT    NOT NULL CHECK (json_valid(value_json)),
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    PRIMARY KEY (scope_key, key)
);

CREATE TRIGGER IF NOT EXISTS trg_memory_kv_auto_updated_at
AFTER UPDATE ON memory_kv
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE memory_kv SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 10. memory_cursors
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_cursors (
    scope_key       TEXT    NOT NULL,
    cursor_name     TEXT    NOT NULL,
    cursor_value    INTEGER NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    PRIMARY KEY (scope_key, cursor_name)
);

CREATE TRIGGER IF NOT EXISTS trg_memory_cursors_auto_updated_at
AFTER UPDATE ON memory_cursors
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE memory_cursors SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 11. memory_revisions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_revisions (
    scope_key       TEXT    PRIMARY KEY,
    message_count   INTEGER NOT NULL DEFAULT 0,
    version         INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE TRIGGER IF NOT EXISTS trg_memory_revisions_auto_updated_at
AFTER UPDATE ON memory_revisions
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE memory_revisions SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 12. memory_archive_entries — append-only (no updated_at, no trigger).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_archive_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key       TEXT    NOT NULL,
    archive_id      INTEGER NOT NULL,
    channel         TEXT    NOT NULL,
    summary         TEXT,
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    UNIQUE (scope_key, archive_id, channel)
);

CREATE INDEX IF NOT EXISTS idx_archive_entries_scope_channel
    ON memory_archive_entries (scope_key, channel, archive_id);

CREATE INDEX IF NOT EXISTS idx_archive_entries_scope
    ON memory_archive_entries (scope_key, archive_id);

-- ---------------------------------------------------------------------------
-- 13. memory_archive_state
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_archive_state (
    scope_key       TEXT    PRIMARY KEY,
    next_archive_id INTEGER NOT NULL DEFAULT 1,
    state_json      TEXT    CHECK (state_json IS NULL OR json_valid(state_json)),
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE TRIGGER IF NOT EXISTS trg_memory_archive_state_auto_updated_at
AFTER UPDATE ON memory_archive_state
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE memory_archive_state SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 14. external_session_map
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS external_session_map (
    modex_session_id       TEXT    PRIMARY KEY,
    scope_key              TEXT    NOT NULL,
    provider_session_id    TEXT    NOT NULL,
    provider_kind          TEXT    NOT NULL CHECK (provider_kind IN ('pi', 'opencode')),
    last_committed_at      INTEGER NOT NULL,
    invalidated            INTEGER NOT NULL DEFAULT 0 CHECK (invalidated IN (0, 1)),
    created_at             INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at             INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE TRIGGER IF NOT EXISTS trg_external_session_map_auto_updated_at
AFTER UPDATE ON external_session_map
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE external_session_map SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 15. pool_routing
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pool_routing (
    session_prefix  TEXT    PRIMARY KEY,
    scope_key       TEXT    NOT NULL,
    pool_name       TEXT    NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_routing_pool_name ON pool_routing (pool_name);

CREATE TRIGGER IF NOT EXISTS trg_pool_routing_auto_updated_at
AFTER UPDATE ON pool_routing
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE pool_routing SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 16. graph_specs — graph definition persistence (full GraphSpec serialization).
--     spec_id is a Snowflake ID (BIGINT, application-generated; not AUTOINCREMENT).
--     Spec is immutable (ADR-0040 change 3): each save with changed content
--     creates a new row (new spec_id). No UNIQUE(name, version) — multiple
--     rows with the same (name, version) are allowed, distinguished by spec_id.
--     No auto-update trigger — immutable rows are never UPDATEd.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS graph_specs (
    spec_id         BIGINT  PRIMARY KEY,
    name            TEXT    NOT NULL,
    version         TEXT    NOT NULL DEFAULT '1.0',
    spec_json       TEXT    NOT NULL CHECK (json_valid(spec_json)),
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_graph_specs_name ON graph_specs (name);

-- ---------------------------------------------------------------------------
-- 17. graph_instances — runtime graph instances (graph_instance_id is the
--     persistence unique key). parent_instance_id enables recursive subgraph
--     nesting (null for top-level). parent_node is the node name in the parent
--     graph that spawned this instance.
--     spec_id references graph_specs.spec_id (FK enforced at app layer; SQLite
--     FK enforcement is off by default).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS graph_instances (
    graph_instance_id   BIGINT  NOT NULL,
    spec_id             BIGINT  NOT NULL,
    version             INTEGER NOT NULL DEFAULT 0,
    parent_instance_id  BIGINT,
    parent_node         TEXT,
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'paused', 'stopped', 'crashed', 'completed', 'failed')),
    node_id_map_json    TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(node_id_map_json)),
    created_at          INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at          INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    PRIMARY KEY (graph_instance_id, version)
);

CREATE INDEX IF NOT EXISTS idx_graph_instances_spec
    ON graph_instances (spec_id);

CREATE INDEX IF NOT EXISTS idx_graph_instances_parent
    ON graph_instances (parent_instance_id)
    WHERE parent_instance_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_graph_instances_active
    ON graph_instances (status)
    WHERE status IN ('running', 'paused', 'crashed');

CREATE TRIGGER IF NOT EXISTS trg_graph_instances_auto_updated_at
AFTER UPDATE ON graph_instances
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE graph_instances SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 18. node_states — per-node invocation version chain.
--     One row per (graph_instance_id, node_id, version). All versions
--     retained for MVCC rollback. `status` tracks the InvocationStatus
--     lifecycle (running → completed/canceled/crashed); no pending or
--     superseded states. `invocation_id` links the version to its producing
--     invocation; `parent_version` chains versions to their predecessor.
--     `suspended` marks a RUNNING invocation paused for HITL resume.
--     graph_instance_id references graph_instances.graph_instance_id (app-layer FK).
--     No auto-update trigger — each version row is write-once on INSERT,
--     then updated via CAS on lifecycle transitions; `updated_at` reflects
--     the last transition.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS node_states (
    node_state_id       BIGINT  PRIMARY KEY,
    graph_instance_id   BIGINT  NOT NULL,
    node_name           TEXT,
    node_id             TEXT    NOT NULL,
    version             INTEGER NOT NULL DEFAULT 0,
    parent_version      INTEGER,
    status              TEXT    NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed',
                                          'canceled', 'crashed')),
    invocation_id       BIGINT  NOT NULL DEFAULT 0,
    state_json          TEXT    NOT NULL CHECK (json_valid(state_json)),
    suspended           INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at          INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    UNIQUE (graph_instance_id, node_id, version)
);

CREATE INDEX IF NOT EXISTS idx_node_states_latest
    ON node_states (graph_instance_id, node_id, version DESC);

CREATE INDEX IF NOT EXISTS idx_node_states_node
    ON node_states (graph_instance_id, node_id);

CREATE INDEX IF NOT EXISTS idx_node_states_status
    ON node_states (graph_instance_id, node_id, status);

CREATE INDEX IF NOT EXISTS idx_node_states_cross
    ON node_states (graph_instance_id, node_id, invocation_id);

CREATE INDEX IF NOT EXISTS idx_node_states_global
    ON node_states (graph_instance_id, invocation_id DESC);

-- ---------------------------------------------------------------------------
-- 19. deliver_states — accumulated deliver payloads with consumption state machine.
--     node_id is the accumulating node; next_node_id is the target downstream.
--     `source_node_id` / `source_invocation_id` record the delivering node;
--     `consumed_by_invocation_id` records the consumer (NULL until consumed).
--     `status` transitions: PENDING → CONSUMED_PENDING → CONSUMED_COMPLETED
--     (three-state machine). Default is 'pending'.
--     graph_instance_id references graph_instances.graph_instance_id (app-layer FK).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deliver_states (
    deliver_id          BIGINT  PRIMARY KEY,
    graph_instance_id   BIGINT  NOT NULL,
    node_name           TEXT,
    node_id             TEXT    NOT NULL,
    next_node           TEXT,
    next_node_id        TEXT    NOT NULL,
    source_node         TEXT    NOT NULL DEFAULT '',
    source_node_id      TEXT    NOT NULL DEFAULT '',
    source_invocation_id INTEGER NOT NULL DEFAULT 0,
    consumed_by_invocation_id INTEGER,
    content_json        TEXT    NOT NULL CHECK (json_valid(content_json)),
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'consumed',
                                          'consumed_pending', 'consumed_completed')),
    created_at          INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at          INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE INDEX IF NOT EXISTS idx_deliver_states_node
    ON deliver_states (graph_instance_id, node_id, status);

CREATE INDEX IF NOT EXISTS idx_deliver_states_target
    ON deliver_states (graph_instance_id, next_node_id, status);

CREATE INDEX IF NOT EXISTS idx_deliver_states_source
    ON deliver_states (graph_instance_id, source_node_id, source_invocation_id);

CREATE TRIGGER IF NOT EXISTS trg_deliver_states_auto_updated_at
AFTER UPDATE ON deliver_states
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE deliver_states SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 20. session_trees — root anchor for a session execution tree.
--     One row per tree; root_node_session_id is the spawning session.
--     status tracks tree lifecycle (active -> completed/cancelled).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_trees (
    tree_id               TEXT    PRIMARY KEY,
    root_node_session_id  TEXT    NOT NULL,
    pool_name             TEXT    NOT NULL,
    workspace_root        TEXT    NOT NULL,
    scope_key             TEXT    NOT NULL COLLATE BINARY,
    owner_scope_key       TEXT    NOT NULL COLLATE BINARY,
    status                TEXT    NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'completed', 'cancelled')),
    created_at            INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at            INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    completed_at          INTEGER
);

CREATE TRIGGER IF NOT EXISTS trg_session_trees_auto_updated_at
AFTER UPDATE ON session_trees
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE session_trees SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 21. tree_nodes — node entries within a session tree.
--     One row per (tree_id, session_id); parent_session_id is NULL for root.
--     version/parent_version chain node re-invocations. status tracks the
--     node lifecycle (running -> completed/cancelled).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tree_nodes (
    tree_id             TEXT    NOT NULL,
    session_id          TEXT    NOT NULL,
    parent_session_id   TEXT,
    agent_name          TEXT    NOT NULL,
    version             INTEGER NOT NULL DEFAULT 0,
    parent_version      INTEGER,
    status              TEXT    NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed', 'cancelled')),
    scope_key           TEXT    NOT NULL COLLATE BINARY,
    owner_scope_key     TEXT    NOT NULL COLLATE BINARY,
    created_at          INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at          INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    UNIQUE (tree_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_tree_nodes_tree    ON tree_nodes (tree_id);
CREATE INDEX IF NOT EXISTS idx_tree_nodes_session ON tree_nodes (session_id);

CREATE TRIGGER IF NOT EXISTS trg_tree_nodes_auto_updated_at
AFTER UPDATE ON tree_nodes
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE tree_nodes SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;

-- ---------------------------------------------------------------------------
-- 22. message_tracks — delivery tracking for inter-node routed messages.
--     message_type is restricted to routed payloads only (task_request,
--     agent_result); external_input and agent_message do not get tracks.
--     Short-lived (dispatched -> consumed/cancelled); no updated_at trigger,
--     mirroring the inbox_delivered_ids append-only pattern.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS message_tracks (
    track_id            TEXT    PRIMARY KEY,
    tree_id             TEXT    NOT NULL,
    message_id          TEXT    NOT NULL,
    message_type        TEXT    NOT NULL
                        CHECK (message_type IN ('task_request', 'agent_result', 'external_input')),
    invocation_id       TEXT,
    target_session_id   TEXT    NOT NULL,
    source_session_id   TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'dispatched'
                        CHECK (status IN ('dispatched', 'consumed', 'cancelled')),
    scope_key           TEXT    NOT NULL COLLATE BINARY,
    owner_scope_key     TEXT    NOT NULL COLLATE BINARY,
    dispatched_at       INTEGER NOT NULL,
    consumed_at         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_message_tracks_tree_status    ON message_tracks (tree_id, status);
CREATE INDEX IF NOT EXISTS idx_message_tracks_target_session ON message_tracks (target_session_id);
