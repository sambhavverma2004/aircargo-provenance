"""Air cargo conflict detection API (ONE Record-inspired, Algorand-anchored).

Pipeline for every incoming record:
  1. filter     - structural JSON-LD and field validation (HTTP 400/422,
                  never a conflict)
  2. detect     - compare with the stored records of the same shipment
                  (api/rules.py for events, api/report_rules.py for reports)
  3. classify   - conflict type + severity + the two records involved
  4. store      - SQLite (records, conflicts, audit, ingestion log)
  5. anchor     - hashes queued for Algorand (api/chain.py), asynchronously
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

try:  # imported as the "api" package (uvicorn api.main:app, pytest)
    from . import catalog, chain, db, report_rules, rules
except ImportError:  # run from inside the api/ folder (uvicorn main:app)
    import catalog
    import chain
    import db
    import report_rules
    import rules


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DB_PATH = Path(os.getenv("AIRCARGO_DB", ROOT / "aircargo_api.db"))
EVIDENCE_DIR = Path(os.getenv("AIRCARGO_EVIDENCE_DIR", ROOT / "evidence_files"))
STATIC_DIR = ROOT / "static"
RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results"
MAX_EVIDENCE_BYTES = 5 * 1024 * 1024

# ONE Record API version used in the response media type. This is the
# version of the standard we follow, not the version of this application.
API_VERSION = "2.3.0"

# The Algorand anchoring service; replaced in lifespan from CHAIN_MODE.
CHAIN = chain.ChainAnchor(chain.OffBackend(), lambda: connect_db())


class _ClosingConnection(sqlite3.Connection):
    """`with connect_db() as conn:` commits (or rolls back) AND closes.
    Plain sqlite3 only commits, leaving the file open; on Windows an open
    file cannot be deleted or replaced."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    return conn


def _add_missing_columns(conn, table, columns):
    """Kept for compatibility; see api/db.py."""
    db.add_missing_columns(conn, table, columns)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global CHAIN
    with connect_db() as conn:
        db.create_schema(conn)

    CHAIN = chain.ChainAnchor(
        chain.backend_from_env(),
        connect_db,
        anchor_events=os.getenv("ANCHOR_EVENTS", "1") == "1",
    )
    CHAIN.start()
    try:
        yield
    finally:
        CHAIN.stop()


app = FastAPI(
    title="Air Cargo Conflict Detection API",
    description=(
        "Ingests ONE Record-style shipments, logistics events and party "
        "reports; detects and classifies data conflicts between parties; "
        "anchors conflict hashes on Algorand; serves a dashboard."
    ),
    version="0.3.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def now_iso():
    return datetime.now(timezone.utc).isoformat()


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
        raise HTTPException(422, "@id must be an absolute HTTP or HTTPS URI")

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


def payload_hash(document) -> str:
    return chain.canonical_hash(document).hex()


def find_record(conn, record_id):
    """Return (kind, payload, row) for an @id in any record table."""
    for kind, table, key in (
        ("object", "logistics_objects", "object_id"),
        ("event", "logistics_events", "event_id"),
        ("report", "party_reports", "report_id"),
    ):
        row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (record_id,)).fetchone()
        if row is not None:
            return kind, json.loads(row["payload"]), row
    return None, None, None


def find_existing_payload(conn, object_id):
    """Return the stored payload for an @id in any table, or None."""
    return find_record(conn, object_id)[1]


def compute_conflict_hash(conn, conflict_id, conflict_type, shipment_id,
                          object_id, related_id, details, object_payload=None):
    if object_payload is None:
        object_payload = find_record(conn, object_id)[1]
    related_payload = find_record(conn, related_id)[1] if related_id else None
    return chain.canonical_hash({
        "conflict_id": conflict_id,
        "conflict_type": conflict_type,
        "shipment_id": shipment_id,
        "object_id": object_id,
        "object_payload_sha256": payload_hash(object_payload) if object_payload else None,
        "related_object_id": related_id,
        "related_payload_sha256": payload_hash(related_payload) if related_payload else None,
        "details": details,
    })


def insert_conflict(conn, object_id, conflict_type, severity, description,
                    detected_at, related_object_id=None, shipment_id=None,
                    field=None, details=None, object_payload=None):
    conflict_id = str(uuid.uuid4())
    details = details or {}
    conflict_hash = compute_conflict_hash(
        conn, conflict_id, conflict_type, shipment_id, object_id,
        related_object_id, details, object_payload,
    )
    conn.execute(
        """
        INSERT INTO conflicts
        (conflict_id, object_id, conflict_type, severity, description, status,
         detected_at, related_object_id, shipment_id, field, details, conflict_hash)
        VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)
        """,
        (
            conflict_id, object_id, conflict_type, severity, description,
            detected_at, related_object_id, shipment_id, field,
            json.dumps(details, ensure_ascii=False, default=str),
            conflict_hash.hex(),
        ),
    )
    job = CHAIN.enqueue(
        chain.conflict_call(conflict_id, shipment_id, object_id, related_object_id,
                            conflict_type, conflict_hash),
        conflict_id, conn,
    )
    return conflict_id, job


def hit_details(hit, new_record, new_org, stored_record, stored_org):
    return {
        "field": hit.field,
        "new": {"record_id": new_record, "org": new_org, "value": hit.value_a},
        "stored": {"record_id": stored_record, "org": stored_org, "value": hit.value_b},
    }


