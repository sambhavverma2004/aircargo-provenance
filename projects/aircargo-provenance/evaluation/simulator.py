"""Synthetic multi-party air cargo data with labelled, injected conflicts.

Each shipment is reported by three parties, as in a real single-leg flow:

  FFW  freight forwarder  - booking (BKD event) and a booking report
  AIR  airline            - acceptance report, MAN / DEP / ARR events
  GHA  ground handler     - warehouse report, RCS / DEP / DLV events

Clean shipments still contain realistic *hard negatives* that must NOT be
flagged: weights in pounds vs kilograms, re-weighing noise inside tolerance,
company-name variants, waybill formatting, reordered handling codes, and
departure times reported a few minutes apart.

Conflicted shipments get one (sometimes two) injected faults of a known
type. The labels say which records were corrupted and which conflict types
are an acceptable consequence, so the evaluation can measure precision,
recall and classification without reusing the detection rules.

A small share of records is deliberately invalid (missing unit, unknown
status code, no timezone...) to measure the input filter.

Usage
  python -m evaluation.simulator --shipments 200 --out evaluation/data/dataset.json
  python -m evaluation.simulator --live --url http://127.0.0.1:8000 --shipments 30 --rate 5
"""

import argparse
import copy
import json
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

CARGO = "https://onerecord.iata.org/ns/cargo#"
CODES = "https://onerecord.iata.org/ns/code-lists/"
CONTEXT = {"cargo": CARGO}

# --------------------------------------------------------------------------- #
# Fictional reference data
# --------------------------------------------------------------------------- #

ROUTES = [  # origin, destination, block hours
    ("BOM", "FRA", 9.0), ("DEL", "LHR", 9.5), ("BLR", "SIN", 4.5),
    ("MAA", "DXB", 4.0), ("HYD", "DOH", 4.0), ("CCU", "HKG", 4.0),
    ("AMD", "DXB", 3.2), ("COK", "AUH", 4.2), ("BOM", "JFK", 16.0),
    ("DEL", "ORD", 15.0), ("BLR", "AMS", 10.0), ("DEL", "CDG", 9.0),
]
AIRPORTS = sorted({a for r in ROUTES for a in r[:2]} | {"MUC", "ZRH", "IST", "NRT", "SYD"})

AIRLINES = [
    ("https://skybridge-cargo.example/org", "SkyBridge Cargo Airlines", "176"),
    ("https://meridian-air.example/org", "Meridian Air Freight", "618"),
    ("https://monsoon-airlines.example/org", "Monsoon Airlines Cargo", "098"),
]
FORWARDERS = [
    ("https://swiftline-logistics.example/org", "Swiftline Logistics Pvt Ltd"),
    ("https://orbit-forwarding.example/org", "Orbit Forwarding LLP"),
    ("https://harbourgate.example/org", "HarbourGate Freight Services"),
]
HANDLERS = [
    ("https://terminalone-gha.example/org", "TerminalOne Ground Handling"),
    ("https://cargodock-services.example/org", "CargoDock Services Ltd"),
]

# Companies with name variants that refer to the SAME organisation.
COMPANIES = [
    ("https://tata-exports.example/org",
     ["Tata Exports Private Limited", "TATA EXPORTS PVT. LTD.", "Tata Exports Pvt Ltd"]),
    ("https://global-intl.example/org",
     ["Global International Logistics LLP", "Global Intl Logistics", "M/s Global International Logistics"]),
    ("https://sharma-sons.example/org",
     ["Sharma & Sons Co.", "Sharma and Sons", "SHARMA & SONS"]),
    ("https://muller-pharma.example/org",
     ["Müller Pharma GmbH", "Muller Pharma", "MÜLLER PHARMA GMBH"]),
    ("https://kaveri-textiles.example/org",
     ["Kaveri Textiles Ltd", "Kaveri Textiles Limited", "KAVERI TEXTILES"]),
    ("https://nordic-fresh.example/org",
     ["Nordic Fresh Foods AB", "Nordic Fresh Foods", "NORDIC FRESH FOODS"]),
    ("https://pacific-electro.example/org",
     ["Pacific Electronics Pte Ltd", "Pacific Electronics", "PACIFIC ELECTRONICS PTE. LTD."]),
    ("https://desert-rose.example/org",
     ["Desert Rose Trading LLC", "Desert Rose Trading", "DESERT ROSE TRADING L.L.C."]),
]
# Variants a simple normaliser may miss (same company, different wording).
HARD_VARIANTS = [
    ("Shree Ganesh Traders", "Shree Ganesh Trading Co"),
    ("Bharat Heavy Engg Works", "Bharat Heavy Engineering Works"),
    ("Coastal Agro Exports", "Coastal Agro Export House"),
]

