from algopy import ARC4Contract, BoxMap, Bytes, Global, String, Txn, UInt64, op
from algopy.arc4 import abimethod


class AirCargoProvenance(ARC4Contract):

    def __init__(self) -> None:
        self.events = BoxMap(String, Bytes, key_prefix="evt")

    @abimethod()
    def record_event(
        self,
        event_id: String,
        shipment_id: String,
        event_type: String,
        event_time: UInt64,
        organization: String,
        location: String,
        weight_grams: UInt64,
        event_hash: Bytes,
    ) -> String:
        # Temporary access control until participant roles are defined:
        # only the account that deployed the contract may write.
        assert Txn.sender == Global.creator_address, "Only the deployer can write"
        assert event_id, "Event ID cannot be empty"
        assert shipment_id, "Shipment ID cannot be empty"
        assert event_type, "Event type cannot be empty"
        assert organization, "Organization cannot be empty"
        assert event_hash.length == 32, "Event hash must be 32 bytes"
        assert event_id not in self.events, "Event ID already exists"

        # UInt64 has no .bytes; op.itob converts it to 8 big-endian bytes.
        record = (
            shipment_id.bytes
            + Bytes(b"|")
            + event_type.bytes
            + Bytes(b"|")
            + op.itob(event_time)
            + Bytes(b"|")
            + organization.bytes
            + Bytes(b"|")
            + location.bytes
            + Bytes(b"|")
            + op.itob(weight_grams)
            + Bytes(b"|")
            + event_hash
        )

        self.events[event_id] = record
        return "Event recorded: " + event_id