def duplicate_response(conflict_id, severity):
    return jsonld_response(
        {
            "@context": {"api": "https://onerecord.iata.org/ns/api#"},
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


def handle_duplicate(conn, record_id, document, shipment_id, now, kind_label):
    """R1. Returns (response, job) if `record_id` already exists, else None."""
    stored_payload = find_existing_payload(conn, record_id)
    if stored_payload is None:
        return None
    severity = rules.duplicate_severity(stored_payload, document)
    description = (
        f"This {kind_label} @id was already received with identical content (replay)."
        if severity == "LOW"
        else "An object with this @id already exists with different content; "
             "events and reports are immutable."
    )
    conflict_id, job = insert_conflict(
        conn, record_id, rules.DUPLICATE_IDENTIFIER, severity, description, now,
        shipment_id=shipment_id, field="@id",
        details={"field": "@id",
                 "new": {"record_id": record_id, "value": payload_hash(document)},
                 "stored": {"record_id": record_id, "value": payload_hash(stored_payload)}},
        object_payload=stored_payload,
    )
    return duplicate_response(conflict_id, severity), job


def upsert_participant(conn, org_id, role, name, now):
    if not org_id:
        return
    conn.execute(
        """
        INSERT INTO participants (org_id, role, name, first_seen) VALUES (?, ?, ?, ?)
        ON CONFLICT(org_id) DO UPDATE SET
            role = COALESCE(excluded.role, participants.role),
            name = COALESCE(excluded.name, participants.name)
        """,
        (org_id, role, name, now),
    )


def organisation_name(document):
    name = document.get("acp:organizationName") if isinstance(document, dict) else None
    name = rules._literal(name)
    return name.strip() if isinstance(name, str) and name.strip() else None


def log_ingestion(kind, record_id, shipment_id, outcome, http_status,
                  conflicts_found, started):
    elapsed = (time.perf_counter() - started) * 1000
    try:
        with connect_db() as conn:
            conn.execute(
                """
                INSERT INTO ingestion_log
                (kind, record_id, shipment_id, outcome, http_status,
                 conflicts_found, processing_ms, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (kind, record_id, shipment_id, outcome, http_status,
                 conflicts_found, elapsed, now_iso()),
            )
    except sqlite3.Error:
        pass  # logging must never break ingestion


def require_shipment(conn, shipment_id):
    row = conn.execute(
        "SELECT object_id FROM logistics_objects WHERE object_id = ?", (shipment_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Shipment not found")


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
        location=row["location"],
        reporter_role=row["reporter_role"],
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
        "reporter_role": row["reporter_role"],
        "location": row["location"],
        "partial": bool(row["partial"]),
        "disputed": bool(row["disputed"]),
        "chain_status": row["chain_status"],
        "chain_tx_id": row["chain_tx_id"],
        "created_at": row["created_at"],
    }


def report_row_to_dict(row):
    return {
        "report_id": row["report_id"],
        "shipment_id": row["shipment_id"],
        "recording_org": row["recording_org"],
        "reporter_role": row["reporter_role"],
        "superseded": bool(row["superseded"]),
        "disputed": bool(row["disputed"]),
        "fields": json.loads(row["parsed"]),
        "created_at": row["created_at"],
    }


def current_reports(conn, shipment_id):
    rows = conn.execute(
        "SELECT * FROM party_reports WHERE shipment_id = ? AND superseded = 0",
        (shipment_id,),
    ).fetchall()
    return [(row, report_rules.ParsedReport.from_dict(json.loads(row["parsed"]))) for row in rows]


def mark_disputed(conn, record_ids):
    for record_id in record_ids:
        conn.execute("UPDATE logistics_events SET disputed = 1 WHERE event_id = ?", (record_id,))
        conn.execute("UPDATE party_reports SET disputed = 1 WHERE report_id = ?", (record_id,))


# --------------------------------------------------------------------------- #
# Basic endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "aircargo-one-record-api",
        "prototype": True,
        "chainMode": CHAIN.backend.mode,
    }


@app.get("/")
def home():
    return {
        "project": "Air Cargo ONE Record Provenance API",
        "status": "running",
        "docs": "/docs",
        "dashboard": "/dashboard",
        "endpoints": [
            "/health",
            "/logistics-objects",
            "/logistics-objects/{shipment_id}/logistics-events",
            "/logistics-objects/{shipment_id}/party-reports",
            "/conflicts",
            "/metrics",
            "/dashboard",
        ]
    }


@app.get("/catalog")
def conflict_catalog():
    return {
        "conflictTypes": catalog.CONFLICT_TYPES,
        "severities": catalog.SEVERITIES,
        "milestones": rules.MILESTONES,
        "statusCodes": rules.STATUS_CODE_TO_RANK,
        "roles": rules.PARTICIPANT_ROLES,
        "tolerances": {
            "sameMilestoneMinutes": rules.SAME_MILESTONE_TOLERANCE.total_seconds() / 60,
            "clockSkewMinutes": rules.CLOCK_SKEW_TOLERANCE.total_seconds() / 60,
            "weightPercent": report_rules.WEIGHT_TOLERANCE * 100,
            "weightAbsoluteKg": report_rules.WEIGHT_ABS_TOLERANCE_KG,
            "volumePercent": report_rules.VOLUME_TOLERANCE * 100,
            "nameSimilarity": report_rules.NAME_SIMILARITY_THRESHOLD,
        },
    }


# --------------------------------------------------------------------------- #
# Ingestion: shipments (logistics objects)
# --------------------------------------------------------------------------- #

@app.post("/logistics-objects", status_code=201)
async def create_logistics_object(request: Request):
    started = time.perf_counter()
    object_id = None
    try:
        document = await read_json_body(request)
        object_id, object_type = validate_jsonld(document)

        if rules.is_event_type(object_type):
            raise HTTPException(
                422,
                "Logistics events must be posted to "
                "/logistics-objects/{shipment_id}/logistics-events",
            )

        now = now_iso()
        with connect_db() as conn:
            duplicate = None
            if find_existing_payload(conn, object_id) is not None:
                stored_payload = find_existing_payload(conn, object_id)
                conflict_id, job = insert_conflict(
                    conn, object_id, rules.DUPLICATE_IDENTIFIER, "MEDIUM",
                    "A logistics object with this @id already exists.", now,
                    field="@id",
                    details={"field": "@id",
                             "new": {"record_id": object_id, "value": payload_hash(document)},
                             "stored": {"record_id": object_id, "value": payload_hash(stored_payload)}},
                    object_payload=stored_payload,
                )
                duplicate = (duplicate_response(conflict_id, "MEDIUM"), job)
            else:
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
        if duplicate:
            CHAIN.submit([duplicate[1]])
            log_ingestion("object", object_id, object_id, "duplicate", 409, 1, started)
            return duplicate[0]
    except HTTPException as error:
        log_ingestion("object", object_id, None, "rejected", error.status_code, 0, started)
        raise

    log_ingestion("object", object_id, object_id, "accepted", 201, 0, started)
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


# Event and report routes are registered BEFORE GET
# /logistics-objects/{object_id:path}, because that route would otherwise
# also match ".../logistics-events" and ".../party-reports".

# --------------------------------------------------------------------------- #
# Ingestion: logistics events
# --------------------------------------------------------------------------- #

@app.post("/logistics-objects/{shipment_id:path}/logistics-events", status_code=201)
async def create_logistics_event(shipment_id: str, request: Request):
    started = time.perf_counter()
    event_id = None
    try:
        document = await read_json_body(request)
        event_id, _ = validate_jsonld(document)

        try:
            event = rules.parse_event(document)
        except rules.EventValidationError as error:
            raise HTTPException(422, str(error))

        if event.shipment_id != shipment_id:
            raise HTTPException(
                422, "cargo:eventFor must match the shipment in the request path",
            )

        now = now_iso()
        jobs = []
        with connect_db() as conn:
            require_shipment(conn, shipment_id)

            # R1 - repeated identifier. Short-circuits all other rules.
            duplicate = handle_duplicate(conn, event_id, document, shipment_id, now, "event")
            if duplicate is None:
                hits = []
                # R2, R3 - against every stored event of this shipment.
                for row in conn.execute(
                    "SELECT * FROM logistics_events WHERE shipment_id = ?", (shipment_id,)
                ).fetchall():
                    hit = rules.compare(event, row_to_parsed_event(row))
                    if hit is not None:
                        hits.append((row["event_id"], row["recording_org"], hit))
                # R4 - event location against each party's current report.
                for row, report in current_reports(conn, shipment_id):
                    hit = report_rules.compare_event_location(event, report)
                    if hit is not None:
                        hits.append((row["report_id"], row["recording_org"], hit))

                conn.execute(
                    """
                    INSERT INTO logistics_events
                    (event_id, shipment_id, status_code, milestone_rank, event_date,
                     time_type, recording_org, partial, disputed, payload, created_at,
                     location, reporter_role)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id, shipment_id, event.status_code, event.milestone_rank,
                        event.event_date.isoformat(), event.time_type,
                        event.recording_org, int(event.partial), int(bool(hits)),
                        json.dumps(document, ensure_ascii=False), now,
                        event.location, event.reporter_role,
                    ),
                )
                upsert_participant(conn, event.recording_org, event.reporter_role,
                                   organisation_name(document), now)

                conflicts = []
                for related_id, related_org, hit in hits:
                    conflict_id, job = insert_conflict(
                        conn, event_id, hit.conflict_type, hit.severity, hit.description,
                        now, related_object_id=related_id, shipment_id=shipment_id,
                        field=hit.field,
                        details=hit_details(hit, event_id, event.recording_org,
                                            related_id, related_org),
                        object_payload=document,
                    )
                    jobs.append(job)
                    conflicts.append({
                        "conflictId": conflict_id,
                        "conflictType": hit.conflict_type,
                        "severity": hit.severity,
                        "relatedEventId": related_id,
                        "relatedRecordId": related_id,
                        "description": hit.description,
                    })
                mark_disputed(conn, [related for related, _, _ in hits])

                if event.comparable or event.in_scope:
                    jobs.append(CHAIN.enqueue(
                        chain.event_call(
                            event_id, shipment_id, event.status_code,
                            int(event.event_date.timestamp()), event.recording_org,
                            event.location, chain.canonical_hash(document),
                        ),
                        event_id, conn,
                    ))
        if duplicate is not None:
            CHAIN.submit([duplicate[1]])
            log_ingestion("event", event_id, shipment_id, "duplicate", 409, 1, started)
            return duplicate[0]
        CHAIN.submit(jobs)
    except HTTPException as error:
        log_ingestion("event", event_id, shipment_id, "rejected", error.status_code, 0, started)
        raise

    disputed = bool(conflicts)
    log_ingestion("event", event_id, shipment_id,
                  "disputed" if disputed else "accepted", 201, len(conflicts), started)
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
            "blockchainStatus": "QUEUED" if CHAIN.enabled else "NOT_SUBMITTED",
        },
        status_code=201,
    )


