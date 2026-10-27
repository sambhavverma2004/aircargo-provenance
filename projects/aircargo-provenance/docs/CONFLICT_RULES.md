# Event Schema and Conflict Rules (v2)

Status: **implemented** in `api/rules.py` (events, R1–R3), `api/report_rules.py` (R4–R12) and `api/main.py`. Tests: `tests/test_events.py`, `tests/test_rules.py`, `tests/test_reports.py`.
Scope: single-leg shipments, prototype only. Decisions D1–D5 were taken with the proposed defaults; sections 11–13 add party reports.

---

## 1. Why events, not repeated records

A shipment is a logistics object. What parties disagree about is **what happened to it and when**. In ONE Record that is expressed as `cargo:LogisticsEvent` objects attached to the shipment, and the specification states that logistics events are immutable: they "MUST NOT be changed after creation" and event lists are append-only.

So the prototype treats every report from a participant as a new, immutable event with its own `@id`. Conflicts are relationships *between two events about the same shipment*. A repeated `@id` is a separate problem (identifier misuse), not a disagreement about the shipment.

Each event is read as one claim:

> Organisation **r** asserts that shipment **S** reached milestone **m** at time **t**.

## 2. Event schema

Endpoint (follows the ONE Record API shape):

```
POST /logistics-objects/{shipment_id}/logistics-events
```

Example body:

```json
{
  "@context": { "cargo": "https://onerecord.iata.org/ns/cargo#" },
  "@id": "https://airline.example/events/evt-0042",
  "@type": "cargo:LogisticsEvent",
  "cargo:eventFor": { "@id": "https://example.org/logistics-objects/shipment-001" },
  "cargo:eventCode": { "@id": "https://onerecord.iata.org/ns/code-lists/StatusCode#DEP" },
  "cargo:eventDate": { "@type": "http://www.w3.org/2001/XMLSchema#dateTime",
                       "@value": "2026-10-27T10:38:00Z" },
  "cargo:eventTimeType": { "@id": "cargo:ACTUAL" },
  "cargo:recordingOrganization": { "@id": "https://airline.example/org" }
}
```

| Field | Required | Rule | Used by rules |
|---|---|---|---|
| `@context` | yes | non-empty (existing check) | — |
| `@id` | yes | absolute HTTP(S) URI (existing check) | R1 |
| `@type` | yes | must be or include `cargo:LogisticsEvent` | — |
| `cargo:eventFor.@id` | yes | must equal `{shipment_id}` in the path; the shipment must already be stored | R2, R3 |
| `cargo:eventCode.@id` | yes | StatusCode IRI; the fragment after `#` is the code | R2, R3 |
| `cargo:eventDate` | yes | ISO 8601 date-time **with timezone**; accepts the `@value` object or a plain string | R2, R3 |
| `cargo:eventTimeType.@id` | no | defaults to `cargo:ACTUAL` | filter |
| `cargo:recordingOrganization.@id` | yes | absolute URI | reported in conflicts |
| `cargo:partialEventIndicator` | no | if `true`, event is stored but not compared | filter |
| `cargo:eventLocation` | no | stored, not used in v1 | — |

Property names are taken from the ONE Record logistics-events examples (`eventFor`, `eventCode`, `eventDate`, `eventTimeType`, `recordingOrganization`, `partialEventIndicator`). The custom `status` field used in the current test is dropped.

**Deviation from ONE Record (*Decision D3*):** the spec's examples omit `@id` and return the new event URI in `Location`, which suggests the server assigns it. Our API acts as an **aggregator** of events that participants have already published on their own servers, so it accepts the publisher's `@id`. That is what makes duplicate detection meaningful. This must be stated in the report as a prototype choice.

## 3. Milestones and the status-code mapping

The milestone sequence is the one already enforced by the `ShipmentState` contract:

