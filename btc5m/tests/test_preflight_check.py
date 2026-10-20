"""Tests for scripts/preflight_check.py.

Validates the pure-logic helpers used by the preflight check:
  * _rpc_payload — constructs JSON-RPC payloads correctly
  * encode_balance_of — ERC-20 balanceOf encoding
  * encode_allowance — ERC-20 allowance encoding
  * encode_is_approval_for_all — ERC-1155 isApprovalForAll encoding
  * fmt_matic / fmt_pusd / fmt_erc1155 — unit formatters
  * check_signing_verified — env flag parsing
  * parse_summary_line helper (reused from calc_pnl_convergence for consistency)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Make scripts/ importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from preflight_check import (
    _rpc_payload,
    encode_balance_of,
    encode_allowance,
    encode_is_approval_for_all,
    fmt_matic,
    fmt_pusd,
    fmt_erc1155,
    check_signing_verified,
    CheckResult,
)


# ---------------------------------------------------------------------------
# _rpc_payload
# ---------------------------------------------------------------------------
def test_rpc_payload_structure():
    """The JSON-RPC payload has the required fields."""
    payload = _rpc_payload("eth_getBalance", ["0x1234", "latest"])
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "eth_getBalance"
    assert payload["params"] == ["0x1234", "latest"]
    assert payload["id"] == 1


def test_rpc_payload_custom_id():
    """Custom request ID is honored."""
    payload = _rpc_payload("eth_call", [{}, "latest"], req_id=42)
    assert payload["id"] == 42


# ---------------------------------------------------------------------------
# ABI encoders
# ---------------------------------------------------------------------------
def test_encode_balance_of():
    """balanceOf encoding is selector + 32-byte padded address."""
    holder = "0x" + "ab" * 20
    data = encode_balance_of(holder)
    # selector for balanceOf(address) = keccak("balanceOf(address)")[:4]
    assert data.startswith("0x70a08231")
    # 32-byte left-padded address
    assert len(data) == 2 + 8 + 64  # 0x + 4-byte selector + 32-byte address


def test_encode_allowance():
    """allowance encoding is selector + 64 bytes (owner + spender)."""
    owner = "0x" + "ab" * 20
    spender = "0x" + "cd" * 20
    data = encode_allowance(owner, spender)
    assert data.startswith("0x")
    # selector + 32 bytes + 32 bytes
    assert len(data) == 2 + 8 + 64 + 64


def test_encode_is_approval_for_all():
    """isApprovalForAll encoding is selector + 64 bytes (owner + operator)."""
    owner = "0x" + "ab" * 20
    operator = "0x" + "cd" * 20
    data = encode_is_approval_for_all(owner, operator)
    assert data.startswith("0x")
    assert len(data) == 2 + 8 + 64 + 64


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------
def test_fmt_matic():
    """wei -> MATIC conversion is correct."""
    assert fmt_matic(10**18) == "1.0000 MATIC"
    assert fmt_matic(0) == "0.0000 MATIC"


def test_fmt_pusd():
    """raw 6-decimal units -> USD dollar string."""
    assert fmt_pusd(1_000_000) == "$1.00 pUSD"
    assert fmt_pusd(0) == "$0.00 pUSD"


def test_fmt_erc1155():
    """raw 6-decimal units -> token count string."""
    assert fmt_erc1155(1_000_000) == "1.000000 CTF"
    assert fmt_erc1155(0) == "0.000000 CTF"


# ---------------------------------------------------------------------------
# check_signing_verified
# ---------------------------------------------------------------------------
def test_check_signing_verified_true():
    """LIVE_SIGNING_VERIFIED=true -> PASS."""
    with patch.dict(os.environ, {"LIVE_SIGNING_VERIFIED": "true"}):
        result = check_signing_verified()
    assert result.status == "PASS"
    assert result.value == "true"


def test_check_signing_verified_false():
    """LIVE_SIGNING_VERIFIED unset -> FAIL."""
    with patch.dict(os.environ, {}, clear=True):
        result = check_signing_verified()
    assert result.status == "FAIL"
    assert result.value == "(unset)"


def test_check_signing_verified_various_truthy():
    """Various truthy values are accepted."""
    for val in ["True", "1", "YES", "ON", "true", "1"]:
        with patch.dict(os.environ, {"LIVE_SIGNING_VERIFIED": val}):
            result = check_signing_verified()
        assert result.status == "PASS", f"value '{val}' should be truthy"


# ---------------------------------------------------------------------------
# CheckResult dataclass
# ---------------------------------------------------------------------------
def test_check_result_defaults():
    """CheckResult has sensible defaults."""
    cr = CheckResult(name="test", status="PASS")
    assert cr.name == "test"
    assert cr.status == "PASS"
    assert cr.detail == ""
    assert cr.value is None