CARGO_PROFILES = [  # special handling codes, density kg/m3
    ([], 220), (["PER", "COL"], 300), (["PIL", "COL"], 180),
    (["ELI"], 250), (["RFL", "CAO"], 400), (["AVI"], 120), (["HEA", "BIG"], 500),
]
DANGEROUS = {"CAO", "ELI", "ELM", "ICE", "RCL", "RFL", "RCX", "RRY", "RMD", "RPB"}

INJECTION_TYPES = [
    "STATUS_MISMATCH", "INVALID_TRANSITION", "LOCATION_MISMATCH",
    "WEIGHT_MISMATCH", "UNIT_MISMATCH", "VOLUME_MISMATCH",
    "PIECE_COUNT_MISMATCH", "PARTY_MISMATCH", "ROUTE_MISMATCH",
    "SPECIAL_HANDLING_MISMATCH", "REFERENCE_MISMATCH", "DUPLICATE_IDENTIFIER",
]
# Conflict types that are a correct, logical consequence of each fault.
ACCEPTABLE = {t: {t} for t in INJECTION_TYPES}
ACCEPTABLE["ROUTE_MISMATCH"] = {"ROUTE_MISMATCH", "LOCATION_MISMATCH"}

INVALID_KINDS = [
    "missing_unit", "unknown_status_code", "no_timezone", "relative_id",
    "missing_context", "bad_location", "negative_weight", "event_for_other_shipment",
]


@dataclass
class Record:
    seq: int
    kind: str                 # shipment | event | report
    shipment_id: str
    path: str
    body: dict
    record_id: str
    expected_status: int = 201
    injection_ids: list = field(default_factory=list)
    invalid_kind: str | None = None


@dataclass
class Injection:
    injection_id: str
    type: str
    shipment_id: str
    corrupted_records: list
    acceptable_types: list
    note: str


# --------------------------------------------------------------------------- #
# Builders (ONE Record-style JSON-LD)
# --------------------------------------------------------------------------- #

def value(number, unit):
    return {"cargo:numericalValue": number,
            "cargo:unit": {"@id": CODES + "MeasurementUnitCode#" + unit}}


def location(code):
    return {"cargo:locationCodes": [{"cargo:code": code}]}


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def awb_number(prefix, rng):
    serial = rng.randrange(1000000, 9999999)
    return f"{prefix}-{serial}{serial % 7}"


def shc_ref(code):
    lst = "DangerousGoodsCode" if code in DANGEROUS else "SpecialHandlingCode"
    return {"@id": f"{CODES}{lst}#{code}"}


