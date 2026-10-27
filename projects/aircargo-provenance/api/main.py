from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import json
import sqlite3
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

try:  # imported as the "api" package (uvicorn api.main:app, pytest)
    from . import rules
except ImportError:  # run from inside the api/ folder (uvicorn main:app)
    import rules


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "aircargo_api.db"

# ONE Record API version used in the response media type. This is the
# version of the standard we follow, not the version of this application.
API_VERSION = "2.3.0"


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _add_missing_columns(conn, table, columns):
    """Add columns to an existing table. CREATE TABLE IF NOT EXISTS does not
    change a table that already exists, so older databases need this."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    with connect_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS logistics_objects (
                object_id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS conflicts (
                conflict_id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL,
                conflict_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL,
                detected_at TEXT NOT NULL
            )
        """)
        _add_missing_columns(
            conn,
            "conflicts",
            {"related_object_id": "TEXT", "shipment_id": "TEXT"},
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS conflict_audit (
                audit_id TEXT PRIMARY KEY,
                conflict_id TEXT NOT NULL,
                action TEXT NOT NULL,
                previous_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (conflict_id)
                    REFERENCES conflicts(conflict_id)
            )
        """)

        conn.execute("""
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
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_shipment
            ON logistics_events (shipment_id)
        """)

    yield


app = FastAPI(
    title="Air Cargo ONE Record Provenance API",
    description=(
        "Prototype for JSON-LD logistics-object and logistics-event "
        "ingestion, rule-based conflict detection, conflict tracking "
        "and metrics."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


def jsonld_response(data, status_code=200):
    return JSONResponse(
        content=data,
        status_code=status_code,
        media_type=f"application/ld+json; version={API_VERSION}",
        headers={"Content-Language": "en-US"},
    )


def validate_jsonld(document):
    if not isinstance(document, dict):
        raise HTTPException(400, "Request body must be a JSON-LD object")

    object_id = document.get("@id")
    object_type = document.get("@type")

    if not isinstance(object_id, str) or not object_id.strip():
        raise HTTPException(422, "JSON-LD @id is required")

    parsed_id = urlparse(object_id)
    if parsed_id.scheme not in ("http", "https") or not parsed_id.netloc:
        raise HTTPException(
            422,
            "@id must be an absolute HTTP or HTTPS URI",
        )

    if not object_type or not isinstance(object_type, (str, list)):
        raise HTTPException(422, "JSON-LD @type is required")

    if isinstance(object_type, list) and not all(
        isinstance(item, str) and item.strip() for item in object_type
    ):
        raise HTTPException(422, "@type list must contain non-empty strings")

    if not document.get("@context"):
        raise HTTPException(422, "JSON-LD @context is required")

    return object_id, object_type


async def read_json_body(request: Request):
    try:
        return await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON request body")


def find_existing_payload(conn, object_id):
    """Return the stored payload for an @id in either table, or None."""
    row = conn.execute(
        "SELECT payload FROM logistics_objects WHERE object_id = ?",
        (object_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT payload FROM logistics_events WHERE event_id = ?",
            (object_id,),
        ).fetchone()
    return None if row is None else json.loads(row["payload"])


def insert_conflict(
    conn,
    object_id,
    conflict_type,
    severity,
    description,
    detected_at,
    related_object_id=None,
    shipment_id=None,
):
    conflict_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO conflicts
        (conflict_id, object_id, conflict_type, severity, description,
         status, detected_at, related_object_id, shipment_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conflict_id,
            object_id,
            conflict_type,
            severity,
            description,
            "OPEN",
            detected_at,
            related_object_id,
            shipment_id,
        ),
    )
    return conflict_id


def duplicate_response(conflict_id, severity):
    return jsonld_response(
        {
            "@context": {
                "api": "https://onerecord.iata.org/ns/api#"
            },
            "@id": f"urn:aircargo:conflict:{conflict_id}",
            "@type": "api:Error",
            "api:hasTitle": "Duplicate logistics object identifier",
            "api:hasErrorDetail": [
                {
                    "@type": "api:ErrorDetail",
                    "api:hasCode": "409",
                    "api:hasMessage": (
                        "The identifier already exists; "
                        "a conflict record was opened."
                    ),
                }
            ],
            "conflictId": conflict_id,
            "conflictType": rules.DUPLICATE_IDENTIFIER,
            "severity": severity,
            "status": "OPEN",
        },
        status_code=409,
    )


def row_to_parsed_event(row):
    return rules.ParsedEvent(
        event_id=row["event_id"],
        shipment_id=row["shipment_id"],
        status_code=row["status_code"],
        milestone_rank=row["milestone_rank"],
        event_date=datetime.fromisoformat(row["event_date"]),
        time_type=row["time_type"],
        recording_org=row["recording_org"],
        partial=bool(row["partial"]),
    )


def event_row_to_dict(row):
    rank = row["milestone_rank"]
    return {
        "event_id": row["event_id"],
        "shipment_id": row["shipment_id"],
        "status_code": row["status_code"],
        "milestone": None if rank is None else rules.MILESTONES[rank],
        "event_date": row["event_date"],
        "time_type": row["time_type"],
        "recording_org": row["recording_org"],
        "partial": bool(row["partial"]),
        "disputed": bool(row["disputed"]),
        "created_at": row["created_at"],
    }


def require_shipment(conn, shipment_id):
    row = conn.execute(
        "SELECT object_id FROM logistics_objects WHERE object_id = ?",
        (shipment_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Shipment not found")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "aircargo-one-record-api",
        "prototype": True,
    }


@app.post("/logistics-objects", status_code=201)
async def create_logistics_object(request: Request):
    document = await read_json_body(request)
    object_id, object_type = validate_jsonld(document)

    if rules.is_event_type(object_type):
        raise HTTPException(
            422,
            "Logistics events must be posted to "
            "/logistics-objects/{shipment_id}/logistics-events",
        )

    now = datetime.now(timezone.utc).isoformat()

    with connect_db() as conn:
        if find_existing_payload(conn, object_id) is not None:
            conflict_id = insert_conflict(
                conn,
                object_id,
                rules.DUPLICATE_IDENTIFIER,
                "MEDIUM",
                "A logistics object with this @id already exists.",
                now,
            )
            return duplicate_response(conflict_id, "MEDIUM")

        conn.execute(
            """
            INSERT INTO logistics_objects
            (object_id, object_type, payload, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                object_id,
                json.dumps(object_type, ensure_ascii=False),
                json.dumps(document, ensure_ascii=False),
                now,
            ),
        )

    return jsonld_response(
        {
            "@context": document["@context"],
            "@id": object_id,
            "@type": object_type,
            "ingestionStatus": "ACCEPTED",
            "conflictDetected": False,
            "blockchainStatus": "NOT_SUBMITTED",
        },
        status_code=201,
    )


