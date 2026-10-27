"""Catalogue of conflict types: one place for labels, categories and the
rule each type implements. Used by the API, the dashboard and the evaluation."""

CONFLICT_TYPES = {
    "DUPLICATE_IDENTIFIER": {
        "label": "Duplicate identifier",
        "category": "IDENTITY",
        "rule": "R1",
        "meaning": "The same @id was received again. Identical content is a replay "
                   "(LOW); different content breaks event immutability (HIGH).",
    },
    "STATUS_MISMATCH": {
        "label": "Timestamp mismatch",
        "category": "EVENT",
        "rule": "R2",
        "meaning": "Two parties report the same milestone (e.g. DEP) at times more "
                   "than 30 minutes apart.",
    },
    "INVALID_TRANSITION": {
        "label": "Impossible sequence",
        "category": "EVENT",
        "rule": "R3",
        "meaning": "Event times contradict the milestone order, e.g. departed after "
                   "delivered (beyond 5 minutes of clock skew).",
    },
    "LOCATION_MISMATCH": {
        "label": "Location mismatch",
        "category": "EVENT",
        "rule": "R4",
        "meaning": "An origin milestone (RCS/MAN/DEP) or destination milestone "
                   "(ARR/DLV) is reported at an airport other than the route in a "
                   "party's shipment report.",
    },
    "WEIGHT_MISMATCH": {
        "label": "Weight mismatch",
        "category": "ATTRIBUTE",
        "rule": "R5",
        "meaning": "Gross weights differ by more than 2% (and 0.5 kg) after unit "
                   "conversion. HIGH above 10%.",
    },
    "UNIT_MISMATCH": {
        "label": "Unit mismatch",
        "category": "ATTRIBUTE",
        "rule": "R6",
        "meaning": "Same number, different unit (e.g. 1250 KGM vs 1250 LBR): the "
                   "converted values disagree although the figures match, which "
                   "indicates a unit entry error.",
    },
    "VOLUME_MISMATCH": {
        "label": "Volume mismatch",
        "category": "ATTRIBUTE",
        "rule": "R7",
        "meaning": "Volumes differ by more than 5% after unit conversion.",
    },
    "PIECE_COUNT_MISMATCH": {
        "label": "Piece count mismatch",
        "category": "ATTRIBUTE",
        "rule": "R8",
        "meaning": "Shipper's load and count (SLAC) differs between parties.",
    },
    "PARTY_MISMATCH": {
        "label": "Party info mismatch",
        "category": "ATTRIBUTE",
        "rule": "R9",
        "meaning": "Shipper/consignee/etc. differ: different organisation ids (HIGH) "
                   "or names whose normalised similarity is below 0.88 (MEDIUM).",
    },
    "ROUTE_MISMATCH": {
        "label": "Route mismatch",
        "category": "ATTRIBUTE",
        "rule": "R10",
        "meaning": "Origin or destination airport differs between parties.",
    },
    "SPECIAL_HANDLING_MISMATCH": {
        "label": "Special handling mismatch",
        "category": "ATTRIBUTE",
        "rule": "R11",
        "meaning": "Special handling codes differ. HIGH when a dangerous-goods code "
                   "is involved.",
    },
    "REFERENCE_MISMATCH": {
        "label": "Waybill mismatch",
        "category": "ATTRIBUTE",
        "rule": "R12",
        "meaning": "Air waybill numbers differ (after removing spaces and dashes).",
    },
}

SEVERITIES = ("HIGH", "MEDIUM", "LOW")


def label(conflict_type: str) -> str:
    return CONFLICT_TYPES.get(conflict_type, {}).get("label", conflict_type)


def category(conflict_type: str) -> str:
    return CONFLICT_TYPES.get(conflict_type, {}).get("category", "OTHER")
