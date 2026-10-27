from algopy import ARC4Contract, BoxMap, Bytes, Global, String, Txn
from algopy.arc4 import abimethod


# SHA-256 hashes are exactly 32 bytes.
HASH_LENGTH = 32


class ConflictRegistry(ARC4Contract):

    def __init__(self) -> None:
        self.conflicts = BoxMap(String, Bytes, key_prefix="cfl")
        # Current status of each conflict: "OPEN" or "RESOLVED".
        self.status = BoxMap(String, String, key_prefix="cst")

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
        # Temporary access control until participant roles are defined:
        # only the account that deployed the contract may write.
        assert Txn.sender == Global.creator_address, "Only the deployer can write"
        assert conflict_id, "Conflict ID cannot be empty"
        assert shipment_id, "Shipment ID cannot be empty"
        assert event_a_id, "First event ID cannot be empty"
        assert event_b_id, "Second event ID cannot be empty"
        assert event_a_id != event_b_id, "Events must be different"
        assert reason, "Reason cannot be empty"
        assert evidence_hash.length == HASH_LENGTH, "Evidence hash must be 32 bytes"
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
        self.status[conflict_id] = String("OPEN")
        return "Conflict reported: " + conflict_id

    @abimethod()
    def resolve_conflict(
        self,
        conflict_id: String,
        resolution: String,
        resolved_by: String,
    ) -> String:
        assert Txn.sender == Global.creator_address, "Only the deployer can write"
        assert conflict_id in self.conflicts, "Conflict does not exist"
        assert self.status[conflict_id] == String("OPEN"), "Conflict is not open"
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
        self.status[conflict_id] = String("RESOLVED")
        return "Conflict resolved: " + conflict_id

    @abimethod(readonly=True)
    def get_status(self, conflict_id: String) -> String:
        assert conflict_id in self.status, "Conflict does not exist"
        return self.status[conflict_id]
