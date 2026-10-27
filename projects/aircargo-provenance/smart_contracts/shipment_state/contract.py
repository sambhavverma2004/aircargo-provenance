from algopy import ARC4Contract, BoxMap, Bytes, Global, String, Txn
from algopy.arc4 import abimethod


class ShipmentState(ARC4Contract):

    def __init__(self) -> None:
        self.shipments = BoxMap(String, Bytes, key_prefix="shp")

    @abimethod()
    def create_shipment(self, shipment_id: String) -> String:
        # Temporary access control until participant roles are defined:
        # only the account that deployed the contract may write.
        assert Txn.sender == Global.creator_address, "Only the deployer can write"
        assert shipment_id, "Shipment ID cannot be empty"
        assert shipment_id not in self.shipments, "Shipment already exists"

        self.shipments[shipment_id] = Bytes(b"CREATED")
        return String("Shipment created")

    @abimethod()
    def advance_shipment(
        self,
        shipment_id: String,
        new_state: String,
    ) -> String:
        assert Txn.sender == Global.creator_address, "Only the deployer can write"
        assert shipment_id in self.shipments, "Shipment does not exist"

        current_state = self.shipments[shipment_id]

        if current_state == Bytes(b"CREATED"):
            assert new_state == String("RECEIVED"), "Invalid transition"
        elif current_state == Bytes(b"RECEIVED"):
            assert new_state == String("LOADED"), "Invalid transition"
        elif current_state == Bytes(b"LOADED"):
            assert new_state == String("DEPARTED"), "Invalid transition"
        elif current_state == Bytes(b"DEPARTED"):
            assert new_state == String("ARRIVED"), "Invalid transition"
        elif current_state == Bytes(b"ARRIVED"):
            assert new_state == String("DELIVERED"), "Invalid transition"
        else:
            assert False, "Shipment is already delivered or state is invalid"

        self.shipments[shipment_id] = new_state.bytes
        return String("Shipment state updated")
