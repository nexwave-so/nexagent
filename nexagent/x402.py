"""
x402 pay-per-signal support for Nexwave.

Implements the x402 exact scheme for Solana (SVM) per:
  https://github.com/coinbase/x402/blob/main/specs/schemes/exact/scheme_exact_svm.md

Flow: server returns HTTP 402 with payment requirements → client builds a
partially-signed Versioned Solana transaction (SPL TransferChecked + Memo +
Compute Budget) → client POSTs it back in the X-Payment header → facilitator
adds its feePayer signature and broadcasts.

Requires: nexagent[x402]  →  solders>=0.21, base58>=2
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import struct
from typing import Any

import httpx

from .config import Config

logger = logging.getLogger(__name__)

# ── Solana program IDs ──────────────────────────────────────────────────────
_SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
_ASSOCIATED_TOKEN = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJe1bRS"
_COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
_MEMO = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

# ── Well-known token mints ──────────────────────────────────────────────────
_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
_USDC_DECIMALS = 6

# ── RPC ─────────────────────────────────────────────────────────────────────
_MAINNET_RPC = "https://api.mainnet-beta.solana.com"


# ── Public entry point ───────────────────────────────────────────────────────

async def sign_and_pay(
    payment_required_resp: httpx.Response,
    config: Config,
) -> str:
    """
    Build a partially-signed Solana transaction for the x402 exact scheme
    and return the base64-encoded PaymentPayload JSON for the X-Payment header.
    """
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction

    req = _parse_payment_required(payment_required_resp)
    logger.debug("x402 payment requirements: %s", req)

    # ── Parse requirements ───────────────────────────────────────────────────
    network = req.get("network", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp")
    amount = int(req.get("amount", req.get("maxAmountRequired", "0")))
    asset_str = req.get("asset", _USDC_MINT)
    pay_to_str = req.get("payTo", req.get("pay_to", ""))
    extra = req.get("extra", {})
    fee_payer_str = extra.get("feePayer", pay_to_str)
    memo_val = extra.get("memo") or secrets.token_hex(16)  # random nonce per spec
    resource_url = req.get("resource", config.nexwave_signals_url)

    # ── Keys ─────────────────────────────────────────────────────────────────
    keypair = _load_keypair(config.nexwave_x402_private_key)
    our_pubkey = keypair.pubkey()
    fee_payer = Pubkey.from_string(fee_payer_str)
    pay_to = Pubkey.from_string(pay_to_str)
    mint = Pubkey.from_string(asset_str)

    # Pick token program (default SPL Token; switch to Token-2022 if needed)
    token_program_str = _TOKEN_2022 if asset_str.startswith("So1") else _SPL_TOKEN
    token_program = Pubkey.from_string(token_program_str)

    # ── Derive ATAs ──────────────────────────────────────────────────────────
    source_ata = _get_ata(our_pubkey, mint, token_program)
    dest_ata = _get_ata(pay_to, mint, token_program)

    # ── Token decimals ───────────────────────────────────────────────────────
    decimals = _USDC_DECIMALS if asset_str == _USDC_MINT else await _fetch_token_decimals(asset_str)

    # ── Recent blockhash ─────────────────────────────────────────────────────
    rpc_url = getattr(config, "solana_rpc_url", _MAINNET_RPC)
    blockhash_str = await _get_latest_blockhash(rpc_url)
    blockhash = Hash.from_string(blockhash_str)

    # ── Build instructions ───────────────────────────────────────────────────
    instructions = [
        *_compute_budget_ixs(),
        _transfer_checked_ix(
            source_ata, mint, dest_ata, our_pubkey,
            amount, decimals, token_program_str,
        ),
        _memo_ix(memo_val),
    ]

    # ── Assemble and partially sign ──────────────────────────────────────────
    # fee_payer is the facilitator — their slot stays empty (zeros)
    msg = MessageV0.try_compile(
        payer=fee_payer,
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(msg, [keypair])
    tx_b64 = base64.b64encode(bytes(tx)).decode()

    # ── Build PaymentPayload (x402 v2) ───────────────────────────────────────
    payload: dict[str, Any] = {
        "x402Version": 2,
        "resource": {
            "url": resource_url if isinstance(resource_url, str) else str(resource_url),
            "description": req.get("description", "Nexwave signal access"),
            "mimeType": req.get("mimeType", "application/json"),
        },
        "accepted": req,
        "payload": {"transaction": tx_b64},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_keypair(private_key_str: str):
    """
    Load a Solana Keypair from:
      - base58-encoded 64-byte keypair  (~88 chars, Phantom "Export Private Key")
      - base58-encoded 32-byte seed     (~44 chars)
      - JSON array of 64 integers       (Solana CLI keygen format)
    """
    import base58
    from solders.keypair import Keypair

    key_str = private_key_str.strip()
    if key_str.startswith("["):
        key_bytes = bytes(json.loads(key_str))
    else:
        try:
            key_bytes = base58.b58decode(key_str)
        except Exception:
            raise ValueError(
                "NEXWAVE_X402_PRIVATE_KEY must be base58-encoded (Phantom export: ~88 chars) "
                "or a JSON array of 64 integers (Solana CLI: [1,2,3,...])."
            )

    if len(key_bytes) == 64:
        return Keypair.from_bytes(key_bytes)
    if len(key_bytes) == 32:
        return Keypair.from_seed(key_bytes)
    raise ValueError(
        f"Solana private key decoded to {len(key_bytes)} bytes; "
        "expected 64 (full keypair) or 32 (seed). "
        "Use Phantom → Settings → Export Private Key (base58, ~88 chars)."
    )


def _get_ata(owner: Any, mint: Any, token_program: Any) -> Any:
    """Derive the Associated Token Account address (PDA)."""
    from solders.pubkey import Pubkey
    ata_program = Pubkey.from_string(_ASSOCIATED_TOKEN)
    seeds = [bytes(owner), bytes(token_program), bytes(mint)]
    ata, _ = Pubkey.find_program_address(seeds, ata_program)
    return ata


def _compute_budget_ixs(compute_units: int = 50_000, micro_lamports: int = 1) -> list:
    """SetComputeUnitLimit + SetComputeUnitPrice instructions."""
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey
    prog = Pubkey.from_string(_COMPUTE_BUDGET)
    limit_data = bytes([2]) + struct.pack("<I", compute_units)   # discriminator 2
    price_data = bytes([3]) + struct.pack("<Q", micro_lamports)  # discriminator 3
    return [
        Instruction(prog, limit_data, []),
        Instruction(prog, price_data, []),
    ]


def _transfer_checked_ix(
    source_ata: Any,
    mint: Any,
    dest_ata: Any,
    authority: Any,
    amount: int,
    decimals: int,
    token_program_str: str = _SPL_TOKEN,
) -> Any:
    """SPL Token TransferChecked instruction (discriminator = 12)."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey
    prog = Pubkey.from_string(token_program_str)
    data = bytes([12]) + struct.pack("<Q", amount) + bytes([decimals])
    accounts = [
        AccountMeta(source_ata, is_signer=False, is_writable=True),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(dest_ata, is_signer=False, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
    ]
    return Instruction(prog, data, accounts)


def _memo_ix(memo: str) -> Any:
    """SPL Memo instruction (ensures transaction uniqueness per x402 SVM spec)."""
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey
    prog = Pubkey.from_string(_MEMO)
    return Instruction(prog, memo.encode("utf-8"), [])


async def _get_latest_blockhash(rpc_url: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash",
                  "params": [{"commitment": "finalized"}]},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["result"]["value"]["blockhash"]


async def _fetch_token_decimals(mint_address: str) -> int:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _MAINNET_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply",
                  "params": [mint_address]},
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json()["result"]["value"]["decimals"]


def _parse_payment_required(resp: httpx.Response) -> dict[str, Any]:
    """Extract PaymentRequirements from a 402 response (header or body)."""
    for header in ("payment-required", "x-payment-required"):
        val = resp.headers.get(header)
        if val:
            try:
                return json.loads(base64.b64decode(val + "=="))
            except Exception:
                try:
                    return json.loads(val)
                except Exception:
                    pass

    try:
        body = resp.json()
        if isinstance(body, dict):
            if "payTo" in body or "pay_to" in body or "scheme" in body:
                return body
            if "paymentRequirements" in body:
                return body["paymentRequirements"]
            # x402 v2 wraps requirements in an array
            if "accepts" in body and isinstance(body["accepts"], list) and body["accepts"]:
                return body["accepts"][0]
    except Exception:
        pass

    raise ValueError(
        "Cannot parse x402 PaymentRequirements from 402 response. "
        f"Status: {resp.status_code}, Headers: {dict(resp.headers)}, "
        f"Body: {resp.text[:400]}"
    )
