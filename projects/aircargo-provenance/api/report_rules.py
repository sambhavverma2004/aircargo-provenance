"""Party reports and attribute conflict rules.

A *party report* is one organisation's version of a shipment's details
(weight, volume, pieces, parties, route, special handling, waybill).
Several parties report on the same shipment; the rules compare the reports
field by field, pairwise, and classify every disagreement.

Rules are defined in docs/CONFLICT_RULES.md (section 10 onwards).
Pure functions only: no web or database code.
"""

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
import re
import unicodedata

try:
    from .rules import (
        CARGO_NS,
        DESTINATION_MILESTONES,
        ORIGIN_MILESTONES,
        PARTICIPANT_ROLES,
        EventValidationError,
        LOCATION_MISMATCH,
        ParsedEvent,
        RuleHit,
        _is_absolute_uri,
        _literal,
        _reference,
        parse_location,
        parse_role,
        short_org,
    )
except ImportError:  # imported without the "api" package
    from rules import (  # type: ignore[no-redef]
        CARGO_NS,
        DESTINATION_MILESTONES,
        ORIGIN_MILESTONES,
        PARTICIPANT_ROLES,
        EventValidationError,
        LOCATION_MISMATCH,
        ParsedEvent,
        RuleHit,
        _is_absolute_uri,
        _literal,
        _reference,
        parse_location,
        parse_role,
        short_org,
    )


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

WEIGHT_TOLERANCE = 0.02          # 2 % relative difference is accepted
WEIGHT_ABS_TOLERANCE_KG = 0.5    # ... and so is up to 0.5 kg absolute
WEIGHT_HIGH_SEVERITY = 0.10      # > 10 % difference is HIGH
VOLUME_TOLERANCE = 0.05          # 5 % relative difference is accepted
VOLUME_HIGH_SEVERITY = 0.20
NAME_SIMILARITY_THRESHOLD = 0.88  # normalised names at or above this match

MEASUREMENT_UNIT_BASE = "https://onerecord.iata.org/ns/code-lists/MeasurementUnitCode#"
SPECIAL_HANDLING_BASE = "https://onerecord.iata.org/ns/code-lists/SpecialHandlingCode#"
DANGEROUS_GOODS_BASE = "https://onerecord.iata.org/ns/code-lists/DangerousGoodsCode#"

# Conversion factors to kilograms / cubic metres (MeasurementUnitCode).
WEIGHT_UNITS_TO_KG = {
    "KGM": 1.0,
    "GRM": 0.001,
    "TNE": 1000.0,
    "LBR": 0.45359237,
    "ONZ": 0.028349523125,
}
VOLUME_UNITS_TO_M3 = {
    "MTQ": 1.0,
    "CMQ": 1e-6,
    "LTR": 0.001,
    "FTQ": 0.028316846592,
    "INQ": 0.000016387064,
}

# ONE Record DangerousGoodsCode list (2026-07 code-list ontology).
DANGEROUS_GOODS_CODES = {
    "CAO", "EBI", "EBM", "ELI", "ELM", "ICE", "MAG", "RBI", "RBM", "RCL",
    "RCM", "RCX", "REX", "RFG", "RFL", "RFS", "RFW", "RGX", "RIS", "RLI",
    "RLM", "RMD", "RNG", "ROP", "ROX", "RPB", "RPG", "RRW", "RRY", "RSB",
    "RSC", "RXB", "RXC", "RXD", "RXE", "RXG", "RXS",
}

REPORT_TYPES = ("cargo:Shipment", CARGO_NS + "Shipment")

# Conflict types produced by this module.
WEIGHT_MISMATCH = "WEIGHT_MISMATCH"
UNIT_MISMATCH = "UNIT_MISMATCH"
VOLUME_MISMATCH = "VOLUME_MISMATCH"
PIECE_COUNT_MISMATCH = "PIECE_COUNT_MISMATCH"
PARTY_MISMATCH = "PARTY_MISMATCH"
ROUTE_MISMATCH = "ROUTE_MISMATCH"
SPECIAL_HANDLING_MISMATCH = "SPECIAL_HANDLING_MISMATCH"
REFERENCE_MISMATCH = "REFERENCE_MISMATCH"

