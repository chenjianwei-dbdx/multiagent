-- 003_web_console.sql — chat console persistence (ADR 0002 amendment)
-- conversations group chat turns; each turn may drive one task.
-- template_meta is UI-level display metadata only: the immutable
-- template/version contracts live in template_versions (v1.1 §3.1).

CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    template_version_id TEXT REFERENCES template_versions(template_version_id),
    data_policy TEXT NOT NULL CHECK (data_policy IN ('local_only', 'llm_allowed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE conversation_messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    kind TEXT NOT NULL DEFAULT 'text'
        CHECK (kind IN ('text', 'task_started', 'task_result', 'task_update')),
    content TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(task_id),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_conv_messages ON conversation_messages(conversation_id, created_at);

CREATE TABLE template_meta (
    template_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
