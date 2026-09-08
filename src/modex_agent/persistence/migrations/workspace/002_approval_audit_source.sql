-- Approval audit provenance + escalation vocabulary.
--
-- 1. Widen the `decision` CHECK to include 'escalated' (guard escalation is
--    never 'approved' — conflating them lies on the audit timeline).
-- 2. Add the `source` column (runtime vs delegation provenance); existing
--    rows backfill to 'runtime' (every pre-migration row came from the
--    in-process approval/guard path).
--
-- SQLite cannot ALTER a CHECK constraint in place, so the table is rebuilt:
-- create new → copy → drop old → rename. Append-only data with no FK
-- references into approval_audit_log, so the rebuild is safe.
-- No transaction-control statements: the runner wraps the whole migration in
-- a single BEGIN IMMEDIATE transaction.

CREATE TABLE approval_audit_log_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_uuid       TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    scope_key       TEXT    NOT NULL,
    agent_id        TEXT    NOT NULL,
    turn_id         TEXT    NOT NULL,
    tool_name       TEXT    NOT NULL,
    tool_call_id    TEXT,
    decision        TEXT    NOT NULL
                    CHECK (decision IN ('approved', 'denied', 'escalated')),
    deny_reason     TEXT,
    decided_at      INTEGER NOT NULL,
    decided_by      TEXT    NOT NULL DEFAULT 'user',
    source          TEXT    NOT NULL DEFAULT 'runtime'
);

INSERT INTO approval_audit_log_new
    (id, turn_uuid, session_id, scope_key, agent_id, turn_id, tool_name,
     tool_call_id, decision, deny_reason, decided_at, decided_by, source)
SELECT id, turn_uuid, session_id, scope_key, agent_id, turn_id, tool_name,
       tool_call_id, decision, deny_reason, decided_at, decided_by, 'runtime'
FROM approval_audit_log;

DROP TABLE approval_audit_log;
ALTER TABLE approval_audit_log_new RENAME TO approval_audit_log;

CREATE INDEX IF NOT EXISTS idx_approval_session ON approval_audit_log (session_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_approval_turn    ON approval_audit_log (turn_uuid);
CREATE INDEX IF NOT EXISTS idx_approval_source  ON approval_audit_log (source);