| Rank | Milestone | StatusCode accepted |
|---|---|---|
| 0 | CREATED | `BKD` (booked) |
| 1 | RECEIVED | `RCS` (received from shipper / ready for carriage) |
| 2 | LOADED | `MAN` (manifested) |
| 3 | DEPARTED | `DEP` (departed) |
| 4 | ARRIVED | `ARR` (arrived) |
| 5 | DELIVERED | `DLV` (delivered) |

`DEP` as the departure code is confirmed by the ONE Record examples. The other five are the standard IATA Cargo-IMP status codes; check them against the ONE Record code-list ontology before the report cites this table.

Any other valid status code (e.g. `FOH`, `RCF`, `NFD`) is **stored but marked out of scope**: no rule compares it (*Decision D5*). `IN_TRANSIT` is not a status code and is not accepted.

**Difference from the contract (*Decision D4*):** `ShipmentState` requires each step to be exactly the next rank. The API **allows gaps** (e.g. RECEIVED followed directly by DEPARTED), because in a multi-party setting a missing report is normal and is not evidence of a conflict. As a result, a gapped history can't currently be replayed onto the contract as written. This only matters once on-chain sync is built.

## 4. Which events are compared

When a new event `e` arrives, it is compared against every stored event `x` where:

- `x` is for the same shipment,
- both `e` and `x` have time type `ACTUAL`,
- neither is partial (`partialEventIndicator` true),
- both map to a milestone in §3.

Planned or expected times differing from actual times are normal operations, not conflicts. Disputed events (§6) remain in the comparison set, so every conflict names a concrete pair of records.

## 5. Rules

Validation runs first. Rules R1 to R3 run only on structurally valid input.

### V — Structural validation (not a conflict)

Any failure in §2 → `422` (or `404` if the shipment in the path is unknown). **No conflict record is created.** Invalid input is a data-quality error, not a disagreement between parties (report Phase 2).

### R1 — `DUPLICATE_IDENTIFIER`

**Condition:** `e.@id` already exists as a stored event or logistics object.
**Outcome:** `409`, event **not stored**, one conflict opened.
**Severity:**
- `LOW` if the payload is byte-for-byte identical after JSON canonicalisation (a replay or retry).
- `HIGH` if the payload differs: the same identifier is carrying different content, which violates event immutability.

R1 short-circuits: R2 and R3 are not evaluated.

### R2 — `STATUS_MISMATCH` (disagreement about the same milestone)

**Condition:** `milestone(e) == milestone(x)` and `|t_e − t_x| > Δ`.
**Meaning:** two reports say the shipment reached the *same* milestone at materially different times.
**Severity:** `MEDIUM`.
If `|t_e − t_x| ≤ Δ` the reports **corroborate** each other and no conflict is opened.

### R3 — `INVALID_TRANSITION` (order contradicts the milestone sequence)

**Condition:** `milestone(e) ≠ milestone(x)` and the time order contradicts the rank order by more than the clock-skew tolerance ε:
- `rank(e) < rank(x)` and `t_e > t_x + ε` (an earlier milestone reported *after* a later one), or
- `rank(e) > rank(x)` and `t_e < t_x − ε` (a later milestone reported *before* an earlier one).

**Severity:** `HIGH`.
Events in the right order, including gaps, never trigger R3. Different milestones within ε of each other are treated as having no reliable order and are accepted.

R2 and R3 are mutually exclusive for any one pair, because one needs equal milestones and the other different ones. A new event can conflict with several stored events. **One conflict record is opened per conflicting pair.**

### Parameters (*Decision D1*)

| Symbol | Meaning | Proposed default |
|---|---|---|
| Δ | max time difference for two reports of the same milestone to corroborate | 30 minutes |
| ε | clock-skew tolerance when ordering different milestones | 5 minutes |

Both are module-level constants so the evaluation can sweep them.

## 6. What happens to a conflicting event (*Decision D2*)

For R2/R3 the event **is stored**, and **both events of each conflicting pair** are marked `disputed = true`. The response is `201` with:

