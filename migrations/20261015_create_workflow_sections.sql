CREATE TABLE IF NOT EXISTS workflow_sections (
    workflow_id TEXT NOT NULL,
    section_id TEXT NOT NULL,
    parent_section_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workflow_id, parent_section_id, section_id)
);
