"""Algorand anchoring for conflicts, events and evidence.

What is written on-chain (via the project's smart contracts):

* every detected conflict -> ConflictRegistry.report_conflict, with a
  SHA-256 hash of the conflict and of BOTH disputed records;
* every conflict resolution -> ConflictRegistry.resolve_conflict;
* every accepted logistics event (optional) -> AirCargoProvenance.record_event;
* every evidence file -> EvidenceRegistry.register_evidence.

Only hashes and short keys go on-chain, never shipment data itself.

Modes (environment variable CHAIN_MODE):
  off        nothing is anchored (default; used by the unit tests)
  simulated  in-memory stand-in for demos without a node. Clearly labelled;
             its timings must never be reported as Algorand measurements.
  algorand   a real Algorand node through algokit-utils. The node is chosen
             with ALGOD_SERVER / ALGOD_TOKEN / ALGOD_PORT (LocalNet if unset);
             the signing account with DEPLOYER_MNEMONIC (LocalNet: automatic).

Transactions are sent by one background worker so that ingestion never waits
for block confirmation. Each transaction is recorded in chain_transactions
with submission, confirmation and retrieval times, fee and box storage cost.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import queue
import threading
import time


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEPLOYED_APPS_FILE = PROJECT_ROOT / "smart_contracts" / "artifacts" / "deployed_apps.json"

# kind of anchored object -> (contract folder, client module, client class)
CONTRACTS = {
    "conflict": ("conflict_registry", "conflict_registry_client", "ConflictRegistryClient"),
    "evidence": ("evidence_registry", "evidence_registry_client", "EvidenceRegistryClient"),
    "event": ("hello_world", "air_cargo_provenance_client", "AirCargoProvenanceClient"),
}

BOX_PREFIX = {"conflict": b"cfl", "status": b"cst", "evidence": b"evd", "event": b"evt"}

# Algorand minimum-balance requirement per box (microAlgos).
BOX_FLAT_MBR = 2500
BOX_BYTE_MBR = 400


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def chain_key(identifier: str, length: int = 32) -> str:
    """Short, fixed-length on-chain key for a long identifier (box names
    are limited to 64 bytes and cost 400 microAlgos per byte)."""
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:length]


def sha256_bytes(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def canonical_hash(document) -> bytes:
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_bytes(text.encode("utf-8"))


def box_mbr(name_length: int, value_length: int) -> int:
    return BOX_FLAT_MBR + BOX_BYTE_MBR * (name_length + value_length)


# --------------------------------------------------------------------------- #
# Box layouts - mirror the smart contracts byte for byte
# --------------------------------------------------------------------------- #

def conflict_record(shipment, event_a, event_b, reason, evidence_hash: bytes) -> bytes:
    return b"|".join([
        shipment.encode(), event_a.encode(), event_b.encode(), reason.encode(),
    ]) + b"|OPEN|" + evidence_hash


def resolved_record(existing: bytes, resolution, resolved_by) -> bytes:
    return existing + b"|RESOLVED|" + resolution.encode() + b"|" + resolved_by.encode()


def evidence_record(evidence_hash: bytes, shipment, submitted_by) -> bytes:
    return evidence_hash + b"|" + shipment.encode() + b"|" + submitted_by.encode()


def event_record(shipment, event_type, event_time: int, organization, location,
                 weight_grams: int, event_hash: bytes) -> bytes:
    return (
        shipment.encode() + b"|" + event_type.encode() + b"|"
        + event_time.to_bytes(8, "big") + b"|" + organization.encode() + b"|"
        + location.encode() + b"|" + weight_grams.to_bytes(8, "big") + b"|"
        + event_hash
    )


def hash_in_conflict_box(value: bytes) -> bytes | None:
    marker = value.find(b"|OPEN|")
    if marker < 0:
        return None
    return value[marker + 6: marker + 6 + 32]


# --------------------------------------------------------------------------- #
# Jobs: what each anchoring call sends
# --------------------------------------------------------------------------- #

@dataclass
class ChainCall:
    kind: str            # conflict | resolve | event | evidence
    contract: str        # conflict | evidence | event  (key of CONTRACTS)
    method: str
    args: list           # ABI args; bytes values are stored hex-encoded
    boxes: list          # box names (bytes)
    main_box: bytes      # the box read back for retrieval / verification
    expected: bytes      # expected value of main_box after the call (simulated)
    check_hash: bytes    # hash that must appear in main_box for "verified"

    def to_json(self):
        def encode(value):
            return {"hex": value.hex()} if isinstance(value, bytes) else value
        return json.dumps({
            "kind": self.kind, "contract": self.contract, "method": self.method,
            "args": [encode(a) for a in self.args],
            "boxes": [b.hex() for b in self.boxes],
            "main_box": self.main_box.hex(), "expected": self.expected.hex(),
            "check_hash": self.check_hash.hex(),
        })

    @staticmethod
    def from_json(text):
        data = json.loads(text)

        def decode(value):
            return bytes.fromhex(value["hex"]) if isinstance(value, dict) else value
        return ChainCall(
            kind=data["kind"], contract=data["contract"], method=data["method"],
            args=[decode(a) for a in data["args"]],
            boxes=[bytes.fromhex(b) for b in data["boxes"]],
            main_box=bytes.fromhex(data["main_box"]),
            expected=bytes.fromhex(data["expected"]),
            check_hash=bytes.fromhex(data["check_hash"]),
        )


def conflict_call(conflict_id, shipment_id, object_id, related_id, conflict_type,
                  conflict_hash: bytes) -> ChainCall:
    ckey = chain_key(conflict_id)
    a = chain_key(object_id)
    # A duplicate has no second record; the contract needs two different ids.
    b = chain_key(related_id if related_id else "previous:" + object_id)
    ship = chain_key(shipment_id or object_id)
    return ChainCall(
        kind="conflict", contract="conflict", method="report_conflict",
        args=[ckey, ship, a, b, conflict_type, conflict_hash],
        boxes=[BOX_PREFIX["conflict"] + ckey.encode(), BOX_PREFIX["status"] + ckey.encode()],
        main_box=BOX_PREFIX["conflict"] + ckey.encode(),
        expected=conflict_record(ship, a, b, conflict_type, conflict_hash),
        check_hash=conflict_hash,
    )


def resolve_call(conflict_id, shipment_id, object_id, related_id, conflict_type,
                 conflict_hash: bytes, resolution: str, resolved_by: str) -> ChainCall:
    ckey = chain_key(conflict_id)
    base = conflict_call(conflict_id, shipment_id, object_id, related_id,
                         conflict_type, conflict_hash)
    resolution = (resolution or "resolved")[:64]
    resolved_by = chain_key(resolved_by or "operator", 16)
    return ChainCall(
        kind="resolve", contract="conflict", method="resolve_conflict",
        args=[ckey, resolution, resolved_by],
        boxes=base.boxes, main_box=base.main_box,
        expected=resolved_record(base.expected, resolution, resolved_by),
        check_hash=conflict_hash,
    )


def event_call(event_id, shipment_id, status_code, event_time: int, org, location,
               payload_hash: bytes) -> ChainCall:
    ekey = chain_key(event_id)
    ship = chain_key(shipment_id)
    org_key = chain_key(org, 16)
    location = location or "-"
    return ChainCall(
        kind="event", contract="event", method="record_event",
        args=[ekey, ship, status_code, event_time, org_key, location, 0, payload_hash],
        boxes=[BOX_PREFIX["event"] + ekey.encode()],
        main_box=BOX_PREFIX["event"] + ekey.encode(),
        expected=event_record(ship, status_code, event_time, org_key, location, 0, payload_hash),
        check_hash=payload_hash,
    )


def evidence_call(evidence_id, shipment_id, file_hash: bytes, party) -> ChainCall:
    evkey = chain_key(evidence_id)
    ship = chain_key(shipment_id)
    party_key = chain_key(party, 16)
    return ChainCall(
        kind="evidence", contract="evidence", method="register_evidence",
        args=[evkey, ship, file_hash, party_key],
        boxes=[BOX_PREFIX["evidence"] + evkey.encode()],
        main_box=BOX_PREFIX["evidence"] + evkey.encode(),
        expected=evidence_record(file_hash, ship, party_key),
        check_hash=file_hash,
    )


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

@dataclass
class TxResult:
    tx_id: str
    confirmed_round: int | None
    app_id: int | None
    fee_microalgos: int
    submit_ms: float
    confirm_ms: float


class OffBackend:
    mode = "off"
    enabled = False

    def info(self):
        return {"mode": self.mode, "enabled": False}


class SimulatedBackend:
    """In-memory stand-in for demos and tests. Not a blockchain."""

    mode = "simulated"
    enabled = True

    def __init__(self):
        self.boxes = {}
        self.round = 1000
        self.lock = threading.Lock()

    def info(self):
        return {
            "mode": self.mode, "enabled": True,
            "warning": "Simulated chain: no Algorand node is used. Do not report these timings.",
            "app_ids": {name: None for name in CONTRACTS},
        }

    def call(self, call: ChainCall) -> TxResult:
        with self.lock:
            key = (call.contract, call.main_box)
            if call.method == "resolve_conflict":
                status_key = (call.contract, BOX_PREFIX["status"] + call.main_box[3:])
                if self.boxes.get(status_key) != b"OPEN":
                    raise RuntimeError("logic eval error: Conflict is not open")
                self.boxes[status_key] = b"RESOLVED"
            else:
                if key in self.boxes:
                    raise RuntimeError("logic eval error: already exists")
                if call.method == "report_conflict":
                    self.boxes[(call.contract, BOX_PREFIX["status"] + call.main_box[3:])] = b"OPEN"
            self.boxes[key] = call.expected
            self.round += 1
            tx_id = "SIM" + hashlib.sha256(call.to_json().encode() + str(self.round).encode()).hexdigest()[:49].upper()
            return TxResult(tx_id, self.round, None, 1000, 0.0, 0.0)

    def read_box(self, contract, name: bytes):
        return self.boxes.get((contract, name))

    def send_only(self, call: ChainCall):
        started = time.perf_counter()
        result = self.call(call)
        return result.tx_id, None, result.fee_microalgos, started, time.perf_counter()

    def wait(self, tx_id):
        return self.round

    def network_info(self):
        return {"genesis_id": "simulated", "last_round": self.round, "min_fee": 1000,
                "server": "in-memory"}


class AlgorandBackend:
    """A real Algorand node through algokit-utils and the generated typed
    clients in smart_contracts/artifacts/."""

    mode = "algorand"
    enabled = True

    def __init__(self, app_ids: dict | None = None):
        import importlib
        import algokit_utils
        from algosdk.transaction import wait_for_confirmation

        self._au = algokit_utils
        self._wait = wait_for_confirmation
        self.algorand = algokit_utils.AlgorandClient.from_environment()
        self.algod = self.algorand.client.algod
        self.sender = self.algorand.account.from_environment("DEPLOYER")
        self.network = os.getenv("ALGOD_SERVER", "localnet (default)")
        self.app_ids = app_ids or load_app_ids()
        self.clients = {}
        for kind, (folder, module, cls) in CONTRACTS.items():
            app_id = self.app_ids.get(folder)
            if not app_id:
                raise RuntimeError(
                    f"No app id for {folder}. Run `algokit project deploy localnet` "
                    f"(writes {DEPLOYED_APPS_FILE.name}) or set {folder.upper()}_APP_ID."
                )
            client_class = getattr(
                importlib.import_module(f"smart_contracts.artifacts.{folder}.{module}"), cls
            )
            self.clients[kind] = self.algorand.client.get_typed_app_client_by_id(
                client_class, app_id=int(app_id), default_sender=self.sender.address,
            )
        self.calls_since_funding = 0
        self.min_app_balance = algokit_utils.AlgoAmount(
            algo=float(os.getenv("CHAIN_APP_MIN_SPENDING_ALGO", "5"))
        )

    def info(self):
        return {
            "mode": self.mode, "enabled": True, "network": self.network,
            "sender": self.sender.address,
            "app_ids": {folder: self.app_ids.get(folder) for folder, _, _ in CONTRACTS.values()},
        }

    def _ensure_funded(self, client):
        """Box storage is paid from the app account's minimum balance."""
        if os.getenv("CHAIN_AUTO_FUND", "1") != "1":
            return
        if self.calls_since_funding % 25 == 0:
            self.algorand.account.ensure_funded(
                client.app_address, self.sender, self.min_app_balance,
                min_funding_increment=self._au.AlgoAmount(algo=5),
            )
        self.calls_since_funding += 1

    def send_only(self, call: ChainCall):
        """Build, sign and submit without waiting. Returns
        (tx_id, app_id, fee_microalgos, started, submitted) with
        perf_counter timestamps."""
        client = self.clients[call.contract]
        self._ensure_funded(client)
        params = self._au.CommonAppCallParams(
            box_references=[self._au.BoxReference(app_id=0, name=name) for name in call.boxes]
        )
        builder = getattr(client.create_transaction, call.method)
        built = builder(args=tuple(call.args), params=params)

        signed = []
        for index, txn in enumerate(built.transactions):
            signer = built.signers.get(index) or self.algorand.account.get_signer(self.sender.address)
            signed.extend(signer.sign_transactions([txn], [0]))

        started = time.perf_counter()
        tx_id = self.algod.send_transactions(signed)
        submitted = time.perf_counter()
        return tx_id, client.app_id, sum(t.fee for t in built.transactions), started, submitted

    def wait(self, tx_id):
        pending = self._wait(self.algod, tx_id, int(os.getenv("CHAIN_MAX_ROUNDS", "20")))
        return pending.get("confirmed-round")

    def network_info(self):
        params = self.algod.suggested_params()
        return {"genesis_id": params.gen, "last_round": params.first,
                "min_fee": params.min_fee, "server": self.network}

    def call(self, call: ChainCall) -> TxResult:
        tx_id, app_id, fee, started, submitted = self.send_only(call)
        confirmed_round = self.wait(tx_id)
        confirmed = time.perf_counter()
        return TxResult(
            tx_id=tx_id,
            confirmed_round=confirmed_round,
            app_id=app_id,
            fee_microalgos=fee,
            submit_ms=(submitted - started) * 1000,
            confirm_ms=(confirmed - started) * 1000,
        )

    def read_box(self, contract, name: bytes):
        import base64
        client = self.clients[contract]
        try:
            box = self.algod.application_box_by_name(client.app_id, name)
        except Exception as error:  # box not found
            if "not found" in str(error).lower() or "404" in str(error):
                return None
            raise
        return base64.b64decode(box["value"])


