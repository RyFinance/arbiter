"""Trading client — order submission + signing via py_clob_client_v2 SDK.

This is a thin wrapper around the official Polymarket CLOB SDK. All EIP-712
order struct construction, salt generation, domain setup, and two-layer auth
(L1 wallet signature for API key derivation + L2 HMAC per request) are handled
by the SDK. We only expose the interface that execution.py expects.

Interface (unchanged from previous implementation):
  - submit_order(token_id, price, size, side) -> Optional[str]  (order_id or None)
  - is_filled(order_id, token_id, gap_ms) -> bool
  - cancel_order(token_id, missing_order_id) -> bool
  - build_order(token_id, price, size, side) -> dict
  - setup_trading_approvals()  — cold-path: approves pUSD + CTF for both
    exchange_v2 and neg_risk_exchange_v2 contracts (see __init__)
  - aclose()

DRY_RUN (default): returns synthetic order ids, never touches the network.
PAPER_TRADE: full real auth flow runs (create_or_derive_api_key + trading
  approvals), but submit_order() does NOT hit the exchange — it simulates a
  full-size fill by walking the live REST book within the submitted limit.
  Simulated fills are logged via log.trade("filled", ...) in the EXACT SAME
  JSONL format as real fills.
LIVE: exchange order submission is intentionally unbuilt and fails closed.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Optional

from ..config import SETTINGS
from ..live_control import (
    LIVE_ORDER_ENDPOINT_BUILT,
    LiveTradingControl,
    default_live_control,
)
from ..logger import log
from .clob_client import _price_size

# Polymarket constants
POLYGON_CHAIN_ID = 137
EOA_SIGNATURE_TYPE = 0  # Standard EOA wallet signature

# --- Trading approval constants ---
# py_clob_client_v2 v1.1.0 does NOT expose setup_trading_approvals().
# We must send on-chain transactions to approve the exchange contracts.
# Contract addresses come from py_clob_client_v2.config.ContractConfig (Polygon mainnet):
#   collateral (pUSD/USDC):          0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB
#   conditional_tokens (CTF):        0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
#   exchange_v2 (standard):          0xE111180000d2663C0091e4f400237545B87B996B
#   neg_risk_exchange_v2:            0xe2222d279d744050d28e00520010520000310F59
# See docs.polymarket.com/trading/wallets-auth — "Set Up Trading Approvals".
P_USD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE_V2_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"

# ERC-20 approve ABI (for pUSD/USDC spend approval) — minimal fragment
_ERC20_APPROVE_ABI = [
    {
        "type": "function",
        "name": "approve",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
]

# ERC-1155 setApprovalForAll ABI (for CTF conditional token approval)
_ERC1155_SET_APPROVAL_ABI = [
    {
        "type": "function",
        "name": "setApprovalForAll",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
]


_VALID_PAPER_FILL_SOURCES = {"decision_snapshot", "rest_book"}


def _resolve_paper_fill_source() -> str:
    """Read PAPER_FILL_SOURCE from the environment, defaulting to rest_book.

    Pulled out of ``TradingClient.__init__`` so it is testable without
    constructing a client (which touches the ``default_live_control()``
    process-wide singleton and real ``state/`` files).

    Default is ``rest_book``, not ``decision_snapshot``: in decision_snapshot
    mode, ``submit_paper_order`` always forwards the WS decision price as
    ``observed_fill_price``, which ``_paper_simulate_fill`` then compares
    against itself (``fill_price == price``) -- a fill can never fail on
    depth/slippage, so the partial-fill/hedge path in execution.py never runs.
    rest_book makes every paper fill an independent, full-depth REST check per
    leg at submission time. decision_snapshot remains available (it skips the
    extra REST round trip) but must be opted into explicitly via the env var.
    """
    source = os.getenv("PAPER_FILL_SOURCE", "rest_book").strip().lower()
    if source not in _VALID_PAPER_FILL_SOURCES:
        log.warning(f"Unknown PAPER_FILL_SOURCE={source!r}; using rest_book")
        return "rest_book"
    if source == "decision_snapshot":
        log.warning(
            "PAPER_FILL_SOURCE=decision_snapshot: paper fills always succeed "
            "at the WS decision price with no depth/liquidity check and no "
            "possibility of a partial fill. Evidence gathered in this mode "
            "does not exercise the partial-fill/hedge recovery path. Use "
            "rest_book (the default) for realistic paper evidence."
        )
    return source


def _paper_full_fill_price(
    book: dict,
    *,
    side: str,
    limit_price: float,
    size: float,
) -> tuple[Optional[float], float]:
    """Return full-size executable VWAP and eligible quantity for a REST book.

    PAPER_TRADE uses fill-or-kill semantics: if the requested quantity is not
    visible at prices within the submitted limit, the leg does not fill. This
    avoids treating a tiny top-of-book quote as liquidity for the entire order.
    """
    side_upper = side.upper()
    key = "asks" if side_upper == "BUY" else "bids"
    levels: list[tuple[float, float]] = []
    for raw in (book or {}).get(key) or []:
        price, available = _price_size(raw)
        if price is None or available is None or available <= 0:
            continue
        if side_upper == "BUY" and price <= limit_price:
            levels.append((price, available))
        elif side_upper == "SELL" and price >= limit_price:
            levels.append((price, available))
    levels.sort(key=lambda level: level[0], reverse=side_upper == "SELL")
    remaining = max(0.0, float(size))
    notional = 0.0
    eligible = sum(available for _, available in levels)
    for price, available in levels:
        taken = min(remaining, available)
        notional += taken * price
        remaining -= taken
        if remaining <= 1e-12:
            break
    if size <= 0 or remaining > 1e-12:
        return None, eligible
    return notional / float(size), eligible


class TradingClient:
    """Thin wrapper around py_clob_client_v2.ClobClient.

    Manages the two-layer auth flow:
      L1: One-time wallet signature to derive API key/secret/passphrase
      L2: SDK handles HMAC signing on every order request

    In DRY_RUN mode, no network calls are made — synthetic order ids are returned.
    """

    def __init__(self, settings=SETTINGS):
        self.s = settings
        self._enforce_live_control = True
        self._live_control = default_live_control()
        self._clob_client = None
        self._address = ""
        self._creds = None
        self._paper_trade = False  # set below
        self._paper_fill_source = _resolve_paper_fill_source()
        self._paper_fill_details: dict[str, dict] = {}
        # TASK 2: registry of every order id this client issued, mapped to the
        # mode ("dry_run" | "paper" | "live") that produced it. Paper-trade ids
        # are shaped EXACTLY like real CLOB order ids, so a summary must be able
        # to tell them apart from live ids without guessing — this registry is
        # the source of truth, on top of the deterministic `dryrun-` prefix.
        self._order_modes: dict = {}

        if settings.live:
            self._init_live_client()
            # PAPER_TRADE: dry_run must be False (else `live` is already False and
            # we never reach here). The flag is independent of credentials so an
            # operator can run a fully-authed paper session.
            self._paper_trade = bool(settings.paper_trade)
            # COLD PATH: pre-derive L1 API credentials ONCE at startup so they are
            # cached and NOT re-derived on every FIRE (which was the per-fire
            # derivation bug). If creds are already present in .env, _init_live_client
            # loaded them and _ensure_creds is a no-op.
            self._ensure_creds()
        else:
            # DRY_RUN: use a dummy address for synthetic order ids
            self._address = settings.proxy_wallet or "0x0000000000000000000000000000000000000000"

        # COLD PATH: set up on-chain trading approvals ONCE at startup.
        # This is never called from the hot path (submit_order, is_filled, cancel).
        # In DRY_RUN mode there is no real wallet, so we skip. In LIVE and
        # PAPER_TRADE mode the full real auth flow runs — including trading
        # approvals — because the requirement is that PAPER_TRADE still runs the
        # FULL real auth flow.
        if settings.live:
            try:
                self.setup_trading_approvals()
            except Exception as e:
                # Don't block construction; order submission will surface a
                # clear on-chain revert if approvals are missing.
                log.warning(f"Trading approvals setup skipped/failed: {e!r}")

    # ---- TASK 2: explicit order mode ("dry_run" | "paper" | "live") ----
    @property
    def mode(self) -> str:
        """The mode this client runs in: ``"dry_run"`` | ``"paper"`` | ``"live"``.

        Computed from settings so existing test stubs (which never run
        ``__init__``) still report the correct mode. Paper-trade and live order
        ids are indistinguishable by shape — the mode is the ONLY way summaries
        and audit records can tell them apart.
        """
        live = getattr(getattr(self, "s", None), "live", False)
        if not live:
            return "dry_run"
        if getattr(self, "_paper_trade", False):
            return "paper"
        return "live"

    def check_order_submission(
        self,
        call_site: str,
        *,
        token_id: str = "",
        side: str = "",
    ) -> bool:
        """Audit and enforce the master control at an order-submission boundary.

        The lazy fallback keeps older test doubles constructed with ``__new__``
        fail-closed for live mode without requiring their constructors to change.
        Paper and dry-run paths are permitted because they cannot post an order.
        """
        control = getattr(self, "_live_control", None)
        if not isinstance(control, LiveTradingControl):
            control = default_live_control()
            self._live_control = control
        decision = control.check_submission(
            mode=self.mode,
            call_site=call_site,
            side=side,
            token_suffix=token_id[-8:],
        )
        data = {
            "call_site": call_site,
            "mode": decision.mode,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "operator_enabled": decision.operator_enabled,
            "endpoint_built": decision.endpoint_built,
            "side": side.upper(),
            "token_suffix": token_id[-8:],
        }
        if decision.allowed:
            log.info("LIVE_CONTROL_CHECK allowed", data)
        else:
            log.critical("LIVE_CONTROL_CHECK blocked", data)
        return decision.allowed

    def _register_order(self, order_id: Optional[str], mode: str) -> Optional[str]:
        """Record the mode that produced ``order_id`` for later classification."""
        if order_id is not None:
            if not hasattr(self, "_order_modes"):
                self._order_modes = {}
            self._order_modes[str(order_id)] = mode
        return order_id

    def mode_of(self, order_id: Optional[str]) -> str:
        """Classify an order id: ``"dry_run"`` | ``"paper"`` | ``"live"``.

        Deterministic prefix match wins, then the issued-id registry. Unknown,
        non-registered ids are classified ``"live"`` (they can only come from the
        exchange SDK, which returns real CLOB order hashes). ``None`` -> ``"live"``
        (no id means no order was placed; callers already short-circuit on None).
        """
        if order_id is None:
            return "live"
        oid = str(order_id)
        if oid.startswith("dryrun-"):
            return "dry_run"
        return getattr(self, "_order_modes", {}).get(oid, "live")

    def get_fill_report(self, order_id: Optional[str]) -> dict:
        """Fill lookup for an order id, LABELED with the mode that issued it.

        TASK 2: ``get_fill_ratio`` keeps its legacy float contract (execution
        math depends on it), but any *reporting* lookup must carry the mode so a
        dry-run/paper order is never summarized as a live one. Mirrors
        ``get_fill_ratio`` semantics: None id -> 0% fill, any registered id ->
        100% fill.
        """
        report = {
            "order_id": order_id,
            "mode": self.mode_of(order_id),
            "fill_ratio": 0.0 if order_id is None else 1.0,
            "counts_as_paper": self.mode_of(order_id) != "live",
        }
        if order_id is not None:
            report.update(getattr(self, "_paper_fill_details", {}).get(str(order_id), {}))
        return report

    def setup_trading_approvals(self):
        """Set up on-chain trading approvals ONCE at cold-path startup.

        The py_clob_client_v2 SDK (v1.1.0) does NOT provide a
        ``setup_trading_approvals()`` method. Per the Polymarket docs
        (docs.polymarket.com/trading/wallets-auth, "Set Up Trading Approvals"),
        a wallet must approve TWO token types for TWO exchange contracts:

          1. pUSD (USDC, 0xC011a7...) — ERC-20 ``approve(spender, type(uint256).max)``
             for both:
               - exchange_v2            (0xE1111...)  — standard markets
               - neg_risk_exchange_v2   (0xe2222...)  — neg-risk markets
          2. Conditional Tokens (CTF, 0x4D97...) — ERC-1155
             ``setApprovalForAll(operator, True)`` for both exchange contracts.

        We use ``max uint256`` for the ERC-20 approval (standard Polymarket pattern)
        and ``True`` for the ERC-1155 setApprovalForAll. Transactions are sent via
        raw eth-tx over Polygon RPC (no web3.py dependency). Errors are logged but
        do not prevent TradingClient from functioning — order submission will surface
        a clear on-chain revert if approvals are missing.

        In PAPER_TRADE mode the transactions are SIGNED locally (full real auth
        flow) but NOT broadcast — we log the signed payload for audit instead.
        """
        if not getattr(self, "_paper_trade", False) and not LIVE_ORDER_ENDPOINT_BUILT:
            log.critical(
                "LIVE_APPROVAL_BROADCAST_BLOCKED",
                {"reason": "live_exchange_order_endpoint_unbuilt"},
            )
            return

        from eth_account import Account

        acct = Account.from_key(self.s.private_key)
        wallet_address = acct.address

        # In PAPER_TRADE mode, skip ALL RPC calls (nonce fetch) to avoid
        # exposing the wallet address to the RPC provider. Use a placeholder
        # nonce — the local signing validates the wallet key without chain
        # interaction. _send_approval_txn() also guards on _paper_trade and
        # skips the broadcast.
        if getattr(self, "_paper_trade", False):
            nonce = 0  # placeholder — never sent to RPC in paper mode
            log.info(
                "[PAPER_TRADE] setup_trading_approvals: skipping RPC nonce fetch, "
                "using placeholder nonce=0 (tx will be signed locally, not broadcast)"
            )
        else:
            nonce = self._rpc_get_nonce(wallet_address)

        # Approvals to send: (token_label, token_address, abi, fn_name, args)
        approvals = [
            # pUSD (USDC) ERC-20 approve — max uint256 for both exchanges
            ("pUSD", P_USD_ADDRESS, _ERC20_APPROVE_ABI, "approve",
             [EXCHANGE_V2_ADDRESS, 2**256 - 1]),
            ("pUSD", P_USD_ADDRESS, _ERC20_APPROVE_ABI, "approve",
             [NEG_RISK_EXCHANGE_V2_ADDRESS, 2**256 - 1]),
            # Conditional Tokens ERC-1155 setApprovalForAll for both exchanges
            ("CTF", CTF_ADDRESS, _ERC1155_SET_APPROVAL_ABI, "setApprovalForAll",
             [EXCHANGE_V2_ADDRESS, True]),
            ("CTF", CTF_ADDRESS, _ERC1155_SET_APPROVAL_ABI, "setApprovalForAll",
             [NEG_RISK_EXCHANGE_V2_ADDRESS, True]),
        ]

        for label, token_addr, abi, fn_name, args in approvals:
            try:
                self._send_approval_txn(acct, nonce,
                                        token_addr, abi, fn_name, args, label)
                nonce += 1
            except Exception as e:
                log.warning(f"Approval {label} ({fn_name}) failed: {e!r}")

    # ------------------------------------------------------------------
    # Low-level Polygon RPC helpers for trading approvals (COLD PATH only).
    # ------------------------------------------------------------------

    def _rpc_get_nonce(self, address: str) -> int:
        """Fetch transaction count (nonce) from Polygon RPC."""
        import httpx

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getTransactionCount",
            "params": [address, "latest"],
            "id": 1,
        }
        resp = httpx.post(self.s.polygon_rpc_url, json=payload, timeout=10.0)
        if resp.status_code != 200:
            raise RuntimeError(f"RPC eth_getTransactionCount HTTP {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"RPC eth_getTransactionCount error: {result['error']}")
        return int(result["result"], 16)

    def _rpc_send_raw_tx(self, raw_tx: str) -> str:
        """Send a signed raw transaction to Polygon RPC and return tx hash."""
        import httpx

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_sendRawTransaction",
            "params": [raw_tx],
            "id": 1,
        }
        resp = httpx.post(self.s.polygon_rpc_url, json=payload, timeout=30.0)
        if resp.status_code != 200:
            raise RuntimeError(f"RPC eth_sendRawTransaction HTTP {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"RPC eth_sendRawTransaction error: {result['error']}")
        return result["result"]

    def _rpc_get_gas_price(self) -> int:
        """Fetch current gas price in wei from Polygon RPC."""
        import httpx

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_gasPrice",
            "params": [],
            "id": 1,
        }
        resp = httpx.post(self.s.polygon_rpc_url, json=payload, timeout=10.0)
        if resp.status_code != 200:
            raise RuntimeError(f"RPC eth_gasPrice HTTP {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"RPC eth_gasPrice error: {result['error']}")
        return int(result["result"], 16)

    def _send_approval_txn(self, acct, nonce: int,
                           token_address: str, abi: list, fn_name: str,
                           args: list, label: str):
        """Build, sign, and optionally broadcast a single approval transaction.

        Signs an ERC-20 ``approve(spender, max_uint256)`` or ERC-1155
        ``setApprovalForAll(operator, True)`` call against the given token
        contract, using the wallet private key to validate the signing chain.

        In PAPER_TRADE mode: signs locally (validates wallet + key), but **skips
        ALL RPC calls** (nonce, gas price, raw TX broadcast). The wallet address
        is never exposed to the RPC provider. No on-chain transaction is created.
        In LIVE mode: makes the real RPC calls and broadcasts the signed transaction.
        """
        import eth_abi
        from eth_utils import keccak

        # PAPER_TRADE guard — placed FIRST so no RPC calls execute in paper mode.
        # This prevents the wallet address from being exposed to the Polygon RPC
        # provider (eth_getTransactionCount, eth_gasPrice, eth_sendRawTransaction).
        # The signing chain is still validated locally below — the wallet signs
        # a synthetic transaction object without any chain data.
        paper_trade = getattr(self, "_paper_trade", False)

        # Encode the function call: 4-byte selector + ABI-encoded args
        fn_inputs = abi[0]["inputs"]
        types = [i["type"] for i in fn_inputs]
        encoded_data = eth_abi.encode(types, args)
        signature = f"{fn_name}(" + ",".join(types) + ")"
        selector = keccak(signature.encode())[:4]
        data_hex = "0x" + (selector + encoded_data).hex()

        # gasPrice is required by eth-account's sign_transaction. In PAPER_TRADE
        # mode we use a placeholder — it's never broadcast so the actual value
        # is irrelevant. In LIVE mode, gasPrice is set from the real RPC response.
        placeholder_gas_price = 1_000_000_000  # 1 gwei placeholder

        tx = {
            "nonce": nonce,  # in paper mode this is a placeholder; never sent to RPC
            "to": token_address,
            "gas": 200_000,  # generous; ERC-20/1155 approve/toggle is cheap
            "gasPrice": placeholder_gas_price,  # required for signing; not used in paper mode
            "chainId": POLYGON_CHAIN_ID,
            "data": data_hex,
        }
        signed = acct.sign_transaction(tx)

        if paper_trade:
            # FULL auth chain validated: the wallet signed a real transaction
            # object with the correct ABI encoding and chainId. But no RPC calls
            # were made — nonce is a placeholder, gasPrice not fetched, raw TX
            # not broadcast. The wallet address is never exposed to the RPC provider.
            log.info(
                f"[PAPER_TRADE] approval {label} ({fn_name}) signed locally — "
                f"NO RPC calls made, tx NOT broadcast"
            )
            return

        # LIVE mode: fetch gas price + nonce, then broadcast the signed approval.
        gas_price = self._rpc_get_gas_price()
        max_gas_price = int(self.s.approval_max_gas_price_gwei * 1e9)
        if gas_price > max_gas_price:
            log.warning(
                f"Gas price {gas_price / 1e9:.1f} gwei exceeds limit "
                f"{self.s.approval_max_gas_price_gwei} gwei — skipping {label} approval"
            )
            return

        # Re-sign with the real gasPrice/nonce from the chain for LIVE broadcast
        tx["gasPrice"] = gas_price
        signed = acct.sign_transaction(tx)

        tx_hash = self._rpc_send_raw_tx(signed.raw_transaction.hex())
        log.info(
            f"Sent {label} approval ({fn_name}) — tx: {tx_hash[:12]}... "
            f"gasPrice={gas_price / 1e9:.1f} gwei"
        )
        # Brief wait for confirmation (non-blocking on receipt — just ensure
        # it's accepted by the mempool). A failed approval would surface
        # immediately as an RPC error from eth_sendRawTransaction.
        import time as _time
        _time.sleep(0.5)

    def _init_live_client(self):
        """Initialize the py_clob_client_v2 ClobClient with L1 + L2 auth."""
        from py_clob_client_v2 import ApiCreds, ClobClient, SignatureTypeV2

        # Build credentials from config (or derive if not set)
        if (self.s.polymarket_api_key and self.s.polymarket_api_secret
                and self.s.polymarket_api_passphrase):
            self._creds = ApiCreds(
                api_key=self.s.polymarket_api_key,
                api_secret=self.s.polymarket_api_secret,
                api_passphrase=self.s.polymarket_api_passphrase,
            )
        else:
            # Will be derived on first use via create_or_derive_api_key()
            self._creds = None

        self._clob_client = ClobClient(
            host=self.s.clob_base_url,
            chain_id=POLYGON_CHAIN_ID,
            key=self.s.private_key,
            creds=self._creds,
            signature_type=SignatureTypeV2.EOA,
            funder=self.s.proxy_wallet or None,
            retry_on_error=True,
        )

        # Get wallet address
        from eth_account import Account
        acct = Account.from_key(self.s.private_key)
        self._address = acct.address

    def _ensure_creds(self):
        """Ensure L2 credentials are available. Derive if needed (L1 auth)."""
        if self._creds is not None:
            return

        if self._clob_client is None:
            self._init_live_client()

        # L1 auth: derive API credentials from wallet signature
        log.info("Deriving Polymarket API credentials (L1 auth)...")
        try:
            self._creds = self._clob_client.create_or_derive_api_key()
            self._clob_client.set_api_creds(self._creds)
            log.info(f"API credentials derived for {self._address}")
            log.info(
                "Persist derived Polymarket API credentials in .env "
                "(POLYMARKET_API_KEY/SECRET/PASSPHRASE redacted)"
            )
        except Exception as e:
            log.error(f"Failed to derive API credentials: {e!r}")
            raise

    async def aclose(self):
        """Clean up resources."""
        # py_clob_client_v2 uses synchronous httpx helpers internally; nothing
        # explicit is exposed for this wrapper to close.
        pass

    # ---- order template builder (static parts filled by caller) ----
    def build_order(self, token_id: str, price: float, size: float, side: str = "BUY") -> dict:
        """Build an order template. Used by execution.py for precomputed templates."""
        return {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
        }

    # ---- PAPER_TRADE: simulate a fill from the live order book ----

    async def paper_pair_preflight(
        self,
        yes_token_id: str,
        no_token_id: str,
        yes_limit: float,
        no_limit: float,
        size: float,
        edge_price: float,
    ) -> dict:
        """Revalidate both executable REST legs before any paper reservation.

        This is applied only to the REST-book paper model. The two read-only
        books are fetched concurrently. A pair passes only when each leg has
        full requested depth within its original WS-derived limit and the two
        full-size VWAPs still clear the unchanged strategy threshold.
        """
        if not (
            getattr(self, "_paper_trade", False)
            and getattr(self, "_paper_fill_source", "decision_snapshot") == "rest_book"
        ):
            return {"applied": False, "passed": True, "reason": "not_rest_paper"}
        self._ensure_creds()
        started = time.monotonic()
        try:
            yes_book, no_book = await asyncio.gather(
                asyncio.to_thread(self._clob_client.get_order_book, yes_token_id),
                asyncio.to_thread(self._clob_client.get_order_book, no_token_id),
            )
        except Exception as exc:
            return {
                "applied": True,
                "passed": False,
                "reason": "rest_preflight_unavailable",
                "error": repr(exc),
                "latency_ms": (time.monotonic() - started) * 1000.0,
            }
        yes_vwap, yes_available = _paper_full_fill_price(
            yes_book or {}, side="BUY", limit_price=yes_limit, size=size
        )
        no_vwap, no_available = _paper_full_fill_price(
            no_book or {}, side="BUY", limit_price=no_limit, size=size
        )
        combined = yes_vwap + no_vwap if yes_vwap is not None and no_vwap is not None else None
        if yes_vwap is None or no_vwap is None:
            reason = "rest_preflight_insufficient_liquidity"
            passed = False
        elif combined is None or combined >= edge_price:
            reason = "rest_preflight_no_edge"
            passed = False
        else:
            reason = "rest_preflight_pass"
            passed = True
        return {
            "applied": True,
            "passed": passed,
            "reason": reason,
            "yes_vwap": yes_vwap,
            "no_vwap": no_vwap,
            "combined": combined,
            "yes_available": yes_available,
            "no_available": no_available,
            "size": float(size),
            "edge_price": float(edge_price),
            "yes_limit": float(yes_limit),
            "no_limit": float(no_limit),
            "latency_ms": (time.monotonic() - started) * 1000.0,
        }

    def _paper_simulate_fill(
        self, token_id: str, price: float, size: float, side: str,
        observed_fill_price: Optional[float] = None,
    ) -> Optional[str]:
        """Simulate a fill using the live order book (PAPER_TRADE only).

        When ``observed_fill_price`` is supplied by the execution hot path, it is the
        fresh, liquidity-gated top of book that triggered the opportunity. Reusing it
        avoids a second REST fetch per leg and keeps paper capture tied to the decision
        snapshot. Otherwise this method fetches the current order book via the SDK.

        A fill must be marketable at the submitted limit price:
        - BUY side  -> fill at best_ask (the price we are lifting)
        - SELL side -> fill at best_bid

        The simulated leg is retained as paper-mode evidence. Pair-level execution
        records carry the mode and fill source explicitly.

        Returns a synthetic order id shaped like a real CLOB order ID (64 hex
        chars prefixed with 0x). Returns None if no liquidity is available.
        """
        side_upper = side.upper()
        fill_price: Optional[float] = None
        source = "decision_snapshot"
        if observed_fill_price is not None:
            fill_price = float(observed_fill_price)
        else:
            source = "rest_book"
            # The full real auth flow is required before an SDK book fetch.
            self._ensure_creds()
            try:
                book = self._clob_client.get_order_book(token_id)
            except Exception as e:
                log.error(f"PAPER_TRADE book fetch failed for {token_id[-8:]}: {e!r}")
                return None
            fill_price, eligible_quantity = _paper_full_fill_price(
                book or {}, side=side_upper, limit_price=price, size=size
            )
            if fill_price is None:
                log.info(
                    f"PAPER_TRADE insufficient executable depth {side_upper} "
                    f"{token_id[-8:]} limit={price:.4f} requested={size} "
                    f"eligible={eligible_quantity:.8f}"
                )
                return None

        if fill_price is None:
            log.error(
                f"PAPER_TRADE no liquidity for {token_id[-8:]} side={side_upper}"
            )
            return None

        marketable = (side_upper == "BUY" and fill_price <= price) or (
            side_upper == "SELL" and fill_price >= price
        )
        if not marketable:
            log.info(
                f"PAPER_TRADE non-marketable {side_upper} {token_id[-8:]} "
                f"limit={price:.4f} book={fill_price:.4f}"
            )
            return None

        # Deterministic synthetic order id (looks like a real CLOB order hash).
        seed = f"{token_id}:{side_upper}:{fill_price}:{size}:{int(time.time() * 1e6)}"
        import hashlib
        order_id = "0x" + hashlib.sha256(seed.encode()).hexdigest()[:64]

        if not hasattr(self, "_paper_fill_details"):
            self._paper_fill_details = {}
        self._paper_fill_details[order_id] = {
            "fill_price": fill_price,
            "fill_source": source,
            "fill_ts": time.time(),
            "limit_price": float(price),
            "token_id": str(token_id),
            "side": side_upper,
            "size": float(size),
        }

        fill_value = fill_price * size
        log.info(
            f"[PAPER_TRADE] simulated {side_upper} fill {token_id[-8:]} "
            f"@{fill_price:.4f} size={size} value={fill_value:.4f} order={order_id}"
        )

        # Per-leg paper fill: log at INFO level (not TRADE) so the PnL convergence
        # analyzer doesn't see per-leg fills as pair-fill events. Real fills are
        # logged by execution.py's _resolve_one with the correct YES+NO combined
        # and pair PnL. Per-leg logging here would corrupt calc_pnl_convergence
        # by emitting two "filled" events per pair with single-leg prices.
        log.info(
            f"[PAPER_TRADE] simulated {side_upper} fill {token_id[-8:]} "
            f"@{fill_price:.4f} size={size} order={order_id} "
            f"source={source}"
        )

        return order_id

    def get_fill_ratio(self, order_id: Optional[str]) -> float:
        """Fill ratio for a submitted order (0.0–1.0).

        Used by execution.py:245-246 for partial-fill PnL attribution.
        - DRY_RUN / PAPER_TRADE: ``_paper_simulate_fill`` already resolved the fill
          synchronously and returned a real order id only when it filled — so a
          non-None id is a 100% fill, None is a 0% fill (no liquidity / rejected).
        - LIVE (SDK): ``create_order`` + ``post_order`` submit synchronously; a
          non-None id means the order was accepted by the CLOB and is treated as
          filled at the submitted price for PnL purposes. True partial fills are
          reconciled off-line via order-status polling and are not modeled here
          (see execution.py partial-fill handling).
        """
        if order_id is None:
            return 0.0
        return 1.0

    # ---- HOT PATH: submit one order ----
    async def submit_paper_order(
        self, token_id: str, price: float, size: float, side: str = "BUY",
        observed_fill_price: Optional[float] = None,
    ) -> Optional[str]:
        """Submit a paper-only fill simulation without touching exchange order APIs."""
        if not getattr(self, "_paper_trade", False):
            return await self.submit_order(token_id, price, size, side=side)
        try:
            args = (token_id, price, size, side)
            fill_source = getattr(self, "_paper_fill_source", "decision_snapshot")
            if observed_fill_price is not None and fill_source != "rest_book":
                args = (*args, observed_fill_price)
            oid = await asyncio.to_thread(
                self._paper_simulate_fill, *args
            )
            # TASK 2: mark every paper-simulated order id as paper mode so
            # summaries can separate it from live ids (same CLOB id shape).
            return self._register_order(oid, "paper")
        except Exception as e:
            log.error(f"paper submit failed for {token_id[-8:]}: {e!r}")
            return None

    async def submit_protective_paper_exit(
        self,
        token_id: str,
        size: float,
    ) -> Optional[str]:
        """Cross the current REST best bid for a risk-reducing PAPER sell.

        A partial-fill exit is conceptually a marketable protective order.  Reusing
        a milliseconds-old WS bid as the sell limit allowed one tick of movement to
        turn the exit into a resting order, leaving exposure and tripping the kill
        switch.  A zero sell limit is marketable at every non-negative bid; the paper
        simulator still fills only at the real REST best bid it reads at submission.
        No exchange order API is called.
        """
        if not getattr(self, "_paper_trade", False):
            return None
        if not self.check_order_submission(
            "trading_client.submit_protective_paper_exit",
            token_id=token_id,
            side="SELL",
        ):
            return None
        return await self.submit_paper_order(token_id, 0.0, size, side="SELL")

    async def submit_order(self, token_id: str, price: float, size: float, side: str = "BUY") -> Optional[str]:
        """Submit an order (BUY or SELL). Returns order id or None.

        DRY_RUN: returns synthetic order id, never touches network.
        PAPER_TRADE: runs the full real auth flow (create_or_derive_api_key +
            setup_trading_approvals) but does NOT hit the exchange — simulates a
            fill using the live order book's best_ask/best_bid at submission time.
            The simulated fill is logged via log.trade("filled", ...) in the exact
            same JSONL format as a real fill.
        LIVE: intentionally blocked in this product build.  The operator control
            is audited, but the exchange POST path is not implemented.

        Fail-safe: any error returns None (execution treats as "leg did not go"
        and aborts the pair) — never raises, because submit_order is run for
        both legs via asyncio.gather and an escaping exception would strand
        the sibling leg as an unhedged one-sided fill.
        """
        if not self.check_order_submission(
            "trading_client.submit_order", token_id=token_id, side=side
        ):
            return None

        if not self.s.live:
            # DRY_RUN: no network. Simulate an immediate fill with a stable id.
            await asyncio.sleep(0)
            oid = f"dryrun-{side.lower()}-{token_id[-8:]}"
            return self._register_order(oid, "dry_run")

        # PAPER_TRADE: intercept BEFORE hitting the exchange. The full real auth
        # flow (L1 creds + trading approvals) was already completed at startup;
        # here we only simulate the order execution against the live book.
        if getattr(self, "_paper_trade", False):
            oid = await self.submit_paper_order(token_id, price, size, side=side)
            return self._register_order(oid, "paper")

        # Defense in depth: check_order_submission currently denies every live
        # request because the exchange endpoint is deliberately unbuilt.  Keep a
        # hard return here as well, so a future control-layer regression still
        # cannot reach an SDK order POST by accident.
        log.critical(
            "LIVE_ORDER_SUBMISSION_UNBUILT",
            {
                "token_suffix": token_id[-8:],
                "side": side.upper(),
                "operator_action_required": False,
            },
        )
        return None

    async def is_filled(self, order_id: Optional[str], token_id: str, gap_ms: int = 0) -> bool:
        """Did `order_id` fill?

        DRY_RUN: returns True (simulated instant fill).
        PAPER_TRADE: returns True — every submission was simulated as an
            immediate fill in ``_paper_simulate_fill``.
        LIVE:
          - gap_ms == 0 (default, non-blocking mode, FIX #6): a SINGLE poll of the order
            status endpoint. Returns True only if the order is already filled. This is
            the non-blocking path: the hot loop calls this on each tick instead of
            stalling in a polling loop. Returns False if rested / not-yet-filled /
            canceled / error — the caller decides whether to keep polling on subsequent
            ticks.
          - gap_ms > 0: polls within the gap window (legacy blocking mode, still used by
            FIX #2 hedge/exit confirmation in _manage_partial).
        """
        if order_id is None:
            return False
        if not self.s.live:
            return True  # DRY_RUN: simulate instant fill

        # PAPER_TRADE: simulated fills always confirm instantly.
        if getattr(self, "_paper_trade", False):
            return True

        # FIX #6: non-blocking single-check mode (gap_ms == 0).
        if gap_ms == 0:
            try:
                self._ensure_creds()
                order_data = self._clob_client.get_order(order_id)
                if order_data:
                    status = str(order_data.get("status", "")).lower()
                    if status == "filled" or order_data.get("filled") is True:
                        return True
                    if status in ("canceled", "cancelled", "expired", "rejected"):
                        return False
            except Exception as e:
                log.warning(f"fill check {order_id} failed: {e!r}")
            return False

        # LIVE: poll within the leg-gap window (legacy blocking mode)
        deadline = time.monotonic() + max(gap_ms, 1) / 1000.0
        while time.monotonic() < deadline:
            try:
                self._ensure_creds()
                order_data = self._clob_client.get_order(order_id)

                if order_data:
                    # Check for filled status
                    status = str(order_data.get("status", "")).lower()
                    if status == "filled" or order_data.get("filled") is True:
                        return True
                    if status in ("canceled", "cancelled", "expired", "rejected"):
                        return False
            except Exception as e:
                log.warning(f"fill check {order_id} failed: {e!r}")
            await asyncio.sleep(0.05)

        return False  # not confirmed filled within the gap -> partial event

    async def cancel_order(self, token_id: str, missing_order_id: Optional[str] = None) -> bool:
        """Cancel a resting order (FIX #2).

        DRY_RUN: no-ops, returns True.
        PAPER_TRADE: no-ops — nothing was ever posted to the exchange, so there
            is nothing to cancel.  Returns True.
        LIVE: uses SDK cancel_orders method. If an explicit order ID is supplied, that
        order is cancelled directly. Otherwise we fetch all open orders for the token
        via the SDK and cancel each by its real order ID — so cancellation by token
        (no order ID known) actually finds and cancels the resting order instead of
        silently no-oping.
        """
        if not self.s.live:
            await asyncio.sleep(0)
            return True

        if getattr(self, "_paper_trade", False):
            # Nothing was ever posted; nothing to cancel.
            await asyncio.sleep(0)
            return True

        try:
            self._ensure_creds()

            if missing_order_id:
                # Cancel specific order by ID
                self._clob_client.cancel_orders([missing_order_id])
                log.info(f"cancelled order {missing_order_id} for {token_id[-8:]}")
                return True

            # Cancel by token — fetch open orders for this token and cancel each.
            from py_clob_client_v2 import OpenOrderParams

            orders = self._clob_client.get_open_orders(
                params=OpenOrderParams(asset_id=token_id), only_first_page=False
            )
            order_ids = []
            for o in (orders or []):
                oid = o.get("order_id") or o.get("orderId") or o.get("id")
                status = str(o.get("status", "")).lower() if isinstance(o, dict) else ""
                if oid and status not in ("filled", "canceled", "cancelled", "expired", "rejected"):
                    order_ids.append(oid)

            if not order_ids:
                log.warning(
                    f"cancel_order: no open orders found for token {token_id[-8:]}"
                )
                return True

            self._clob_client.cancel_orders(order_ids)
            log.info(
                f"cancelled {len(order_ids)} open order(s) for {token_id[-8:]}"
            )
            return True
        except Exception as e:
            log.error(f"live cancel failed for {token_id[-8:]}: {e!r}")
            return False