_LEGAL_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "llc", "llp", "inc", "incorporated",
    "co", "company", "corp", "corporation", "corpn", "gmbh", "plc", "sa",
    "ag", "bv", "nv", "spa", "srl", "pte", "pty", "ms",
}
_ABBREVIATIONS = {
    "intl": "international",
    "int'l": "international",
    "mfg": "manufacturing",
    "mfrs": "manufacturers",
    "bros": "brothers",
    "svcs": "services",
    "svc": "services",
    "exp": "exports",
    "imp": "imports",
    "ind": "industries",
    "inds": "industries",
    "tech": "technologies",
    "logistic": "logistics",
}


# --------------------------------------------------------------------------- #
# Parsed structures
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str
    normalised: float  # kg or m3

    def display(self) -> str:
        return f"{self.value:g} {self.unit}"


@dataclass(frozen=True)
class PartyInfo:
    role: str
    org_id: str | None
    name: str | None


@dataclass(frozen=True)
class ParsedReport:
    report_id: str
    shipment_id: str
    recording_org: str
    reporter_role: str | None
    waybill: str | None = None
    weight: Quantity | None = None
    volume: Quantity | None = None
    pieces: int | None = None
    origin: str | None = None
    destination: str | None = None
    special_handling: tuple[str, ...] | None = None
    parties: dict = field(default_factory=dict)  # role -> PartyInfo

    def to_dict(self) -> dict:
        data = asdict(self)
        data["parties"] = {
            role: asdict(info) for role, info in self.parties.items()
        }
        if self.special_handling is not None:
            data["special_handling"] = list(self.special_handling)
        return data

    @staticmethod
    def from_dict(data: dict) -> "ParsedReport":
        def quantity(value):
            return None if value is None else Quantity(**value)

        shc = data.get("special_handling")
        return ParsedReport(
            report_id=data["report_id"],
            shipment_id=data["shipment_id"],
            recording_org=data["recording_org"],
            reporter_role=data.get("reporter_role"),
            waybill=data.get("waybill"),
            weight=quantity(data.get("weight")),
            volume=quantity(data.get("volume")),
            pieces=data.get("pieces"),
            origin=data.get("origin"),
            destination=data.get("destination"),
            special_handling=None if shc is None else tuple(shc),
            parties={
                role: PartyInfo(**info)
                for role, info in (data.get("parties") or {}).items()
            },
        )


# --------------------------------------------------------------------------- #
# Parsing (validation errors -> HTTP 422, never a conflict)
# --------------------------------------------------------------------------- #

def is_report_type(object_type) -> bool:
    types = object_type if isinstance(object_type, list) else [object_type]
    return any(t in REPORT_TYPES for t in types)


def _code_from(value, field_name: str, base: str | None = None) -> str:
    raw = _reference(value, field_name)
    if "#" in raw:
        if base is not None and not raw.startswith(base):
            raise EventValidationError(f"{field_name} uses an unknown code list")
        raw = raw.split("#", 1)[1]
    return raw.strip().upper()


def parse_quantity(value, field_name: str, units: dict) -> Quantity | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise EventValidationError(
            f"{field_name} must be a Value with cargo:numericalValue and cargo:unit"
        )
    number = _literal(value.get("cargo:numericalValue"))
    if isinstance(number, str):
        try:
            number = float(number)
        except ValueError:
            number = None
    if isinstance(number, bool) or not isinstance(number, (int, float)):
        raise EventValidationError(f"{field_name} needs a numeric cargo:numericalValue")
    if number <= 0:
        raise EventValidationError(f"{field_name} must be greater than zero")
    if value.get("cargo:unit") is None:
        raise EventValidationError(f"{field_name} is missing cargo:unit")
    unit = _code_from(value.get("cargo:unit"), f"{field_name} unit", MEASUREMENT_UNIT_BASE)
    if unit not in units:
        raise EventValidationError(
            f"{field_name} unit '{unit}' is not supported here "
            f"(expected one of {', '.join(sorted(units))})"
        )
    number = float(number)
    return Quantity(value=number, unit=unit, normalised=number * units[unit])


