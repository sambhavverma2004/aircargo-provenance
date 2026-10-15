from algopy import ARC4Contract, BoxMap, Bytes, String, UInt64
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
        event_hash: Bytes,
    ) -> String:
        assert event_id, "Event ID cannot be empty"
        assert shipment_id, "Shipment ID cannot be empty"
        assert event_hash, "Event hash cannot be empty"
        assert event_id not in self.events, "Event ID already exists"

        record = (
            shipment_id.bytes
            + Bytes(b"|")
            + event_type.bytes
            + Bytes(b"|")
            + event_hash
        )

        self.events[event_id] = record
        return "Event recorded: " + event_id