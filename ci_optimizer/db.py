from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS modules (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    layer TEXT NOT NULL,
    loc INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS module_deps (
    source_id INTEGER NOT NULL REFERENCES modules(id),
    target_id INTEGER NOT NULL REFERENCES modules(id),
    dep_kind TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_deps_target ON module_deps(target_id);

CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY,
    nodeid TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    class_name TEXT NOT NULL,
    name TEXT NOT NULL,
    domain TEXT NOT NULL,
    suite TEXT NOT NULL,
    owner TEXT NOT NULL,
    base_duration REAL NOT NULL,
    truth_flaky INTEGER NOT NULL DEFAULT 0,
    archetype_gt TEXT,
    flake_rate REAL NOT NULL DEFAULT 0,
    polluter_test_id INTEGER REFERENCES tests(id),
    healed_seq INTEGER,
    broken_seq INTEGER,
    created_seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tests_domain ON tests(domain);
CREATE INDEX IF NOT EXISTS idx_tests_truth ON tests(truth_flaky);

CREATE TABLE IF NOT EXISTS coverage (
    test_id INTEGER NOT NULL REFERENCES tests(id),
    module_id INTEGER NOT NULL REFERENCES modules(id),
    hit_count INTEGER NOT NULL,
    confidence REAL NOT NULL,
    observed INTEGER NOT NULL DEFAULT 1,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    PRIMARY KEY (test_id, module_id)
);
CREATE INDEX IF NOT EXISTS idx_coverage_module ON coverage(module_id);
CREATE INDEX IF NOT EXISTS idx_coverage_observed_module
    ON coverage(module_id, observed) WHERE observed = 1;

CREATE TABLE IF NOT EXISTS commits (
    sha TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    message TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    size_bucket TEXT NOT NULL,
    domains_json TEXT NOT NULL,
    layers_json TEXT NOT NULL,
    parent_sha TEXT
);
CREATE INDEX IF NOT EXISTS idx_commits_seq ON commits(seq);

CREATE TABLE IF NOT EXISTS commit_files (
    commit_sha TEXT NOT NULL REFERENCES commits(sha),
    path TEXT NOT NULL,
    module_id INTEGER REFERENCES modules(id),
    change_type TEXT NOT NULL,
    PRIMARY KEY (commit_sha, path)
);
CREATE INDEX IF NOT EXISTS idx_commit_files_module ON commit_files(module_id);

CREATE TABLE IF NOT EXISTS ci_runs (
    id INTEGER PRIMARY KEY,
    commit_sha TEXT NOT NULL REFERENCES commits(sha),
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    trigger TEXT NOT NULL,
    parallelism INTEGER NOT NULL,
    load REAL NOT NULL,
    order_seed INTEGER NOT NULL,
    total_count INTEGER NOT NULL,
    executed_count INTEGER NOT NULL,
    passed_count INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    retry_recovered_count INTEGER NOT NULL DEFAULT 0,
    duration_sec REAL NOT NULL,
    queue_sec REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_seq ON ci_runs(seq);
CREATE INDEX IF NOT EXISTS idx_runs_sha ON ci_runs(commit_sha);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    introduced_sha TEXT NOT NULL REFERENCES commits(sha),
    fixed_sha TEXT REFERENCES commits(sha),
    introduced_seq INTEGER NOT NULL,
    fixed_seq INTEGER,
    module_id INTEGER REFERENCES modules(id),
    domain TEXT NOT NULL,
    severity INTEGER NOT NULL,
    title TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_seq ON incidents(introduced_seq);

CREATE TABLE IF NOT EXISTS incident_tests (
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    test_id INTEGER NOT NULL REFERENCES tests(id),
    impact_strength REAL NOT NULL DEFAULT 1,
    PRIMARY KEY (incident_id, test_id)
);
CREATE INDEX IF NOT EXISTS idx_incident_tests_test ON incident_tests(test_id);

CREATE TABLE IF NOT EXISTS test_observations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES ci_runs(id),
    commit_sha TEXT NOT NULL,
    seq INTEGER NOT NULL,
    test_id INTEGER NOT NULL REFERENCES tests(id),
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    worker INTEGER,
    shard INTEGER,
    order_index INTEGER,
    preceding_test_id INTEGER,
    parallelism INTEGER,
    load REAL,
    error_type TEXT,
    message TEXT,
    category_gt TEXT,
    incident_id INTEGER,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_test_seq ON test_observations(test_id, seq);
CREATE INDEX IF NOT EXISTS idx_obs_sha_attempt ON test_observations(commit_sha, attempt);
CREATE INDEX IF NOT EXISTS idx_obs_seq_status ON test_observations(seq, status);
CREATE INDEX IF NOT EXISTS idx_obs_category ON test_observations(category_gt);

CREATE TABLE IF NOT EXISTS selection_snapshots (
    sha TEXT PRIMARY KEY REFERENCES commits(sha),
    seq INTEGER NOT NULL,
    tier TEXT NOT NULL,
    selected_count INTEGER NOT NULL,
    total_count INTEGER NOT NULL,
    skipped_count INTEGER NOT NULL,
    quarantined_count INTEGER NOT NULL,
    selected_duration_sec REAL NOT NULL,
    baseline_duration_sec REAL NOT NULL,
    estimated_missed_failures REAL NOT NULL,
    estimated_miss_rate REAL NOT NULL,
    miss_rate_upper95 REAL NOT NULL,
    true_failures INTEGER NOT NULL,
    selected_true_failures INTEGER NOT NULL,
    missed_true_failures INTEGER NOT NULL,
    new_true_failures INTEGER NOT NULL,
    missed_new_true_failures INTEGER NOT NULL,
    flaky_failures INTEGER NOT NULL,
    missed_test_ids_json TEXT NOT NULL,
    nightly_full INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_selection_seq ON selection_snapshots(seq);

CREATE TABLE IF NOT EXISTS selection_items (
    sha TEXT NOT NULL REFERENCES commits(sha),
    test_id INTEGER NOT NULL REFERENCES tests(id),
    score REAL NOT NULL,
    reason_codes TEXT NOT NULL,
    PRIMARY KEY (sha, test_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_selection_items_test ON selection_items(test_id);

CREATE TABLE IF NOT EXISTS quarantine_events (
    id INTEGER PRIMARY KEY,
    seq INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    sha TEXT NOT NULL,
    test_id INTEGER NOT NULL REFERENCES tests(id),
    event_type TEXT NOT NULL,
    reason TEXT NOT NULL,
    flake_score REAL NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_q_events_test ON quarantine_events(test_id, seq);
CREATE INDEX IF NOT EXISTS idx_q_events_seq ON quarantine_events(seq);

CREATE TABLE IF NOT EXISTS flake_findings (
    test_id INTEGER PRIMARY KEY REFERENCES tests(id),
    detected INTEGER NOT NULL,
    flake_score REAL NOT NULL,
    first_seq INTEGER,
    last_seq INTEGER,
    total_runs INTEGER NOT NULL,
    fail_count INTEGER NOT NULL,
    retry_pass_count INTEGER NOT NULL,
    mixed_sha_count INTEGER NOT NULL,
    flip_count INTEGER NOT NULL,
    pass_rate REAL NOT NULL,
    boundary_seq INTEGER,
    boundary_sha TEXT,
    post_boundary_fail_rate REAL,
    classification TEXT NOT NULL,
    status TEXT NOT NULL,
    causes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    updated_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_health (
    test_id INTEGER NOT NULL REFERENCES tests(id),
    day TEXT NOT NULL,
    run_count INTEGER NOT NULL,
    fail_count INTEGER NOT NULL,
    retry_pass_count INTEGER NOT NULL,
    pass_rate REAL NOT NULL,
    PRIMARY KEY (test_id, day)
);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def reset_database(db_path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