def parse_pieces(value) -> int | None:
    value = _literal(value)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EventValidationError("cargo:slac must be a positive integer")
    return value


def parse_waybill(value) -> str | None:
    value = _literal(value)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EventValidationError("cargo:waybillNumber must be a non-empty string")
    return re.sub(r"[\s\-]", "", value).upper()


def parse_special_handling(value) -> tuple[str, ...] | None:
    if value is None:
        return None
    items = value if isinstance(value, list) else [value]
    codes = []
    for item in items:
        raw = _reference(item, "cargo:specialHandlingCodes")
        if "#" in raw:
            if not (raw.startswith(SPECIAL_HANDLING_BASE)
                    or raw.startswith(DANGEROUS_GOODS_BASE)):
                raise EventValidationError(
                    "cargo:specialHandlingCodes uses an unknown code list"
                )
            raw = raw.split("#", 1)[1]
        code = raw.strip().upper()
        if not re.match(r"^[A-Z]{3}$", code):
            raise EventValidationError(
                f"special handling code '{code}' must be three letters"
            )
        codes.append(code)
    return tuple(sorted(set(codes)))


def parse_parties(value) -> dict:
    if value is None:
        return {}
    items = value if isinstance(value, list) else [value]
    parties = {}
    for item in items:
        if not isinstance(item, dict):
            raise EventValidationError("cargo:involvedParties entries must be objects")
        role = parse_role(item.get("cargo:partyRole"), "cargo:partyRole")
        if role is None:
            raise EventValidationError("cargo:involvedParties entry needs cargo:partyRole")
        details = item.get("cargo:partyDetails") or {}
        if isinstance(details, str):
            details = {"@id": details}
        org_id = details.get("@id")
        if org_id is not None and not _is_absolute_uri(org_id):
            raise EventValidationError("party @id must be an absolute HTTP or HTTPS URI")
        name = _literal(details.get("cargo:name"))
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise EventValidationError("party cargo:name must be a non-empty string")
        if org_id is None and name is None:
            raise EventValidationError("party needs an @id or a cargo:name")
        if role in parties:
            raise EventValidationError(f"party role {role} is listed twice")
        parties[role] = PartyInfo(role=role, org_id=org_id, name=name and name.strip())
    return parties


def parse_report(document: dict) -> ParsedReport:
    if not is_report_type(document.get("@type")):
        raise EventValidationError("@type must be cargo:Shipment for a party report")

    shipment_id = _reference(document.get("acp:reportFor"), "acp:reportFor")
    recording_org = _reference(
        document.get("acp:recordingOrganization"), "acp:recordingOrganization"
    )
    if not _is_absolute_uri(recording_org):
        raise EventValidationError(
            "acp:recordingOrganization must be an absolute HTTP or HTTPS URI"
        )
    dimensions = document.get("cargo:totalDimensions") or {}
    if not isinstance(dimensions, dict):
        raise EventValidationError("cargo:totalDimensions must be an object")

    return ParsedReport(
        report_id=document["@id"],
        shipment_id=shipment_id,
        recording_org=recording_org,
        reporter_role=parse_role(document.get("acp:reporterRole")),
        waybill=parse_waybill(document.get("cargo:waybillNumber")),
        weight=parse_quantity(
            document.get("cargo:totalGrossWeight"),
            "cargo:totalGrossWeight",
            WEIGHT_UNITS_TO_KG,
        ),
        volume=parse_quantity(
            dimensions.get("cargo:volume"), "cargo:volume", VOLUME_UNITS_TO_M3
        ),
        pieces=parse_pieces(document.get("cargo:slac")),
        origin=parse_location(document.get("cargo:departureLocation"), "cargo:departureLocation"),
        destination=parse_location(document.get("cargo:arrivalLocation"), "cargo:arrivalLocation"),
        special_handling=parse_special_handling(document.get("cargo:specialHandlingCodes")),
        parties=parse_parties(document.get("cargo:involvedParties")),
    )