```json
{ "ingestionStatus": "ACCEPTED_DISPUTED", "conflictDetected": true,
  "conflicts": [ { "conflictId": "…", "conflictType": "INVALID_TRANSITION",
                   "severity": "HIGH", "relatedEventId": "…" } ] }
```

Rationale: events are immutable evidence. Rejecting the second report would throw away exactly the record the dispute is about. Only R1 rejects, because a second row with the same `@id` can't be stored.

Alternative: return `409` and store nothing, which matches the current duplicate behaviour. It's simpler, but conflicts could then never be resolved by looking at both records.

## 7. Storage changes

- New table `logistics_events`: `event_id` (PK), `shipment_id`, `status_code`, `milestone_rank` (nullable), `event_date` (UTC ISO), `time_type`, `recording_org`, `partial`, `disputed`, `payload`, `created_at`.
- `conflicts` gains `related_object_id` (nullable; the other event in the pair) and `shipment_id` (nullable).
- `CREATE TABLE IF NOT EXISTS` does not add columns to an existing database. The startup code must `ALTER TABLE … ADD COLUMN` when columns are missing, or the bundled `api/aircargo_api.db` will break.
- Posting a `cargo:LogisticsEvent` to `POST /logistics-objects` returns `422` and points to the events endpoint, so there is one ingestion path per kind of object.

## 8. Worked test cases

All on one shipment `S`. Times are UTC and use Δ = 30 min, ε = 5 min. "Org A/B" are different recording organisations.

| # | Stored events | New event | Expected | Rule |
|---|---|---|---|---|
| 1 | — | DEP 10:00 A | 201, no conflict | — |
| 2 | RCS 08:00 A | DEP 10:00 B | 201, no conflict (gap allowed) | — |
| 3 | DEP 10:00 A | DLV 18:00 B | 201, no conflict (the original "in transit then delivered" case is legitimate) | — |
| 4 | DLV 18:00 A | DEP 19:00 B | 201 disputed, `INVALID_TRANSITION` HIGH | R3 |
| 5 | DEP 10:00 A | RCS 12:00 B (receipt two hours after departure) | 201 disputed, `INVALID_TRANSITION` HIGH | R3 |
| 6 | DEP 10:00 A | ARR 09:58 B | 201, no conflict (within ε) | — |
| 7 | DEP 10:00 A | DEP 10:20 B | 201, no conflict (corroborates) | — |
| 8 | DEP 10:00 A | DEP 14:00 B | 201 disputed, `STATUS_MISMATCH` MEDIUM | R2 |
| 9 | DEP 10:00 A, DEP 14:00 B | DEP 14:05 C | 201 disputed, **one** `STATUS_MISMATCH` (vs A only; corroborates B) | R2 |
| 10 | DEP 10:00 A, ARR 12:00 A | RCS 13:00 B | 201 disputed, **two** `INVALID_TRANSITION` (vs DEP and vs ARR) | R3 |
| 11 | event E stored | E again, identical payload | 409, `DUPLICATE_IDENTIFIER` LOW, not stored | R1 |
| 12 | event E stored | E's `@id`, different `eventDate` | 409, `DUPLICATE_IDENTIFIER` HIGH, not stored | R1 |
| 13 | DEP 10:00 A (ACTUAL) | DEP 15:00 B, `PLANNED` | 201, no conflict (not compared) | filter |
| 14 | DEP 10:00 A | DEP 15:00 B, partial | 201, no conflict (not compared) | filter |
| 15 | DEP 10:00 A | `FOH` 11:00 B | 201, no conflict, marked out of scope | filter |
| 16 | — | event for unknown shipment | 404, no conflict | V |
| 17 | — | `eventFor` ≠ path shipment | 422, no conflict | V |
| 18 | — | `eventDate` without timezone | 422, no conflict | V |
| 19 | — | unknown/missing `eventCode` | 422, no conflict | V |

