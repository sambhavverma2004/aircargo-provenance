from algopy import ARC4Contract, BoxMap, Bytes, String
from algopy.arc4 import abimethod


class ConflictRegistry(ARC4Contract):

    def __init__(self) -> None:
        self.conflicts = BoxMap(String, Bytes, key_prefix="cfl")

    @abimethod()
    def report_conflict(
        self,
        conflict_id: String,
        shipment_id: String,
        event_a_id: String,
        event_b_id: String,
        reason: String,
        evidence_hash: Bytes,
    ) -> String:
        assert conflict_id, "Conflict ID cannot be empty"
        assert shipment_id, "Shipment ID cannot be empty"
        assert event_a_id, "First event ID cannot be empty"
        assert event_b_id, "Second event ID cannot be empty"
        assert event_a_id != event_b_id, "Events must be different"
        assert evidence_hash, "Evidence hash cannot be empty"
        assert conflict_id not in self.conflicts, "Conflict already exists"

        record = (
            shipment_id.bytes
            + Bytes(b"|")
            + event_a_id.bytes
            + Bytes(b"|")
            + event_b_id.bytes
            + Bytes(b"|")
            + reason.bytes
            + Bytes(b"|OPEN|")
            + evidence_hash
        )

        self.conflicts[conflict_id] = record
        return "Conflict reported: " + conflict_id

    @abimethod()
    def resolve_conflict(
        self,
        conflict_id: String,
        resolution: String,
        resolved_by: String,
    ) -> String:
        assert conflict_id in self.conflicts, "Conflict does not exist"
        assert resolution, "Resolution cannot be empty"
        assert resolved_by, "Resolver cannot be empty"

        existing_record = self.conflicts[conflict_id]
        updated_record = (
            existing_record
            + Bytes(b"|RESOLVED|")
            + resolution.bytes
            + Bytes(b"|")
            + resolved_by.bytes
        )

        self.conflicts[conflict_id] = updated_record
        return "Conflict resolved: " + conflict_id
