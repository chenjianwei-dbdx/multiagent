-- 003_qa_turns.sql — conversation triage: Q&A and clarification turns (ADR 0002 rev B)
-- SQLite CHECK constraints cannot be altered in place: rebuild the table
-- with the extended message kinds, preserving rows.

CREATE TABLE conversation_messages_new (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    kind TEXT NOT NULL DEFAULT 'text'
        CHECK (kind IN ('text', 'task_started', 'task_result', 'task_update',
                        'answer', 'clarify', 'note')),
    content TEXT NOT NULL,
    task_id TEXT REFERENCES tasks(task_id),
    created_at TEXT NOT NULL
);
INSERT INTO conversation_messages_new (message_id, conversation_id, role, kind, content, task_id, created_at)
    SELECT message_id, conversation_id, role, kind, content, task_id, created_at
    FROM conversation_messages;
DROP TABLE conversation_messages;
ALTER TABLE conversation_messages_new RENAME TO conversation_messages;
CREATE INDEX idx_conv_messages_new ON conversation_messages(conversation_id, created_at);
