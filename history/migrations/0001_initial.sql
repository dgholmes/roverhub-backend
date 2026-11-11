-- History: incidents, decimated telemetry, perception status, the audit log.
-- Portable SQL; {{ID}}, {{JSON}} and {{REAL}} are filled in per engine by
-- history/db.py. Times are UTC epoch seconds. Statements end with ";" at the
-- end of a line.

CREATE TABLE incidents (
    incident_id TEXT PRIMARY KEY,
    robot_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    rule_id TEXT,
    label TEXT NOT NULL,
    severity TEXT NOT NULL,
    opened_at {{REAL}} NOT NULL,
    camera TEXT,
    message TEXT,
    confidence {{REAL}},
    payload {{JSON}} NOT NULL,
    evidence_snapshot_ids {{JSON}} NOT NULL,
    x_m {{REAL}},
    y_m {{REAL}},
    map_id TEXT,
    run_id TEXT,
    received_at {{REAL}} NOT NULL,
    last_seen {{REAL}} NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    acknowledged_by TEXT,
    acknowledged_at {{REAL}},
    resolved_by TEXT,
    resolved_at {{REAL}},
    outcome TEXT,
    note TEXT
);
CREATE INDEX incidents_by_opened ON incidents (opened_at, incident_id);
CREATE INDEX incidents_by_site ON incidents (site_id, opened_at);
CREATE INDEX incidents_by_robot ON incidents (robot_id, opened_at);
CREATE INDEX incidents_by_state ON incidents (state, opened_at);

CREATE TABLE telemetry_samples (
    id {{ID}},
    robot_id TEXT NOT NULL,
    site_id TEXT,
    at {{REAL}} NOT NULL,
    battery_pct {{REAL}},
    x_m {{REAL}},
    y_m {{REAL}},
    heading_deg {{REAL}},
    position_quality TEXT,
    speed_mps {{REAL}},
    abstract_state TEXT,
    sdk_state TEXT,
    posture TEXT,
    control_owner TEXT,
    active_run_id TEXT,
    link TEXT,
    reason TEXT NOT NULL
);
CREATE INDEX telemetry_by_robot ON telemetry_samples (robot_id, at);
CREATE INDEX telemetry_by_at ON telemetry_samples (at);

CREATE TABLE perception_status (
    id {{ID}},
    robot_id TEXT NOT NULL,
    site_id TEXT,
    at {{REAL}} NOT NULL,
    source_state TEXT,
    fps {{REAL}},
    camera TEXT,
    detectors {{JSON}} NOT NULL,
    payload {{JSON}} NOT NULL
);
CREATE INDEX perception_by_robot ON perception_status (robot_id, at);

CREATE TABLE audit_log (
    id {{ID}},
    at {{REAL}} NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    outcome TEXT,
    site_id TEXT,
    detail {{JSON}} NOT NULL
);
CREATE INDEX audit_by_at ON audit_log (at);
CREATE INDEX audit_by_actor ON audit_log (actor, at);
CREATE INDEX audit_by_action ON audit_log (action, at);
CREATE INDEX audit_by_target ON audit_log (target, at);
