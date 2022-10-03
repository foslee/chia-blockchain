import math

from typing import Any, Dict, List, Optional, Tuple

from chia.types.announcement import Announcement
from chia.types.blockchain_format.coin import Coin, coin_as_list
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.ints import uint16, uint64
from chia.wallet.outer_puzzles import AssetType
from chia.wallet.payment import Payment
from chia.wallet.puzzle_drivers import cast_to_int, PuzzleInfo, Solver
from chia.wallet.puzzles.puzzle_utils import (
    make_assert_coin_announcement,
    make_create_coin_announcement,
    make_create_coin_condition,
    make_create_puzzle_announcement,
    make_reserve_fee_condition,
)
from chia.wallet.trading.offer import OFFER_MOD
from chia.wallet.trading.offer_dependencies import ADD_CONDITIONS, DEPENDENCY_WRAPPERS, DLDataInclusion, OfferDependency, RequestedPayment
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.wallet_protocol import WalletProtocol


async def old_request_to_new(
    wallet_state_manager: Any,
    offer_dict: Dict[Optional[bytes32], int],
    driver_dict: Dict[bytes32, PuzzleInfo],
    solver: Solver,
    fee: uint64,
) -> Tuple[Solver, Dict[bytes32, PuzzleInfo]]:
    final_solver: Dict[str, Any] = solver.info

    offered_assets: Dict[Optional[bytes32], int] = {k: v for k, v in offer_dict.items() if v < 0}
    requested_assets: Dict[Optional[bytes32], int] = {k: v for k, v in offer_dict.items() if v > 0}

    # When offers first came out, they only supported CATs and driver_dict did not exist
    # We need to fill in any requested assets that do not exist in driver_dict already as CATs
    cat_assets: Dict[bytes32, PuzzleInfo] = {
        key: PuzzleInfo({"type": AssetType.CAT.value, "tail": "0x" + key.hex()})
        for key in requested_assets
        if key is not None and key not in driver_dict
    }
    driver_dict.update(cat_assets)

    # Keep track of the DL assets since they show up under the offered asset's name
    dl_dependencies: List[Solver] = []

    if "assets" not in final_solver:
        final_solver.setdefault("assets", [])
        for asset_id, amount in offered_assets.items():
            # We're passing everything in as a dictionary now instead of a single asset_id/amount pair
            if asset_id is None:
                offered_asset: Dict[str, Any] = {"asset_id": "()"}
                wallet = wallet_state_manager.main_wallet
            else:
                offered_asset = {"asset_id": "0x" + asset_id.hex()}
                wallet = await wallet_state_manager.get_wallet_for_asset_id(asset_id.hex())

            # We need to fill in driver dict entries that we can and raise on discrepencies
            if callable(getattr(wallet, "get_puzzle_info", None)):
                puzzle_driver: PuzzleInfo = await wallet.get_puzzle_info(asset_id)
                if asset_id in driver_dict and driver_dict[asset_id] != puzzle_driver:
                    raise ValueError(f"driver_dict specified {driver_dict[asset_id]}, was expecting {puzzle_driver}")
                else:
                    driver_dict[asset_id] = puzzle_driver
            else:
                raise ValueError(f"Wallet for asset id {asset_id} is not properly integrated for trading")

            if wallet.type() == WalletType.DATA_LAYER:
                try:
                    this_solver: Solver = solver[asset_id.hex()]
                except KeyError:
                    this_solver = solver["0x" + asset_id.hex()]
                # Data Layer offers initially were metadata updates, so we shouldn't allow any kind of sending
                offered_asset["actions"] = [
                    [
                        {
                            "type": "update_state",
                            "update": {
                                # The request used to require "new_root" be in solver so the potential KeyError is good
                                "new_root": "0x"
                                + this_solver["new_root"].hex()
                            },
                        }
                    ],
                    [
                        {
                            "type": "make_announcement",
                            "announcement_type": "puzzle",
                            "announcement_data": "0x24",  # $
                        },
                    ],
                ]

                dl_dependencies.extend(
                    [
                        {
                            "type": "dl_data_inclusion",
                            "launcher_id": "0x" + dep["launcher_id"].hex(),
                            "values_to_prove": ["0x" + v.hex() for v in dep["values_to_prove"]],
                        }
                        for dep in this_solver["dependencies"]
                    ]
                )
            else:
                action_batch = [
                    # This is the parallel to just specifying an amount to offer
                    {
                        "type": "offer_amount",
                        "amount": str(abs(amount)),
                    }
                ]
                # Royalty payments are automatically worked in when you offer fungible assets for an NFT
                if asset_id is None or driver_dict[asset_id].type() != AssetType.SINGLETON.value:
                    action_batch.extend(
                        [
                            {
                                "type": "royalty_payment",
                                "nonce": "0x" + asset_id.hex(),
                                "payment": {
                                    "puzhash": "0x" + payment.address.hex(),
                                    "amount": str(payment.amount),
                                    "memos": ["0x" + memo.hex() for memo in payment.memos],
                                },
                            }
                            for payment in calculate_royalty_payments(requested_assets, abs(amount), driver_dict)
                        ]
                    )
                    if asset_id is None and fee > 0:
                        action_batch.append(
                            {
                                "type": "fee",
                                "amount": str(fee),
                            }
                        )

                # Provenant NFTs by default clear their ownership on transfer
                elif driver_dict[asset].check_type(
                    [
                        AssetType.SINGLETON.value,
                        AssetType.METADATA.value,
                        AssetType.OWNERSHIP.value,
                    ]
                ):
                    action_batch.append(
                        {
                            "type": "update_state",
                            "update": {
                                "new_owner": "()",
                            },
                        }
                    )
                offered_asset["actions"] = [action_batch]

            final_solver["assets"].append(offered_asset)

    # Make sure the fee gets into the solver
    if None not in offer_dict and fee > 0:
        final_solver["assets"].append(
            {
                "asset_id": "()",
                "actions": [
                    [
                        {
                            "type": "fee",
                            "amount": str(fee),
                        }
                    ],
                ],
            }
        )

    # Now lets use the requested items to fill in the bundle dependencies
    if "dependencies" not in final_solver:
        final_solver.setdefault("dependencies", dl_dependencies)
        for asset_id, amount in requested_assets.items():
            if asset_id is None:
                wallet = wallet_state_manager.main_wallet
            else:
                wallet = await wallet_state_manager.get_wallet_for_asset_id(asset_id.hex())

            p2_ph = await wallet_state_manager.main_wallet.get_new_puzzlehash()

            if wallet.type() != WalletType.DATA_LAYER:  # DL singletons are not sent as part of offers by default
                # Asset/amount pairs are assumed to mean requested_payments
                asset_types: List[Solver] = []
                asset_driver = driver_dict[asset_id]
                while True:
                    if asset_driver.type() == AssetType.CAT.value:
                        asset_types.append(
                            Solver(
                                {
                                    "type": AssetType.CAT.value,
                                    "asset_id": asset_driver["tail"],
                                }
                            )
                        )
                    elif asset_driver.type() == AssetType.SINGLETON.value:
                        asset_types.append(
                            Solver(
                                {
                                    "type": AssetType.SINGLETON.value,
                                    "launcher_id": asset_driver["launcher_id"],
                                    "launcher_ph": asset_driver["launcher_ph"],
                                }
                            )
                        )
                    elif asset_driver.type() == AssetType.METADATA.value:
                        asset_types.append(
                            Solver(
                                {
                                    "type": AssetType.METADATA.value,
                                    "metadata": asset_driver["metadata"],
                                    "metadata_updater_hash": asset_driver["updater_hash"],
                                }
                            )
                        )
                    elif asset_driver.type() == AssetType.OWNERSHIP.value:
                        asset_types.append(
                            Solver(
                                {
                                    "type": AssetType.OWNERSHIP.value,
                                    "owner": asset_driver["owner"],
                                    "transfer_program": asset_driver["transfer_program"],
                                }
                            )
                        )

                    if asset_driver.also() is None:
                        break
                    else:
                        asset_driver = asset_driver.also()

                final_solver["dependencies"].append(
                    {
                        "type": "requested_payment",
                        "asset_types": asset_types,
                        "payment": {
                            "puzhash": "0x" + p2_ph.hex(),
                            "amount": str(amount),
                            "memos": ["0x" + p2_ph.hex()],
                        },
                    }
                )

            # Also request the royalty payment as a formality
            if asset_id is None or driver_dict[asset_id].type() != AssetType.SINGLETON.value:
                final_solver["dependencies"].extend(
                    [
                        {
                            "type": "requested_payment",
                            "asset_id": "0x" + asset_id.hex(),
                            "payment": {
                                "puzhash": "0x" + payment.address.hex(),
                                "amount": str(payment.amount),
                                "memos": ["0x" + memo.hex() for memo in payment.memos],
                            },
                        }
                        for payment in calculate_royalty_payments(offered_assets, amount, driver_dict)
                    ]
                )

    # Finally, we need to special case any stuff that the solver was previously used for
    if "solving_information" not in final_solver:
        final_solver.setdefault("solving_information", [])


    return Solver(final_solver), driver_dict


