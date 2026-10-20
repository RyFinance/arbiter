"""Read-only prediction-market account connectors (Polymarket, Kalshi).

Linking a venue lets the dashboard show the operator's REAL account next to
the paper engine: cash, open positions, account value, P&L and volume traded.
It never trades. This module is the whole boundary and it is read-only by
construction, not by flag:

  * Polymarket is read through the public Data API (GET only) plus a
    Polygon ``eth_call`` of the collateral ``balanceOf``. It needs only the
    account's public wallet address -- no private key, no CLOB credentials.
  * Kalshi is read through its signed REST API using GET requests only.
    ``_KalshiClient._get`` is the single request path and it hard-codes the
    method; there is no POST/DELETE code path, so no order can be created
    or cancelled from here.

Credentials come from the environment first (see .env.example), then from
``state/venues.local.json`` written by the dashboard's link form (mode 0600,
git-ignored). Adding a venue = subclass ``VenueConnector`` and register it in
``CONNECTORS``.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
CREDENTIALS_PATH = STATE_DIR / "venues.local.json"
KALSHI_KEY_PATH = STATE_DIR / "kalshi_private_key.local.pem"

CACHE_TTL_S = float(os.getenv("VENUE_REFRESH_S", "20"))
VOLUME_TTL_S = 300.0  # trade history is paged; refresh it less often than balances
HTTP_TIMEOUT_S = 8.0

POLYMARKET_DATA_API = os.getenv("POLYMARKET_DATA_API", "https://data-api.polymarket.com")
# Polymarket collateral (pUSD), 6 decimals. Same address btc5m's trading client pins.
POLYMARKET_COLLATERAL = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
POLYGON_RPC_URLS = tuple(
    url for url in (
        os.getenv("POLYGON_RPC_URL", "").strip(),
        "https://polygon.drpc.org",
        "https://polygon-rpc.com",
    ) if url
)
KALSHI_BASE_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
KEY_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Position:
    market: str
    outcome: str
    size: float
    avg_price: Optional[float] = None
    cost_usd: Optional[float] = None
    value_usd: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    realized_pnl: Optional[float] = None
    url: Optional[str] = None


@dataclass
class VenueAccount:
    venue: str
    label: str
    linked: bool = False
    status: str = "unlinked"  # unlinked | ok | error
    identity: Optional[str] = None  # masked address / key id
    environment: Optional[str] = None
    cash_usd: Optional[float] = None
    positions_value_usd: Optional[float] = None
    account_value_usd: Optional[float] = None
    cost_basis_usd: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    realized_pnl: Optional[float] = None
    volume_usd: Optional[float] = None
    volume_note: Optional[str] = None
    open_positions: int = 0
    positions: List[Position] = field(default_factory=list)
    latency_ms: Optional[int] = None
    checked_at: Optional[float] = None
    error: Optional[str] = None
    read_only: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _mask(value: str, keep: int = 4) -> str:
    if len(value) <= keep * 2 + 2:
        return value
    return f"{value[:keep + 2]}…{value[-keep:]}"


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def _sum(values) -> Optional[float]:
    numbers = [v for v in values if v is not None]
    return round(sum(numbers), 4) if numbers else None


# --------------------------------------------------------------------------
# Credential store
# --------------------------------------------------------------------------

_store_lock = threading.Lock()


def _read_store() -> Dict[str, Dict[str, str]]:
    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def _write_store(data: Dict[str, Dict[str, str]]) -> None:
    _write_private(CREDENTIALS_PATH, json.dumps(data, indent=2, sort_keys=True))


# --------------------------------------------------------------------------
# Connectors
# --------------------------------------------------------------------------

class VenueConnector:
    venue = ""
    label = ""

    def credentials(self) -> Optional[Dict[str, str]]:
        raise NotImplementedError

    def fetch(self, creds: Dict[str, str], client: httpx.Client) -> VenueAccount:
        raise NotImplementedError

    def validate_link(self, payload: Dict[str, Any]) -> Dict[str, str]:
        raise NotImplementedError

    def save_link(self, creds: Dict[str, str]) -> None:
        with _store_lock:
            store = _read_store()
            store[self.venue] = creds
            _write_store(store)

    def unlink(self) -> None:
        with _store_lock:
            store = _read_store()
            store.pop(self.venue, None)
            _write_store(store)

    def env_managed(self) -> bool:
        return False

    def account(self, client: httpx.Client) -> VenueAccount:
        creds = self.credentials()
        if not creds:
            return VenueAccount(venue=self.venue, label=self.label)
        started = time.perf_counter()
        try:
            acct = self.fetch(creds, client)
            acct.status = "ok"
        except Exception as exc:  # surfaced on the dashboard, never raised
            acct = VenueAccount(venue=self.venue, label=self.label, status="error",
                                error=f"{type(exc).__name__}: {exc}"[:240])
            acct.identity = self.identity(creds)
        acct.linked = True
        acct.latency_ms = int((time.perf_counter() - started) * 1000)
        acct.checked_at = time.time()
        return acct

    def identity(self, creds: Dict[str, str]) -> str:
        return ""


class PolymarketConnector(VenueConnector):
    venue = "polymarket"
    label = "Polymarket"

    def env_managed(self) -> bool:
        return bool(os.getenv("POLYMARKET_ADDRESS") or os.getenv("PROXY_WALLET"))

    def credentials(self) -> Optional[Dict[str, str]]:
        address = (os.getenv("POLYMARKET_ADDRESS") or os.getenv("PROXY_WALLET") or "").strip()
        if not address:
            address = str(_read_store().get(self.venue, {}).get("address", "")).strip()
        return {"address": address} if ADDRESS_RE.match(address) else None

    def identity(self, creds: Dict[str, str]) -> str:
        return _mask(creds["address"])

    def validate_link(self, payload: Dict[str, Any]) -> Dict[str, str]:
        address = str(payload.get("address", "")).strip()
        if not ADDRESS_RE.match(address):
            raise ValueError("Enter the 0x… wallet address shown on your Polymarket profile (42 characters).")
        return {"address": address}

    def _cash(self, address: str, client: httpx.Client) -> Optional[float]:
        data = "0x70a08231" + address.lower().replace("0x", "").rjust(64, "0")  # balanceOf(address)
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": POLYMARKET_COLLATERAL, "data": data}, "latest"]}
        for url in POLYGON_RPC_URLS:
            try:
                result = client.post(url, json=payload).json().get("result")
                if isinstance(result, str) and result.startswith("0x"):
                    return int(result, 16) / 1e6
            except (httpx.HTTPError, ValueError):
                continue
        return None

    _volume_cache: Dict[str, tuple] = {}

    def _volume(self, address: str, client: httpx.Client) -> tuple[Optional[float], Optional[str]]:
        cached = self._volume_cache.get(address)
        if cached and time.time() - cached[0] < VOLUME_TTL_S:
            return cached[1]
        result = self._walk_volume(address, client)
        self._volume_cache[address] = (time.time(), result)
        return result

    def _walk_volume(self, address: str, client: httpx.Client) -> tuple[Optional[float], Optional[str]]:
        total, seen, page = 0.0, 0, 500
        for offset in range(0, 3000, page):
            resp = client.get(f"{POLYMARKET_DATA_API}/activity",
                              params={"user": address, "type": "TRADE", "limit": page, "offset": offset})
            resp.raise_for_status()
            rows = resp.json() or []
            total += sum(_num(r.get("usdcSize")) or 0.0 for r in rows)
            seen += len(rows)
            if len(rows) < page:
                return round(total, 2), f"{seen} trades"
        return round(total, 2), f"last {seen} trades"

    def fetch(self, creds: Dict[str, str], client: httpx.Client) -> VenueAccount:
        address = creds["address"]
        resp = client.get(f"{POLYMARKET_DATA_API}/positions",
                          params={"user": address, "sizeThreshold": 0.01, "limit": 500})
        resp.raise_for_status()
        positions = []
        for row in resp.json() or []:
            # Resolved losers stay in the feed at $0; they are history, not open risk.
            if (_num(row.get("currentValue")) or 0.0) < 0.01:
                continue
            slug = row.get("eventSlug") or row.get("slug")
            positions.append(Position(
                market=str(row.get("title") or row.get("slug") or "?"),
                outcome=str(row.get("outcome") or ""),
                size=_num(row.get("size")) or 0.0,
                avg_price=_num(row.get("avgPrice")),
                cost_usd=_num(row.get("initialValue")),
                value_usd=_num(row.get("currentValue")),
                unrealized_pnl=_num(row.get("cashPnl")),
                realized_pnl=_num(row.get("realizedPnl")),
                url=f"https://polymarket.com/event/{slug}" if slug else None,
            ))
        positions.sort(key=lambda p: p.value_usd or 0.0, reverse=True)
        cash = self._cash(address, client)
        positions_value = _sum(p.value_usd for p in positions) or 0.0
        volume, note = self._volume(address, client)
        return VenueAccount(
            venue=self.venue, label=self.label, identity=self.identity(creds),
            environment="polygon",
            cash_usd=cash, positions_value_usd=positions_value,
            account_value_usd=round(positions_value + (cash or 0.0), 2),
            cost_basis_usd=_sum(p.cost_usd for p in positions),
            unrealized_pnl=_sum(p.unrealized_pnl for p in positions),
            realized_pnl=_sum(p.realized_pnl for p in positions),
            volume_usd=volume, volume_note=note,
            open_positions=len(positions), positions=positions[:50],
        )


class _KalshiClient:
    """Signed, GET-only Kalshi REST client. There is deliberately no other verb."""

    def __init__(self, key_id: str, pem: bytes, environment: str, client: httpx.Client):
        from cryptography.hazmat.primitives import serialization

        self.key_id = key_id
        self.base = KALSHI_BASE_URLS.get(environment, KALSHI_BASE_URLS["prod"])
        self.prefix = httpx.URL(self.base).path
        self.key = serialization.load_pem_private_key(pem, password=None)
        self.client = client

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}GET{self.prefix}{path}".encode()
        signature = self.key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        resp = self.client.get(f"{self.base}{path}", params=params, headers={
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        })
        if resp.status_code == 401:
            raise PermissionError("Kalshi rejected the key (check key ID, private key and prod/demo)")
        resp.raise_for_status()
        return resp.json()


def _usd(row: Dict[str, Any], name: str) -> Optional[float]:
    """Kalshi returns cents (``x``) and, on newer API versions, dollars (``x_dollars``)."""
    dollars = _num(row.get(f"{name}_dollars"))
    if dollars is not None:
        return dollars
    cents = _num(row.get(name))
    return None if cents is None else cents / 100.0


class KalshiConnector(VenueConnector):
    venue = "kalshi"
    label = "Kalshi"

    def env_managed(self) -> bool:
        return bool(os.getenv("KALSHI_API_KEY_ID"))

    def credentials(self) -> Optional[Dict[str, str]]:
        key_id = os.getenv("KALSHI_API_KEY_ID", "").strip()
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        environment = os.getenv("KALSHI_ENV", "prod").strip().lower()
        if not key_id:
            stored = _read_store().get(self.venue, {})
            key_id = str(stored.get("key_id", "")).strip()
            key_path = str(stored.get("key_path", "")).strip()
            environment = str(stored.get("environment", "prod")).strip().lower()
        if not key_id or not key_path:
            return None
        return {"key_id": key_id, "key_path": os.path.expanduser(key_path), "environment": environment}

    def identity(self, creds: Dict[str, str]) -> str:
        return _mask(creds["key_id"])

    def validate_link(self, payload: Dict[str, Any]) -> Dict[str, str]:
        from cryptography.hazmat.primitives import serialization

        key_id = str(payload.get("key_id", "")).strip()
        pem = str(payload.get("private_key", "")).strip()
        environment = str(payload.get("environment", "prod")).strip().lower()
        if not KEY_ID_RE.match(key_id):
            raise ValueError("Enter the API key ID from Kalshi → Account → API keys.")
        if environment not in KALSHI_BASE_URLS:
            raise ValueError("Environment must be prod or demo.")
        try:
            serialization.load_pem_private_key(pem.encode(), password=None)
        except Exception:
            raise ValueError("Paste the full RSA private key (-----BEGIN … PRIVATE KEY----- block).")
        _write_private(KALSHI_KEY_PATH, pem + "\n")
        return {"key_id": key_id, "key_path": str(KALSHI_KEY_PATH), "environment": environment}

    def unlink(self) -> None:
        super().unlink()
        KALSHI_KEY_PATH.unlink(missing_ok=True)

    def fetch(self, creds: Dict[str, str], client: httpx.Client) -> VenueAccount:
        pem = Path(creds["key_path"]).read_bytes()
        api = _KalshiClient(creds["key_id"], pem, creds["environment"], client)
        balance = api._get("/portfolio/balance")
        rows: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(10):
            params = {"limit": 200, "count_filter": "position"}
            if cursor:
                params["cursor"] = cursor
            page = api._get("/portfolio/positions", params)
            rows.extend(page.get("market_positions") or [])
            cursor = page.get("cursor")
            if not cursor:
                break
        positions = []
        for row in rows:
            contracts = _num(row.get("position_fp")) or _num(row.get("position")) or 0.0
            exposure = _usd(row, "market_exposure")
            positions.append(Position(
                market=str(row.get("ticker") or "?"),
                outcome="YES" if contracts >= 0 else "NO",
                size=abs(contracts),
                avg_price=(exposure / abs(contracts)) if exposure is not None and contracts else None,
                cost_usd=exposure,
                realized_pnl=_usd(row, "realized_pnl"),
                url=f"https://kalshi.com/markets/{str(row.get('ticker', '')).lower()}",
            ))
        open_positions = [p for p in positions if p.size]
        open_positions.sort(key=lambda p: p.cost_usd or 0.0, reverse=True)
        cash = _usd(balance, "balance")
        positions_value = _usd(balance, "portfolio_value")
        cost = _sum(p.cost_usd for p in open_positions)
        return VenueAccount(
            venue=self.venue, label=self.label, identity=self.identity(creds),
            environment=creds["environment"],
            cash_usd=cash, positions_value_usd=positions_value,
            account_value_usd=round((cash or 0.0) + (positions_value or 0.0), 2),
            cost_basis_usd=cost,
            unrealized_pnl=(round(positions_value - cost, 2)
                            if positions_value is not None and cost is not None else None),
            realized_pnl=_sum(p.realized_pnl for p in positions),
            volume_usd=_sum(_usd(r, "total_traded") for r in rows), volume_note="lifetime, open + settled",
            open_positions=len(open_positions), positions=open_positions[:50],
        )


CONNECTORS: Dict[str, VenueConnector] = {
    c.venue: c for c in (PolymarketConnector(), KalshiConnector())
}


# --------------------------------------------------------------------------
# Trading modes per venue: read_only | paper | live
# --------------------------------------------------------------------------

MODES_PATH = STATE_DIR / "venue_modes.local.json"
MODE_AUDIT_PATH = STATE_DIR / "venue_mode_audit.jsonl"
MODES = ("read_only", "paper", "live")
DEFAULT_MODES = {"polymarket": "paper", "kalshi": "paper"}


def get_mode(venue: str) -> str:
    try:
        data = json.loads(MODES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    mode = str(data.get(venue, DEFAULT_MODES.get(venue, "read_only")))
    if mode == "live" and not live_ready(venue)[0]:
        return "paper"  # fail closed if the env opt-in or key disappeared
    return mode if mode in MODES else "read_only"


def live_ready(venue: str) -> tuple[bool, str]:
    if venue == "kalshi":
        return kalshi_live_ready()
    return polymarket_live_ready()


def kalshi_live_ready() -> tuple[bool, str]:
    if os.getenv("ARBITER_LIVE_TRADING", "").strip().lower() != "enabled":
        return False, "Set ARBITER_LIVE_TRADING=enabled in .env to allow live trading."
    if CONNECTORS["kalshi"].credentials() is None:
        return False, "Live Kalshi trading needs a linked Kalshi API key (Venues sheet)."
    return True, ""


def polymarket_live_ready() -> tuple[bool, str]:
    from .config import SETTINGS
    if os.getenv("ARBITER_LIVE_TRADING", "").strip().lower() != "enabled":
        return False, "Set ARBITER_LIVE_TRADING=enabled in .env to allow live trading."
    try:
        from eth_account import Account
        Account.from_key(str(SETTINGS.private_key or ""))
    except Exception:
        return False, "Live trading needs a valid PRIVATE_KEY in .env (the account's signer)."
    return True, ""


def live_setup_status(venue: str = "polymarket") -> Dict[str, Any]:
    """What is still missing before a venue can trade live, in setup order."""
    from .config import SETTINGS
    if venue == "kalshi":
        steps = [
            {"id": "link", "label": "Link your Kalshi API key",
             "done": CONNECTORS["kalshi"].credentials() is not None,
             "how": "Venues sheet → Kalshi → API key ID + private key (or KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH in .env). Use KALSHI_ENV=demo to practise."},
            {"id": "opt_in", "label": "Opt in: ARBITER_LIVE_TRADING=enabled",
             "done": os.getenv("ARBITER_LIVE_TRADING", "").strip().lower() == "enabled",
             "how": "universe/.env → ARBITER_LIVE_TRADING=enabled, then restart the dashboard."},
            {"id": "mode", "label": "Pick LIVE for Kalshi",
             "done": get_mode("kalshi") == "live",
             "how": "Venues sheet → Kalshi → Mode → Live, type LIVE to confirm. Then choose Kalshi at the top and press Start."},
        ]
        return {"ready": all(step["done"] for step in steps[:2]), "live": steps[2]["done"], "steps": steps}
    try:
        from eth_account import Account
        Account.from_key(str(SETTINGS.private_key or ""))
        key_ok = True
    except Exception:
        key_ok = False
    steps = [
        {"id": "link", "label": "Link your Polymarket wallet address",
         "done": CONNECTORS["polymarket"].credentials() is not None,
         "how": "Venues sheet → Polymarket → paste the 0x… address from your profile."},
        {"id": "key", "label": "Add PRIVATE_KEY to .env",
         "done": key_ok,
         "how": "universe/.env → PRIVATE_KEY=0x… (your account's signer; for email accounts also PROXY_WALLET and POLYMARKET_SIGNATURE_TYPE=proxy). Restart the dashboard."},
        {"id": "opt_in", "label": "Opt in: ARBITER_LIVE_TRADING=enabled",
         "done": os.getenv("ARBITER_LIVE_TRADING", "").strip().lower() == "enabled",
         "how": "universe/.env → ARBITER_LIVE_TRADING=enabled, then restart the dashboard."},
        {"id": "mode", "label": "Pick LIVE for Polymarket",
         "done": get_mode("polymarket") == "live",
         "how": "Venues sheet → Polymarket → Mode → Live, type LIVE to confirm. Applies on the next Start."},
    ]
    return {"ready": all(step["done"] for step in steps[:3]), "live": steps[3]["done"], "steps": steps}


def mode_options(venue: str) -> Dict[str, Dict[str, Any]]:
    from .config import SETTINGS
    caps = (f"Real orders, fill-or-kill, max ${SETTINGS.live_max_order_usd:g}/order, "
            f"${SETTINGS.live_max_open_usd:g} open, stops at -${SETTINGS.live_max_session_loss_usd:g}. "
            "Applies on the next Start.")
    if venue == "polymarket":
        ready, reason = polymarket_live_ready()
        return {
            "read_only": {"available": True, "note": "View the linked account; the engine still paper-trades."},
            "paper": {"available": True, "note": "The engine trades simulated fills against live Polymarket books."},
            "live": {"available": ready, "reason": reason, "note": caps},
        }
    ready, reason = kalshi_live_ready()
    return {
        "read_only": {"available": True, "note": "View the linked account; the engine still paper-trades."},
        "paper": {"available": True, "note": "Choose Kalshi at the top: the engine trades simulated fills against live Kalshi books. No key needed."},
        "live": {"available": ready, "reason": reason, "note": caps},
    }


def set_mode(venue: str, mode: str, confirm: str = "") -> None:
    if venue not in CONNECTORS:
        raise KeyError(venue)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    option = mode_options(venue)[mode]
    if not option.get("available"):
        raise ValueError(option.get("reason") or f"{mode} is not available for {venue}")
    if mode == "live" and confirm != "LIVE":
        raise ValueError("Live mode needs the typed confirmation LIVE.")
    with _store_lock:
        try:
            data = json.loads(MODES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        previous = data.get(venue, DEFAULT_MODES.get(venue, "read_only"))
        data[venue] = mode
        _write_private(MODES_PATH, json.dumps(data, indent=2, sort_keys=True))
        MODE_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MODE_AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), "venue": venue, "from": previous, "to": mode}) + "\n")
    # The audited operator flag is on while any venue is live; leaving live
    # revokes permission for the running process on its next order.
    from .live_control import default_live_control
    any_live = any(data.get(name) == "live" for name in CONNECTORS)
    default_live_control().set_operator_enabled(
        any_live, actor="dashboard", reason=f"{venue} mode {previous} -> {mode}"
    )


# --------------------------------------------------------------------------
# Cached aggregate used by the dashboard
# --------------------------------------------------------------------------

_cache: Dict[str, Any] = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()


def invalidate() -> None:
    with _cache_lock:
        _cache["at"] = 0.0


def snapshot(force: bool = False, client_factory: Callable[[], httpx.Client] = None) -> Dict[str, Any]:
    with _cache_lock:
        if not force and _cache["data"] is not None and time.time() - _cache["at"] < CACHE_TTL_S:
            return _cache["data"]
        factory = client_factory or (lambda: httpx.Client(timeout=HTTP_TIMEOUT_S))
        with factory() as client:
            accounts = [c.account(client) for c in CONNECTORS.values()]
        linked = [a for a in accounts if a.linked and a.status == "ok"]
        data = {
            "generated_at": time.time(),
            "read_only": True,
            "live_setup": live_setup_status("polymarket"),
            "live_setup_kalshi": live_setup_status("kalshi"),
            "venues": [dict(a.to_dict(), env_managed=CONNECTORS[a.venue].env_managed(),
                            mode=get_mode(a.venue), modes=mode_options(a.venue)) for a in accounts],
            "totals": {
                "linked": len([a for a in accounts if a.linked]),
                "account_value_usd": _sum(a.account_value_usd for a in linked),
                "cash_usd": _sum(a.cash_usd for a in linked),
                "positions_value_usd": _sum(a.positions_value_usd for a in linked),
                "unrealized_pnl": _sum(a.unrealized_pnl for a in linked),
                "realized_pnl": _sum(a.realized_pnl for a in linked),
                "volume_usd": _sum(a.volume_usd for a in linked),
                "open_positions": sum(a.open_positions for a in linked),
            },
        }
        _cache.update(at=time.time(), data=data)
        return data


def link(venue: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    connector = CONNECTORS.get(venue)
    if connector is None:
        raise KeyError(venue)
    connector.save_link(connector.validate_link(payload))
    invalidate()
    return snapshot(force=True)


def change_mode(venue: str, mode: str, confirm: str = "") -> Dict[str, Any]:
    set_mode(venue, mode, confirm)
    invalidate()
    return snapshot(force=True)


def unlink(venue: str) -> Dict[str, Any]:
    connector = CONNECTORS.get(venue)
    if connector is None:
        raise KeyError(venue)
    connector.unlink()
    invalidate()
    return snapshot(force=True)