# --------------------------------------------------------------------------- #
# Name normalisation
# --------------------------------------------------------------------------- #

def normalise_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"\bm/s\b", " ", text)
    # Dotted acronyms such as "L.L.C." or "P.T.E." become one word ("llc").
    text = re.sub(r"\b(?:[a-z]\.){2,}", lambda m: m.group(0).replace(".", "") + " ", text)
    text = re.sub(r"[^\w\s']", " ", text)
    tokens = []
    for token in text.split():
        token = token.strip("'")
        token = _ABBREVIATIONS.get(token, token)
        if token and token not in _LEGAL_SUFFIXES:
            tokens.append(token)
    return " ".join(tokens)


def name_similarity(a: str, b: str) -> float:
    na, nb = normalise_name(a), normalise_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


# --------------------------------------------------------------------------- #
# Report-vs-report rules
# --------------------------------------------------------------------------- #

def _relative_difference(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b))


def _quantity_rule(new: Quantity | None, stored: Quantity | None, *,
                   field_name: str, mismatch_type: str, tolerance: float,
                   high: float, abs_tolerance: float = 0.0,
                   label: str, base_unit: str) -> RuleHit | None:
    if new is None or stored is None:
        return None
    rel = _relative_difference(new.normalised, stored.normalised)
    if rel <= tolerance or abs(new.normalised - stored.normalised) <= abs_tolerance:
        return None

    raw_rel = _relative_difference(new.value, stored.value)
    if new.unit != stored.unit and raw_rel <= tolerance:
        return RuleHit(
            UNIT_MISMATCH,
            "HIGH",
            (
                f"Both parties report {label} {new.value:g} vs {stored.value:g} "
                f"but in different units ({new.unit} vs {stored.unit}); "
                f"converted that is {new.normalised:.2f} vs "
                f"{stored.normalised:.2f} {base_unit}. Likely a unit entry error."
            ),
            field=field_name,
            value_a=new.display(),
            value_b=stored.display(),
        )
    return RuleHit(
        mismatch_type,
        "HIGH" if rel > high else "MEDIUM",
        (
            f"{label.capitalize()} differs by {rel * 100:.1f}% after unit "
            f"conversion: {new.normalised:.2f} vs {stored.normalised:.2f} {base_unit} "
            f"(tolerance {tolerance * 100:g}%)."
        ),
        field=field_name,
        value_a=new.display(),
        value_b=stored.display(),
    )


