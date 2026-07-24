"""Real trade execution against pump.fun — non-custodial. This module never
sends your private key anywhere. It asks PumpPortal (pumpportal.fun) to
build an unsigned transaction for the requested trade, signs it locally
with your own keypair, simulates it, then submits it to Solana via your
own RPC connection. Ported from live-trader.js.

IMPORTANT — two things in this file were written from recalled documented
behavior, without the ability to `pip install` and test against the real
network from the sandbox this was written in:

1. PumpPortal's "local transaction" trade API (POST /api/trade-local,
   returns a serialized transaction to sign yourself). Verify the field
   names below still match https://pumpportal.fun/local-trading-api/trading-api/
   before trusting this with real funds.
2. The `solders` Python API used to deserialize/sign/serialize the
   transaction (VersionedTransaction.from_bytes / the signing constructor /
   __bytes__). This mirrors solders' documented usage patterns, but if your
   installed solders version differs, an AttributeError here will point
   exactly at what changed — check https://github.com/kevinheavey/solders
   for the current API if that happens.

Test with a trivial amount first, regardless.
"""

import asyncio
import base64

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from wallet import RpcConnection

TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"
CONFIRM_POLL_INTERVAL_SEC = 1.5
CONFIRM_TIMEOUT_SEC = 45


class TradeError(Exception):
    pass


async def execute_trade(
    *,
    connection: RpcConnection,
    wallet: Keypair,
    mint: str,
    action: str,  # "buy" | "sell"
    amount,  # SOL amount (denominated_in_sol) or token amount / "100%" (sell)
    denominated_in_sol: bool,
    slippage_pct: float,
    priority_fee_sol: float,
    pool: str = "auto",
) -> dict:
    payload = {
        "publicKey": str(wallet.pubkey()),
        "action": action,
        "mint": mint,
        "amount": amount,
        "denominatedInSol": "true" if denominated_in_sol else "false",
        "slippage": slippage_pct,
        "priorityFee": priority_fee_sol,
        "pool": pool,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(TRADE_LOCAL_URL, json=payload)
    except httpx.HTTPError as e:
        raise TradeError(f"Could not reach PumpPortal to build the trade: {e}")

    if res.status_code != 200:
        raise TradeError(f"PumpPortal rejected the trade request ({res.status_code}): {res.text[:300]}")

    try:
        unsigned_tx = VersionedTransaction.from_bytes(res.content)
    except Exception as e:
        raise TradeError(f"Could not parse the transaction PumpPortal returned — its API may have changed: {e}")

    try:
        signed_tx = VersionedTransaction(unsigned_tx.message, [wallet])
    except Exception as e:
        raise TradeError(f"Could not sign the transaction locally: {e}")

    tx_b64 = base64.b64encode(bytes(signed_tx)).decode("ascii")

    # Simulate before broadcasting — catches bad instructions, insufficient
    # balance, stale blockhash, etc. without risking funds or a wasted fee.
    sim = await connection.simulate_transaction(tx_b64)
    if sim.get("err"):
        logs = sim.get("logs") or []
        raise TradeError(f"Simulation failed, not sending: {sim['err']} — logs: {' | '.join(logs[-5:])}")

    try:
        signature = await connection.send_transaction(tx_b64)
    except Exception as e:
        raise TradeError(f"Failed to submit transaction: {e}")

    confirmed_err = await _await_confirmation(connection, signature)
    if confirmed_err is not None:
        raise TradeError(f"Transaction landed but failed on-chain: {confirmed_err} — https://solscan.io/tx/{signature}")

    return {"signature": signature, "explorerUrl": f"https://solscan.io/tx/{signature}"}


async def _await_confirmation(connection: RpcConnection, signature: str):
    """Polls getSignatureStatuses until confirmed/finalized or timeout.
    Returns the on-chain error object (or None if it landed cleanly)."""
    elapsed = 0.0
    while elapsed < CONFIRM_TIMEOUT_SEC:
        status = await connection.get_signature_status(signature)
        if status is not None:
            if status.get("err") is not None:
                return status["err"]
            if status.get("confirmationStatus") in ("confirmed", "finalized"):
                return None
        await asyncio.sleep(CONFIRM_POLL_INTERVAL_SEC)
        elapsed += CONFIRM_POLL_INTERVAL_SEC
    raise TradeError(f"Timed out waiting for confirmation of {signature} — check https://solscan.io/tx/{signature} manually.")
