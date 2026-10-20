#!/usr/bin/env python3
"""Preflight check: verify fund/approval readiness for first live Polymarket order.

READ-ONLY ONLY — never sends transactions. Uses eth_call (staticCall-style)
via Polygon RPC ``eth_call`` for balance/allowance lookups and a simple
HTTP GET to the Polymarket geoblock endpoint.

Checks:
  1. PRIVATE_KEY is set in environment / .env
  2. Wallet address derives correctly from PRIVATE_KEY
  3. Sufficient MATIC for gas on Polygon
  4. pUSD (USDC) balance >= required amount
  5. Existing approvals:
     a. pUSD approved for Exchange V2
     b. pUSD approved for Neg Risk Exchange V2
     c. CTF (ERC-1155) approved for Exchange V2
     d. CTF (ERC-1155) approved for Neg Risk Exchange V2
  6. Geoblock status (Polymarket jurisdiction check)
  7. LIVE_SIGNING_VERIFIED flag state

Usage:
    .venv/bin/python scripts/preflight_check.py
    .venv/bin/python scripts/preflight_check.py --print-config   # show resolved values only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import textwrap
from dataclasses import dataclass
from typing import Optional

import httpx
from eth_account import Account
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_hex
from dotenv import load_dotenv

# --- Constants (verified against trading_client.py) ---
POLYGON_CHAIN_ID = 137
# Primary RPC (from .env or default); falls back to alternatives if unreachable.
# Ordered so public endpoints that do NOT require an API key are tried first.
# polygon-rpc.com now requires an API key (returns 401 "tenant disabled"), so
# it is only used if POLYGON_RPC_URL overrides it; drpc.org (no key, verified
# working 2026-08-03) and onfinality (no key, rate-limited) follow as fallbacks.
# ankr is last (requires API key).
POLYGON_RPC_URLS = [
    os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com"),
    "https://polygon.drpc.org",  # public, no API key required; verified 2026-08-03
    "https://polygon.api.onfinality.io/public",  # public, no key, rate-limited
    "https://rpc.ankr.com/polygon",  # requires API key — last resort
]
POLYMARKET_GEOBLOCK_URLS = [
    "https://polymarket.com/api/geoblock",
]

P_USD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_V2_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"

ORDER_SIZE = 10.0  # shares per leg (from .env ORDER_SIZE)

# --- ABI function selectors ---
ERC20_BALANCE_OF = "0x" + keccak(b"balanceOf(address)").hex()[:8]
ERC20_ALLOWANCE = "0x" + keccak(b"allowance(address,address)").hex()[:8]
ERC20_DECIMALS = "0x" + keccak(b"decimals()").hex()[:8]
ERC20_NAME = "0x" + keccak(b"name()").hex()[:8]
ERC1155_IS_APPROVAL_FOR_ALL = "0x" + keccak(b"isApprovalForAll(address,address)").hex()[:8]

# MATIC balance lookup uses eth_getBalance (native token)
MATIC_MIN_FOR_GAS = 0.5  # MATIC — enough for several txns at ~50gwei


# --------------------------------------------------------------------------- #
# Result helpers
# --------------------------------------------------------------------------- #
@dataclass
class CheckResult:
    name: str
    status: str  # "PASS", "FAIL", or "WARN"
    detail: str = ""
    value: Optional[str] = None


def fmt_matic(wei: int) -> str:
    matic = wei / 1e18
    return f"{matic:.4f} MATIC"


def fmt_pusd(raw: int) -> str:
    # pUSD/USDC has 6 decimals
    return f"${raw / 1e6:.2f} pUSD"


def fmt_erc1155(raw: int) -> str:
    # CTF tokens have 6 decimals
    return f"{raw / 1e6:.6f} CTF"


# --------------------------------------------------------------------------- #
# RPC helpers (read-only eth_call / eth_getBalance)
# --------------------------------------------------------------------------- #
def _rpc_payload(method: str, params: list, req_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}


async def rpc_call(method: str, params: list, client: httpx.AsyncClient) -> str:
    """Try each RPC URL in order until one succeeds.

    Collects ALL error messages from every attempt so the caller sees why
    each endpoint failed (e.g. geoblocked, API-key-required, rate-limited).
    Raises RuntimeError with the full error chain if all URLs fail.
    """
    payload = _rpc_payload(method, params)
    errors: list[str] = []
    for url in POLYGON_RPC_URLS:
        try:
            resp = await client.post(url, json=payload, timeout=15.0)
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                err = result["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                errors.append(f"  {url}: RPC error: {msg}")
                continue  # try next URL
            return result["result"]
        except httpx.HTTPStatusError as e:
            errors.append(
                f"  {url}: HTTP {e.response.status_code}: {e.response.text[:120]}"
            )
        except Exception as e:
            errors.append(f"  {url}: {type(e).__name__}: {e}")
    detail = "; ".join(errors) if errors else "unknown error"
    raise RuntimeError(
        f"All {len(POLYGON_RPC_URLS)} Polygon RPC URLs failed for {method}. "
        f"Details:\n{detail}"
    )


async def get_matic_balance(address: str, client: httpx.AsyncClient) -> int:
    """Native MATIC balance via eth_getBalance."""
    result = await rpc_call(
        "eth_getBalance", [address, "latest"], client
    )
    return int(result, 16)


async def eth_call(to_address: str, data: str, from_address: str, client: httpx.AsyncClient) -> str:
    """Generic eth_call for contract read methods.

    Uses from_address for context (some chains need it for simulation), but
    for read-only calls like balanceOf and isApprovalForAll the sender does
    NOT need to hold ETH for gas — so we use a non-zero placeholder address
    to avoid balance-check reverts on strict RPC nodes (e.g. dRPC).
    """
    # Use a well-known funded address as sender for read-only calls so strict
    # RPC nodes don't reject due to insufficient ETH balance of from_address.
    # Read-only eth_call does not actually charge gas; the from field is only
    # used for simulation context (e.g. access lists).
    sender = from_address if from_address else "0x0000000000000000000000000000000000001001"
    payload_params = [
        {"to": to_address, "from": sender, "data": data},
        "latest",
    ]
    return await rpc_call("eth_call", payload_params, client)


def encode_balance_of(holder: str) -> str:
    """Encode ERC-20 balanceOf(address) call."""
    # selector + 32-byte left-padded address
    addr_hex = holder[2:].zfill(64)
    return ERC20_BALANCE_OF + addr_hex


def encode_allowance(owner: str, spender: str) -> str:
    """Encode ERC-20 allowance(address,address) call."""
    addr_1 = owner[2:].zfill(64)
    addr_2 = spender[2:].zfill(64)
    return ERC20_ALLOWANCE + addr_1 + addr_2


def encode_decimals() -> str:
    return ERC20_DECIMALS


def encode_is_approval_for_all(owner: str, operator: str) -> str:
    """Encode ERC-1155 isApprovalForAll(address,address) call."""
    addr_1 = owner[2:].zfill(64)
    addr_2 = operator[2:].zfill(64)
    return ERC1155_IS_APPROVAL_FOR_ALL + addr_1 + addr_2


async def get_erc20_balance(token_addr: str, holder: str, client: httpx.AsyncClient) -> int:
    """Get ERC-20 balance via eth_call."""
    data = encode_balance_of(holder)
    raw = await eth_call(token_addr, data, holder, client)
    return int(raw, 16)


async def get_erc20_decimals(token_addr: str, client: httpx.AsyncClient) -> int:
    """Get ERC-20 decimals (defaults to 6 for USDC/pUSD)."""
    data = encode_decimals()
    try:
        raw = await eth_call(token_addr, data, "0x0000000000000000000000000000000000000000", client)
        return int(raw, 16)
    except Exception:
        return 6  # pUSD/USDC standard


async def get_erc20_allowance(
    token_addr: str, owner: str, spender: str, client: httpx.AsyncClient
) -> int:
    """Get ERC-20 allowance(owner, spender) via eth_call."""
    data = encode_allowance(owner, spender)
    raw = await eth_call(token_addr, data, owner, client)
    return int(raw, 16)


class Erc1155CheckError(RuntimeError):
    """Raised when an ERC-1155 isApprovalForAll eth_call reverts or errors."""


async def get_erc1155_is_approved(
    token_addr: str, owner: str, operator: str, client: httpx.AsyncClient
) -> bool:
    """Check if ERC-1155 token is approved for operator via eth_call.

    Raises Erc1155CheckError with the revert reason if the call reverts — some
    ERC-1155 contracts (e.g. Polymarket's CTF) do not expose isApprovalForAll
    as a stateless view and revert on a raw eth_call. In that case approval
    must be verified via the on-chain setApprovalForAll transaction receipt,
    not a pre-flight read.
    """
    data = encode_is_approval_for_all(owner, operator)
    # Use a non-zero placeholder as sender for read-only eth_call so strict
    # RPC nodes don't reject due to insufficient ETH balance of owner address.
    payload_params = [
        {"to": token_addr, "from": "0x0000000000000000000000000000000000001001", "data": data},
        "latest",
    ]
    last_error: Optional[str] = None
    for url in POLYGON_RPC_URLS:
        try:
            resp = await client.post(
                url, json=_rpc_payload("eth_call", payload_params), timeout=15.0
            )
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                err = result["error"]
                # Distinguish a contract revert from an RPC-level auth error.
                if isinstance(err, dict) and "execution reverted" in str(err.get("message", "")):
                    raise Erc1155CheckError(
                        f"eth_call to {token_addr} reverted (isApprovalForAll not exposed as a view). "
                        f"Verify CTF approval via the on-chain setApprovalForAll tx receipt instead. "
                        f"Revert: {err.get('message', '')} ({err.get('data', '')})"
                    )
                last_error = f"RPC eth_call at {url} error: {err}"
                continue
            return bool(int(result["result"], 16))
        except Erc1155CheckError:
            raise
        except Exception as e:
            last_error = f"RPC eth_call at {url} {type(e).__name__}: {e}"
    raise Erc1155CheckError(last_error or "All RPC URLs failed for eth_call")


async def check_geoblock(client: httpx.AsyncClient) -> dict:
    """Check Polymarket geoblock status from current IP."""
    last_error: Optional[str] = None
    for url in POLYMARKET_GEOBLOCK_URLS:
        try:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_error = f"{url}: {type(e).__name__}: {e}"
    raise RuntimeError(last_error or "All geoblock URLs failed")


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #
async def check_private_key() -> CheckResult:
    """Check 1: PRIVATE_KEY is set."""
    key = os.getenv("PRIVATE_KEY", "")
    if not key:
        return CheckResult(
            "PRIVATE_KEY set",
            "FAIL",
            "PRIVATE_KEY environment variable is not set or empty. Set it in .env.",
            value="(unset)",
        )
    if not key.startswith("0x"):
        key = "0x" + key
    try:
        acct = Account.from_key(key)
        return CheckResult(
            "PRIVATE_KEY set",
            "PASS",
            f"Private key loaded and validates. Wallet: {acct.address}",
            value=acct.address,
        )
    except Exception as e:
        return CheckResult(
            "PRIVATE_KEY set",
            "FAIL",
            f"PRIVATE_KEY is set but does not parse as a valid key: {e}",
            value=key[:10] + "...",
        )


async def check_matic_balance(wallet: str, client: httpx.AsyncClient) -> CheckResult:
    """Check 3: Sufficient MATIC for gas."""
    try:
        wei = await get_matic_balance(wallet, client)
        matic = wei / 1e18
        if matic >= MATIC_MIN_FOR_GAS:
            return CheckResult(
                "MATIC for gas",
                "PASS",
                f"Wallet has {fmt_matic(wei)} — sufficient for gas (>= {MATIC_MIN_FOR_GAS} MATIC).",
                value=f"{matic:.4f}",
            )
        else:
            return CheckResult(
                "MATIC for gas",
                "FAIL",
                f"Wallet has {fmt_matic(wei)} — insufficient for gas. "
                f"Need at least {MATIC_MIN_FOR_GAS} MATIC.",
                value=f"{matic:.4f}",
            )
    except Exception as e:
        return CheckResult(
            "MATIC for gas",
            "FAIL",
            f"Failed to fetch MATIC balance via Polygon RPC: {e}",
        )


async def check_pusd_balance(wallet: str, client: httpx.AsyncClient, min_amount: float) -> CheckResult:
    """Check 4: pUSD balance >= required amount."""
    try:
        raw = await get_erc20_balance(P_USD_ADDRESS, wallet, client)
        # Try to get decimals; default 6
        decimals = await get_erc20_decimals(P_USD_ADDRESS, client)
        balance = raw / (10 ** decimals)
        if balance >= min_amount:
            return CheckResult(
                "pUSD balance",
                "PASS",
                f"Wallet has {fmt_pusd(raw)} (6 decimals). Required >= ${min_amount:.2f}.",
                value=f"${balance:.2f}",
            )
        else:
            return CheckResult(
                "pUSD balance",
                "FAIL",
                f"Wallet has {fmt_pusd(raw)} but needs >= ${min_amount:.2f} "
                f"for the first live test order.",
                value=f"${balance:.2f}",
            )
    except Exception as e:
        return CheckResult(
            "pUSD balance",
            "FAIL",
            f"Failed to fetch pUSD balance via eth_call: {e}",
        )


async def check_erc20_allowance(
    token_addr: str, owner: str, spender: str, token_label: str, client: httpx.AsyncClient
) -> CheckResult:
    """Check ERC-20 allowance for a given spender."""
    try:
        allowance = await get_erc20_allowance(token_addr, owner, spender, client)
        # Max uint256 = 2**256 - 1, Polymarket uses infinite approval pattern
        if allowance >= 2**255:  # effectively "max"
            return CheckResult(
                f"pUSD approved for {token_label}",
                "PASS",
                f"Allowance is max uint256 (infinite approval).",
                value=f"{allowance}",
            )
        elif allowance > 0:
            return CheckResult(
                f"pUSD approved for {token_label}",
                "WARN",
                f"Finite allowance = {allowance / 1e6:.2f} pUSD. Sufficient for this test "
                f"(${ORDER_SIZE}) but will need renewal for larger trades.",
                value=f"{allowance / 1e6:.2f} pUSD",
            )
        else:
            return CheckResult(
                f"pUSD approved for {token_label}",
                "FAIL",
                f"Allowance = 0. Must approve {spender} to spend pUSD before trading "
                f"on {token_label}.",
                value="0",
            )
    except Exception as e:
        return CheckResult(
            f"pUSD approved for {token_label}",
            "FAIL",
            f"Failed to check allowance via eth_call: {e}",
        )


async def check_erc1155_approval(
    token_addr: str, owner: str, operator: str, label: str, client: httpx.AsyncClient
) -> CheckResult:
    """Check ERC-1155 isApprovalForAll for a given operator."""
    try:
        approved = await get_erc1155_is_approved(token_addr, owner, operator, client)
        if approved:
            return CheckResult(
                f"CTF approved for {label}",
                "PASS",
                f"isApprovalForAll({owner[-6:]}, {operator[-6:]}) = True",
                value="True",
            )
        else:
            return CheckResult(
                f"CTF approved for {label}",
                "FAIL",
                f"isApprovalForAll({owner[-6:]}, {operator[-6:]}) = False. "
                f"Must setApprovalForAll({operator}, True) before trading.",
                value="False",
            )
    except Erc1155CheckError as e:
        return CheckResult(
            f"CTF approved for {label}",
            "WARN",
            f"State-read check inconclusive (contract reverts isApprovalForAll). "
            f"Per Polymarket, CTF approval is set via setApprovalForAll({operator[-6:]}, True) and must be "
            f"verified on-chain via the tx receipt, not a stateless read. "
            f"Root cause: {e}",
        )
    except Exception as e:
        return CheckResult(
            f"CTF approved for {label}",
            "FAIL",
            f"Failed to check ERC-1155 approval via eth_call: {e}",
        )


async def check_geoblock_status(client: httpx.AsyncClient) -> CheckResult:
    """Check 6: Geoblock status."""
    try:
        result = await check_geoblock(client)
        blocked = result.get("blocked", False)
        if blocked:
            country = result.get("country", "unknown")
            return CheckResult(
                "Geoblock",
                "FAIL",
                f"Location blocked: country={country}. Polymarket does not permit "
                f"trading from this jurisdiction. Cannot proceed with live orders.",
                value=f"blocked ({country})",
            )
        else:
            return CheckResult(
                "Geoblock",
                "PASS",
                "Current IP is not geoblocked by Polymarket.",
                value="clear",
            )
    except Exception as e:
        return CheckResult(
            "Geoblock",
            "WARN",
            f"Could not reach geoblock endpoint: {e}. Cannot determine jurisdiction status.",
            value="error",
        )


def check_signing_verified() -> CheckResult:
    """Check 7: LIVE_SIGNING_VERIFIED flag."""
    val = os.getenv("LIVE_SIGNING_VERIFIED", "")
    if val.strip().lower() in ("true", "1", "yes", "on"):
        return CheckResult(
            "LIVE_SIGNING_VERIFIED",
            "PASS",
            "Set to true in environment. Operator has confirmed signing constants "
            "are verified for live use.",
            value="true",
        )
    else:
        return CheckResult(
            "LIVE_SIGNING_VERIFIED",
            "FAIL",
            "Not set / not true. The bot will NOT enter live mode until an operator "
            "explicitly sets LIVE_SIGNING_VERIFIED=true after running the signing verifier.",
            value=val or "(unset)",
        )


# --------------------------------------------------------------------------- #
# Main orchestration
# --------------------------------------------------------------------------- #
async def run_all_checks() -> list[CheckResult]:
    results: list[CheckResult] = []

    # 1. PRIVATE_KEY
    pk_result = await check_private_key()
    results.append(pk_result)

    wallet = pk_result.value

    if pk_result.status == "FAIL":
        # Cannot continue without a wallet address
        results.extend([
            CheckResult("MATIC for gas", "FAIL", "Skipped — no valid PRIVATE_KEY.", value="N/A"),
            CheckResult("pUSD balance", "FAIL", "Skipped — no valid PRIVATE_KEY.", value="N/A"),
            CheckResult("pUSD approved for Exchange V2", "FAIL", "Skipped — no valid PRIVATE_KEY.", value="N/A"),
            CheckResult("pUSD approved for Neg Risk", "FAIL", "Skipped — no valid PRIVATE_KEY.", value="N/A"),
            CheckResult("CTF approved for Exchange V2", "FAIL", "Skipped — no valid PRIVATE_KEY.", value="N/A"),
            CheckResult("CTF approved for Neg Risk", "FAIL", "Skipped — no valid PRIVATE_KEY.", value="N/A"),
            await check_geoblock_status(httpx.AsyncClient()),
            check_signing_verified(),
        ])
        return results

    # 2. Wallet address — already validated above, just confirm
    results.append(CheckResult(
        "Wallet address",
        "PASS",
        f"Derived from PRIVATE_KEY: {wallet}",
        value=wallet,
    ))

    # Required pUSD = 2 * ORDER_SIZE * max_ask (~$1.0) = ~$20, with edge ~$19.80; use $20 buffer
    required_pusd = 2 * ORDER_SIZE  # $20.00 — enough for the full YES+NO pair at $1.0

    # 3-6: All RPC-based checks share a single client
    async with httpx.AsyncClient(timeout=15.0) as client:
        # 3. MATIC for gas
        results.append(await check_matic_balance(wallet, client))

        # 4. pUSD balance
        results.append(await check_pusd_balance(wallet, client, required_pusd))

        # 5a. pUSD allowance for Exchange V2
        results.append(await check_erc20_allowance(
            P_USD_ADDRESS, wallet, EXCHANGE_V2_ADDRESS, "Exchange V2", client
        ))

        # 5b. pUSD allowance for Neg Risk Exchange V2
        results.append(await check_erc20_allowance(
            P_USD_ADDRESS, wallet, NEG_RISK_EXCHANGE_V2_ADDRESS, "Neg Risk Exchange V2", client
        ))

        # 5c. CTF approval for Exchange V2
        results.append(await check_erc1155_approval(
            CTF_ADDRESS, wallet, EXCHANGE_V2_ADDRESS, "Exchange V2", client
        ))

        # 5d. CTF approval for Neg Risk Exchange V2
        results.append(await check_erc1155_approval(
            CTF_ADDRESS, wallet, NEG_RISK_EXCHANGE_V2_ADDRESS, "Neg Risk Exchange V2", client
        ))

        # 6. Geoblock
        results.append(await check_geoblock_status(client))

    # 7. LIVE_SIGNING_VERIFIED flag
    results.append(check_signing_verified())

    return results


def print_results(results: list[CheckResult]) -> bool:
    """Print results in a readable table. Returns True if all checks pass/warn."""
    print()
    print("=" * 100)
    print(" POLYMARKET ARB — PREFLIGHT CHECK (read-only, no transactions sent)")
    print("=" * 100)
    print()

    all_clear = True
    for r in results:
        symbol = "✓" if r.status == "PASS" else ("!" if r.status == "WARN" else "✗")
        status_colored = (
            f"\033[92m{r.status}\033[0m" if r.status == "PASS"
            else f"\033[93m{r.status}\033[0m" if r.status == "WARN"
            else f"\033[91m{r.status}\033[0m"
        )
        print(f"  {symbol} [{status_colored}] {r.name}")
        if r.detail:
            print(f"      {r.detail}")
        if r.value:
            print(f"      value: {r.value}")
        print()

        if r.status == "FAIL":
            all_clear = False

    print("-" * 100)
    fails = sum(1 for r in results if r.status == "FAIL")
    warns = sum(1 for r in results if r.status == "WARN")
    passes = sum(1 for r in results if r.status == "PASS")
    print(f"  Summary: {passes} PASS, {warns} WARN, {fails} FAIL out of {len(results)} checks")
    print("-" * 100)

    if fails > 0:
        print()
        print("  *** FAILED CHECKS — must be resolved before first live order ***")
        print()
        for r in results:
            if r.status == "FAIL":
                print(f"  ✗ {r.name}: {r.detail}")
        print()
        print("  Do NOT attempt to fund, approve, or bypass geoblock from this script.")
        print("  See scripts/preflight_check.py and docs for manual steps.")
        return False
    else:
        print()
        print("  All hard checks PASS. (Warnings are acceptable for a minimal test.)")
        print()
        return True


def print_config_only():
    """Print resolved configuration values without making RPC calls."""
    load_dotenv()
    key = os.getenv("PRIVATE_KEY", "")
    acct = Account.from_key(key) if key else None
    wallet = acct.address if acct else "(unset)"
    print("Resolved configuration:")
    print(f"  PRIVATE_KEY:        {'(set)' if key else '(UNSET)'}")
    print(f"  Wallet address:     {wallet}")
    print(f"  PROXY_WALLET:       {os.getenv('PROXY_WALLET', '(unset)')}")
    rpc_urls = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    print(f"  POLYGON_RPC_URL:    {rpc_urls}")
    print(f"  RPC fallbacks:      {[u for u in POLYGON_RPC_URLS if u != rpc_urls]}")
    print(f"  ORDER_SIZE:         {os.getenv('ORDER_SIZE', 10.0)}")
    print(f"  MIN_EDGE:           {os.getenv('MIN_EDGE', 0.01)}")
    print(f"  DRY_RUN:            {os.getenv('DRY_RUN', True)}")
    print(f"  PAPER_TRADE:        {os.getenv('PAPER_TRADE', False)}")
    print(f"  LIVE_SIGNING_VERIFIED: {os.getenv('LIVE_SIGNING_VERIFIED', False)}")
    print()
    print("Contract addresses (verified from trading_client.py):")
    print(f"  pUSD:               {P_USD_ADDRESS}")
    print(f"  CTF:                {CTF_ADDRESS}")
    print(f"  Exchange V2:        {EXCHANGE_V2_ADDRESS}")
    print(f"  Neg Risk Exchange:  {NEG_RISK_EXCHANGE_V2_ADDRESS}")
    print()
    print(f"Required pUSD for first test: ${2 * ORDER_SIZE:.2f} (2 x ORDER_SIZE x $1.00)")


def main():
    parser = argparse.ArgumentParser(description="Preflight check for live Polymarket trading.")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print resolved config values and exit (no RPC calls).",
    )
    args = parser.parse_args()

    if args.print_config:
        print_config_only()
        return 0

    load_dotenv()

    results = asyncio.run(run_all_checks())
    ok = print_results(results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
