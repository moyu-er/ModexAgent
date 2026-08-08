CREATE TABLE bot_webui_transcript_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_prefix TEXT NOT NULL,
    pool_name TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    turn_id TEXT,
    timestamp_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_bot_transcript_session_order
    ON bot_webui_transcript_events (session_id, event_id);

CREATE INDEX idx_bot_transcript_prefix_order
    ON bot_webui_transcript_events (session_prefix, timestamp_ms, event_id);

CREATE INDEX idx_bot_transcript_pool_prefix_order
    ON bot_webui_transcript_events (
        pool_name,
        session_prefix,
        timestamp_ms,
        event_id
    );

CREATE INDEX idx_bot_transcript_turn
    ON bot_webui_transcript_events (turn_id, event_id)
    WHERE turn_id IS NOT NULL;

-- ── KB: knowledge base entries ─────────────────────────────
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

CREATE INDEX IF NOT EXISTS idx_kb_entries_task ON kb_entries (task_id);

CREATE INDEX IF NOT EXISTS idx_kb_entries_category ON kb_entries (category);

-- ── KB: FTS5 external content table ────────────────────────
CREATE VIRTUAL TABLE IF NOT EXISTS kb_entries_fts USING fts5(
    value, tags,
    content=kb_entries, content_rowid=entry_id,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS kb_fts_insert AFTER INSERT ON kb_entries BEGIN
    INSERT INTO kb_entries_fts(rowid, value, tags) VALUES (new.entry_id, new.value, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS kb_fts_delete AFTER DELETE ON kb_entries BEGIN
    INSERT INTO kb_entries_fts(kb_entries_fts, rowid, value, tags) VALUES ('delete', old.entry_id, old.value, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS kb_fts_update AFTER UPDATE ON kb_entries BEGIN
    INSERT INTO kb_entries_fts(kb_entries_fts, rowid, value, tags) VALUES ('delete', old.entry_id, old.value, old.tags);
    INSERT INTO kb_entries_fts(rowid, value, tags) VALUES (new.entry_id, new.value, new.tags);
END;