The existing 24 tests stay as they are (shipment ingestion and `DUPLICATE_IDENTIFIER` for logistics objects). `test_status_mismatch_opens_conflict` is replaced by cases 7–9.

## 9. Known limitations of v1 (state these in the report)

- **Single leg only.** `DEP`/`ARR` happen once per flight leg. On a multi-leg routing the "each milestone happens once" assumption is false, and R2 would flag legitimate events. Supporting multi-leg needs a leg or segment key.
- **Comparison is pairwise.** The rules can't tell which party is right; they only establish that two records can't both be true. Resolution stays a human or process decision.
- **Timestamps are trusted as reported.** A party reporting a wrong time consistently with its own other events can't be detected.
- **Thresholds are assumptions.** Δ and ε are not derived from operational data. The evaluation should report sensitivity to them.
- **Not full ONE Record conformance:** client-assigned event `@id`, structural rather than ontology-based validation, and a six-code subset of StatusCode.

## 10. Decisions needed

| ID | Question | Proposed |
|---|---|---|
| D1 | Δ and ε values | 30 min / 5 min, configurable |
| D2 | Conflicting event: store as disputed (201) or reject (409)? | Store as disputed |
| D3 | Accept client-supplied event `@id`? | Yes (aggregator role), documented as deviation |
| D4 | Allow milestone gaps in the API? | Yes; contract alignment deferred to on-chain sync |
| D5 | Unmapped status codes: store-not-compare or reject? | Store, mark out of scope |


## 11. Party reports (v2)

Events say *when* something happened. Shipment details (weight, pieces, parties, route...) come from each party's **party report**: that party's own version of the shipment, posted to

```
POST /logistics-objects/{shipment_id}/party-reports
```

```json
{
  "@context": { "cargo": "https://onerecord.iata.org/ns/cargo#" },
  "@id": "https://airline.example/reports/r-1",
  "@type": "cargo:Shipment",
  "acp:reportFor": { "@id": "https://aggregator.example/logistics-objects/shp-1" },
  "acp:recordingOrganization": { "@id": "https://airline.example/org" },
  "acp:reporterRole": "AIR",
  "acp:organizationName": "SkyBridge Cargo Airlines",
  "cargo:waybillNumber": "176-12345675",
  "cargo:totalGrossWeight": { "cargo:numericalValue": 1250,
      "cargo:unit": { "@id": "https://onerecord.iata.org/ns/code-lists/MeasurementUnitCode#KGM" } },
  "cargo:totalDimensions": { "cargo:volume": { "cargo:numericalValue": 7.5, "cargo:unit": "MTQ" } },
  "cargo:slac": 10,
  "cargo:departureLocation": { "cargo:locationCodes": [{ "cargo:code": "BOM" }] },
  "cargo:arrivalLocation":   { "cargo:locationCodes": [{ "cargo:code": "FRA" }] },
  "cargo:specialHandlingCodes": [{ "@id": "https://onerecord.iata.org/ns/code-lists/SpecialHandlingCode#PER" }],
  "cargo:involvedParties": [
    { "cargo:partyRole": { "@id": "https://onerecord.iata.org/ns/code-lists/ParticipantIdentifier#SHP" },
      "cargo:partyDetails": { "@id": "https://shipper.example/org", "cargo:name": "Tata Exports Pvt Ltd" } }
  ]
}
```

