"""A minimal fake algod HTTP server for testing AlgorandBackend without a
node. It accepts real signed transactions from algosdk/algokit-utils,
decodes the ABI app calls, applies the same box writes the contracts make,
and serves them back. It does NOT execute TEAL; contract logic itself is
tested with algorand-python-testing (tests/test_contracts.py)."""

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import msgpack
from algosdk import abi, encoding
from algosdk.transaction import SignedTransaction

from api import chain

GENESIS_HASH = base64.b64encode(hashlib.sha256(b"fake-genesis").digest()).decode()

METHODS = {
    "report_conflict(string,string,string,string,string,byte[])string":
        ["string", "string", "string", "string", "string", "bytes"],
    "resolve_conflict(string,string,string)string": ["string", "string", "string"],
    "register_evidence(string,string,byte[],string)string": ["string", "string", "bytes", "string"],
    "record_event(string,string,string,uint64,string,string,uint64,byte[])string":
        ["string", "string", "string", "uint64", "string", "string", "uint64", "bytes"],
}
SELECTORS = {abi.Method.from_signature(sig).get_selector(): (sig.split("(")[0], kinds)
             for sig, kinds in METHODS.items()}


def decode_arg(kind, raw: bytes):
    if kind == "uint64":
        return int.from_bytes(raw, "big")
    length = int.from_bytes(raw[:2], "big")
    value = raw[2:2 + length]
    return value.decode() if kind == "string" else value


class FakeAlgod:
    def __init__(self):
        self.round = 100
        self.boxes = {}          # (app_id, name) -> bytes
        self.pending = {}        # txid -> info
        self.calls = []          # decoded app calls, for assertions
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()

    # -- app call semantics (mirror of the contracts' box writes) --------- #

    def apply(self, txn):
        args = txn.app_args or []
        method, kinds = SELECTORS[bytes(args[0])]
        values = [decode_arg(k, bytes(a)) for k, a in zip(kinds, args[1:])]
        refs = {bytes(ref.name) for ref in (txn.boxes or [])}
        app = txn.index
        self.calls.append({"method": method, "args": values, "app_id": app,
                           "boxes": refs, "sender": txn.sender, "fee": txn.fee})

        def need(name):
            if name not in refs:
                raise ValueError(f"logic eval error: invalid Box reference {name!r}")

        if method == "report_conflict":
            key = values[0].encode()
            need(b"cfl" + key), need(b"cst" + key)
            if (app, b"cfl" + key) in self.boxes:
                raise ValueError("logic eval error: Conflict already exists")
            self.boxes[(app, b"cfl" + key)] = chain.conflict_record(*values[1:])
            self.boxes[(app, b"cst" + key)] = b"OPEN"
        elif method == "resolve_conflict":
            key = values[0].encode()
            need(b"cfl" + key), need(b"cst" + key)
            if self.boxes.get((app, b"cst" + key)) != b"OPEN":
                raise ValueError("logic eval error: Conflict is not open")
            self.boxes[(app, b"cfl" + key)] = chain.resolved_record(
                self.boxes[(app, b"cfl" + key)], values[1], values[2])
            self.boxes[(app, b"cst" + key)] = b"RESOLVED"
        elif method == "register_evidence":
            key = values[0].encode()
            need(b"evd" + key)
            if len(values[2]) != 32:
                raise ValueError("logic eval error: Evidence hash must be 32 bytes")
            self.boxes[(app, b"evd" + key)] = chain.evidence_record(values[2], values[1], values[3])
        elif method == "record_event":
            key = values[0].encode()
            need(b"evt" + key)
            self.boxes[(app, b"evt" + key)] = chain.event_record(*values[1:])

    # -- HTTP --------------------------------------------------------------- #

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                parts = url.path.strip("/").split("/")
                with fake.lock:
                    if url.path == "/v2/transactions/params":
                        return self.reply(200, {
                            "consensus-version": "fake", "fee": 0, "min-fee": 1000,
                            "genesis-hash": GENESIS_HASH, "genesis-id": "fakenet-v1",
                            "last-round": fake.round,
                        })
                    if url.path == "/v2/status" or url.path.startswith("/v2/status/wait-for-block-after"):
                        return self.reply(200, {"last-round": fake.round, "time-since-last-round": 0,
                                                "catchup-time": 0, "last-version": "fake",
                                                "next-version": "fake", "next-version-round": fake.round + 1,
                                                "next-version-supported": True, "stopped-at-unsupported-round": False})
                    if parts[:3] == ["v2", "transactions", "pending"]:
                        info = fake.pending.get(parts[3])
                        return self.reply(200 if info else 404, info or {"message": "not found"})
                    if parts[:2] == ["v2", "applications"] and parts[-1] == "box":
                        name = parse_qs(url.query)["name"][0]
                        assert name.startswith("b64:")
                        raw = base64.b64decode(name[4:])
                        value = fake.boxes.get((int(parts[2]), raw))
                        if value is None:
                            return self.reply(404, {"message": "box not found"})
                        return self.reply(200, {"name": name[4:], "round": fake.round,
                                                "value": base64.b64encode(value).decode()})
                    if parts[:2] == ["v2", "accounts"]:
                        return self.reply(200, {
                            "address": parts[2], "amount": 10**12,
                            "amount-without-pending-rewards": 10**12, "min-balance": 100000,
                            "pending-rewards": 0, "rewards": 0, "round": fake.round,
                            "status": "Offline", "total-apps-opted-in": 0,
                            "total-assets-opted-in": 0, "total-created-apps": 0,
                            "total-created-assets": 0, "total-box-bytes": 0, "total-boxes": 0,
                        })
                return self.reply(404, {"message": f"unsupported GET {url.path}"})

            def do_POST(self):
                url = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if url.path != "/v2/transactions":
                    return self.reply(404, {"message": "unsupported POST"})
                unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)
                unpacker.feed(body)
                first_txid = None
                with fake.lock:
                    try:
                        for item in unpacker:
                            packed = base64.b64encode(msgpack.packb(item, use_bin_type=True)).decode()
                            stx = encoding.msgpack_decode(packed)
                            assert isinstance(stx, SignedTransaction)
                            txid = stx.get_txid()
                            first_txid = first_txid or txid
                            if stx.transaction.type == "appl":
                                fake.apply(stx.transaction)
                            fake.round += 1
                            fake.pending[txid] = {
                                "confirmed-round": fake.round, "pool-error": "",
                                "txn": {"sig": stx.signature, "txn": {"type": stx.transaction.type}},
                            }
                    except Exception as error:
                        return self.reply(400, {"message": str(error)})
                return self.reply(200, {"txId": first_txid})

        return Handler


def _unused():  # keep imports used by type checkers
    return encoding
