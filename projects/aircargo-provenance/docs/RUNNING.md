# Running the Cargo Conflict Desk

All commands run in **PowerShell** from the project folder:

```powershell
cd C:\Users\AIDSHPCLAB16\aircargo-provenance\projects\aircargo-provenance
```

## 1. Install and test (once)

```powershell
poetry install
poetry run pytest -q          # expect: 182 passed
algokit project run build     # compiles the 4 contracts and generates their clients
```

## 2. Demo without a blockchain node (simulated chain)

Use this for a quick demo or when Docker is not available. Anchoring is simulated in memory and clearly labelled "Simulated" on the dashboard.

**Terminal 1: start the API**

```powershell
$env:CHAIN_MODE = "simulated"
$env:AIRCARGO_DB = "demo.db"            # keeps your real api\aircargo_api.db untouched
poetry run uvicorn api.main:app --port 8000
```

**Terminal 2: stream simulated multi-party data into it**

```powershell
poetry run python -m evaluation.simulator --live --url http://127.0.0.1:8000 --shipments 40 --rate 5
```

Open **http://127.0.0.1:8000/dashboard**. The overview fills up as records arrive:

- **Conflicts:** filter by type or severity; open one to see both parties' values side by side, attach evidence, resolve it and check its integrity.
- **Shipments:** a timeline per party and a table of what each party reported, with mismatches highlighted.
- **Metrics:** detection accuracy and Algorand performance, after you run step 4.
- **Algorand:** contract app ids and every anchoring transaction.

To start again from empty, stop the API and delete `demo.db`.

## 3. On Algorand LocalNet (real node, needs Docker Desktop)

```powershell
algokit localnet start
algokit project deploy localnet         # deploys + funds the contracts, writes
                                        # smart_contracts\artifacts\deployed_apps.json
$env:CHAIN_MODE = "algorand"
$env:AIRCARGO_DB = "localnet.db"
poetry run uvicorn api.main:app --port 8000
```

Then run the simulator from step 2 again. On the dashboard, the top bar now says "Algorand". Each conflict shows its transaction id and confirmed round, and **Check integrity** reads the hash back from the chain.

Run the performance benchmark (submission, confirmation and retrieval latency, throughput, fees and box deposits):

```powershell
$env:CHAIN_MODE = "algorand"
poetry run python -m evaluation.benchmark_chain --n 30 --pipelined 50
```

Results go to `evaluation\results\chain_benchmark_latest.md` and appear on the Metrics page. LocalNet is one local node, so its latency is **not** comparable with a public network; use TestNet for that (next step).

## 4. Evaluate detection accuracy

```powershell
poetry run python -m evaluation.evaluate --shipments 300 --seed 11 --scaling 50,200,500
```

This prints per-type precision, recall and F1, the false-alarm rate, filter accuracy, latency and throughput, and writes `evaluation\results\evaluation_latest.md`. See `docs\EVALUATION.md` for what each number means.

## 5. On Algorand TestNet (latency comparable with the papers)

1. Create an account (for example with `algokit task wallet` or Pera) and fund it from a TestNet dispenser.
2. Point AlgoKit at TestNet:

```powershell
$env:ALGOD_SERVER = "https://testnet-api.4160.nodely.dev"
$env:ALGOD_PORT = "443"
$env:ALGOD_TOKEN = ""
$env:INDEXER_SERVER = "https://testnet-idx.4160.nodely.dev"
$env:INDEXER_PORT = "443"
$env:DEPLOYER_MNEMONIC = "your 25 words ..."     # never commit this
$env:CHAIN_NETWORK = "testnet"
poetry run python -m smart_contracts deploy
$env:CHAIN_MODE = "algorand"
poetry run python -m evaluation.benchmark_chain --n 20 --pipelined 20
```

Each anchored record locks about 0.05–0.1 ALGO in box deposits (see `docs\EVALUATION.md`). The API tops up the app accounts from the deployer automatically. Set `$env:ANCHOR_EVENTS = "0"` to anchor only conflicts and evidence.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CHAIN_MODE` | `off` | `off`, `simulated` or `algorand` |
| `ANCHOR_EVENTS` | `1` | `0` = anchor conflicts and evidence only |
| `AIRCARGO_DB` | `api\aircargo_api.db` | SQLite database file |
| `AIRCARGO_EVIDENCE_DIR` | `api\evidence_files` | where uploaded evidence is stored |
| `CHAIN_NETWORK` | `localnet` | which section of `deployed_apps.json` to use |
| `CONFLICT_REGISTRY_APP_ID`, `EVIDENCE_REGISTRY_APP_ID`, `HELLO_WORLD_APP_ID` | from `deployed_apps.json` | override app ids |
| `ALGOD_SERVER`, `ALGOD_PORT`, `ALGOD_TOKEN` | LocalNet | Algorand node (standard AlgoKit variables) |
| `DEPLOYER_MNEMONIC` | LocalNet account | signing account; must be the account that deployed the contracts |
| `CHAIN_AUTO_FUND` | `1` | top up app accounts for box storage |

## API overview

| Method and path | Purpose |
|---|---|
| `POST /logistics-objects` | register a shipment |
| `POST /logistics-objects/{shipment}/logistics-events` | a party's event (`cargo:LogisticsEvent`) |
| `POST /logistics-objects/{shipment}/party-reports` | a party's version of the shipment details |
| `GET /conflicts?type=&severity=&status=&shipment=&q=` | list conflicts |
| `GET /conflicts/{id}` | both records, values, evidence, Algorand transactions, history |
| `PATCH /conflicts/{id}/resolve` | body `{"acceptedRecord", "note", "resolvedBy"}`, all optional |
| `GET /conflicts/{id}/verify` | recompute the conflict hash and compare with Algorand |
| `POST /conflicts/{id}/evidence` | `{"partyOrg", "filename", "contentBase64", "note"}` |
| `POST /evidence/{id}/verify` | check a file against the stored and on-chain hash |
| `GET /metrics`, `/metrics/live`, `/metrics/evaluation`, `/metrics/chain-benchmark` | counters and measurements |
| `GET /chain/status`, `/chain/transactions` | anchoring status |
| `GET /dashboard` | the dashboard |
| `GET /docs` | interactive API documentation (FastAPI) |

Shipment ids are URLs, so encode them in paths when calling the API by hand, for example with `[uri]::EscapeDataString($id)` in PowerShell. The dashboard and simulator do this automatically.

## Troubleshooting

- **`No module named 'api'`:** run commands from `projects\aircargo-provenance`. `pyproject.toml` already sets the test path.
- **"No app id for conflict_registry"** in algorand mode: run `algokit project deploy localnet` first, or set the `*_APP_ID` variables.
- **Anchoring shows "Anchor failed":** see the error on the Algorand page. Usually LocalNet isn't running, or `DEPLOYER_MNEMONIC` is not the account that deployed the contracts (writes are deployer-only).
- **Dashboard fonts look plain:** the Barlow fonts load from Google Fonts. Offline, the page falls back to system fonts and still works.
