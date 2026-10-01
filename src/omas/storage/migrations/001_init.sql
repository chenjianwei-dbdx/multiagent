-- OMAS ledger schema, initial version (P0).
--
-- Invariant I4: the ledger stores refs and finite metadata only; body text
-- and file bytes live in the artifact pool on the filesystem.
--
-- Note: operations.output_artifact_ids_json is the persisted form of the
-- domain Operation.output_artifact_ids DTO field (recorded at commit time);
-- every other column follows the agreed P0 DDL verbatim.

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_payload_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'created', 'running', 'awaiting_user', 'parked',
            'completed', 'failed', 'cancelled'
        )
    ),
    data_policy TEXT NOT NULL CHECK (data_policy IN ('local_only', 'llm_allowed')),
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    template_version_id TEXT,
    active_plan_artifact_id TEXT,
    active_binding_artifact_id TEXT,
    active_render_ir_artifact_id TEXT,
    active_candidate_artifact_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE template_versions (
    template_version_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    docx_sha256 TEXT NOT NULL,
    contract_sha256 TEXT NOT NULL,
    styles_sha256 TEXT NOT NULL,
    static_map_sha256 TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (template_id, version)
);

CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    operation_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('intent', 'committed', 'abandoned')),
    task_id TEXT REFERENCES tasks(task_id),
    node_name TEXT,
    epoch INTEGER,
    payload_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    output_artifact_ids_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id),
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    created_by_operation_id TEXT REFERENCES operations(operation_id),
    lineage_json TEXT NOT NULL DEFAULT '[]',
    content_type TEXT,
    corrupt_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_artifacts_task ON artifacts(task_id);

CREATE TABLE node_runs (
    node_run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    node_name TEXT NOT NULL,
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'done', 'failed_final', 'skipped', 'corrupt')
    ),
    operation_id TEXT REFERENCES operations(operation_id),
    error_code TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX idx_node_runs_task ON node_runs(task_id, node_name);

CREATE TABLE bindings (
    binding_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    epoch INTEGER NOT NULL,
    binding_version INTEGER NOT NULL,
    slot_id TEXT NOT NULL,
    binding_status TEXT NOT NULL CHECK (binding_status IN ('bound', 'missing', 'invalid')),
    producer TEXT CHECK (producer IN ('user', 'upstream', 'viz') OR producer IS NULL),
    source_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE (task_id, epoch, binding_version, slot_id)
);

CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    event_code TEXT NOT NULL,
    refs_json TEXT NOT NULL DEFAULT '{}',
    counts_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX idx_events_task ON events(task_id, seq);

CREATE TABLE awaiting_events (
    awaiting_event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    epoch INTEGER NOT NULL,
    kind TEXT NOT NULL,
    missing_slot_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE user_decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    expected_epoch INTEGER NOT NULL,
    awaiting_event_id TEXT NOT NULL REFERENCES awaiting_events(awaiting_event_id),
    action TEXT NOT NULL CHECK (action IN ('provide_material', 'omit_slot')),
    payload_digest TEXT NOT NULL,
    attachment_refs_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE slot_overrides (
    override_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    slot_id TEXT NOT NULL,
    decision_id TEXT NOT NULL REFERENCES user_decisions(decision_id),
    epoch INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action = 'omit'),
    reason_artifact_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, epoch, slot_id)
);

CREATE TABLE gate_reports (
    gate_report_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    gate TEXT NOT NULL,
    candidate_sha256 TEXT,
    render_ir_sha256 TEXT NOT NULL,
    template_version_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    overall_status TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    report_artifact_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE llm_calls (
    call_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES tasks(task_id),
    node_name TEXT,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_revision TEXT,
    prompt_template_digest TEXT,
    system_prompt_digest TEXT,
    tool_schema_version TEXT,
    model_config_json TEXT,
    input_artifact_refs_json TEXT,
    raw_output_artifact_id TEXT,
    parsed_output_artifact_id TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    cost REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE deliveries (
    delivery_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    candidate_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    final_sha256 TEXT NOT NULL,
    manifest_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    created_at TEXT NOT NULL
);

CREATE TABLE resolved_spans (
    span_handle TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    run_id TEXT,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    canonical_sha256 TEXT NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    span_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, artifact_id, start, end)
);