@app.get("/logistics-objects/{shipment_id:path}/logistics-events")
def list_logistics_events(shipment_id: str):
    with connect_db() as conn:
        require_shipment(conn, shipment_id)
        rows = conn.execute(
            """
            SELECT * FROM logistics_events WHERE shipment_id = ?
            ORDER BY event_date, created_at
            """,
            (shipment_id,),
        ).fetchall()
    return {
        "shipment_id": shipment_id,
        "count": len(rows),
        "items": [event_row_to_dict(row) for row in rows],
    }


# --------------------------------------------------------------------------- #
# Ingestion: party reports (each party's version of the shipment details)
# --------------------------------------------------------------------------- #

@app.post("/logistics-objects/{shipment_id:path}/party-reports", status_code=201)
async def create_party_report(shipment_id: str, request: Request):
    started = time.perf_counter()
    report_id = None
    try:
        document = await read_json_body(request)
        report_id, _ = validate_jsonld(document)

        try:
            report = report_rules.parse_report(document)
        except rules.EventValidationError as error:
            raise HTTPException(422, str(error))

        if report.shipment_id != shipment_id:
            raise HTTPException(
                422, "acp:reportFor must match the shipment in the request path",
            )

        now = now_iso()
        jobs = []
        with connect_db() as conn:
            require_shipment(conn, shipment_id)

            duplicate = handle_duplicate(conn, report_id, document, shipment_id, now, "report")
            if duplicate is None:
                # A newer report from the same party replaces its previous one.
                superseded = conn.execute(
                    """
                    UPDATE party_reports SET superseded = 1
                    WHERE shipment_id = ? AND recording_org = ? AND superseded = 0
                    """,
                    (shipment_id, report.recording_org),
                ).rowcount

                hits = []
                # R5-R12 - against the current report of every other party.
                for row, stored in current_reports(conn, shipment_id):
                    for hit in report_rules.compare_reports(report, stored):
                        hits.append((row["report_id"], row["recording_org"], hit))
                # R4 - stored events' locations against this report.
                for row in conn.execute(
                    "SELECT * FROM logistics_events WHERE shipment_id = ?", (shipment_id,)
                ).fetchall():
                    hit = report_rules.compare_event_location(row_to_parsed_event(row), report)
                    if hit is not None:
                        # value_a is the event's location, value_b the route.
                        hits.append((row["event_id"], row["recording_org"], rules.RuleHit(
                            hit.conflict_type, hit.severity, hit.description, hit.field,
                            value_a=hit.value_b, value_b=hit.value_a,
                        )))

                conn.execute(
                    """
                    INSERT INTO party_reports
                    (report_id, shipment_id, recording_org, reporter_role, superseded,
                     disputed, parsed, payload, created_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        report_id, shipment_id, report.recording_org, report.reporter_role,
                        int(bool(hits)), json.dumps(report.to_dict(), ensure_ascii=False),
                        json.dumps(document, ensure_ascii=False), now,
                    ),
                )
                upsert_participant(conn, report.recording_org, report.reporter_role,
                                   organisation_name(document), now)

                conflicts = []
                for related_id, related_org, hit in hits:
                    conflict_id, job = insert_conflict(
                        conn, report_id, hit.conflict_type, hit.severity, hit.description,
                        now, related_object_id=related_id, shipment_id=shipment_id,
                        field=hit.field,
                        details=hit_details(hit, report_id, report.recording_org,
                                            related_id, related_org),
                        object_payload=document,
                    )
                    jobs.append(job)
                    conflicts.append({
                        "conflictId": conflict_id,
                        "conflictType": hit.conflict_type,
                        "severity": hit.severity,
                        "field": hit.field,
                        "relatedRecordId": related_id,
                        "description": hit.description,
                    })
                mark_disputed(conn, [related for related, _, _ in hits])
        if duplicate is not None:
            CHAIN.submit([duplicate[1]])
            log_ingestion("report", report_id, shipment_id, "duplicate", 409, 1, started)
            return duplicate[0]
        CHAIN.submit(jobs)
    except HTTPException as error:
        log_ingestion("report", report_id, shipment_id, "rejected", error.status_code, 0, started)
        raise

    log_ingestion("report", report_id, shipment_id,
                  "disputed" if conflicts else "accepted", 201, len(conflicts), started)
    return jsonld_response(
        {
            "@context": document["@context"],
            "@id": report_id,
            "@type": document["@type"],
            "ingestionStatus": "ACCEPTED_DISPUTED" if conflicts else "ACCEPTED",
            "conflictDetected": bool(conflicts),
            "conflicts": conflicts,
            "supersededPrevious": bool(superseded),
            "blockchainStatus": "QUEUED" if CHAIN.enabled and conflicts else "NOT_SUBMITTED",
        },
        status_code=201,
    )


@app.get("/logistics-objects/{shipment_id:path}/party-reports")
def list_party_reports(shipment_id: str, include_superseded: bool = True):
    with connect_db() as conn:
        require_shipment(conn, shipment_id)
        query = "SELECT * FROM party_reports WHERE shipment_id = ?"
        if not include_superseded:
            query += " AND superseded = 0"
        rows = conn.execute(query + " ORDER BY created_at", (shipment_id,)).fetchall()
    return {
        "shipment_id": shipment_id,
        "count": len(rows),
        "items": [report_row_to_dict(row) for row in rows],
    }


# --------------------------------------------------------------------------- #
# Logistics object retrieval
# --------------------------------------------------------------------------- #

@app.get("/logistics-objects/{object_id:path}")
def get_logistics_object(object_id: str):
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT object_id, object_type, payload, created_at
            FROM logistics_objects WHERE object_id = ?
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


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #

def conflict_row_to_dict(row):
    data = dict(row)
    data["details"] = json.loads(row["details"]) if row["details"] else None
    data["label"] = catalog.label(row["conflict_type"])
    data["category"] = catalog.category(row["conflict_type"])
    return data


def record_summary(conn, record_id, role):
    kind, payload, row = find_record(conn, record_id)
    if kind is None:
        return None
    org = None
    reporter_role = None
    if kind == "event":
        org, reporter_role = row["recording_org"], row["reporter_role"]
    elif kind == "report":
        org, reporter_role = row["recording_org"], row["reporter_role"]
    participant = None
    if org:
        participant = conn.execute(
            "SELECT * FROM participants WHERE org_id = ?", (org,)
        ).fetchone()
    summary = None
    if kind == "event":
        summary = event_row_to_dict(row)
    elif kind == "report":
        summary = report_row_to_dict(row)
    return {
        "side": role,
        "record_id": record_id,
        "kind": kind,
        "recording_org": org,
        "reporter_role": reporter_role or (participant["role"] if participant else None),
        "role_name": rules.PARTICIPANT_ROLES.get(
            reporter_role or (participant["role"] if participant else None) or "", None),
        "org_name": participant["name"] if participant else None,
        "payload_sha256": payload_hash(payload),
        "summary": summary,
        "payload": payload,
    }


@app.get("/conflicts")
def list_conflicts(status: str | None = None, type: str | None = None,
                   severity: str | None = None, shipment: str | None = None,
                   q: str | None = None, limit: int = 500, offset: int = 0):
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status.upper())
    if type:
        clauses.append("conflict_type = ?")
        params.append(type.upper())
    if severity:
        clauses.append("severity = ?")
        params.append(severity.upper())
    if shipment:
        clauses.append("shipment_id = ?")
        params.append(shipment)
    if q:
        clauses.append("(description LIKE ? OR object_id LIKE ? OR shipment_id LIKE ?)")
        params.extend([f"%{q}%"] * 3)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    limit = max(1, min(limit, 5000))
    with connect_db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM conflicts{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM conflicts{where} ORDER BY detected_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return {
        "count": len(rows),
        "total": total,
        "items": [conflict_row_to_dict(row) for row in rows],
    }


@app.get("/conflicts/{conflict_id}")
def get_conflict(conflict_id: str):
    with connect_db() as conn:
        row = conn.execute(
            "SELECT * FROM conflicts WHERE conflict_id = ?", (conflict_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Conflict not found")
        data = conflict_row_to_dict(row)
        data["records"] = [
            summary for summary in (
                record_summary(conn, row["object_id"], "new"),
                record_summary(conn, row["related_object_id"], "stored")
                if row["related_object_id"] else None,
            ) if summary
        ]
        data["evidence"] = [
            evidence_row_to_dict(e) for e in conn.execute(
                "SELECT * FROM evidence WHERE conflict_id = ? ORDER BY created_at",
                (conflict_id,),
            ).fetchall()
        ]
        data["chain_transactions"] = [
            dict(t) for t in conn.execute(
                "SELECT tx_row_id, kind, mode, status, tx_id, confirmed_round, app_id, "
                "fee_microalgos, submit_ms, confirm_ms, retrieve_ms, verified, error "
                "FROM chain_transactions WHERE ref_id = ? ORDER BY tx_row_id",
                (conflict_id,),
            ).fetchall()
        ]
        data["audit"] = [
            dict(a) for a in conn.execute(
                "SELECT * FROM conflict_audit WHERE conflict_id = ? ORDER BY recorded_at",
                (conflict_id,),
            ).fetchall()
        ]
    return data


@app.patch("/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: str, request: Request):
    body = {}
    raw = await request.body()
    if raw.strip():
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(400, "Invalid JSON request body")
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be a JSON object")

    job = None
    with connect_db() as conn:
        row = conn.execute(
            "SELECT * FROM conflicts WHERE conflict_id = ?", (conflict_id,)
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
            raise HTTPException(409, f"Cannot resolve conflict with status {row['status']}")

        accepted = body.get("acceptedRecord")
        if accepted is not None and accepted not in (row["object_id"], row["related_object_id"]):
            raise HTTPException(422, "acceptedRecord must be one of the two conflicting records")
        note = body.get("note")
        resolved_by = body.get("resolvedBy") or "operator"
        now = now_iso()

        conn.execute(
            """
            UPDATE conflicts SET status = 'RESOLVED', accepted_record = ?,
                resolution_note = ?, resolved_by = ?, resolved_at = ?
            WHERE conflict_id = ?
            """,
            (accepted, note, resolved_by, now, conflict_id),
        )
        conn.execute(
            """
            INSERT INTO conflict_audit (
                audit_id, conflict_id, action, previous_status, new_status, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), conflict_id, "CONFLICT_RESOLVED", row["status"],
             "RESOLVED", now),
        )
        if row["chain_status"] in ("QUEUED", "CONFIRMED") and row["conflict_hash"]:
            resolution = "accepted:" + chain.chain_key(accepted, 16) if accepted else "resolved"
            job = CHAIN.enqueue(
                chain.resolve_call(
                    conflict_id, row["shipment_id"], row["object_id"],
                    row["related_object_id"], row["conflict_type"],
                    bytes.fromhex(row["conflict_hash"]), resolution, resolved_by,
                ),
                conflict_id, conn,
            )
    CHAIN.submit([job])

    return {
        "conflictId": conflict_id,
        "status": "RESOLVED",
        "message": "Conflict resolved successfully",
        "acceptedRecord": accepted,
        "blockchainStatus": "QUEUED" if job else "NOT_SUBMITTED",
    }