def calculate_royalty_payments(
    requested_assets: Dict[Optional[bytes32], int],
    offered_amount: int,
    driver_dict: Dict[bytes32, PuzzleInfo],
) -> List[Payment]:
    # First, let's take note of all the royalty enabled NFTs
    royalty_nft_assets: List[bytes32] = [
        asset
        for asset in requested_assets
        if asset is not None
        and driver_dict[asset].check_type(  # check if asset is an Royalty Enabled NFT
            [
                AssetType.SINGLETON.value,
                AssetType.METADATA.value,
                AssetType.OWNERSHIP.value,
            ]
        )
    ]

    # Then build what royalty payments we need to make
    royalty_payments: List[Payment] = []
    for asset_id in royalty_nft_assets:
        transfer_info = driver_dict[asset_id].also().also()  # type: ignore
        assert isinstance(transfer_info, PuzzleInfo)
        address: bytes32 = bytes32(transfer_info["transfer_program"]["royalty_address"])
        pts: uint16 = uint16(transfer_info["transfer_program"]["royalty_percentage"])
        extra_royalty_amount = uint64(math.floor(math.floor(offered_amount / len(royalty_nft_assets)) * (pts / 10000)))
        royalty_payments.append(Payment(address, extra_royalty_amount, [address]))

    return royalty_payments


