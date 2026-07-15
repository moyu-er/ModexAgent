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
