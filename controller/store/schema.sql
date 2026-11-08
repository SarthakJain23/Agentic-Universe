-- Controller store. One SQLite file per scenario run (WAL). Everything the controller knows or decided
-- lives here; the simulator's own logs are never read.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- String <-> small int dictionary (workflow ids, option names, cohort ids, dispositions, feature values).
CREATE TABLE IF NOT EXISTS codes (
    kind  TEXT NOT NULL,
    value TEXT NOT NULL,
    id    INTEGER NOT NULL,
    PRIMARY KEY (kind, value)
);

-- Ingestion checkpoint: a chunk row is written in the same transaction as the chunk's facts.
CREATE TABLE IF NOT EXISTS chunks (
    chunk         INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL,
    events        INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL,
    until_minute  INTEGER NOT NULL,
    ingested_wall REAL NOT NULL,
    wall_secs     REAL NOT NULL
);

-- feat packs the four lead features as bytes: lang | tier<<8 | age<<16 | value<<24 (codes table ids).
CREATE TABLE IF NOT EXISTS leads (
    wf       INTEGER NOT NULL,
    lead     INTEGER NOT NULL,
    person   INTEGER NOT NULL,
    campaign INTEGER NOT NULL,
    feat     INTEGER NOT NULL,
    minute   INTEGER NOT NULL,
    PRIMARY KEY (wf, lead)
) WITHOUT ROWID;

-- One row per dial. Raw facts: the analysis source and the compliance answer to
-- "what did this customer hear on that day, and why".
CREATE TABLE IF NOT EXISTS calls (
    event_id    INTEGER PRIMARY KEY,
    wf          INTEGER NOT NULL,
    minute      INTEGER NOT NULL,
    lead        INTEGER NOT NULL,
    person      INTEGER NOT NULL,
    feat        INTEGER NOT NULL,
    attempt     INTEGER NOT NULL,
    cfg_version INTEGER NOT NULL,
    cohort      INTEGER NOT NULL,
    w           INTEGER NOT NULL,
    v           INTEGER NOT NULL,
    d           INTEGER NOT NULL,
    p           INTEGER NOT NULL,
    connected   INTEGER NOT NULL,
    engaged     INTEGER NOT NULL,
    flag        INTEGER NOT NULL,
    disp        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_wf_minute ON calls (wf, minute);
CREATE INDEX IF NOT EXISTS calls_person ON calls (wf, person);

-- Non-call events (campaign_created, config_changed, approval_decided), verbatim.
CREATE TABLE IF NOT EXISTS sim_events (
    event_id INTEGER PRIMARY KEY,
    type     TEXT NOT NULL,
    wf       TEXT,
    minute   INTEGER NOT NULL,
    body     TEXT NOT NULL
);

-- Mirror of each workflow's config history (from GET /config/history + /config), incl. supervisor edits.
CREATE TABLE IF NOT EXISTS config_versions (
    wf           TEXT NOT NULL,
    version      INTEGER NOT NULL,
    actor        TEXT NOT NULL,
    config       TEXT NOT NULL,
    changed_dims TEXT NOT NULL,
    applied_minute INTEGER,
    proposal_id  TEXT,
    approval_id  TEXT,
    PRIMARY KEY (wf, version)
);

-- Every change we attempted, via approval or auto-apply. `state` is the governance state machine.
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id    TEXT PRIMARY KEY,
    wf             TEXT NOT NULL,
    route          TEXT NOT NULL,          -- approval | auto
    kind           TEXT NOT NULL,          -- routine | incident | safety
    state          TEXT NOT NULL,
    base_version   INTEGER NOT NULL,
    config         TEXT NOT NULL,          -- full proposed config
    request        TEXT NOT NULL,          -- full body sent (summary, diff, dry run, impact, rationale ...)
    intents        TEXT NOT NULL,          -- learning-state transitions applied only if the change applies
    approval_id    TEXT,
    reasons        TEXT,
    created_minute INTEGER NOT NULL,
    decided_minute INTEGER,
    applied_minute INTEGER,
    applied_version INTEGER,
    supersedes     TEXT,
    updated_minute INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS proposals_wf_state ON proposals (wf, state);

-- Arm registry: what the controller believes about each option of each agent dimension, per segment.
CREATE TABLE IF NOT EXISTS arms (
    wf       TEXT NOT NULL,
    dim      TEXT NOT NULL,
    segment  TEXT NOT NULL,                -- '*' or a Segment id
    option   TEXT NOT NULL,
    status   TEXT NOT NULL,                -- champion | challenger | holdout | dropped | retired
    since_minute INTEGER NOT NULL,
    note     TEXT,
    PRIMARY KEY (wf, dim, segment, option)
);

-- Open and closed incidents (hallucination, caller ID, decay, guardrail).
CREATE TABLE IF NOT EXISTS incidents (
    id            TEXT PRIMARY KEY,
    wf            TEXT NOT NULL,
    kind          TEXT NOT NULL,
    subject       TEXT NOT NULL,           -- prompt version, caller id, option ...
    state         TEXT NOT NULL,           -- open | fixing | verifying | resolved | abandoned
    opened_minute INTEGER NOT NULL,
    resolved_minute INTEGER,
    data          TEXT NOT NULL            -- JSON working state (candidates tried, validation ids ...)
);

-- The audit trail. Every detection, decision, no-op-with-reason and outcome.
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    wf          TEXT,
    minute      INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    summary     TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    proposal_id TEXT,
    incident_id TEXT,
    dedup_key   TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS decisions_wf_minute ON decisions (wf, minute);

CREATE TABLE IF NOT EXISTS credits_ledger (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    wf       TEXT NOT NULL,
    minute   INTEGER NOT NULL,
    what     TEXT NOT NULL,
    cost     REAL NOT NULL,
    ref      TEXT,
    remaining REAL
);

-- Loop health, one row per tick.
CREATE TABLE IF NOT EXISTS metrics (
    minute        INTEGER PRIMARY KEY,
    wall          REAL NOT NULL,
    events        INTEGER NOT NULL,
    ingest_secs   REAL NOT NULL,
    decide_secs   REAL NOT NULL,
    advance_secs  REAL NOT NULL,
    lag_minutes   INTEGER NOT NULL,
    decisions     INTEGER NOT NULL,
    credits       REAL NOT NULL,
    rss_mb        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    minute    INTEGER NOT NULL,
    severity  TEXT NOT NULL,               -- page | ticket | info
    wf        TEXT,
    message   TEXT NOT NULL,
    dedup_key TEXT UNIQUE
);