def amount_for_action(total_action: Solver) -> uint64:
    sum: int = 0
    for action in total_action:
        if action["type"] in ["direct_payment", "royalty_payment"]:
            sum += cast_to_int(action["payment"]["amount"])
        elif action["type"] in ["offered_amount", "fee"]:
            sum += cast_to_int(action["amount"])

    return uint64(sum)


def parse_dependency(dependency: Solver, nonce: bytes32) -> OfferDependency:
    if dependency["type"] == "requested_payment":
        payment: Solver = dependency["payment"]
        return RequestedPayment(
            nonce,
            dependency["asset_types"],
            Payment(payment["puzhash"], cast_to_int(payment["amount"]), payment["memos"]),
        )
    elif dependency["type"] == "dl_data_inclusion":
        return DLDataInclusion(nonce, dependency["launcher_id"], dependency["values_to_prove"])


def parse_delegated_puzzles(delegated_puzzle: Program, delegated_solution: Program) -> List[OfferDependency]:
    dependencies: List[OfferDependency] = []
    while True:
        mod, curried_args = delegated_puzzle.uncurry()
        try:
            dependency = DEPENDENCY_WRAPPERS[mod]
        except KeyError:
            raise ValueError(f"Saw a delegated puzzle that we are not aware of {mod}")
        dependencies.append(dependency.from_puzzle(mod, curried_args))
    return dependencies



