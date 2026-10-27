"""SQLite schema and upgrades for older databases.

`CREATE TABLE IF NOT EXISTS` never changes a table that already exists, so
every column added after the first version is also listed in MIGRATIONS and
added with ALTER TABLE when it is missing.
"""

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS logistics_objects (
        object_id TEXT PRIMARY KEY,
        object_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conflicts (
        conflict_id TEXT PRIMARY KEY,
        object_id TEXT NOT NULL,
        conflict_type TEXT NOT NULL,
        severity TEXT NOT NULL,
        description TEXT NOT NULL,
        status TEXT NOT NULL,
        detected_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conflict_audit (
        audit_id TEXT PRIMARY KEY,
        conflict_id TEXT NOT NULL,
        action TEXT NOT NULL,
        previous_status TEXT NOT NULL,
        new_status TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        FOREIGN KEY (conflict_id) REFERENCES conflicts(conflict_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS logistics_events (
        event_id TEXT PRIMARY KEY,
        shipment_id TEXT NOT NULL,
        status_code TEXT NOT NULL,
        milestone_rank INTEGER,
        event_date TEXT NOT NULL,
        time_type TEXT NOT NULL,
        recording_org TEXT NOT NULL,
        partial INTEGER NOT NULL,
        disputed INTEGER NOT NULL DEFAULT 0,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS party_reports (
        report_id TEXT PRIMARY KEY,
        shipment_id TEXT NOT NULL,
        recording_org TEXT NOT NULL,
        reporter_role TEXT,
        superseded INTEGER NOT NULL DEFAULT 0,
        disputed INTEGER NOT NULL DEFAULT 0,
        parsed TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS participants (
        org_id TEXT PRIMARY KEY,
        role TEXT,
        name TEXT,
        first_seen TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence (
        evidence_id TEXT PRIMARY KEY,
        conflict_id TEXT NOT NULL,
        party_org TEXT NOT NULL,
        party_role TEXT,
        filename TEXT NOT NULL,
        content_type TEXT,
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        note TEXT,
        stored_path TEXT NOT NULL,
        created_at TEXT NOT NULL,
        chain_status TEXT NOT NULL DEFAULT 'NOT_SUBMITTED',
        chain_tx_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chain_transactions (
        tx_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        ref_id TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        request TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        submitted_at TEXT,
        confirmed_at TEXT,
        confirmed_round INTEGER,
        tx_id TEXT,
        app_id INTEGER,
        fee_microalgos INTEGER,
        box_name TEXT,
        box_value_bytes INTEGER,
        mbr_microalgos INTEGER,
        submit_ms REAL,
        confirm_ms REAL,
        retrieve_ms REAL,
        verified INTEGER,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ingestion_log (
        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        record_id TEXT,
        shipment_id TEXT,
        outcome TEXT NOT NULL,
        http_status INTEGER NOT NULL,
        conflicts_found INTEGER NOT NULL DEFAULT 0,
        processing_ms REAL NOT NULL,
        received_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_shipment ON logistics_events (shipment_id)",
    "CREATE INDEX IF NOT EXISTS idx_reports_shipment ON party_reports (shipment_id)",
    "CREATE INDEX IF NOT EXISTS idx_evidence_conflict ON evidence (conflict_id)",
    "CREATE INDEX IF NOT EXISTS idx_chain_ref ON chain_transactions (ref_id)",
]

# Columns added after a table's first version: table -> {column: definition}.
MIGRATIONS = {
    "conflicts": {
        "related_object_id": "TEXT",
        "shipment_id": "TEXT",
        "field": "TEXT",
        "details": "TEXT",
        "conflict_hash": "TEXT",
        "chain_status": "TEXT NOT NULL DEFAULT 'NOT_SUBMITTED'",
        "chain_tx_id": "TEXT",
        "accepted_record": "TEXT",
        "resolution_note": "TEXT",
        "resolved_by": "TEXT",
        "resolved_at": "TEXT",
    },
    "logistics_events": {
        "location": "TEXT",
        "reporter_role": "TEXT",
        "chain_status": "TEXT NOT NULL DEFAULT 'NOT_SUBMITTED'",
        "chain_tx_id": "TEXT",
    },
}

# Index that needs a migrated column, so it is created after MIGRATIONS.
POST_MIGRATION = [
    "CREATE INDEX IF NOT EXISTS idx_conflicts_shipment ON conflicts (shipment_id)",
]


def add_missing_columns(conn, table, columns):
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def create_schema(conn):
    for statement in SCHEMA:
        conn.execute(statement)
    for table, columns in MIGRATIONS.items():
        add_missing_columns(conn, table, columns)
    for statement in POST_MIGRATION:
        conn.execute(statement)