# Event routes are registered BEFORE GET /logistics-objects/{object_id:path},
# because that route would otherwise also match ".../logistics-events".

@app.post(
    "/logistics-objects/{shipment_id:path}/logistics-events",
    status_code=201,
)
async def create_logistics_event(shipment_id: str, request: Request):
    document = await read_json_body(request)
    event_id, _ = validate_jsonld(document)

    try:
        event = rules.parse_event(document)
    except rules.EventValidationError as error:
        raise HTTPException(422, str(error))

    if event.shipment_id != shipment_id:
        raise HTTPException(
            422,
            "cargo:eventFor must match the shipment in the request path",
        )

    now = datetime.now(timezone.utc).isoformat()

    with connect_db() as conn:
        require_shipment(conn, shipment_id)

        # R1 - repeated identifier. Short-circuits R2 and R3.
        stored_payload = find_existing_payload(conn, event_id)
        if stored_payload is not None:
            severity = rules.duplicate_severity(stored_payload, document)
            description = (
                "An event with this @id was already received with identical "
                "content (replay)."
                if severity == "LOW"
                else "An object with this @id already exists with different "
                "content; events are immutable."
            )
            conflict_id = insert_conflict(
                conn,
                event_id,
                rules.DUPLICATE_IDENTIFIER,
                severity,
                description,
                now,
                shipment_id=shipment_id,
            )
            return duplicate_response(conflict_id, severity)

        # R2 and R3 - compare with every stored event for this shipment.
        stored_rows = conn.execute(
            "SELECT * FROM logistics_events WHERE shipment_id = ?",
            (shipment_id,),
        ).fetchall()

        hits = []
        for row in stored_rows:
            hit = rules.compare(event, row_to_parsed_event(row))
            if hit is not None:
                hits.append((row["event_id"], hit))

        disputed = bool(hits)
        conn.execute(
            """
            INSERT INTO logistics_events
            (event_id, shipment_id, status_code, milestone_rank, event_date,
             time_type, recording_org, partial, disputed, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                shipment_id,
                event.status_code,
                event.milestone_rank,
                event.event_date.isoformat(),
                event.time_type,
                event.recording_org,
                int(event.partial),
                int(disputed),
                json.dumps(document, ensure_ascii=False),
                now,
            ),
        )

        conflicts = []
        for related_id, hit in hits:
            conflict_id = insert_conflict(
                conn,
                event_id,
                hit.conflict_type,
                hit.severity,
                hit.description,
                now,
                related_object_id=related_id,
                shipment_id=shipment_id,
            )
            # Both events in a conflicting pair are disputed.
            conn.execute(
                "UPDATE logistics_events SET disputed = 1 WHERE event_id = ?",
                (related_id,),
            )
            conflicts.append(
                {
                    "conflictId": conflict_id,
                    "conflictType": hit.conflict_type,
                    "severity": hit.severity,
                    "relatedEventId": related_id,
                    "description": hit.description,
                }
            )

    return jsonld_response(
        {
            "@context": document["@context"],
            "@id": event_id,
            "@type": document["@type"],
            "ingestionStatus": "ACCEPTED_DISPUTED" if disputed else "ACCEPTED",
            "conflictDetected": disputed,
            "conflicts": conflicts,
            "milestone": event.milestone,
            "inScope": event.in_scope,
            "compared": event.comparable,
            "blockchainStatus": "NOT_SUBMITTED",
        },
        status_code=201,
    )


@app.get("/logistics-objects/{shipment_id:path}/logistics-events")
def list_logistics_events(shipment_id: str):
    with connect_db() as conn:
        require_shipment(conn, shipment_id)
        rows = conn.execute(
            """
            SELECT * FROM logistics_events
            WHERE shipment_id = ?
            ORDER BY event_date, created_at
            """,
            (shipment_id,),
        ).fetchall()

    return {
        "shipment_id": shipment_id,
        "count": len(rows),
        "items": [event_row_to_dict(row) for row in rows],
    }


@app.get("/logistics-objects/{object_id:path}")
def get_logistics_object(object_id: str):
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT object_id, object_type, payload, created_at
            FROM logistics_objects
            WHERE object_id = ?
            """,
            (object_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(404, "Logistics object not found")

    return {
        "object_id": row["object_id"],
        "object_type": json.loads(row["payload"])["@type"],
        "created_at": row["created_at"],
        "document": json.loads(row["payload"]),
    }


@app.get("/logistics-objects")
def list_logistics_objects():
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT object_id, object_type, payload, created_at
            FROM logistics_objects ORDER BY created_at DESC
            """
        ).fetchall()

    return {
        "count": len(rows),
        "items": [
            {
                "object_id": row["object_id"],
                "object_type": json.loads(row["payload"])["@type"],
                "created_at": row["created_at"],
                "document": json.loads(row["payload"]),
            }
            for row in rows
        ],
    }


@app.get("/conflicts/{conflict_id}")
def get_conflict(conflict_id: str):
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM conflicts
            WHERE conflict_id = ?
            """,
            (conflict_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(404, "Conflict not found")

    return dict(row)


@app.patch("/conflicts/{conflict_id}/resolve")
def resolve_conflict(conflict_id: str):
    with connect_db() as conn:
        row = conn.execute(
            "SELECT status FROM conflicts WHERE conflict_id = ?",
            (conflict_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(404, "Conflict not found")

        if row["status"] == "RESOLVED":
            return {
                "conflictId": conflict_id,
                "status": "RESOLVED",
                "message": "Conflict is already resolved",
            }

        if row["status"] != "OPEN":
            raise HTTPException(
                409,
                f"Cannot resolve conflict with status {row['status']}",
            )

        now = datetime.now(timezone.utc).isoformat()

        conn.execute(
            "UPDATE conflicts SET status = 'RESOLVED' WHERE conflict_id = ?",
            (conflict_id,),
        )

        conn.execute(
            """
            INSERT INTO conflict_audit (
                audit_id, conflict_id, action,
                previous_status, new_status, recorded_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                conflict_id,
                "CONFLICT_RESOLVED",
                row["status"],
                "RESOLVED",
                now,
            ),
        )

    return {
        "conflictId": conflict_id,
        "status": "RESOLVED",
        "message": "Conflict resolved successfully",
    }


@app.get("/conflicts")
def list_conflicts(status: str | None = None):
    with connect_db() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT * FROM conflicts
                WHERE status = ? ORDER BY detected_at DESC
                """,
                (status.upper(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM conflicts ORDER BY detected_at DESC"
            ).fetchall()

    return {"count": len(rows), "items": [dict(row) for row in rows]}


@app.get("/metrics")
def metrics():
    with connect_db() as conn:
        total_objects = conn.execute(
            "SELECT COUNT(*) FROM logistics_objects"
        ).fetchone()[0]
        total_events = conn.execute(
            "SELECT COUNT(*) FROM logistics_events"
        ).fetchone()[0]
        disputed_events = conn.execute(
            "SELECT COUNT(*) FROM logistics_events WHERE disputed = 1"
        ).fetchone()[0]
        total_conflicts = conn.execute(
            "SELECT COUNT(*) FROM conflicts"
        ).fetchone()[0]
        open_conflicts = conn.execute(
            "SELECT COUNT(*) FROM conflicts WHERE status = 'OPEN'"
        ).fetchone()[0]
        by_type_rows = conn.execute(
            """
            SELECT conflict_type, COUNT(*) AS n
            FROM conflicts GROUP BY conflict_type
            """
        ).fetchall()

    return {
        "logisticsObjectsStored": total_objects,
        "logisticsEventsStored": total_events,
        "disputedEvents": disputed_events,
        "conflictsDetected": total_conflicts,
        "openConflicts": open_conflicts,
        "conflictsByType": {
            row["conflict_type"]: row["n"] for row in by_type_rows
        },
        "blockchainTransactionsSubmitted": 0,
        "note": (
            "Prototype counters only. These are not accuracy or performance "
            "measurements; those require a labelled evaluation dataset."
        ),
    }


@app.get("/")
def home():
    return {
        "project": "Air Cargo ONE Record Provenance API",
        "status": "running",
        "docs": "/docs",
        "endpoints": [
            "/health",
            "/logistics-objects",
            "/logistics-objects/{shipment_id}/logistics-events",
            "/conflicts",
            "/metrics"
        ]
    }