def sort_coin_list(coins: List[Coin]) -> List[Coin]:
    # This sort should be reproducible in CLVM with `>s`
    return sorted(coins, key=Coin.name)


def select_independent_coin(coins: List[Coin]) -> Coin:
    return sort_coin_list(coins)[0]


def nonce_coin_list(coins: List[Coin]) -> bytes32:
    sorted_coin_list: List[List[Union[bytes32, uint64]]] = [coin_as_list(c) for c in coins]
    return Program.to(sorted_coin_list).get_tree_hash()


def create_delegated_puzzles_for_actions(
    coins: List[Coin],
    all_actions: List[Solver],
    wallet: WalletProtocol,
    independent_coin: Coin,
    depend_on_coin: Coin,
    bundle_nonce: bytes32,
) -> Dict[Coin, Tuple[Program, Program]]:
    delegated_puzs_and_sols: Dict[Coin, Tuple[Program, Program]] = {}
    for total_action in all_actions:
        group_nonce: bytes32 = nonce_coin_list(sort_coin_list(coins))
        new_coins: List[Coin] = []
        for coin in coins:
            condition_list = []
            unknown_actions = []
            if coin == independent_coin:  # One coin will be the main coin that creates all the conditions
                total_sum: int = sum(c.amount for c in coins)
                amount_output: int = 0
                for action in total_action:
                    # An announcement for other coins of this type to depend on
                    condition_list.append(make_create_coin_announcement(group_nonce))
                    # An announcement for other coins in the same bundle to depend on
                    condition_list.append(make_create_coin_announcement(bundle_nonce))
                    # Depend on the next coin in the bundle
                    condition_list.append(
                        make_assert_coin_announcement(Announcement(depend_on_coin.name(), bundle_nonce).name())
                    )

                    # Add conditions for each type of action
                    if action["type"] == "direct_payment":
                        payment = action["payment"]
                        condition_list.append(
                            make_create_coin_condition(
                                payment["puzhash"], cast_to_int(payment["amount"]), payment["memos"]
                            )
                        )
                        if "ours" in action and action["ours"] != Program.to(None):
                            new_coins.append(Coin(coin.name(), payment["puzhash"], payment["amount"]))
                        amount_output += cast_to_int(payment["amount"])
                    elif action["type"] in ["offered_amount", "royalty_payment"]:
                        condition_list.append(
                            make_create_coin_condition(OFFER_MOD.get_tree_hash(), cast_to_int(action["amount"]), None)
                        )
                        amount_output += cast_to_int(payment["amount"])
                        if action["type"] == "royalty_payment":
                            royalty_coin = Coin(coin.name(), OFFER_MOD.get_tree_hash(), cast_to_int(payment["amount"]))
                            delegated_puzs_and_sols[royalty_coin] = (
                                Program.to(None),
                                Program.to(
                                    [
                                        (
                                            group_nonce,
                                            [payment["puzhash"], cast_to_int(payment["amount"]), payment["memos"]],
                                        )
                                    ],
                                ),
                            )
                            if "ours" in action and action["ours"] != Program.to(None):
                                new_coins.append(
                                    Coin(royalty_coin.name(), payment["puzhash"], cast_to_int(payment["amount"]))
                                )
                    elif action["type"] == "fee":
                        condition_list.append(make_reserve_fee_condition(cast_to_int(action["amount"])))
                        amount_output += cast_to_int(action["amount"])
                    elif action["type"] == "make_announcement":
                        if action["announcement_type"] == "coin":
                            condition_list.append(make_create_coin_announcement(action["announcement_data"]))
                        elif action["announcement_type"] == "puzzle":
                            condition_list.append(make_create_puzzle_announcement(action["announcement_data"]))
                        else:
                            raise ValueError(f"No known announcement type: {action['announcement_type']}")
                    else:
                        unknown_actions.append(action)

                if total_sum > amount_output:  # Change required
                    condition_list.append(
                        make_create_coin_condition(coin.puzzle_hash, uint64(total_sum - amount_output), None)
                    )
            else:
                # Depend on the independent coin
                condition_list.append(
                    make_assert_coin_announcement(Announcement(independent_coin.name(), group_nonce).name())
                )

            delegated_puzzle: Program = Program.to((1, condition_list))
            delegated_solution: Program = Program.to(None)
            delegated_puzzle, delegated_solution = wallet.handle_unknown_actions(
                unknown_actions, delegated_puzzle, delegated_solution
            )
            delegated_puzs_and_sols[coin] = (delegated_puzzle, delegated_solution)

            coins = new_coins
            independent_coin = select_independent_coin(new_coins)

    return delegated_puzs_and_sols


