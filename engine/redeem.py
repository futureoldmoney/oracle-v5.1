"""
Redeem v5 — Convert winning outcome tokens back to USDC.
Called via Discord: !redeem or as standalone: python -m engine.redeem
"""

import os
import sys
import httpx
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account


USDC_ADDR = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CT_ADDR = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ABI = [{"constant": True, "inputs": [{"name": "owner", "type": "address"}],
             "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
             "type": "function"}]
CT_ABI = [
    {"inputs": [{"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"}],
     "name": "redeemPositions", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"},
                {"name": "id", "type": "uint256"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]


def main():
    pk = os.environ.get("PRIVATE_KEY", "")
    rpc = os.environ.get("POLYGON_RPC_URL", "")
    if not pk or not rpc:
        print("ERROR: Missing PRIVATE_KEY or POLYGON_RPC_URL")
        return

    eoa = Account.from_key(pk).address
    w3 = Web3(Web3.HTTPProvider(rpc))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDR), abi=USDC_ABI)
    bal_before = usdc.functions.balanceOf(Web3.to_checksum_address(eoa)).call()
    print(f"EOA: {eoa}")
    print(f"USDC before: ${bal_before / 1e6:.2f}")

    # Find resolved condition IDs from Supabase
    from supabase import create_client
    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    # Get recently settled trades
    resp = sb.table("live_settled").select(
        "market_id, won"
    ).eq("won", True).order("settled_at", desc=True).limit(20).execute()

    condition_ids = set()
    for row in (resp.data or []):
        cid = row.get("market_id")
        if cid:
            condition_ids.add(cid)

    if not condition_ids:
        print("No winning trades to redeem")
        return

    ct = w3.eth.contract(address=Web3.to_checksum_address(CT_ADDR), abi=CT_ABI)
    parent = b'\x00' * 32
    redeemed = 0

    for cid in condition_ids:
        try:
            cid_bytes = bytes.fromhex(cid.replace("0x", ""))
            tx = ct.functions.redeemPositions(
                parent, cid_bytes, [1, 2]
            ).build_transaction({
                "from": eoa,
                "gas": 300_000,
                "gasPrice": w3.eth.gas_price,
                "nonce": w3.eth.get_transaction_count(eoa),
            })
            signed = w3.eth.account.sign_transaction(tx, pk)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            status = "✓" if receipt.status == 1 else "✗"
            print(f"  {status} Redeem {cid[:12]}... tx={tx_hash.hex()[:16]}...")
            redeemed += 1
        except Exception as e:
            print(f"  ✗ Redeem {cid[:12]}... error: {e}")

    bal_after = usdc.functions.balanceOf(Web3.to_checksum_address(eoa)).call()
    gained = (bal_after - bal_before) / 1e6
    print(f"\nRedeemed {redeemed} positions")
    print(f"USDC after: ${bal_after / 1e6:.2f} (+${gained:.2f})")


if __name__ == "__main__":
    main()