def load_app_ids() -> dict:
    """App ids written by the deploy scripts, overridable per contract with
    environment variables such as CONFLICT_REGISTRY_APP_ID."""
    ids = {}
    network = os.getenv("CHAIN_NETWORK", "localnet")
    if DEPLOYED_APPS_FILE.exists():
        data = json.loads(DEPLOYED_APPS_FILE.read_text())
        ids.update(data.get(network, {}))
    for folder, _, _ in CONTRACTS.values():
        override = os.getenv(f"{folder.upper()}_APP_ID")
        if override:
            ids[folder] = int(override)
    return ids


def backend_from_env():
    mode = os.getenv("CHAIN_MODE", "off").strip().lower()
    if mode == "simulated":
        return SimulatedBackend()
    if mode == "algorand":
        return AlgorandBackend()
    return OffBackend()


# --------------------------------------------------------------------------- #
# The anchoring service
# --------------------------------------------------------------------------- #

REF_TABLE = {
    "conflict": ("conflicts", "conflict_id"),
    "resolve": ("conflicts", "conflict_id"),
    "event": ("logistics_events", "event_id"),
    "evidence": ("evidence", "evidence_id"),
}


class ChainAnchor:
    def __init__(self, backend, connect, anchor_events: bool = True):
        self.backend = backend
        self.connect = connect
        self.anchor_events = anchor_events
        self.jobs = queue.Queue()
        self.thread = None
        self.stopping = threading.Event()
        self.last_error = None

    @property
    def enabled(self):
        return self.backend.enabled

    def info(self):
        info = dict(self.backend.info())
        info["queue_depth"] = self.jobs.qsize()
        info["anchor_events"] = self.anchor_events and self.enabled
        info["last_error"] = self.last_error
        return info

    # -- lifecycle ---------------------------------------------------------- #

    def start(self):
        if not self.enabled or self.thread is not None:
            return
        self.stopping.clear()
        self.thread = threading.Thread(target=self._run, name="chain-anchor", daemon=True)
        self.thread.start()
        # Re-queue anything left unsent by a previous run.
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT tx_row_id FROM chain_transactions WHERE status = 'QUEUED' "
                "AND mode = ? ORDER BY tx_row_id", (self.backend.mode,)
            ).fetchall()
        for row in rows:
            self.jobs.put(row[0])

    def stop(self, timeout=5.0):
        if self.thread is None:
            return
        self.stopping.set()
        self.jobs.put(None)
        self.thread.join(timeout)
        self.thread = None

    def drain(self, timeout=30.0) -> bool:
        """Wait until every queued job has been processed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.jobs.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    # -- queueing ----------------------------------------------------------- #

    def enqueue(self, call: ChainCall, ref_id: str, conn=None):
        """Record the job and queue it. Pass `conn` to write the job inside
        the caller's transaction; it is queued after that commits."""
        if not self.enabled:
            return None
        if call.kind == "event" and not self.anchor_events:
            return None
        value_length = len(call.expected)
        own = conn is None
        if own:
            conn = self.connect()
        try:
            cursor = conn.execute(
                """
                INSERT INTO chain_transactions
                (kind, ref_id, mode, status, request, queued_at, box_name,
                 box_value_bytes, mbr_microalgos)
                VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?)
                """,
                (
                    call.kind, ref_id, self.backend.mode, call.to_json(), now_iso(),
                    call.main_box.decode(errors="replace"), value_length,
                    0 if call.kind == "resolve" else sum(
                        box_mbr(len(name), value_length if name == call.main_box else len(b"OPEN"))
                        for name in call.boxes
                    ),
                ),
            )
            row_id = cursor.lastrowid
            table, key = REF_TABLE[call.kind]
            if call.kind != "resolve":
                conn.execute(
                    f"UPDATE {table} SET chain_status = 'QUEUED' WHERE {key} = ?",
                    (ref_id,),
                )
            if own:
                conn.commit()
        finally:
            if own:
                conn.close()
        return row_id

    def submit(self, row_ids):
        for row_id in row_ids:
            if row_id is not None:
                self.jobs.put(row_id)

    # -- worker ------------------------------------------------------------- #

    def _run(self):
        while not self.stopping.is_set():
            row_id = self.jobs.get()
            try:
                if row_id is None:
                    return
                self._process(row_id)
            except Exception as error:  # never kill the worker
                self.last_error = f"{type(error).__name__}: {error}"
            finally:
                self.jobs.task_done()

    def _process(self, row_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM chain_transactions WHERE tx_row_id = ?", (row_id,)
            ).fetchone()
        if row is None or row["status"] != "QUEUED":
            return
        call = ChainCall.from_json(row["request"])
        submitted_at = now_iso()
        try:
            result = self.backend.call(call)
        except Exception as error:
            message = f"{type(error).__name__}: {error}"[:500]
            self.last_error = message
            with self.connect() as conn:
                conn.execute(
                    "UPDATE chain_transactions SET status='FAILED', submitted_at=?, error=? "
                    "WHERE tx_row_id=?", (submitted_at, message, row_id),
                )
                self._update_ref(conn, call.kind, row["ref_id"], "FAILED", None)
            return

        confirmed_at = now_iso()
        started = time.perf_counter()
        value = self.backend.read_box(call.contract, call.main_box)
        retrieve_ms = (time.perf_counter() - started) * 1000
        verified = int(value is not None and call.check_hash in value)

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE chain_transactions SET status='CONFIRMED', submitted_at=?,
                    confirmed_at=?, confirmed_round=?, tx_id=?, app_id=?,
                    fee_microalgos=?, submit_ms=?, confirm_ms=?, retrieve_ms=?,
                    verified=?
                WHERE tx_row_id=?
                """,
                (
                    submitted_at, confirmed_at, result.confirmed_round, result.tx_id,
                    result.app_id, result.fee_microalgos, result.submit_ms,
                    result.confirm_ms, retrieve_ms, verified, row_id,
                ),
            )
            status = "RESOLVED_ON_CHAIN" if call.kind == "resolve" else "CONFIRMED"
            self._update_ref(conn, call.kind, row["ref_id"], status, result.tx_id)

    def _update_ref(self, conn, kind, ref_id, status, tx_id):
        table, key = REF_TABLE[kind]
        if kind == "resolve" and status == "FAILED":
            status = "RESOLVE_FAILED"
        if tx_id is None:
            conn.execute(f"UPDATE {table} SET chain_status=? WHERE {key}=?", (status, ref_id))
        elif kind == "resolve":
            conn.execute(f"UPDATE {table} SET chain_status=? WHERE {key}=?", (status, ref_id))
        else:
            conn.execute(
                f"UPDATE {table} SET chain_status=?, chain_tx_id=? WHERE {key}=?",
                (status, tx_id, ref_id),
            )

    # -- reads -------------------------------------------------------------- #

    def read_main_box(self, call: ChainCall):
        if not self.enabled:
            return None
        return self.backend.read_box(call.contract, call.main_box)
