from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import replace
from typing import Any, AsyncIterator, Dict, List, Literal, Tuple

import pytest

from chia.consensus.constants import ConsensusConstants
from chia.consensus.cost_calculator import NPCResult
from chia.full_node.full_node import FullNode
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.peer_info import PeerInfo
from chia.util.ints import uint32, uint64, uint128
from chia.wallet.util.tx_config import DEFAULT_TX_CONFIG, TXConfig
from chia.wallet.wallet_node import Balance
from chia.wallet.wallet_state_manager import WalletStateManager
from tests.environments.wallet import WalletEnvironment, WalletState, WalletTestFramework
from tests.util.setup_nodes import setup_simulators_and_wallets_service
from tests.wallet.wallet_block_tools import WalletBlockTools


@pytest.fixture(scope="function", autouse=True)
def block_is_current_at(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mocked_synced(self: Any, block_is_current_at: int = 0) -> bool:
        return await original_synced(self, block_is_current_at)

    original_synced = WalletStateManager.synced

    monkeypatch.setattr(WalletStateManager, "synced", mocked_synced)


@pytest.fixture(scope="function", autouse=True)
def ignore_block_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def validate_block_body(*args: Any, **kwargs: Any) -> Tuple[Literal[None], NPCResult]:
        return None, args[7]

    def create_wrapper(original_create: Any) -> Any:
        async def new_create(*args: Any, **kwargs: Any) -> Any:
            # Modify the config argument directly since it's a mutable dictionary
            if "config" in kwargs:
                kwargs["config"]["single_threaded"] = True
            else:
                args[0]["single_threaded"] = True  # Assuming config is the first positional argument

            # Call the original function with modified arguments
            full_node = await original_create(*args, **kwargs)
            return full_node

        return new_create

    monkeypatch.setattr("chia.simulator.block_tools.BlockTools", WalletBlockTools)
    monkeypatch.setattr(FullNode, "create", create_wrapper(FullNode.create))
    monkeypatch.setattr("chia.consensus.blockchain.validate_block_body", validate_block_body)
    monkeypatch.setattr(
        "chia.consensus.block_header_validation.validate_unfinished_header_block", lambda *_, **__: (uint64(1), None)
    )
    monkeypatch.setattr(
        "chia.wallet.wallet_blockchain.validate_finished_header_block", lambda *_, **__: (uint64(1), None)
    )
    monkeypatch.setattr(
        "chia.consensus.multiprocess_validation.validate_finished_header_block", lambda *_, **__: (uint64(1), None)
    )
    monkeypatch.setattr(
        "chia.consensus.multiprocess_validation.verify_and_get_quality_string", lambda *_, **__: bytes32([0] * 32)
    )
    monkeypatch.setattr("chia.consensus.block_record.BlockRecord.sp_total_iters", lambda *_: uint128(0))
    monkeypatch.setattr("chia.consensus.block_record.BlockRecord.ip_sub_slot_total_iters", lambda *_: uint128(0))
    monkeypatch.setattr("chia.consensus.make_sub_epoch_summary.calculate_sp_iters", lambda *_: uint64(0))
    monkeypatch.setattr("chia.consensus.make_sub_epoch_summary.calculate_ip_iters", lambda *_: uint64(0))


@pytest.fixture(scope="function", params=[True, False])
def trusted_full_node(request: Any) -> bool:
    trusted: bool = request.param
    return trusted


@pytest.fixture(scope="function", params=[True, False])
def tx_config(request: Any) -> TXConfig:
    return replace(DEFAULT_TX_CONFIG, reuse_puzhash=request.param)


# This fixture automatically creates 4 parametrized tests trusted/untrusted x reuse/new derivations
# These parameterizations can be skipped by manually specifying "trusted" or "reuse puzhash" to the fixture
@pytest.fixture(scope="function")
async def wallet_environments(
    trusted_full_node: bool,
    tx_config: TXConfig,
    blockchain_constants: ConsensusConstants,
    request: pytest.FixtureRequest,
) -> AsyncIterator[WalletTestFramework]:
    if "trusted" in request.param:
        if request.param["trusted"] != trusted_full_node:
            pytest.skip("Skipping not specified trusted mode")
    if "reuse_puzhash" in request.param:
        if request.param["reuse_puzhash"] != tx_config.reuse_puzhash:
            pytest.skip("Skipping not specified reuse_puzhash mode")
    assert len(request.param["blocks_needed"]) == request.param["num_environments"]
    if "config_overrides" in request.param:
        config_overrides: Dict[str, Any] = request.param["config_overrides"]
    else:  # pragma: no cover
        config_overrides = {}
    async with setup_simulators_and_wallets_service(
        1, request.param["num_environments"], blockchain_constants
    ) as wallet_nodes_services:
        full_node, wallet_services, bt = wallet_nodes_services

        full_node[0]._api.full_node.config = {**full_node[0]._api.full_node.config, **config_overrides}

        rpc_clients: List[WalletRpcClient] = []
        async with AsyncExitStack() as astack:
            for service in wallet_services:
                service._node.config = {
                    **service._node.config,
                    "trusted_peers": {full_node[0]._api.server.node_id.hex(): full_node[0]._api.server.node_id.hex()}
                    if trusted_full_node
                    else {},
                    **config_overrides,
                }
                service._node.wallet_state_manager.config = service._node.config
                await service._node.server.start_client(
                    PeerInfo(bt.config["self_hostname"], full_node[0]._api.full_node.server.get_port()), None
                )
                rpc_clients.append(
                    await astack.enter_async_context(
                        WalletRpcClient.create_as_context(
                            bt.config["self_hostname"],
                            # Semantics guarantee us a non-None value here
                            service.rpc_server.listen_port,  # type: ignore[union-attr]
                            service.root_path,
                            service.config,
                        )
                    )
                )

            wallet_states: List[WalletState] = []
            for service, blocks_needed in zip(wallet_services, request.param["blocks_needed"]):
                await full_node[0]._api.farm_blocks_to_wallet(
                    count=blocks_needed, wallet=service._node.wallet_state_manager.main_wallet
                )
                await full_node[0]._api.wait_for_wallet_synced(wallet_node=service._node, timeout=20)
                wallet_states.append(
                    WalletState(
                        Balance(
                            confirmed_wallet_balance=uint128(2_000_000_000_000 * blocks_needed),
                            unconfirmed_wallet_balance=uint128(2_000_000_000_000 * blocks_needed),
                            spendable_balance=uint128(2_000_000_000_000 * blocks_needed),
                            pending_change=uint64(0),
                            max_send_amount=uint128(2_000_000_000_000 * blocks_needed),
                            unspent_coin_count=uint32(2 * blocks_needed),
                            pending_coin_removal_count=uint32(0),
                        ),
                    )
                )

            yield WalletTestFramework(
                full_node[0]._api,
                trusted_full_node,
                [
                    WalletEnvironment(
                        service=service,
                        rpc_client=rpc_client,
                        wallet_states={uint32(1): wallet_state},
                    )
                    for service, rpc_client, wallet_state in zip(wallet_services, rpc_clients, wallet_states)
                ],
                tx_config,
            )
