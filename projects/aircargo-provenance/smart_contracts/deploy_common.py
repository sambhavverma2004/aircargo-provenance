"""Shared deploy logic: deploy (idempotently), fund the app account for box
storage, and record the app id in smart_contracts/artifacts/deployed_apps.json
so the API (api/chain.py) knows which apps to call."""

import json
import logging
from pathlib import Path

import algokit_utils

logger = logging.getLogger(__name__)

DEPLOYED_APPS_FILE = Path(__file__).resolve().parent / "artifacts" / "deployed_apps.json"


def network_name(algorand: algokit_utils.AlgorandClient) -> str:
    if algorand.client.is_localnet():
        return "localnet"
    if algorand.client.is_testnet():
        return "testnet"
    if algorand.client.is_mainnet():
        return "mainnet"
    return "custom"


def record_app_id(network: str, folder: str, app_id: int) -> None:
    data = {}
    if DEPLOYED_APPS_FILE.exists():
        data = json.loads(DEPLOYED_APPS_FILE.read_text())
    data.setdefault(network, {})[folder] = app_id
    DEPLOYED_APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEPLOYED_APPS_FILE.write_text(json.dumps(data, indent=2) + "\n")


def deploy_typed(folder: str, factory_class, fund_algo: float = 5.0) -> None:
    algorand = algokit_utils.AlgorandClient.from_environment()
    deployer = algorand.account.from_environment("DEPLOYER")

    factory = algorand.client.get_typed_app_factory(
        factory_class, default_sender=deployer.address
    )
    app_client, result = factory.deploy(
        on_update=algokit_utils.OnUpdate.AppendApp,
        on_schema_break=algokit_utils.OnSchemaBreak.AppendApp,
    )

    # Box storage is paid from the app account's minimum balance.
    algorand.account.ensure_funded(
        app_client.app_address,
        deployer,
        algokit_utils.AlgoAmount(algo=fund_algo),
    )

    network = network_name(algorand)
    record_app_id(network, folder, app_client.app_id)
    logger.info(
        f"{app_client.app_name} on {network}: app id {app_client.app_id}, "
        f"address {app_client.app_address} ({result.operation_performed.name}); "
        f"recorded in {DEPLOYED_APPS_FILE.name}"
    )