class ShipmentBuilder:
    def __init__(self, index, rng, base_time):
        self.rng = rng
        self.index = index
        self.id = f"https://aggregator.example/logistics-objects/shp-{index:05d}"
        self.origin, self.destination, self.hours = rng.choice(ROUTES)
        self.airline = rng.choice(AIRLINES)
        self.forwarder = rng.choice(FORWARDERS)
        self.handler = rng.choice(HANDLERS)
        shipper = rng.choice(COMPANIES)
        consignee = rng.choice([c for c in COMPANIES if c is not shipper])
        self.shipper, self.consignee = shipper, consignee
        self.profile, density = rng.choice(CARGO_PROFILES)
        self.weight = round(rng.uniform(40, 4500), 1)
        self.volume = round(self.weight / density * rng.uniform(0.9, 1.1), 2)
        self.pieces = rng.randint(1, 40)
        self.awb = awb_number(self.airline[2], rng)
        self.t_rcs = base_time
        self.t_man = self.t_rcs + timedelta(hours=rng.uniform(2, 4))
        self.t_dep = self.t_man + timedelta(hours=rng.uniform(1.5, 3))
        self.t_arr = self.t_dep + timedelta(hours=self.hours + rng.uniform(-0.25, 0.25))
        self.t_dlv = self.t_arr + timedelta(hours=rng.uniform(2, 10))
        self.t_bkd = self.t_rcs - timedelta(hours=rng.uniform(24, 72))
        self.n = 0

    def new_id(self, owner, kind):
        self.n += 1
        host = owner.split("/")[2]
        return f"https://{host}/{kind}/{self.index:05d}-{self.n}"

    def shipment(self):
        return {"@context": CONTEXT, "@id": self.id, "@type": "cargo:Shipment",
                "cargo:waybillNumber": self.awb}

    def event(self, org, role, name, code, when, loc=None):
        doc = {
            "@context": CONTEXT,
            "@id": self.new_id(org, "events"),
            "@type": "cargo:LogisticsEvent",
            "cargo:eventFor": {"@id": self.id},
            "cargo:eventCode": {"@id": CODES + "StatusCode#" + code},
            "cargo:eventDate": {"@type": "http://www.w3.org/2001/XMLSchema#dateTime",
                                "@value": iso(when)},
            "cargo:eventTimeType": {"@id": "cargo:ACTUAL"},
            "cargo:recordingOrganization": {"@id": org},
            "acp:reporterRole": role,
            "acp:organizationName": name,
        }
        if loc:
            doc["cargo:eventLocation"] = location(loc)
        return doc

    def report(self, org, role, name, *, weight, weight_unit, volume, volume_unit,
               awb, shc, shipper_name, consignee_name, pieces=None, origin=None,
               destination=None, ids=True):
        parties = []
        for code, (org_id, _), party_name in (("SHP", self.shipper, shipper_name),
                                              ("CNE", self.consignee, consignee_name)):
            details = {"cargo:name": party_name}
            if ids:
                details["@id"] = org_id
            parties.append({"cargo:partyRole": {"@id": CODES + "ParticipantIdentifier#" + code},
                            "cargo:partyDetails": details})
        return {
            "@context": CONTEXT,
            "@id": self.new_id(org, "reports"),
            "@type": "cargo:Shipment",
            "acp:reportFor": {"@id": self.id},
            "acp:recordingOrganization": {"@id": org},
            "acp:reporterRole": role,
            "acp:organizationName": name,
            "cargo:waybillNumber": awb,
            "cargo:totalGrossWeight": value(weight, weight_unit),
            "cargo:totalDimensions": {"cargo:volume": value(volume, volume_unit)},
            "cargo:slac": pieces if pieces is not None else self.pieces,
            "cargo:departureLocation": location(origin or self.origin),
            "cargo:arrivalLocation": location(destination or self.destination),
            "cargo:specialHandlingCodes": [shc_ref(c) for c in shc],
            "cargo:involvedParties": parties,
        }


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

