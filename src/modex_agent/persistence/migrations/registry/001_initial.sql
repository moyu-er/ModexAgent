-- Registry initial schema: workspaces and session->workspace mapping.
-- Target schema per ADR-0029 (epoch-ms timestamps + updated_at triggers) and
-- ADR-0031 (workspace_meta removal happens on the workspace DB; registry
-- adopts int-ms timestamps, is_home CHECK, metadata_json NOT NULL DEFAULT '{}').
-- schema_migrations is created by MigrationRunner._ensure_table(); not duplicated here.
-- No transaction-control statements: the runner wraps the whole migration in a
-- single BEGIN IMMEDIATE transaction.

CREATE TABLE workspaces (
    workspace_id     TEXT    PRIMARY KEY,
    target_path      TEXT    NOT NULL UNIQUE,
    display_name     TEXT,
    created_at       INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    last_active      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    is_home          INTEGER NOT NULL DEFAULT 0 CHECK (is_home IN (0, 1)),
    metadata_json    TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json))
);

CREATE INDEX idx_workspaces_last_active ON workspaces (last_active);
CREATE INDEX idx_workspaces_created_at  ON workspaces (created_at);

CREATE TABLE session_workspace_map (
    session_prefix   TEXT    PRIMARY KEY,
    workspace_id     TEXT    NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    created_at       INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at       INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
);

CREATE INDEX idx_session_ws_workspace ON session_workspace_map (workspace_id);

CREATE TRIGGER trg_session_workspace_map_auto_updated_at
AFTER UPDATE ON session_workspace_map
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE session_workspace_map SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;
