from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from polymarket_arb.wallet_status import (
    _read_only_rpc,
    collect_wallet_status,
    encode_is_approved_for_all,
    save_wallet_status,
)


PRIVATE_KEY = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def _settings():
    return SimpleNamespace(
        private_key=PRIVATE_KEY,
        proxy_wallet="",
        order_size=10.0,
        polygon_rpc_url="https://rpc.test",
    )


def _transport(*, blocked: bool = False):
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"blocked": blocked, "country": "CA", "region": "BC"},
            )
        payload = json.loads(request.content)
        method = payload["method"]
        methods.append(method)
        if method == "eth_getBalance":
            result = hex(10**18)
        else:
            data = payload["params"][0]["data"]
            selector = data[:10]
            if selector == "0x70a08231":  # balanceOf(address)
                result = hex(50_000_000)
            elif selector == "0xdd62ed3e":  # allowance(address,address)
                result = hex(2**256 - 1)
            elif selector == "0xe985e9c5":  # isApprovalForAll(address,address)
                result = hex(1)
            else:
                return httpx.Response(200, json={"error": {"message": "unknown call"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return httpx.MockTransport(handler), methods


def test_collect_wallet_status_uses_only_read_only_rpc_and_exact_values():
    transport, methods = _transport()

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await collect_wallet_status(
                _settings(),
                client=client,
                rpc_urls=["https://rpc.test"],
                geoblock_url="https://geo.test",
            )

    report = asyncio.run(run())
    assert set(methods) == {"eth_getBalance", "eth_call"}
    assert report["transaction_methods_available"] is False
    assert report["configured_rpc_healthy"] is True
    assert report["rpc_fallback_used"] is False
    assert report["balances"]["polygon_native_wei"] == 10**18
    assert report["balances"]["polygon_native"] == 1.0
    assert report["balances"]["pusd_raw"] == 50_000_000
    assert report["balances"]["pusd"] == 50.0
    assert report["balances"]["required_pusd"] == 20.0
    assert report["balances"]["funding_wallet_address"] == report["balances"]["wallet_address"]
    assert report["funding_ready"] is True
    assert report["approvals_ready"] is True
    assert report["trading_ready"] is True
    assert report["jurisdiction"]["status"] == "clear"


def test_erc1155_approval_encoder_uses_standard_selector():
    encoded = encode_is_approved_for_all(
        "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
        "0xE111180000d2663C0091e4f400237545B87B996B",
    )
    assert encoded.startswith("0xe985e9c5")


def test_configured_proxy_is_the_balance_and_approval_owner():
    transport, _ = _transport()
    settings = _settings()
    settings.proxy_wallet = "0x1111111111111111111111111111111111111111"

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await collect_wallet_status(
                settings,
                client=client,
                rpc_urls=["https://rpc.test"],
                geoblock_url="https://geo.test",
            )

    report = asyncio.run(run())
    assert report["balances"]["signer_wallet_address"] != report["balances"]["funding_wallet_address"]
    assert report["balances"]["funding_wallet_address"] == settings.proxy_wallet
    assert report["trading_ready"] is True


def test_blocked_jurisdiction_is_reported_not_bypassed():
    transport, _ = _transport(blocked=True)

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await collect_wallet_status(
                _settings(),
                client=client,
                rpc_urls=["https://rpc.test"],
                geoblock_url="https://geo.test",
            )

    report = asyncio.run(run())
    assert report["jurisdiction"]["blocked"] is True
    jurisdiction_check = next(
        item for item in report["checks"] if item["name"] == "jurisdiction"
    )
    assert jurisdiction_check["status"] == "fail"


def test_rpc_helper_rejects_transaction_method_before_network():
    transport, methods = _transport()

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ValueError, match="not read-only"):
                await _read_only_rpc(
                    client,
                    ["https://rpc.test"],
                    "eth_sendRawTransaction",
                    ["0xdeadbeef"],
                )

    asyncio.run(run())
    assert methods == []


def test_saved_snapshot_contains_no_private_key(tmp_path):
    transport, _ = _transport()
    settings = _settings()
    settings.polygon_rpc_url = "https://rpc.test/private-rpc-key"

    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await collect_wallet_status(
                settings, client=client, rpc_urls=[settings.polygon_rpc_url]
            )

    report = asyncio.run(run())
    path = save_wallet_status(report, tmp_path / "wallet.json")
    text = path.read_text(encoding="utf-8")
    assert PRIVATE_KEY not in text
    assert "private-rpc-key" not in text
    assert json.loads(text)["read_only"] is True
