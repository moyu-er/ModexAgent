-- Registry initial schema: workspaces and session->workspace mapping.
-- Reference: docs/design/hybrid-persistence/SCHEMA-DESIGN.md (Registry DB Schema).
-- schema_migrations is created by MigrationRunner._ensure_table() and must not
-- be declared here. No transaction-control statements: the runner wraps the
-- whole migration in a single BEGIN IMMEDIATE transaction.

CREATE TABLE workspaces (
    workspace_id     TEXT    PRIMARY KEY,
    target_path      TEXT    NOT NULL UNIQUE,
    display_name     TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    last_active      TEXT    NOT NULL DEFAULT (datetime('now')),
    is_home          INTEGER NOT NULL DEFAULT 0,
    metadata_json    TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_workspaces_last_active ON workspaces (last_active);

CREATE TABLE session_workspace_map (
    session_prefix   TEXT    PRIMARY KEY,
    workspace_id     TEXT    NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE
);

CREATE INDEX idx_session_ws_workspace ON session_workspace_map (workspace_id);
