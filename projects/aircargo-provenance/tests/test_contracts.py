"""Offline tests for the smart contracts using algorand-python-testing.

No Algorand node is needed. Install the emulator once with:
    poetry add --group dev algorand-python-testing
These tests are skipped if it is not installed.
"""

import hashlib

import pytest

algopy_testing = pytest.importorskip("algopy_testing")

from algopy import Bytes, String  # noqa: E402

from smart_contracts.conflict_registry.contract import ConflictRegistry  # noqa: E402
from smart_contracts.evidence_registry.contract import EvidenceRegistry  # noqa: E402
from smart_contracts.hello_world.contract import AirCargoProvenance  # noqa: E402
from smart_contracts.shipment_state.contract import ShipmentState  # noqa: E402


GOOD_HASH = Bytes(hashlib.sha256(b"evidence document").digest())
OTHER_HASH = Bytes(hashlib.sha256(b"altered document").digest())


@pytest.fixture
def context():
    with algopy_testing.algopy_testing_context() as ctx:
        yield ctx


def as_other_account(context, contract, method, *args):
    """Call `method` with a sender that is not the contract creator."""
    stranger = context.any.account()
    with context.txn.create_group(active_txn_overrides={"sender": stranger}):
        return method(*args)


# ------------------------------ EvidenceRegistry --------------------------- #

def test_evidence_matches_and_detects_alteration(context):
    registry = EvidenceRegistry()
    registry.register_evidence(
        String("e1"), String("s1"), GOOD_HASH, String("airline")
    )

    assert registry.verify_evidence(String("e1"), GOOD_HASH) == "Evidence hash matches"
    assert registry.verify_evidence(String("e1"), OTHER_HASH) == "Evidence hash mismatch"


@pytest.mark.parametrize("length", [0, 3, 31, 33, 64])
def test_evidence_hash_must_be_32_bytes(context, length):
    registry = EvidenceRegistry()
    with pytest.raises(AssertionError, match="32 bytes"):
        registry.register_evidence(
            String("e1"), String("s1"), Bytes(b"x" * length), String("airline")
        )


def test_verify_rejects_wrong_length_hash(context):
    registry = EvidenceRegistry()
    registry.register_evidence(
        String("e1"), String("s1"), GOOD_HASH, String("airline")
    )
    with pytest.raises(AssertionError, match="32 bytes"):
        registry.verify_evidence(String("e1"), Bytes(b"abc"))


def test_evidence_cannot_be_registered_twice(context):
    registry = EvidenceRegistry()
    registry.register_evidence(String("e1"), String("s1"), GOOD_HASH, String("a"))
    with pytest.raises(AssertionError, match="already registered"):
        registry.register_evidence(String("e1"), String("s1"), GOOD_HASH, String("a"))


def test_only_creator_can_register_evidence(context):
    registry = EvidenceRegistry()
    with pytest.raises(AssertionError, match="Only the deployer"):
        as_other_account(
            context, registry, registry.register_evidence,
            String("e1"), String("s1"), GOOD_HASH, String("a"),
        )


# ------------------------------ ConflictRegistry --------------------------- #

def report(registry, conflict_id="c1"):
    return registry.report_conflict(
        String(conflict_id), String("s1"), String("evt-a"), String("evt-b"),
        String("STATUS_MISMATCH"), GOOD_HASH,
    )


def test_conflict_report_and_resolve(context):
    registry = ConflictRegistry()
    report(registry)
    assert registry.get_status(String("c1")) == "OPEN"

    registry.resolve_conflict(String("c1"), String("airline time accepted"), String("ops"))
    assert registry.get_status(String("c1")) == "RESOLVED"


def test_conflict_cannot_be_resolved_twice(context):
    registry = ConflictRegistry()
    report(registry)
    registry.resolve_conflict(String("c1"), String("fixed"), String("ops"))

    with pytest.raises(AssertionError, match="not open"):
        registry.resolve_conflict(String("c1"), String("again"), String("someone"))


def test_conflict_requires_reason_and_32_byte_hash(context):
    registry = ConflictRegistry()
    with pytest.raises(AssertionError, match="Reason"):
        registry.report_conflict(
            String("c1"), String("s1"), String("a"), String("b"), String(""), GOOD_HASH
        )
    with pytest.raises(AssertionError, match="32 bytes"):
        registry.report_conflict(
            String("c2"), String("s1"), String("a"), String("b"),
            String("STATUS_MISMATCH"), Bytes(b"short"),
        )


def test_conflict_events_must_differ(context):
    registry = ConflictRegistry()
    with pytest.raises(AssertionError, match="different"):
        registry.report_conflict(
            String("c1"), String("s1"), String("a"), String("a"),
            String("STATUS_MISMATCH"), GOOD_HASH,
        )


def test_only_creator_can_resolve_conflict(context):
    registry = ConflictRegistry()
    report(registry)
    with pytest.raises(AssertionError, match="Only the deployer"):
        as_other_account(
            context, registry, registry.resolve_conflict,
            String("c1"), String("fixed"), String("ops"),
        )


# ------------------------------ ShipmentState ------------------------------ #

def test_shipment_follows_full_sequence(context):
    contract = ShipmentState()
    contract.create_shipment(String("s1"))
    for state in ("RECEIVED", "LOADED", "DEPARTED", "ARRIVED", "DELIVERED"):
        contract.advance_shipment(String("s1"), String(state))
    assert contract.shipments[String("s1")] == Bytes(b"DELIVERED")


def test_shipment_rejects_skipped_state(context):
    contract = ShipmentState()
    contract.create_shipment(String("s1"))
    with pytest.raises(AssertionError, match="Invalid transition"):
        contract.advance_shipment(String("s1"), String("DEPARTED"))


def test_shipment_rejects_change_after_delivery(context):
    contract = ShipmentState()
    contract.create_shipment(String("s1"))
    for state in ("RECEIVED", "LOADED", "DEPARTED", "ARRIVED", "DELIVERED"):
        contract.advance_shipment(String("s1"), String(state))
    with pytest.raises(AssertionError, match="already delivered"):
        contract.advance_shipment(String("s1"), String("RECEIVED"))


def test_only_creator_can_advance_shipment(context):
    contract = ShipmentState()
    contract.create_shipment(String("s1"))
    with pytest.raises(AssertionError, match="Only the deployer"):
        as_other_account(
            context, contract, contract.advance_shipment,
            String("s1"), String("RECEIVED"),
        )


# ------------------------------ AirCargoProvenance ------------------------- #

def test_record_event_stores_integers_as_8_bytes(context):
    contract = AirCargoProvenance()
    contract.record_event(
        String("evt-1"), String("s1"), String("DEP"),
        context.any.uint64(1_700_000_000, 1_700_000_000),
        String("airline"), String("DEL"),
        context.any.uint64(1250, 1250),
        GOOD_HASH,
    )
    record = contract.events[String("evt-1")].value
    assert (1_700_000_000).to_bytes(8, "big") in record
    assert (1250).to_bytes(8, "big") in record


def test_record_event_rejects_duplicate_id(context):
    contract = AirCargoProvenance()
    args = (
        String("evt-1"), String("s1"), String("DEP"),
        context.any.uint64(1, 1), String("airline"), String("DEL"),
        context.any.uint64(1, 1), GOOD_HASH,
    )
    contract.record_event(*args)
    with pytest.raises(AssertionError, match="already exists"):
        contract.record_event(*args)
