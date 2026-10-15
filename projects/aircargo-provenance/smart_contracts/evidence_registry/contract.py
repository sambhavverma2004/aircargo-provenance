from algopy import ARC4Contract, BoxMap, Bytes, String
from algopy.arc4 import abimethod


class EvidenceRegistry(ARC4Contract):

    def __init__(self) -> None:
        self.evidence = BoxMap(String, Bytes, key_prefix="evd")

    @abimethod()
    def register_evidence(
        self,
        evidence_id: String,
        shipment_id: String,
        evidence_hash: Bytes,
        submitted_by: String,
    ) -> String:
        assert evidence_id, "Evidence ID cannot be empty"
        assert shipment_id, "Shipment ID cannot be empty"
        assert evidence_hash, "Evidence hash cannot be empty"
        assert submitted_by, "Submitter cannot be empty"
        assert evidence_id not in self.evidence, "Evidence already registered"

        # SHA-256 hashes are exactly 32 bytes.

        record = (
            evidence_hash
            + Bytes(b"|")
            + shipment_id.bytes
            + Bytes(b"|")
            + submitted_by.bytes
        )

        self.evidence[evidence_id] = record
        return String("Evidence registered")

    @abimethod()
    def verify_evidence(
        self,
        evidence_id: String,
        submitted_hash: Bytes,
    ) -> String:
        assert evidence_id in self.evidence, "Evidence does not exist"

        record = self.evidence[evidence_id]
        stored_hash = record[0:32]

        if stored_hash == submitted_hash:
            return String("Evidence hash matches")
        return String("Evidence hash mismatch")

