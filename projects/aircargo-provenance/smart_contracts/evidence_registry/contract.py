from algopy import ARC4Contract, BoxMap, Bytes, Global, String, Txn
from algopy.arc4 import abimethod


# SHA-256 hashes are exactly 32 bytes.
HASH_LENGTH = 32


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
        # Temporary access control until participant roles are defined:
        # only the account that deployed the contract may write.
        assert Txn.sender == Global.creator_address, "Only the deployer can write"
        assert evidence_id, "Evidence ID cannot be empty"
        assert shipment_id, "Shipment ID cannot be empty"
        assert evidence_hash.length == HASH_LENGTH, "Evidence hash must be 32 bytes"
        assert submitted_by, "Submitter cannot be empty"
        assert evidence_id not in self.evidence, "Evidence already registered"

        record = (
            evidence_hash
            + Bytes(b"|")
            + shipment_id.bytes
            + Bytes(b"|")
            + submitted_by.bytes
        )

        self.evidence[evidence_id] = record
        return String("Evidence registered")

    @abimethod(readonly=True)
    def verify_evidence(
        self,
        evidence_id: String,
        submitted_hash: Bytes,
    ) -> String:
        assert evidence_id in self.evidence, "Evidence does not exist"
        assert submitted_hash.length == HASH_LENGTH, "Submitted hash must be 32 bytes"

        record = self.evidence[evidence_id]
        stored_hash = record[0:HASH_LENGTH]

        if stored_hash == submitted_hash:
            return String("Evidence hash matches")
        return String("Evidence hash mismatch")