@app.get("/conflicts/{conflict_id}/verify")
def verify_conflict(conflict_id: str):
    """Recompute the conflict hash from the stored records and compare it
    with the hash anchored on Algorand."""
    with connect_db() as conn:
        row = conn.execute(
            "SELECT * FROM conflicts WHERE conflict_id = ?", (conflict_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Conflict not found")
        details = json.loads(row["details"]) if row["details"] else {}
        object_payload = None
        if row["conflict_type"] == rules.DUPLICATE_IDENTIFIER:
            object_payload = find_record(conn, row["object_id"])[1]
        recomputed = compute_conflict_hash(
            conn, conflict_id, row["conflict_type"], row["shipment_id"],
            row["object_id"], row["related_object_id"], details, object_payload,
        ).hex()
        tx = conn.execute(
            "SELECT * FROM chain_transactions WHERE ref_id = ? AND kind = 'conflict' "
            "ORDER BY tx_row_id DESC LIMIT 1", (conflict_id,),
        ).fetchone()

    stored = row["conflict_hash"]
    on_chain = None
    if CHAIN.enabled and stored:
        call = chain.conflict_call(
            conflict_id, row["shipment_id"], row["object_id"], row["related_object_id"],
            row["conflict_type"], bytes.fromhex(stored),
        )
        try:
            value = CHAIN.read_main_box(call)
        except Exception as error:
            raise HTTPException(502, f"Could not read from the chain: {error}")
        found = chain.hash_in_conflict_box(value) if value else None
        on_chain = found.hex() if found else None

    return {
        "conflictId": conflict_id,
        "mode": CHAIN.backend.mode,
        "storedHash": stored,
        "recomputedHash": recomputed,
        "onChainHash": on_chain,
        "databaseIntact": stored == recomputed,
        "onChainMatch": on_chain is not None and on_chain == recomputed,
        "txId": tx["tx_id"] if tx else None,
        "confirmedRound": tx["confirmed_round"] if tx else None,
    }


# --------------------------------------------------------------------------- #
# Evidence (showcase): each party can attach a file to a conflict
# --------------------------------------------------------------------------- #

def evidence_row_to_dict(row):
    data = dict(row)
    data.pop("stored_path", None)
    return data


@app.post("/conflicts/{conflict_id}/evidence", status_code=201)
async def add_evidence(conflict_id: str, request: Request):
    body = await read_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    party_org = body.get("partyOrg")
    filename = body.get("filename")
    content = body.get("contentBase64")
    if not isinstance(party_org, str) or not party_org.strip():
        raise HTTPException(422, "partyOrg is required")
    if not isinstance(filename, str) or not filename.strip():
        raise HTTPException(422, "filename is required")
    if not isinstance(content, str) or not content:
        raise HTTPException(422, "contentBase64 is required")
    try:
        data = base64.b64decode(content, validate=True)
    except ValueError:
        raise HTTPException(422, "contentBase64 is not valid base64")
    if not data:
        raise HTTPException(422, "Evidence file is empty")
    if len(data) > MAX_EVIDENCE_BYTES:
        raise HTTPException(413, "Evidence file is larger than 5 MB")

    with connect_db() as conn:
        row = conn.execute(
            "SELECT * FROM conflicts WHERE conflict_id = ?", (conflict_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Conflict not found")
        parties = {}
        for record_id in (row["object_id"], row["related_object_id"]):
            if not record_id:
                continue
            kind, _, record = find_record(conn, record_id)
            if kind in ("event", "report"):
                parties[record["recording_org"]] = record["reporter_role"]
        if parties and party_org not in parties:
            raise HTTPException(
                422, "partyOrg must be one of the organisations in this conflict",
            )

        evidence_id = str(uuid.uuid4())
        digest = hashlib.sha256(data).hexdigest()
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[:80] or "evidence"
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        stored_path = EVIDENCE_DIR / f"{evidence_id}_{safe_name}"
        stored_path.write_bytes(data)
        now = now_iso()
        conn.execute(
            """
            INSERT INTO evidence
            (evidence_id, conflict_id, party_org, party_role, filename, content_type,
             size_bytes, sha256, note, stored_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id, conflict_id, party_org, parties.get(party_org),
                filename, body.get("contentType"), len(data), digest,
                body.get("note"), str(stored_path), now,
            ),
        )
        conn.execute(
            """
            INSERT INTO conflict_audit (
                audit_id, conflict_id, action, previous_status, new_status, recorded_at
            ) VALUES (?, ?, 'EVIDENCE_ADDED', ?, ?, ?)
            """,
            (str(uuid.uuid4()), conflict_id, row["status"], row["status"], now),
        )
        job = CHAIN.enqueue(
            chain.evidence_call(evidence_id, row["shipment_id"] or row["object_id"],
                                bytes.fromhex(digest), party_org),
            evidence_id, conn,
        )
    CHAIN.submit([job])
    return {
        "evidenceId": evidence_id,
        "conflictId": conflict_id,
        "sha256": digest,
        "sizeBytes": len(data),
        "blockchainStatus": "QUEUED" if job else "NOT_SUBMITTED",
    }


@app.get("/conflicts/{conflict_id}/evidence")
def list_evidence(conflict_id: str):
    with connect_db() as conn:
        if conn.execute("SELECT 1 FROM conflicts WHERE conflict_id = ?", (conflict_id,)).fetchone() is None:
            raise HTTPException(404, "Conflict not found")
        rows = conn.execute(
            "SELECT * FROM evidence WHERE conflict_id = ? ORDER BY created_at", (conflict_id,)
        ).fetchall()
    return {"count": len(rows), "items": [evidence_row_to_dict(r) for r in rows]}


@app.get("/evidence/{evidence_id}/file")
def download_evidence(evidence_id: str):
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()
    if row is None or not Path(row["stored_path"]).exists():
        raise HTTPException(404, "Evidence not found")
    return FileResponse(row["stored_path"], filename=row["filename"],
                        media_type=row["content_type"] or "application/octet-stream")


@app.post("/evidence/{evidence_id}/verify")
async def verify_evidence(evidence_id: str, request: Request):
    """Check a file against the hash stored for this evidence, in the
    database and (when anchored) on Algorand."""
    body = await read_json_body(request)
    try:
        data = base64.b64decode((body or {}).get("contentBase64") or "", validate=True)
    except ValueError:
        raise HTTPException(422, "contentBase64 is not valid base64")
    if not data:
        raise HTTPException(422, "contentBase64 is required")
    with connect_db() as conn:
        row = conn.execute("SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()
        conflict = row and conn.execute(
            "SELECT shipment_id, object_id FROM conflicts WHERE conflict_id = ?",
            (row["conflict_id"],),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Evidence not found")
    digest = hashlib.sha256(data).hexdigest()
    on_chain = None
    if CHAIN.enabled:
        call = chain.evidence_call(evidence_id, conflict["shipment_id"] or conflict["object_id"],
                                   bytes.fromhex(row["sha256"]), row["party_org"])
        value = CHAIN.read_main_box(call)
        on_chain = value[:32].hex() if value else None
    return {
        "evidenceId": evidence_id,
        "submittedHash": digest,
        "storedHash": row["sha256"],
        "onChainHash": on_chain,
        "matchesStored": digest == row["sha256"],
        "matchesOnChain": on_chain is not None and digest == on_chain,
        "mode": CHAIN.backend.mode,
    }


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (k - low), 3)


def latency_stats(values):
    return {
        "count": len(values),
        "mean": round(sum(values) / len(values), 3) if values else None,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": round(max(values), 3) if values else None,
    }


@app.get("/metrics")
def metrics():
    with connect_db() as conn:
        def count(sql, *params):
            return conn.execute(sql, params).fetchone()[0]

        by_type_rows = conn.execute(
            "SELECT conflict_type, COUNT(*) AS n FROM conflicts GROUP BY conflict_type"
        ).fetchall()
        result = {
            "logisticsObjectsStored": count("SELECT COUNT(*) FROM logistics_objects"),
            "logisticsEventsStored": count("SELECT COUNT(*) FROM logistics_events"),
            "partyReportsStored": count("SELECT COUNT(*) FROM party_reports"),
            "disputedEvents": count("SELECT COUNT(*) FROM logistics_events WHERE disputed = 1"),
            "disputedReports": count("SELECT COUNT(*) FROM party_reports WHERE disputed = 1"),
            "conflictsDetected": count("SELECT COUNT(*) FROM conflicts"),
            "openConflicts": count("SELECT COUNT(*) FROM conflicts WHERE status = 'OPEN'"),
            "conflictsByType": {row["conflict_type"]: row["n"] for row in by_type_rows},
            "rejectedByFilter": count("SELECT COUNT(*) FROM ingestion_log WHERE outcome = 'rejected'"),
            "blockchainTransactionsSubmitted": count(
                "SELECT COUNT(*) FROM chain_transactions WHERE status = 'CONFIRMED' AND mode = 'algorand'"
            ),
            "chainMode": CHAIN.backend.mode,
            "note": (
                "Prototype counters. Detection accuracy comes from "
                "evaluation/evaluate.py (/metrics/evaluation); Algorand "
                "performance from evaluation/benchmark_chain.py."
            ),
        }
    return result


@app.get("/metrics/live")
def live_metrics():
    """Latency and throughput measured on the records this server received."""
    with connect_db() as conn:
        logs = conn.execute(
            "SELECT kind, outcome, processing_ms, received_at FROM ingestion_log"
        ).fetchall()
        txs = conn.execute(
            "SELECT kind, mode, status, submit_ms, confirm_ms, retrieve_ms, "
            "fee_microalgos, mbr_microalgos, verified FROM chain_transactions"
        ).fetchall()
    processing = [row["processing_ms"] for row in logs if row["outcome"] != "rejected"]
    by_kind = {}
    for kind in ("object", "event", "report"):
        by_kind[kind] = latency_stats(
            [r["processing_ms"] for r in logs if r["kind"] == kind and r["outcome"] != "rejected"]
        )
    throughput = None
    if len(logs) >= 2:
        first = datetime.fromisoformat(logs[0]["received_at"])
        last = datetime.fromisoformat(logs[-1]["received_at"])
        seconds = (last - first).total_seconds()
        throughput = round(len(logs) / seconds, 2) if seconds > 0 else None

    chain_stats = {}
    for mode in sorted({t["mode"] for t in txs}):
        confirmed = [t for t in txs if t["mode"] == mode and t["status"] == "CONFIRMED"]
        chain_stats[mode] = {
            "confirmed": len(confirmed),
            "failed": sum(1 for t in txs if t["mode"] == mode and t["status"] == "FAILED"),
            "queued": sum(1 for t in txs if t["mode"] == mode and t["status"] == "QUEUED"),
            "verified": sum(1 for t in confirmed if t["verified"]),
            "submitMs": latency_stats([t["submit_ms"] for t in confirmed if t["submit_ms"] is not None]),
            "confirmMs": latency_stats([t["confirm_ms"] for t in confirmed if t["confirm_ms"] is not None]),
            "retrieveMs": latency_stats([t["retrieve_ms"] for t in confirmed if t["retrieve_ms"] is not None]),
            "totalFeeMicroAlgos": sum(t["fee_microalgos"] or 0 for t in confirmed),
            "totalBoxMbrMicroAlgos": sum(t["mbr_microalgos"] or 0 for t in confirmed),
        }
    return {
        "ingestion": {
            "records": len(logs),
            "rejected": sum(1 for r in logs if r["outcome"] == "rejected"),
            "processingMs": latency_stats(processing),
            "byKind": by_kind,
            "throughputRecordsPerSecond": throughput,
        },
        "chain": chain_stats,
        "chainMode": CHAIN.backend.mode,
    }


def _latest_result(prefix):
    path = RESULTS_DIR / f"{prefix}_latest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


@app.get("/metrics/evaluation")
def evaluation_metrics():
    result = _latest_result("evaluation")
    if result is None:
        raise HTTPException(404, "No evaluation yet. Run: python -m evaluation.evaluate")
    return result


@app.get("/metrics/chain-benchmark")
def chain_benchmark_metrics():
    result = _latest_result("chain_benchmark")
    if result is None:
        result = _latest_result("chain_benchmark_simulated")
    if result is None:
        raise HTTPException(404, "No benchmark yet. Run: python -m evaluation.benchmark_chain")
    return result


# --------------------------------------------------------------------------- #
# Blockchain status
# --------------------------------------------------------------------------- #

@app.get("/chain/status")
def chain_status():
    with connect_db() as conn:
        counts = {
            row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM chain_transactions WHERE mode = ? GROUP BY status",
                (CHAIN.backend.mode,),
            )
        }
    info = CHAIN.info()
    info["transactions"] = counts
    return info


@app.get("/chain/transactions")
def chain_transactions(limit: int = 100, kind: str | None = None):
    limit = max(1, min(limit, 1000))
    query = (
        "SELECT tx_row_id, kind, ref_id, mode, status, queued_at, confirmed_at, "
        "confirmed_round, tx_id, app_id, fee_microalgos, box_name, box_value_bytes, "
        "mbr_microalgos, submit_ms, confirm_ms, retrieve_ms, verified, error "
        "FROM chain_transactions"
    )
    params = []
    if kind:
        query += " WHERE kind = ?"
        params.append(kind)
    query += " ORDER BY tx_row_id DESC LIMIT ?"
    params.append(limit)
    with connect_db() as conn:
        rows = conn.execute(query, params).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    page = STATIC_DIR / "dashboard.html"
    if not page.exists():
        raise HTTPException(404, "Dashboard file missing (api/static/dashboard.html)")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/dashboard/summary")
def dashboard_summary():
    with connect_db() as conn:
        def count(sql, *params):
            return conn.execute(sql, params).fetchone()[0]

        by_type = {
            r["conflict_type"]: {"total": r["total"], "open": r["open"], "high": r["high"]}
            for r in conn.execute(
                """
                SELECT conflict_type, COUNT(*) AS total,
                       SUM(status = 'OPEN') AS open,
                       SUM(severity = 'HIGH') AS high
                FROM conflicts GROUP BY conflict_type
                """
            )
        }
        by_severity = {
            r["severity"]: r["n"] for r in conn.execute(
                "SELECT severity, COUNT(*) AS n FROM conflicts WHERE status = 'OPEN' GROUP BY severity"
            )
        }
        by_category = {}
        for conflict_type, values in by_type.items():
            cat = catalog.category(conflict_type)
            by_category[cat] = by_category.get(cat, 0) + values["total"]
        recent = [
            conflict_row_to_dict(r) for r in conn.execute(
                "SELECT * FROM conflicts ORDER BY detected_at DESC LIMIT 12"
            )
        ]
        chain_counts = {
            r["status"]: r["n"] for r in conn.execute(
                "SELECT chain_status AS status, COUNT(*) AS n FROM conflicts GROUP BY chain_status"
            )
        }
        per_minute = [
            {"minute": r["minute"], "records": r["n"], "conflicts": r["c"] or 0}
            for r in conn.execute(
                """
                SELECT substr(received_at, 1, 16) AS minute, COUNT(*) AS n,
                       SUM(conflicts_found) AS c
                FROM ingestion_log GROUP BY minute ORDER BY minute DESC LIMIT 30
                """
            )
        ][::-1]
        return {
            "shipments": count("SELECT COUNT(*) FROM logistics_objects"),
            "events": count("SELECT COUNT(*) FROM logistics_events"),
            "reports": count("SELECT COUNT(*) FROM party_reports"),
            "participants": count("SELECT COUNT(*) FROM participants"),
            "rejected": count("SELECT COUNT(*) FROM ingestion_log WHERE outcome = 'rejected'"),
            "records": count("SELECT COUNT(*) FROM ingestion_log"),
            "conflicts": count("SELECT COUNT(*) FROM conflicts"),
            "openConflicts": count("SELECT COUNT(*) FROM conflicts WHERE status = 'OPEN'"),
            "openHigh": count("SELECT COUNT(*) FROM conflicts WHERE status = 'OPEN' AND severity = 'HIGH'"),
            "shipmentsWithConflicts": count("SELECT COUNT(DISTINCT shipment_id) FROM conflicts WHERE shipment_id IS NOT NULL"),
            "evidence": count("SELECT COUNT(*) FROM evidence"),
            "byType": by_type,
            "bySeverityOpen": by_severity,
            "byCategory": by_category,
            "conflictChainStatus": chain_counts,
            "recent": recent,
            "perMinute": per_minute,
            "chain": CHAIN.info(),
        }


@app.get("/dashboard/shipments")
def dashboard_shipments(limit: int = 300):
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT o.object_id, o.created_at,
              (SELECT COUNT(*) FROM logistics_events e WHERE e.shipment_id = o.object_id) AS events,
              (SELECT COUNT(*) FROM party_reports r WHERE r.shipment_id = o.object_id AND r.superseded = 0) AS reports,
              (SELECT COUNT(*) FROM conflicts c WHERE c.shipment_id = o.object_id) AS conflicts,
              (SELECT COUNT(*) FROM conflicts c WHERE c.shipment_id = o.object_id AND c.status = 'OPEN') AS open,
              (SELECT COUNT(*) FROM conflicts c WHERE c.shipment_id = o.object_id AND c.status = 'OPEN' AND c.severity = 'HIGH') AS high
            FROM logistics_objects o ORDER BY o.created_at DESC LIMIT ?
            """,
            (max(1, min(limit, 2000)),),
        ).fetchall()
    return {"count": len(rows), "items": [dict(r) for r in rows]}


@app.get("/dashboard/shipment")
def dashboard_shipment(id: str):
    with connect_db() as conn:
        shipment = conn.execute(
            "SELECT * FROM logistics_objects WHERE object_id = ?", (id,)
        ).fetchone()
        if shipment is None:
            raise HTTPException(404, "Shipment not found")
        events = [event_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM logistics_events WHERE shipment_id = ? ORDER BY event_date, created_at", (id,)
        )]
        reports = [report_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM party_reports WHERE shipment_id = ? ORDER BY created_at", (id,)
        )]
        conflicts = [conflict_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM conflicts WHERE shipment_id = ? ORDER BY detected_at", (id,)
        )]
        orgs = {e["recording_org"] for e in events} | {r["recording_org"] for r in reports}
        participants = {
            r["org_id"]: dict(r) for r in conn.execute(
                f"SELECT * FROM participants WHERE org_id IN ({','.join('?' * len(orgs))})",
                tuple(orgs),
            )
        } if orgs else {}
    return {
        "shipment": {
            "object_id": shipment["object_id"],
            "created_at": shipment["created_at"],
            "document": json.loads(shipment["payload"]),
        },
        "events": events,
        "reports": reports,
        "conflicts": conflicts,
        "participants": participants,
    }
