"""Local Solana wallet loading. The private key is read from a file that
lives only on this machine (see .gitignore — the wallet/ dir is never
committed) and never leaves this process except to sign transactions
locally. Ported from wallet.js.

Uses `solders` for the keypair object and a minimal raw JSON-RPC client
(httpx) for talking to a Solana RPC endpoint — see the note at the top of
live_trader.py about why raw JSON-RPC was chosen over a Python RPC wrapper
library for this port.
"""

import json
import os

import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey


class WalletError(Exception):
    pass


_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(s: str) -> bytes:
    """Decodes base58 without an extra dependency — Phantom/Solflare's
    "Export Private Key" gives you a plain base58 string, not a JSON array."""
    num = 0
    for char in s:
        idx = _BASE58_ALPHABET.find(char)
        if idx == -1:
            raise ValueError(f"'{char}' is not a valid base58 character")
        num = num * 58 + idx
    n_bytes = (num.bit_length() + 7) // 8
    combined = num.to_bytes(n_bytes, "big")
    n_leading_zeros = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_leading_zeros + combined


def load_wallet(keypair_path: str) -> Keypair:
    if not os.path.exists(keypair_path):
        raise WalletError(
            f"No keypair file found at {keypair_path}. Export your private key from your wallet "
            f"(Phantom/Solflare: Settings > Export Private Key) and save it into this file exactly as given "
            f'— either their base58 string or a JSON array both work — or generate one with the Solana CLI '
            f'("solana-keygen new -o {keypair_path}"), fund it with a small amount of SOL, then try again.'
        )
    try:
        with open(keypair_path, "r", encoding="utf-8") as f:
            raw_text = f.read().strip()
    except Exception as e:
        raise WalletError(f"Could not read {keypair_path}: {e}")

    # Solana CLI / this bot's own format: a JSON array of 64 secret-key bytes.
    if raw_text.startswith("["):
        try:
            raw = json.loads(raw_text)
        except Exception as e:
            raise WalletError(f"Could not parse {keypair_path} as JSON: {e}")
        if not isinstance(raw, list) or len(raw) != 64:
            raise WalletError(f"{keypair_path} does not look like a Solana secret key (expected a JSON array of 64 numbers).")
        try:
            return Keypair.from_bytes(bytes(raw))
        except Exception as e:
            raise WalletError(f"Invalid keypair bytes in {keypair_path}: {e}")

    # Phantom/Solflare "Export Private Key" format: a single base58 string.
    try:
        decoded = _base58_decode(raw_text)
    except Exception as e:
        raise WalletError(
            f"{keypair_path} doesn't look like a valid Solana secret key. It should be either a JSON array of "
            f"64 numbers (Solana CLI format) or a single base58 string (Phantom/Solflare export format). ({e})"
        )
    if len(decoded) != 64:
        raise WalletError(f"{keypair_path} decoded to {len(decoded)} bytes, expected 64 — this doesn't look like a valid secret key.")
    try:
        return Keypair.from_bytes(decoded)
    except Exception as e:
        raise WalletError(f"Invalid keypair bytes in {keypair_path}: {e}")


class RpcConnection:
    """Minimal Solana JSON-RPC client — just the handful of methods
    live_trader.py needs (getBalance, simulateTransaction, sendTransaction,
    getSignatureStatuses, getLatestBlockhash). Talking to the raw RPC spec
    directly instead of a Python wrapper library, since the JSON-RPC method
    shapes are a long-stable public spec (https://solana.com/docs/rpc) —
    more reliable to write from memory than a wrapper library's exact
    version-specific Python API.
    """

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    async def _call(self, method: str, params: list):
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise WalletError(f"RPC error from {method}: {body['error']}")
        return body["result"]

    async def get_balance(self, pubkey: Pubkey) -> int:
        result = await self._call("getBalance", [str(pubkey), {"commitment": "confirmed"}])
        return result["value"]

    async def get_latest_blockhash(self) -> dict:
        result = await self._call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return result["value"]

    async def simulate_transaction(self, tx_b64: str) -> dict:
        result = await self._call("simulateTransaction", [tx_b64, {"sigVerify": True, "commitment": "confirmed", "encoding": "base64"}])
        return result["value"]

    async def send_transaction(self, tx_b64: str) -> str:
        return await self._call("sendTransaction", [tx_b64, {"skipPreflight": True, "maxRetries": 3, "encoding": "base64"}])

    async def get_signature_status(self, signature: str) -> dict | None:
        result = await self._call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        values = result["value"]
        return values[0] if values else None


def make_connection(rpc_url: str) -> RpcConnection:
    return RpcConnection(rpc_url)


async def get_sol_balance(connection: RpcConnection, pubkey: Pubkey) -> float:
    lamports = await connection.get_balance(pubkey)
    return lamports / 1e9
