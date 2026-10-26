from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import json
import sqlite3
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "aircargo_api.db"
API_VERSION = "2.3.0"


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    yield


app = FastAPI(
    title="Air Cargo ONE Record Provenance API",
    description=(
        "Prototype for JSON-LD logistics-object ingestion, "
        "duplicate detection, conflict tracking and metrics."
    ),
    version="0.1.0",
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


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "aircargo-one-record-api",
        "prototype": True,
    }


@app.post("/logistics-objects", status_code=201)
async def create_logistics_object(request: Request):
    try:
        document = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON request body")

    object_id, object_type = validate_jsonld(document)
    now = datetime.now(timezone.utc).isoformat()

    with connect_db() as conn:
        existing = conn.execute(
            "SELECT object_id FROM logistics_objects WHERE object_id = ?",
            (object_id,),
        ).fetchone()

        if existing:
            conflict_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO conflicts
                (conflict_id, object_id, conflict_type, severity,
                 description, status, detected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conflict_id,
                    object_id,
                    "DUPLICATE_IDENTIFIER",
                    "MEDIUM",
                    "A logistics object with this @id already exists.",
                    "OPEN",
                    now,
                ),
            )
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
                    "conflictType": "DUPLICATE_IDENTIFIER",
                    "status": "OPEN",
                },
                status_code=409,
            )

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
        "object_type": row["object_type"],
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
                "object_type": row["object_type"],
                "created_at": row["created_at"],
                "document": json.loads(row["payload"]),
            }
            for row in rows
        ],
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
        total_conflicts = conn.execute(
            "SELECT COUNT(*) FROM conflicts"
        ).fetchone()[0]
        open_conflicts = conn.execute(
            "SELECT COUNT(*) FROM conflicts WHERE status = 'OPEN'"
        ).fetchone()[0]

    return {
        "logisticsObjectsStored": total_objects,
        "conflictsDetected": total_conflicts,
        "openConflicts": open_conflicts,
        "conflictRatePercent": (
            round(total_conflicts / (total_objects + total_conflicts) * 100, 2)
            if total_objects + total_conflicts
            else 0
        ),
        "blockchainTransactionsSubmitted": 0,
        "note": (
            "Prototype counters only. Duplicate submissions are counted as "
            "conflicts; full operational conflict detection and performance "
            "measurements are not implemented yet."
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
            "/conflicts",
            "/metrics"
        ]
    }