async def build_offer() -> None:
    new_solver, new_driver_dict = await old_request_to_new(
        wallet_state_manager,
        offer_dict,
        driver_dict,
        solver,
        fee,
    )

    asset_to_coins: Dict[Optional[bytes32], List[Coin]] = {}
    for asset in new_solver["assets"]:
        # Get the relevant wallet
        asset_id: Optional[bytes32] = None if asset["asset_id"] == Program.to(None) else bytes32(asset["asset_id"])
        if asset_id is None:
            outer_wallet = wallet_state_manager.main_wallet
        else:
            outer_wallet = await wallet_state_manager.get_wallet_for_asset_id(asset_id.hex())

        # Get the coins for the first action (subsequent actions use outputs from the previous spend)
        need_amount: uint64 = amount_for_action(asset["actions"][0])
        coins: List[Coin] = list(await outer_wallet.get_coins_to_offer(asset_id, need_amount, min_coin_amount))
        asset_to_coins[asset_id] = coins

    all_coins: List[Coin] = [coin for coins in asset_to_coins.values() for coin in coins]
    bundle_nonce: bytes32 = nonce_coin_list(sort_coin_list(all_coins))

    dependencies: List[OfferDependency] = [
        parse_dependency(dep, bundle_nonce) for dep in new_solver["dependencies"]
    ]

    for i, asset in enumerate(new_solver["assets"]):
        # Get the relevant wallet
        asset_id: Optional[bytes32] = None if asset["asset_id"] == Program.to(None) else bytes32(asset["asset_id"])
        coins = asset_to_coins[asset_id]
        independent_coin: Coin = select_independent_coin(coins)

        next_index: int = 0 if i == len(new_solver["assets"]) - 1 else i + 1
        next_asset: Solver = new_solver["assets"][next_index]
        next_asset_id: Optional[bytes32] = (
            None if next_asset["asset_id"] == Program.to(None) else bytes32(next_asset["asset_id"])
        )
        next_coins: List[Coin] = asset_to_coins[next_asset_id]
        next_independent_coin: Coin = select_independent_coin(next_coins)

        if asset_id is None:
            outer_wallet = wallet_state_manager.main_wallet
        else:
            outer_wallet = await wallet_state_manager.get_wallet_for_asset_id(asset_id.hex())

        delegated_puzs_and_sols: Dict[Coin, Tuple[Program, Program]] = create_delegated_puzzles_for_actions(
            coins,
            asset["actions"],
            outer_wallet,
            independent_coin,
            next_independent_coin,
            bundle_nonce,
        )

        if i == 0:
            independent_delegated_puz, independent_delegated_sol = delegated_puzs_and_sols[independent_coin]
            for dep in dependencies:
                independent_delegated_puz, independent_delegated_sol = dep.apply(
                    independent_delegated_puz, independent_delegated_sol
                )
            delegated_puzs_and_sols[independent_coin] = (independent_delegated_puz, independent_delegated_sol)

        coin_spends: List[CoinSpend] = await outer_wallet.solve_coins_with_delegated_puzzles(
            delegated_puzs_and_sols
        )