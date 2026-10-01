-- Research materials: web-sourced artifacts registered by the research node.
-- ADR D27 — every artifact whose bytes were fetched from the web (as opposed
-- to uploaded by the user) carries its origin URL + the query that led to it,
-- so the delivery's body text stays traceable to a network source, not just to
-- "a registered material".
CREATE TABLE research_sources (
    source_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    source_url TEXT NOT NULL,
    query TEXT NOT NULL,
    http_status INTEGER NOT NULL CHECK (http_status >= 0),
    fetched_at TEXT NOT NULL,
    UNIQUE (task_id, source_url)
);

CREATE INDEX research_sources_artifact_idx ON research_sources(artifact_id);