def generate(shipments=200, seed=7, conflict_rate=0.6, double_rate=0.1,
             invalid_rate=0.03, hard_name_rate=0.05, start=None):
    rng = random.Random(seed)
    start = start or datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)
    per_shipment = []
    injections = []
    shipment_labels = {}

    for index in range(shipments):
        b = ShipmentBuilder(index, rng, start + timedelta(minutes=rng.randint(0, 60 * 24 * 25)))
        records = []
        ffw_org, ffw_name = b.forwarder
        air_org, air_name, _ = b.airline
        gha_org, gha_name = b.handler

        # ---- clean data with hard negatives ----
        use_ids = rng.random() < 0.5
        variants_s = list(b.shipper[1])
        variants_c = list(b.consignee[1])
        rng.shuffle(variants_s)
        rng.shuffle(variants_c)
        hard_name = rng.random() < hard_name_rate
        if hard_name:
            pair = rng.choice(HARD_VARIANTS)
            variants_c = [pair[0], pair[1], pair[0]]
            use_ids = False
        shc = list(b.profile)
        awb_plain = b.awb.replace("-", "")
        awb_spaced = f"{b.awb[:3]} {b.awb[4:8]} {b.awb[8:]}"

        air_weight = round(b.weight * rng.uniform(0.988, 1.012), 1)   # re-weigh noise
        gha_weight = round(b.weight * rng.uniform(0.992, 1.008), 1)
        gha_in_pounds = rng.random() < 0.35
        gha_volume_ft3 = rng.random() < 0.3

        reports = {
            "FFW": dict(org=ffw_org, role="FFW", name=ffw_name, weight=b.weight,
                        weight_unit="KGM", volume=b.volume, volume_unit="MTQ",
                        awb=b.awb, shc=shc, shipper_name=variants_s[0],
                        consignee_name=variants_c[0], ids=use_ids),
            "AIR": dict(org=air_org, role="AIR", name=air_name, weight=air_weight,
                        weight_unit="KGM", volume=round(b.volume * rng.uniform(0.98, 1.02), 2),
                        volume_unit="MTQ", awb=awb_plain, shc=rng.sample(shc, len(shc)),
                        shipper_name=variants_s[1], consignee_name=variants_c[1], ids=use_ids),
            "GHA": dict(org=gha_org, role="GHA", name=gha_name,
                        weight=round(gha_weight / 0.45359237, 1) if gha_in_pounds else gha_weight,
                        weight_unit="LBR" if gha_in_pounds else "KGM",
                        volume=round(b.volume / 0.028316846592, 1) if gha_volume_ft3 else b.volume,
                        volume_unit="FTQ" if gha_volume_ft3 else "MTQ",
                        awb=awb_spaced, shc=list(reversed(shc)), shipper_name=variants_s[2],
                        consignee_name=variants_c[2], ids=use_ids),
        }
        dep_gha_offset = timedelta(minutes=rng.uniform(-20, 20))
        events = {
            "BKD": (ffw_org, "FFW", ffw_name, "BKD", b.t_bkd, None),
            "RCS": (gha_org, "GHA", gha_name, "RCS", b.t_rcs, b.origin),
            "MAN": (air_org, "AIR", air_name, "MAN", b.t_man, b.origin),
            "DEP_AIR": (air_org, "AIR", air_name, "DEP", b.t_dep, b.origin),
            "DEP_GHA": (gha_org, "GHA", gha_name, "DEP", b.t_dep + dep_gha_offset, b.origin),
            "ARR": (air_org, "AIR", air_name, "ARR", b.t_arr, b.destination),
            "DLV": (gha_org, "GHA", gha_name, "DLV", b.t_dlv, b.destination),
        }

        # ---- injections ----
        injected = []
        if rng.random() < conflict_rate:
            first = rng.choice(INJECTION_TYPES)
            injected.append(first)
            # A second, independent attribute fault on some shipments. Event
            # and route faults are kept single so their effects stay separable.
            if rng.random() < double_rate and first not in ("ROUTE_MISMATCH", "DUPLICATE_IDENTIFIER"):
                second = rng.choice([t for t in INJECTION_TYPES
                                     if t not in (first, "DUPLICATE_IDENTIFIER", "ROUTE_MISMATCH",
                                                  "LOCATION_MISMATCH", "STATUS_MISMATCH",
                                                  "INVALID_TRANSITION")])
                injected.append(second)

        pending = []  # (injection type, key of corrupted record, note)
        replay = None
        for kind in injected:
            if kind == "STATUS_MISMATCH":
                max_shift = min(3.0, b.hours - 0.75)
                shift = timedelta(hours=rng.uniform(0.75, max_shift))
                org, role, name, code, when, loc = events["DEP_GHA"]
                events["DEP_GHA"] = (org, role, name, code, b.t_dep + shift, loc)
                pending.append((kind, "event:DEP_GHA", f"GHA departure +{shift}"))
            elif kind == "INVALID_TRANSITION":
                if rng.random() < 0.5:
                    org, role, name, code, when, loc = events["DLV"]
                    when = b.t_arr - timedelta(minutes=rng.uniform(30, 90))
                    events["DLV"] = (org, role, name, code, when, loc)
                    pending.append((kind, "event:DLV", "delivered before arrival"))
                else:
                    org, role, name, code, when, loc = events["RCS"]
                    when = b.t_dep + timedelta(minutes=rng.uniform(40, 90))
                    events["RCS"] = (org, role, name, code, when, loc)
                    pending.append((kind, "event:RCS", "received after departure"))
            elif kind == "LOCATION_MISMATCH":
                org, role, name, code, when, loc = events["DEP_AIR"]
                wrong = rng.choice([a for a in AIRPORTS if a not in (b.origin, b.destination)])
                events["DEP_AIR"] = (org, role, name, code, when, wrong)
                pending.append((kind, "event:DEP_AIR", f"departure at {wrong}"))
            elif kind == "WEIGHT_MISMATCH":
                factor = 1 + rng.choice([-1, 1]) * rng.uniform(0.04, 0.25)
                reports["AIR"]["weight"] = round(b.weight * factor, 1)
                pending.append((kind, "report:AIR", f"airline weight x{factor:.3f}"))
            elif kind == "UNIT_MISMATCH":
                r = reports["GHA"]
                r["weight"] = gha_weight  # the kilogram figure ...
                r["weight_unit"] = "LBR"  # ... entered as pounds
                pending.append((kind, "report:GHA", "kg figure entered as LBR"))
            elif kind == "VOLUME_MISMATCH":
                factor = 1 + rng.uniform(0.10, 0.40)
                reports["AIR"]["volume"] = round(b.volume * factor, 2)
                reports["AIR"]["volume_unit"] = "MTQ"
                pending.append((kind, "report:AIR", f"airline volume x{factor:.2f}"))
            elif kind == "PIECE_COUNT_MISMATCH":
                delta = rng.choice([-3, -2, -1, 1, 2, 3])
                if b.pieces + delta < 1:
                    delta = abs(delta)
                reports["GHA"]["pieces"] = b.pieces + delta
                pending.append((kind, "report:GHA", f"pieces {delta:+d}"))
            elif kind == "PARTY_MISMATCH":
                other = rng.choice([c for c in COMPANIES if c not in (b.shipper, b.consignee)])
                reports["AIR"]["consignee_name"] = other[1][0]
                reports["AIR"]["consignee_override_id"] = other[0]
                pending.append((kind, "report:AIR", f"consignee {other[1][0]}"))
            elif kind == "ROUTE_MISMATCH":
                wrong = rng.choice([a for a in AIRPORTS if a not in (b.origin, b.destination)])
                reports["AIR"]["destination"] = wrong
                pending.append((kind, "report:AIR", f"destination {wrong}"))
            elif kind == "SPECIAL_HANDLING_MISMATCH":
                if shc and rng.random() < 0.6:
                    dropped = rng.choice(shc)
                    reports["AIR"]["shc"] = [c for c in shc if c != dropped]
                    pending.append((kind, "report:AIR", f"dropped {dropped}"))
                else:
                    extra = rng.choice([c for c in ("RCL", "PER", "COL", "ELI") if c not in shc])
                    reports["AIR"]["shc"] = shc + [extra]
                    pending.append((kind, "report:AIR", f"added {extra}"))
            elif kind == "REFERENCE_MISMATCH":
                digits = list(awb_spaced)
                position = rng.choice([i for i, ch in enumerate(digits) if ch.isdigit() and i > 3])
                digits[position] = str((int(digits[position]) + rng.randint(1, 9)) % 10)
                reports["GHA"]["awb"] = "".join(digits)
                pending.append((kind, "report:GHA", "waybill digit changed"))
            elif kind == "DUPLICATE_IDENTIFIER":
                replay = rng.choice(["DEP_AIR", "MAN", "RCS", "report:FFW"])
                pending.append((kind, f"replay:{replay}", "record re-sent"))

        # ---- materialise ----
        built = {}
        built["shipment"] = ("shipment", "/logistics-objects", b.shipment())
        for key, (org, role, name, code, when, loc) in events.items():
            doc = b.event(org, role, name, code, when, loc)
            built[f"event:{key}"] = ("event", f"/logistics-objects/{b.id}/logistics-events", doc)
        for role, spec in reports.items():
            spec = dict(spec)
            override = spec.pop("consignee_override_id", None)
            doc = b.report(**spec)
            if override:
                for party in doc["cargo:involvedParties"]:
                    if party["cargo:partyRole"]["@id"].endswith("CNE"):
                        if "@id" in party["cargo:partyDetails"]:
                            party["cargo:partyDetails"]["@id"] = override
            built[f"report:{role}"] = ("report", f"/logistics-objects/{b.id}/party-reports", doc)

        extra_records = []
        if replay:
            source_key = replay if replay.startswith("report:") else f"event:{replay}"
            kind_, path_, doc_ = built[source_key]
            duplicate = copy.deepcopy(doc_)
            if rng.random() < 0.5:  # same id, changed content -> HIGH
                if kind_ == "event":
                    old = datetime.fromisoformat(duplicate["cargo:eventDate"]["@value"].replace("Z", "+00:00"))
                    duplicate["cargo:eventDate"]["@value"] = iso(old + timedelta(minutes=7))
                else:
                    duplicate["cargo:slac"] = duplicate["cargo:slac"] + 1
            extra_records.append((f"replay:{replay}", kind_, path_, duplicate, 409))

        shipment_injections = []
        for kind, key, note in pending:
            injection_id = f"inj-{index:05d}-{len(injections)}"
            if key.startswith("replay:"):
                corrupted = [extra_records[0][3]["@id"]]
            else:
                corrupted = [built[key][2]["@id"]]
            injections.append(Injection(injection_id, kind, b.id, corrupted,
                                        sorted(ACCEPTABLE[kind]), note))
            shipment_injections.append(injection_id)

        ordered = [("shipment", *built["shipment"], 201)]
        rest = [(key, *value, 201) for key, value in built.items() if key != "shipment"]
        rest += extra_records
        rng.shuffle(rest)
        # A replay must come after the original record.
        if extra_records:
            original_key = extra_records[0][0].replace("replay:", "")
            original_key = original_key if original_key.startswith("report:") else f"event:{original_key}"
            rest = [r for r in rest if r[0] != extra_records[0][0]]
            pos = next(i for i, r in enumerate(rest) if r[0] == original_key)
            rest.insert(rng.randint(pos + 1, len(rest)), extra_records[0])
        ordered += rest

        # ---- invalid records for the filter ----
        if rng.random() < invalid_rate * 10:
            for _ in range(rng.randint(1, 2)):
                if rng.random() < 0.5:
                    ordered.insert(rng.randint(1, len(ordered)), invalid_record(b, rng))

        label_injections = {}
        for kind, key, _ in pending:
            label_injections.setdefault(key, []).append(kind)
        records = []
        for key, kind, path, body, expected in ordered:
            injection_ids = [inj for inj in shipment_injections
                             if body.get("@id") in next(i for i in injections if i.injection_id == inj).corrupted_records]
            records.append(dict(kind=kind, shipment_id=b.id, path=path, body=body,
                                record_id=body.get("@id"), expected_status=expected,
                                injection_ids=injection_ids,
                                invalid_kind=key if key.startswith("invalid:") else None))
        per_shipment.append(records)
        shipment_labels[b.id] = {
            "clean": not pending,
            "injections": shipment_injections,
            "hard_name_variant": hard_name,
            "route": f"{b.origin}-{b.destination}",
        }

    # Interleave shipments as a stream: records of different shipments mix.
    stream = []
    cursors = [0] * len(per_shipment)
    active = list(range(len(per_shipment)))
    while active:
        i = rng.choice(active[: max(1, min(len(active), 8))])
        stream.append(per_shipment[i][cursors[i]])
        cursors[i] += 1
        if cursors[i] >= len(per_shipment[i]):
            active.remove(i)
    records = [Record(seq=n, **r) for n, r in enumerate(stream)]

    return {
        "meta": {
            "generator": "evaluation/simulator.py",
            "seed": seed, "shipments": shipments, "conflict_rate": conflict_rate,
            "double_rate": double_rate, "invalid_rate": invalid_rate,
            "hard_name_rate": hard_name_rate, "records": len(records),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
        "records": [asdict(r) for r in records],
        "injections": [asdict(i) for i in injections],
        "shipments": shipment_labels,
    }


def invalid_record(b: ShipmentBuilder, rng):
    kind = rng.choice(INVALID_KINDS)
    air_org, air_name, _ = b.airline
    doc = b.event(air_org, "AIR", air_name, "DEP", b.t_dep, b.origin)
    path = f"/logistics-objects/{b.id}/logistics-events"
    record_kind = "event"
    if kind == "missing_unit":
        doc = b.report(air_org, "AIR", air_name, weight=b.weight, weight_unit="KGM",
                       volume=b.volume, volume_unit="MTQ", awb=b.awb, shc=[],
                       shipper_name="x", consignee_name="y")
        del doc["cargo:totalGrossWeight"]["cargo:unit"]
        path, record_kind = f"/logistics-objects/{b.id}/party-reports", "report"
    elif kind == "negative_weight":
        doc = b.report(air_org, "AIR", air_name, weight=-5, weight_unit="KGM",
                       volume=b.volume, volume_unit="MTQ", awb=b.awb, shc=[],
                       shipper_name="x", consignee_name="y")
        path, record_kind = f"/logistics-objects/{b.id}/party-reports", "report"
    elif kind == "unknown_status_code":
        doc["cargo:eventCode"] = "IN_TRANSIT"
    elif kind == "no_timezone":
        doc["cargo:eventDate"] = b.t_dep.replace(tzinfo=None).isoformat()
    elif kind == "relative_id":
        doc["@id"] = "event-123"
    elif kind == "missing_context":
        del doc["@context"]
    elif kind == "bad_location":
        doc["cargo:eventLocation"] = location("Mumbai")
    elif kind == "event_for_other_shipment":
        doc["cargo:eventFor"] = {"@id": b.id + "-other"}
    return (f"invalid:{kind}", record_kind, path, doc, 422)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def send_live(dataset, url, rate):
    import urllib.error
    import urllib.request
    from urllib.parse import quote

    delay = 1.0 / rate if rate > 0 else 0
    ok = conflicts = rejected = 0
    for record in dataset["records"]:
        path = record["path"]
        if path != "/logistics-objects":
            shipment = record["shipment_id"]
            suffix = path.rsplit("/", 1)[1]
            path = f"/logistics-objects/{quote(shipment, safe='')}/{suffix}"
        request = urllib.request.Request(
            url.rstrip("/") + path, data=json.dumps(record["body"]).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read() or b"{}")
                ok += 1
                conflicts += len(body.get("conflicts", []))
        except urllib.error.HTTPError as error:
            if error.code == 409:
                conflicts += 1
            else:
                rejected += 1
        sent = record["seq"] + 1
        if sent % 25 == 0 or sent == len(dataset["records"]):
            print(f"{sent}/{len(dataset['records'])} sent, "
                  f"{conflicts} conflicts, {rejected} rejected", flush=True)
        time.sleep(delay)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shipments", type=int, default=200)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--conflict-rate", type=float, default=0.6)
    parser.add_argument("--out", type=Path, default=None, help="write the labelled dataset here")
    parser.add_argument("--live", action="store_true", help="stream records to a running API")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--rate", type=float, default=5.0, help="records per second in --live mode")
    args = parser.parse_args(argv)

    dataset = generate(args.shipments, args.seed, args.conflict_rate)
    meta = dataset["meta"]
    print(f"Generated {meta['records']} records for {meta['shipments']} shipments "
          f"({len(dataset['injections'])} injected conflicts, seed {meta['seed']}).")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(dataset, indent=1))
        print(f"Dataset written to {args.out}")
    if args.live:
        send_live(dataset, args.url, args.rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
