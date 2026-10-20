"""Read-only wallet, funding, approval, and jurisdiction status.

Only ``eth_getBalance`` and ``eth_call`` are permitted RPC methods.  This
module has no transaction builder, signer, or send method and therefore cannot
move funds or change approvals.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from eth_account import Account
from eth_utils import keccak, to_checksum_address
import httpx

from .clients.trading_client import (
    CTF_ADDRESS,
    EXCHANGE_V2_ADDRESS,
    NEG_RISK_EXCHANGE_V2_ADDRESS,
    P_USD_ADDRESS,
)
from .config import SETTINGS


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "state" / "wallet_status.json"
DEFAULT_RPC_URLS = (
    "https://polygon.drpc.org",
    "https://polygon.api.onfinality.io/public",
)
GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
READ_ONLY_RPC_METHODS = frozenset({"eth_getBalance", "eth_call"})
MATIC_MIN_FOR_GAS = 0.5


def _selector(signature: str) -> str:
    return "0x" + keccak(signature.encode("ascii"))[:4].hex()


def _address_word(address: str) -> str:
    checked = to_checksum_address(address)
    return checked[2:].lower().rjust(64, "0")


def encode_balance_of(owner: str) -> str:
    return _selector("balanceOf(address)") + _address_word(owner)


def encode_allowance(owner: str, spender: str) -> str:
    return (
        _selector("allowance(address,address)")
        + _address_word(owner)
        + _address_word(spender)
    )


def encode_is_approved_for_all(owner: str, operator: str) -> str:
    return (
        _selector("isApprovedForAll(address,address)")
        + _address_word(owner)
        + _address_word(operator)
    )


def _rpc_urls(settings: Any) -> tuple[str, ...]:
    configured = str(getattr(settings, "polygon_rpc_url", "") or "").strip()
    urls: list[str] = []
    if configured:
        urls.append(configured)
    urls.extend(url for url in DEFAULT_RPC_URLS if url not in urls)
    return tuple(urls)


async def _read_only_rpc(
    client: httpx.AsyncClient,
    urls: Iterable[str],
    method: str,
    params: list[Any],
) -> tuple[str, str]:
    if method not in READ_ONLY_RPC_METHODS:
        raise ValueError(f"RPC method is not read-only: {method}")
    failures: list[str] = []
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    for url in urls:
        try:
            response = await client.post(url, json=payload, timeout=15.0)
            response.raise_for_status()
            body = response.json()
            if body.get("error"):
                failures.append(f"{url}: {body['error']}")
                continue
            value = body.get("result")
            if not isinstance(value, str):
                failures.append(f"{url}: missing result")
                continue
            return value, url
        except Exception as exc:
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
    raise RuntimeError("; ".join(failures) or "no Polygon RPC URLs configured")


async def _eth_call(
    client: httpx.AsyncClient,
    urls: Iterable[str],
    to: str,
    data: str,
) -> tuple[int, str]:
    result, endpoint = await _read_only_rpc(
        client,
        urls,
        "eth_call",
        [{"to": to_checksum_address(to), "data": data}, "latest"],
    )
    return int(result, 16), endpoint


def _item(name: str, status: str, value: Any, detail: str = "") -> dict[str, Any]:
    return {"name": name, "status": status, "value": value, "detail": detail}


def _safe_error_detail(exc: Exception) -> str:
    """Keep actionable failure text without persisting RPC URLs or embedded keys."""
    detail = re.sub(r"https?://[^;\s]+", "<rpc>", str(exc))
    return f"{type(exc).__name__}: {detail}"[:1000]


async def collect_wallet_status(
    settings: Any = SETTINGS,
    *,
    client: httpx.AsyncClient | None = None,
    rpc_urls: Iterable[str] | None = None,
    geoblock_url: str = GEOBLOCK_URL,
) -> dict[str, Any]:
    """Collect one real, read-only status snapshot with exact raw values."""
    created_client = client is None
    client = client or httpx.AsyncClient()
    urls = tuple(rpc_urls or _rpc_urls(settings))
    checks: list[dict[str, Any]] = []
    rpc_endpoints_used: set[str] = set()
    signer_wallet = ""
    funding_wallet = ""
    key = str(getattr(settings, "private_key", "") or "")
    proxy_wallet = str(getattr(settings, "proxy_wallet", "") or "")
    configured_rpc_url = str(getattr(settings, "polygon_rpc_url", "") or "").strip()
    configured_rpc_healthy = False
    try:
        if not key:
            checks.append(_item("signer_wallet", "fail", None, "PRIVATE_KEY is unset"))
        else:
            try:
                signer_wallet = Account.from_key(key).address
                checks.append(
                    _item("signer_wallet", "pass", signer_wallet, "derived locally")
                )
            except Exception as exc:
                checks.append(
                    _item("signer_wallet", "fail", None, f"invalid PRIVATE_KEY: {exc}")
                )

        if proxy_wallet:
            try:
                funding_wallet = to_checksum_address(proxy_wallet)
                checks.append(
                    _item("funding_wallet", "pass", funding_wallet, "configured proxy")
                )
            except ValueError as exc:
                checks.append(
                    _item("funding_wallet", "fail", None, f"invalid PROXY_WALLET: {exc}")
                )
        elif signer_wallet:
            funding_wallet = signer_wallet
            checks.append(
                _item("funding_wallet", "pass", funding_wallet, "signer is funder")
            )
        else:
            checks.append(
                _item("funding_wallet", "fail", None, "no valid signer or proxy wallet")
            )

        order_size = float(getattr(settings, "order_size", 10.0))
        required_pusd = 2.0 * order_size
        balances: dict[str, Any] = {
            "wallet_address": signer_wallet or None,
            "signer_wallet_address": signer_wallet or None,
            "funding_wallet_address": funding_wallet or None,
            "proxy_wallet": proxy_wallet or None,
            "required_pusd": required_pusd,
            "polygon_native_wei": None,
            "polygon_native": None,
            "pusd_raw": None,
            "pusd_decimals": 6,
            "pusd": None,
        }
        approvals: dict[str, Any] = {}

        if configured_rpc_url and signer_wallet:
            try:
                await _read_only_rpc(
                    client,
                    (configured_rpc_url,),
                    "eth_getBalance",
                    [signer_wallet, "latest"],
                )
                configured_rpc_healthy = True
                checks.append(
                    _item(
                        "configured_polygon_rpc",
                        "pass",
                        True,
                        "configured endpoint answered a read-only balance query",
                    )
                )
            except Exception as exc:
                checks.append(
                    _item(
                        "configured_polygon_rpc",
                        "fail",
                        False,
                        _safe_error_detail(exc),
                    )
                )
        else:
            checks.append(
                _item(
                    "configured_polygon_rpc",
                    "fail",
                    False,
                    "POLYGON_RPC_URL or valid signer is missing",
                )
            )

        if signer_wallet:
            try:
                raw, endpoint = await _read_only_rpc(
                    client, urls, "eth_getBalance", [signer_wallet, "latest"]
                )
                rpc_endpoints_used.add(endpoint)
                wei = int(raw, 16)
                native = wei / 10**18
                balances["polygon_native_wei"] = wei
                balances["polygon_native"] = native
                checks.append(
                    _item(
                        "polygon_native_balance",
                        "pass" if native >= MATIC_MIN_FOR_GAS else "fail",
                        native,
                        f"minimum={MATIC_MIN_FOR_GAS}",
                    )
                )
            except Exception as exc:
                checks.append(
                    _item("polygon_native_balance", "error", None, _safe_error_detail(exc))
                )

        if funding_wallet:
            try:
                raw_balance, endpoint = await _eth_call(
                    client, urls, P_USD_ADDRESS, encode_balance_of(funding_wallet)
                )
                rpc_endpoints_used.add(endpoint)
                pusd = raw_balance / 10**6
                balances["pusd_raw"] = raw_balance
                balances["pusd"] = pusd
                checks.append(
                    _item(
                        "pusd_balance",
                        "pass" if pusd >= required_pusd else "fail",
                        pusd,
                        f"minimum={required_pusd}",
                    )
                )
            except Exception as exc:
                checks.append(
                    _item("pusd_balance", "error", None, _safe_error_detail(exc))
                )

            for label, operator in (
                ("exchange_v2", EXCHANGE_V2_ADDRESS),
                ("neg_risk_exchange_v2", NEG_RISK_EXCHANGE_V2_ADDRESS),
            ):
                allowance_name = f"pusd_allowance_{label}"
                try:
                    allowance, endpoint = await _eth_call(
                        client,
                        urls,
                        P_USD_ADDRESS,
                        encode_allowance(funding_wallet, operator),
                    )
                    rpc_endpoints_used.add(endpoint)
                    approvals[allowance_name] = allowance
                    checks.append(
                        _item(
                            allowance_name,
                            "pass" if allowance >= int(required_pusd * 10**6) else "fail",
                            allowance,
                            f"required_raw={int(required_pusd * 10**6)}",
                        )
                    )
                except Exception as exc:
                    approvals[allowance_name] = None
                    checks.append(
                        _item(allowance_name, "error", None, _safe_error_detail(exc))
                    )

                ctf_name = f"ctf_approval_{label}"
                try:
                    approved_raw, endpoint = await _eth_call(
                        client,
                        urls,
                        CTF_ADDRESS,
                        encode_is_approved_for_all(funding_wallet, operator),
                    )
                    rpc_endpoints_used.add(endpoint)
                    approved = bool(approved_raw)
                    approvals[ctf_name] = approved
                    checks.append(_item(ctf_name, "pass" if approved else "fail", approved))
                except Exception as exc:
                    approvals[ctf_name] = None
                    checks.append(
                        _item(ctf_name, "unknown", None, _safe_error_detail(exc))
                    )

        try:
            response = await client.get(
                geoblock_url,
                headers={"User-Agent": "polymarket-arb-read-only-status/1.0"},
                timeout=10.0,
            )
            response.raise_for_status()
            geoblock = response.json()
            blocked = bool(geoblock.get("blocked", False))
            jurisdiction = {
                "status": "blocked" if blocked else "clear",
                "blocked": blocked,
                "country": geoblock.get("country"),
                "region": geoblock.get("region"),
            }
            checks.append(
                _item(
                    "jurisdiction",
                    "fail" if blocked else "pass",
                    jurisdiction["status"],
                    f"country={jurisdiction['country']}",
                )
            )
        except Exception as exc:
            jurisdiction = {
                "status": "unknown",
                "blocked": None,
                "country": None,
                "region": None,
                "error": _safe_error_detail(exc),
            }
            checks.append(
                _item("jurisdiction", "unknown", "unknown", _safe_error_detail(exc))
            )

        hard_failures = sum(item["status"] == "fail" for item in checks)
        unknowns = sum(item["status"] in {"error", "unknown"} for item in checks)
        funding_check_names = {
            "signer_wallet",
            "funding_wallet",
            "configured_polygon_rpc",
            "polygon_native_balance",
            "pusd_balance",
        }
        approval_check_names = {
            "pusd_allowance_exchange_v2",
            "ctf_approval_exchange_v2",
            "pusd_allowance_neg_risk_exchange_v2",
            "ctf_approval_neg_risk_exchange_v2",
        }
        funding_ready = all(
            item["status"] == "pass"
            for item in checks
            if item["name"] in funding_check_names
        ) and funding_check_names <= {item["name"] for item in checks}
        approvals_ready = all(
            item["status"] == "pass"
            for item in checks
            if item["name"] in approval_check_names
        ) and approval_check_names <= {item["name"] for item in checks}
        return {
            "schema_version": 1,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "rpc_methods_allowed": sorted(READ_ONLY_RPC_METHODS),
            "transaction_methods_available": False,
            "rpc_endpoints_used": sorted(
                {urlparse(endpoint).hostname or "unknown" for endpoint in rpc_endpoints_used}
            ),
            "configured_rpc_host": urlparse(configured_rpc_url).hostname,
            "configured_rpc_healthy": configured_rpc_healthy,
            "rpc_fallback_used": bool(
                configured_rpc_url
                and rpc_endpoints_used
                and configured_rpc_url not in rpc_endpoints_used
            ),
            "balances": balances,
            "approvals": approvals,
            "jurisdiction": jurisdiction,
            "funding_ready": funding_ready,
            "approvals_ready": approvals_ready,
            "trading_ready": funding_ready and approvals_ready,
            "hard_failure_count": hard_failures,
            "unknown_count": unknowns,
            "checks": checks,
        }
    finally:
        if created_client:
            await client.aclose()


def save_wallet_status(report: dict[str, Any], path: Path = DEFAULT_OUTPUT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    return path


def refresh_wallet_status(
    settings: Any = SETTINGS,
    *,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    report = asyncio.run(collect_wallet_status(settings))
    save_wallet_status(report, output_path)
    return report