def compare_reports(new: ParsedReport, stored: ParsedReport) -> list[RuleHit]:
    """All attribute conflicts between two reports from DIFFERENT parties."""
    if new.shipment_id != stored.shipment_id:
        return []
    if new.recording_org == stored.recording_org:
        return []

    hits: list[RuleHit] = []

    weight = _quantity_rule(
        new.weight, stored.weight,
        field_name="totalGrossWeight", mismatch_type=WEIGHT_MISMATCH,
        tolerance=WEIGHT_TOLERANCE, high=WEIGHT_HIGH_SEVERITY,
        abs_tolerance=WEIGHT_ABS_TOLERANCE_KG, label="gross weight",
        base_unit="kg",
    )
    if weight:
        hits.append(weight)

    volume = _quantity_rule(
        new.volume, stored.volume,
        field_name="volume", mismatch_type=VOLUME_MISMATCH,
        tolerance=VOLUME_TOLERANCE, high=VOLUME_HIGH_SEVERITY,
        label="volume", base_unit="m3",
    )
    if volume:
        hits.append(volume)

    if new.pieces is not None and stored.pieces is not None and new.pieces != stored.pieces:
        hits.append(RuleHit(
            PIECE_COUNT_MISMATCH,
            "MEDIUM",
            f"Piece count (SLAC) {new.pieces} vs {stored.pieces}.",
            field="slac", value_a=new.pieces, value_b=stored.pieces,
        ))

    for field_name, a, b in (
        ("origin", new.origin, stored.origin),
        ("destination", new.destination, stored.destination),
    ):
        if a is not None and b is not None and a != b:
            hits.append(RuleHit(
                ROUTE_MISMATCH,
                "HIGH",
                f"{field_name.capitalize()} airport {a} vs {b}.",
                field=field_name, value_a=a, value_b=b,
            ))

    if new.special_handling is not None and stored.special_handling is not None:
        a, b = set(new.special_handling), set(stored.special_handling)
        if a != b:
            differing = sorted(a ^ b)
            dangerous = [c for c in differing if c in DANGEROUS_GOODS_CODES]
            hits.append(RuleHit(
                SPECIAL_HANDLING_MISMATCH,
                "HIGH" if dangerous else "MEDIUM",
                (
                    f"Special handling codes differ ({', '.join(differing)})"
                    + (f"; dangerous-goods codes involved: {', '.join(dangerous)}."
                       if dangerous else ".")
                ),
                field="specialHandlingCodes",
                value_a=sorted(a), value_b=sorted(b),
            ))

    if new.waybill is not None and stored.waybill is not None and new.waybill != stored.waybill:
        hits.append(RuleHit(
            REFERENCE_MISMATCH,
            "HIGH",
            f"Waybill number {new.waybill} vs {stored.waybill}.",
            field="waybillNumber", value_a=new.waybill, value_b=stored.waybill,
        ))

    for role in sorted(set(new.parties) & set(stored.parties)):
        hit = _party_rule(new.parties[role], stored.parties[role])
        if hit:
            hits.append(hit)

    return hits


def _party_label(info: PartyInfo) -> str:
    if info.name and info.org_id:
        return f"{info.name} <{info.org_id}>"
    return info.name or info.org_id


def _party_rule(new: PartyInfo, stored: PartyInfo) -> RuleHit | None:
    role_name = PARTICIPANT_ROLES.get(new.role, new.role).lower()
    if new.org_id and stored.org_id:
        if new.org_id == stored.org_id:
            return None
        return RuleHit(
            PARTY_MISMATCH,
            "HIGH",
            f"Different {role_name} organisation identifiers.",
            field=f"party:{new.role}",
            value_a=_party_label(new), value_b=_party_label(stored),
        )
    if new.name and stored.name:
        similarity = name_similarity(new.name, stored.name)
        if similarity >= NAME_SIMILARITY_THRESHOLD:
            return None
        return RuleHit(
            PARTY_MISMATCH,
            "MEDIUM",
            (
                f"Different {role_name} names (similarity {similarity:.2f} "
                f"< {NAME_SIMILARITY_THRESHOLD})."
            ),
            field=f"party:{new.role}",
            value_a=_party_label(new), value_b=_party_label(stored),
        )
    return None


# --------------------------------------------------------------------------- #
# Event-vs-report rule
# --------------------------------------------------------------------------- #

def compare_event_location(event: ParsedEvent, report: ParsedReport) -> RuleHit | None:
    """LOCATION_MISMATCH: an origin milestone (RCS/MAN/DEP) reported away from
    the report's origin, or a destination milestone (ARR/DLV) away from its
    destination. Single-leg shipments only."""
    if event.shipment_id != report.shipment_id:
        return None
    if not event.comparable or event.location is None:
        return None
    if event.milestone_rank in ORIGIN_MILESTONES:
        expected, which = report.origin, "origin"
    elif event.milestone_rank in DESTINATION_MILESTONES:
        expected, which = report.destination, "destination"
    else:
        return None
    if expected is None or expected == event.location:
        return None
    return RuleHit(
        LOCATION_MISMATCH,
        "HIGH",
        (
            f"{event.milestone} reported at {event.location} by "
            f"{short_org(event.recording_org)}, but {short_org(report.recording_org)} "
            f"gives the {which} as {expected}."
        ),
        field=f"eventLocation/{which}",
        value_a=event.location,
        value_b=expected,
    )