Property names were checked against the ONE Record 2026-07 ontology (`IATA-1R-DM-Ontology.ttl`) and code lists (`IATA-1R-CL-Ontology.ttl`): `totalGrossWeight`, `numericalValue`, `unit` (MeasurementUnitCode `KGM`, `LBR`, `MTQ`, `FTQ`...), `totalDimensions`/`volume`, `slac` (shipper's load and count), `departureLocation`/`arrivalLocation`/`locationCodes`/`code`, `specialHandlingCodes` (SpecialHandlingCode and DangerousGoodsCode lists), `involvedParties`/`partyRole` (ParticipantIdentifier `SHP`, `CNE`, `FFW`, `AIR`, `GHA`...)/`partyDetails`/`name`, `waybillNumber`.

Prototype-only properties use the `acp:` prefix and are **not** ONE Record: `acp:reportFor`, `acp:recordingOrganization`, `acp:reporterRole`, `acp:organizationName`. Events may also carry `cargo:eventLocation` and `acp:reporterRole`.

Every field except the identifiers is optional; a field is compared only when both reports contain it. A missing or unknown unit, a negative weight, a 4-letter airport code etc. are **filter errors (HTTP 422), never conflicts**.

A newer report from the same organisation **supersedes** its previous one (`superseded = 1`); comparisons use each party's current report. Reports from the same organisation are never compared with each other.

## 12. Attribute and location rules

Each rule compares one pair of records from **different** parties. One conflict is raised per disagreeing field per pair.

| Rule | Type | Condition | Severity |
|---|---|---|---|
| R4 | `LOCATION_MISMATCH` | RCS/MAN/DEP event location ≠ a report's origin, or ARR/DLV location ≠ its destination (ACTUAL, non-partial events) | HIGH |
| R5 | `WEIGHT_MISMATCH` | gross weights differ by > 2 % **and** > 0.5 kg after converting to kg | MEDIUM; HIGH > 10 % |
| R6 | `UNIT_MISMATCH` | converted weights (or volumes) disagree, the units differ, **but the raw numbers agree within tolerance**: e.g. 1250 KGM vs 1250 LBR, a unit entry error | HIGH |
| R7 | `VOLUME_MISMATCH` | volumes differ by > 5 % after converting to m³ | MEDIUM; HIGH > 20 % |
| R8 | `PIECE_COUNT_MISMATCH` | `slac` differs | MEDIUM |
| R9 | `PARTY_MISMATCH` | same role, different organisation `@id` (HIGH); without ids, normalised-name similarity < 0.88 (MEDIUM) | HIGH / MEDIUM |
| R10 | `ROUTE_MISMATCH` | origin or destination airport differs (one conflict per end) | HIGH |
| R11 | `SPECIAL_HANDLING_MISMATCH` | the sets of handling codes differ (order ignored) | HIGH if a DangerousGoodsCode is involved, else MEDIUM |
| R12 | `REFERENCE_MISMATCH` | waybill numbers differ after removing spaces and dashes | HIGH |

**Units are converted before comparing**, so 1250 KGM vs 2755.8 LBR is *not* a conflict. Supported: weight `KGM`, `GRM`, `TNE`, `LBR`, `ONZ`; volume `MTQ`, `CMQ`, `LTR`, `FTQ`, `INQ`.

**Name normalisation (R9)**: Unicode accents removed, case folded, `&` → `and`, `M/s` removed, dotted acronyms joined (`L.L.C.` → `llc`), punctuation removed, common abbreviations expanded (`Intl` → `international`...), legal-form words dropped (`Pvt`, `Ltd`, `LLC`, `GmbH`...). Then `difflib.SequenceMatcher` similarity ≥ 0.88 counts as the same party. Organisation ids, when both reports have them, always win over names.

R4 is checked in both directions (a new event against stored reports, and a new report against stored events), so the result does not depend on arrival order. This was tested explicitly.

## 13. Conflict records

Every conflict stores: type, severity, the two record ids (`object_id` = newer, `related_object_id` = earlier), the shipment, the disputed `field`, `details` with both values and organisations, a description, and a **conflict hash**:

```
SHA-256( canonical JSON of { conflict id, type, shipment, both record ids,
                             SHA-256 of both record payloads, details } )
```

The hash is anchored on Algorand (`ConflictRegistry.report_conflict`), so `GET /conflicts/{id}/verify` can later prove that neither record changed after detection (see `docs/RUNNING.md`).
