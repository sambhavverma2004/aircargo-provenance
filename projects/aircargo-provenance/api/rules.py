"""Event parsing and conflict rules for logistics events.

The rules are defined in docs/CONFLICT_RULES.md. This module contains no web
or database code, so every rule can be tested on its own.

Each event is read as one claim:
    organisation r asserts that shipment S reached milestone m at time t.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import json
import re


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Delta: two reports of the SAME milestone further apart than this disagree.
SAME_MILESTONE_TOLERANCE = timedelta(minutes=30)

# Epsilon: clock-skew tolerance when ordering DIFFERENT milestones.
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)

# Milestone sequence, identical to the ShipmentState smart contract.
MILESTONES = (
    "CREATED",
    "RECEIVED",
    "LOADED",
    "DEPARTED",
    "ARRIVED",
    "DELIVERED",
)

# IATA status codes accepted for each milestone (prototype mapping).
STATUS_CODE_TO_RANK = {
    "BKD": 0,  # booked                         -> CREATED
    "RCS": 1,  # received from shipper           -> RECEIVED
    "MAN": 2,  # manifested                      -> LOADED
    "DEP": 3,  # departed                        -> DEPARTED
    "ARR": 4,  # arrived                         -> ARRIVED
    "DLV": 5,  # delivered                       -> DELIVERED
}

STATUS_CODE_BASE = "https://onerecord.iata.org/ns/code-lists/StatusCode#"
CARGO_NS = "https://onerecord.iata.org/ns/cargo#"
EVENT_TYPES = ("cargo:LogisticsEvent", CARGO_NS + "LogisticsEvent")
ACTUAL = "ACTUAL"

STATUS_MISMATCH = "STATUS_MISMATCH"
INVALID_TRANSITION = "INVALID_TRANSITION"
DUPLICATE_IDENTIFIER = "DUPLICATE_IDENTIFIER"

SEVERITY = {
    STATUS_MISMATCH: "MEDIUM",
    INVALID_TRANSITION: "HIGH",
}

_STATUS_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")


class EventValidationError(ValueError):
    """Raised when an event is structurally invalid (HTTP 422, no conflict)."""


@dataclass(frozen=True)
class ParsedEvent:
    event_id: str
    shipment_id: str
    status_code: str
    milestone_rank: int | None
    event_date: datetime
    time_type: str
    recording_org: str
    partial: bool

    @property
    def milestone(self) -> str | None:
        if self.milestone_rank is None:
            return None
        return MILESTONES[self.milestone_rank]

    @property
    def in_scope(self) -> bool:
        return self.milestone_rank is not None

    @property
    def comparable(self) -> bool:
        """Only actual, complete, mapped events take part in R2 and R3."""
        return self.time_type == ACTUAL and not self.partial and self.in_scope


@dataclass(frozen=True)
class RuleHit:
    conflict_type: str
    severity: str
    description: str


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def is_event_type(object_type) -> bool:
    types = object_type if isinstance(object_type, list) else [object_type]
    return any(t in EVENT_TYPES for t in types)


def _is_absolute_uri(value) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _reference(value, field: str) -> str:
    """Accept {"@id": "..."} or a plain string and return the string."""
    if isinstance(value, dict):
        value = value.get("@id")
    if not isinstance(value, str) or not value.strip():
        raise EventValidationError(f"{field} is required")
    return value.strip()


def _literal(value):
    """Accept {"@value": x} or a plain value and return x."""
    if isinstance(value, dict) and "@value" in value:
        return value["@value"]
    return value


def parse_status_code(value) -> str:
    raw = _reference(value, "cargo:eventCode")
    if "#" in raw:
        if not raw.startswith(STATUS_CODE_BASE):
            raise EventValidationError(
                "cargo:eventCode must be a ONE Record StatusCode"
            )
        raw = raw.split("#", 1)[1]
    code = raw.strip()
    if not _STATUS_CODE_PATTERN.match(code):
        raise EventValidationError(
            f"cargo:eventCode '{code}' is not a valid status code"
        )
    return code


def parse_event_date(value) -> datetime:
    raw = _literal(value)
    if not isinstance(raw, str) or not raw.strip():
        raise EventValidationError("cargo:eventDate is required")
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        raise EventValidationError(
            "cargo:eventDate must be an ISO 8601 date-time"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventValidationError("cargo:eventDate must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_time_type(value) -> str:
    if value is None:
        return ACTUAL
    raw = _reference(value, "cargo:eventTimeType")
    token = re.split(r"[#:/]", raw)[-1].strip().upper()
    if not token:
        raise EventValidationError("cargo:eventTimeType is invalid")
    return token


def parse_partial(value) -> bool:
    if value is None:
        return False
    raw = _literal(value)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    raise EventValidationError("cargo:partialEventIndicator must be a boolean")


def parse_event(document: dict) -> ParsedEvent:
    """Validate the event-specific fields of an already structurally valid
    JSON-LD document. Raises EventValidationError on any problem."""
    if not is_event_type(document.get("@type")):
        raise EventValidationError("@type must be cargo:LogisticsEvent")

    shipment_id = _reference(document.get("cargo:eventFor"), "cargo:eventFor")
    status_code = parse_status_code(document.get("cargo:eventCode"))
    event_date = parse_event_date(document.get("cargo:eventDate"))
    time_type = parse_time_type(document.get("cargo:eventTimeType"))
    recording_org = _reference(
        document.get("cargo:recordingOrganization"),
        "cargo:recordingOrganization",
    )
    if not _is_absolute_uri(recording_org):
        raise EventValidationError(
            "cargo:recordingOrganization must be an absolute HTTP or HTTPS URI"
        )
    partial = parse_partial(document.get("cargo:partialEventIndicator"))

    return ParsedEvent(
        event_id=document["@id"],
        shipment_id=shipment_id,
        status_code=status_code,
        milestone_rank=STATUS_CODE_TO_RANK.get(status_code),
        event_date=event_date,
        time_type=time_type,
        recording_org=recording_org,
        partial=partial,
    )


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

def canonical_json(document) -> str:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def duplicate_severity(stored_document, new_document) -> str:
    """R1: identical replay is LOW; same @id with different content is HIGH."""
    if canonical_json(stored_document) == canonical_json(new_document):
        return "LOW"
    return "HIGH"


def compare(new: ParsedEvent, stored: ParsedEvent) -> RuleHit | None:
    """Apply R2 and R3 to one pair of events. Returns None if consistent."""
    if new.shipment_id != stored.shipment_id:
        return None
    if not (new.comparable and stored.comparable):
        return None

    gap = new.event_date - stored.event_date

    # R2 - same milestone reported at materially different times.
    if new.milestone_rank == stored.milestone_rank:
        if abs(gap) > SAME_MILESTONE_TOLERANCE:
            return RuleHit(
                STATUS_MISMATCH,
                SEVERITY[STATUS_MISMATCH],
                (
                    f"{new.milestone} reported at "
                    f"{new.event_date.isoformat()} by {new.recording_org}, "
                    f"but at {stored.event_date.isoformat()} by "
                    f"{stored.recording_org}."
                ),
            )
        return None

    # R3 - time order contradicts milestone order (beyond clock skew).
    earlier_milestone_reported_later = (
        new.milestone_rank < stored.milestone_rank
        and gap > CLOCK_SKEW_TOLERANCE
    )
    later_milestone_reported_earlier = (
        new.milestone_rank > stored.milestone_rank
        and gap < -CLOCK_SKEW_TOLERANCE
    )
    if earlier_milestone_reported_later or later_milestone_reported_earlier:
        return RuleHit(
            INVALID_TRANSITION,
            SEVERITY[INVALID_TRANSITION],
            (
                f"{new.milestone} at {new.event_date.isoformat()} "
                f"({new.recording_org}) contradicts {stored.milestone} at "
                f"{stored.event_date.isoformat()} ({stored.recording_org})."
            ),
        )
    return None
