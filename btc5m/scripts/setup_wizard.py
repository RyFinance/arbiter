#!/usr/bin/env python3
"""Interactive setup and validation for dry-run or live-data paper mode.

This wizard never enables real order submission.  It supports only ``dry-run``
and ``paper`` profiles, initializes the master control disabled, masks secrets
in output, and can perform read-only network checks with ``--online``.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from getpass import getpass
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from typing import Any, Mapping
from urllib.parse import urlparse

from dotenv import dotenv_values
from eth_account import Account
from eth_utils import to_checksum_address

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from polymarket_arb.live_control import LiveTradingControl  # noqa: E402
from polymarket_arb.wallet_status import collect_wallet_status, save_wallet_status  # noqa: E402


SUPPORTED_PROFILES = ("dry-run", "paper")
DEFAULTS = {
    "GAMMA_BASE_URL": "https://gamma-api.polymarket.com",
    "CLOB_BASE_URL": "https://clob.polymarket.com",
    "CLOB_WS_URL": "wss://ws-subscriptions-clob.polymarket.com",
    "POLYGON_RPC_URL": "https://polygon.drpc.org",
    "WINDOW_MODE": "true",
    "WINDOW_ASSET": "btc",
    "WINDOW_TIMEFRAME": "5m",
    "WINDOW_SECONDS": "300",
    "MIN_EDGE": "0.01",
    "ORDER_SIZE": "10.0",
    "MAX_QUOTE_AGE_MS": "500",
    "CROSS_LEG_SETTLE_MS": "10.0",
    "MAX_LEG_GAP_MS": "300",
    "MAX_POSITION_PER_MARKET": "250.0",
    "MAX_TOTAL_CAPITAL": "1000.0",
    "ALERT_WEBHOOK_URL": "",
    "PRIVATE_KEY": "",
    "PROXY_WALLET": "",
    "POLYMARKET_API_KEY": "",
    "POLYMARKET_API_SECRET": "",
    "POLYMARKET_API_PASSPHRASE": "",
    "LSE_API_KEY": "",
}
SECRET_KEYS = {
    "PRIVATE_KEY",
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "LSE_API_KEY",
}
REQUIRED_IMPORTS = (
    "httpx",
    "streamlit",
    "eth_account",
    "py_clob_client_v2",
)


@dataclass(frozen=True)
class ValidationResult:
    name: str
    status: str
    detail: str


def _bool(value: Any) -> bool | None:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def profile_values(profile: str, supplied: Mapping[str, Any]) -> dict[str, str]:
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(f"unsupported profile {profile!r}; choose dry-run or paper")
    values = {key: str(value) for key, value in DEFAULTS.items()}
    values.update({key: str(value) for key, value in supplied.items() if value is not None})
    if profile == "dry-run":
        values.update(
            {
                "DRY_RUN": "true",
                "PAPER_TRADE": "false",
                "LIVE_SIGNING_VERIFIED": "false",
            }
        )
    else:
        values.update(
            {
                "DRY_RUN": "false",
                "PAPER_TRADE": "true",
                # The existing client requires this to initialize authenticated
                # paper mode.  The independent live-control layer remains OFF and
                # the live exchange POST endpoint is absent.
                "LIVE_SIGNING_VERIFIED": "true",
            }
        )
    return values


def validate_config(values: Mapping[str, Any]) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    dry_run = _bool(values.get("DRY_RUN", ""))
    paper = _bool(values.get("PAPER_TRADE", ""))
    if dry_run is None or paper is None:
        results.append(ValidationResult("execution_mode", "fail", "invalid boolean value"))
    elif dry_run and not paper:
        results.append(ValidationResult("execution_mode", "pass", "dry-run; no network orders"))
    elif paper and not dry_run:
        results.append(
            ValidationResult("execution_mode", "pass", "paper; real market data, simulated fills")
        )
    else:
        results.append(
            ValidationResult(
                "execution_mode",
                "fail",
                "must select exactly one safe mode; real-order mode is unsupported",
            )
        )

    key = str(values.get("PRIVATE_KEY", "") or "").strip()
    if paper:
        if not key:
            results.append(
                ValidationResult("private_key", "fail", "required for authenticated paper mode")
            )
        else:
            try:
                wallet = Account.from_key(key).address
                results.append(
                    ValidationResult("private_key", "pass", f"valid signer {wallet}")
                )
            except Exception as exc:
                results.append(ValidationResult("private_key", "fail", f"invalid: {exc}"))
    elif key:
        try:
            Account.from_key(key)
            results.append(ValidationResult("private_key", "pass", "valid; optional in dry-run"))
        except Exception as exc:
            results.append(ValidationResult("private_key", "fail", f"invalid: {exc}"))
    else:
        results.append(ValidationResult("private_key", "pass", "not required in dry-run"))

    proxy_wallet = str(values.get("PROXY_WALLET", "") or "").strip()
    if not proxy_wallet:
        results.append(
            ValidationResult("proxy_wallet", "pass", "not set; signer is funding wallet")
        )
    else:
        try:
            checked_proxy = to_checksum_address(proxy_wallet)
            results.append(
                ValidationResult("proxy_wallet", "pass", f"valid address {checked_proxy}")
            )
        except ValueError as exc:
            results.append(ValidationResult("proxy_wallet", "fail", f"invalid: {exc}"))

    for name, schemes in (
        ("GAMMA_BASE_URL", {"https"}),
        ("CLOB_BASE_URL", {"https"}),
        ("CLOB_WS_URL", {"wss"}),
        ("POLYGON_RPC_URL", {"https"}),
    ):
        value = str(values.get(name, "") or "").strip()
        parsed = urlparse(value)
        valid = parsed.scheme in schemes and bool(parsed.netloc)
        results.append(
            ValidationResult(
                name.lower(), "pass" if valid else "fail", value if valid else "invalid secure URL"
            )
        )

    webhook = str(values.get("ALERT_WEBHOOK_URL", "") or "").strip()
    if not webhook:
        results.append(
            ValidationResult("alert_webhook_url", "pass", "not set; durable local alerts only")
        )
    else:
        parsed_webhook = urlparse(webhook)
        loopback_http = (
            parsed_webhook.scheme == "http"
            and parsed_webhook.hostname in {"127.0.0.1", "localhost", "::1"}
        )
        webhook_valid = bool(parsed_webhook.netloc) and (
            parsed_webhook.scheme == "https" or loopback_http
        )
        results.append(
            ValidationResult(
                "alert_webhook_url",
                "pass" if webhook_valid else "fail",
                (
                    "configured secure URL"
                    if webhook_valid
                    else "must use https (http is allowed only for loopback)"
                ),
            )
        )

    numeric_rules = {
        "MIN_EDGE": lambda value: 0.0 <= value < 1.0,
        "ORDER_SIZE": lambda value: value > 0.0,
        "MAX_QUOTE_AGE_MS": lambda value: value > 0.0,
        "CROSS_LEG_SETTLE_MS": lambda value: value > 0.0,
        "MAX_LEG_GAP_MS": lambda value: value >= 0.0,
        "MAX_POSITION_PER_MARKET": lambda value: value > 0.0,
        "MAX_TOTAL_CAPITAL": lambda value: value > 0.0,
    }
    parsed_numbers: dict[str, float] = {}
    for name, predicate in numeric_rules.items():
        try:
            value = float(values.get(name, ""))
            parsed_numbers[name] = value
            valid = predicate(value)
            detail = str(value) if valid else "outside permitted range"
        except (TypeError, ValueError):
            valid = False
            detail = "not numeric"
        results.append(ValidationResult(name.lower(), "pass" if valid else "fail", detail))
    if (
        parsed_numbers.get("MAX_POSITION_PER_MARKET", 0)
        > parsed_numbers.get("MAX_TOTAL_CAPITAL", -1)
    ):
        results.append(
            ValidationResult(
                "capital_caps",
                "fail",
                "MAX_POSITION_PER_MARKET cannot exceed MAX_TOTAL_CAPITAL",
            )
        )
    else:
        results.append(ValidationResult("capital_caps", "pass", "internally consistent"))

    for module in REQUIRED_IMPORTS:
        available = importlib.util.find_spec(module) is not None
        results.append(
            ValidationResult(
                f"dependency_{module}",
                "pass" if available else "fail",
                "installed" if available else "missing; install project dependencies",
            )
        )

    optional_creds = (
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
    )
    present = [name for name in optional_creds if str(values.get(name, "") or "").strip()]
    if present and len(present) != len(optional_creds):
        results.append(
            ValidationResult(
                "polymarket_l2_credentials",
                "fail",
                "provide all key/secret/passphrase fields or leave all blank for derivation",
            )
        )
    else:
        results.append(
            ValidationResult(
                "polymarket_l2_credentials",
                "pass",
                "complete" if present else "blank; client will derive in authenticated paper mode",
            )
        )
    return results


def _quote_env(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("configuration values cannot contain newlines")
    return json.dumps(value)


def write_env(values: Mapping[str, Any], path: Path, *, force: bool = False) -> Path:
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [key for key in DEFAULTS if key in values]
    ordered.extend(key for key in ("DRY_RUN", "PAPER_TRADE", "LIVE_SIGNING_VERIFIED") if key in values)
    ordered.extend(sorted(key for key in values if key not in ordered))
    lines = ["# Generated by scripts/setup_wizard.py; real order submission is unbuilt."]
    lines.extend(f"{key}={_quote_env(str(values[key]))}" for key in ordered)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(path)
    return path


def _prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    prompt = f"{label}{suffix}: "
    value = getpass(prompt) if secret else input(prompt)
    return value.strip() or default


def interactive_values(profile: str) -> dict[str, str]:
    print("This wizard supports dry-run and PAPER_TRADE only; real submission remains unbuilt.")
    supplied = dict(DEFAULTS)
    if profile == "paper":
        supplied["PRIVATE_KEY"] = _prompt("Wallet private key (hidden)", secret=True)
        supplied["PROXY_WALLET"] = _prompt("Proxy wallet address (optional)")
        print("Existing L2 credentials are optional; leave all three blank to derive them.")
        supplied["POLYMARKET_API_KEY"] = _prompt("Polymarket API key (hidden, optional)", secret=True)
        supplied["POLYMARKET_API_SECRET"] = _prompt("Polymarket API secret (hidden, optional)", secret=True)
        supplied["POLYMARKET_API_PASSPHRASE"] = _prompt("Polymarket API passphrase (hidden, optional)", secret=True)
    supplied["POLYGON_RPC_URL"] = _prompt("Read-only Polygon RPC URL", DEFAULTS["POLYGON_RPC_URL"])
    supplied["ORDER_SIZE"] = _prompt("Paper order size", DEFAULTS["ORDER_SIZE"])
    supplied["MIN_EDGE"] = _prompt("Minimum edge", DEFAULTS["MIN_EDGE"])
    supplied["ALERT_WEBHOOK_URL"] = _prompt("Alert webhook URL (optional)")
    supplied["LSE_API_KEY"] = _prompt("LSE calendar API key (hidden, optional)", secret=True)
    return profile_values(profile, supplied)


def print_results(results: list[ValidationResult]) -> bool:
    for result in results:
        print(f"[{result.status.upper()}] {result.name}: {result.detail}")
    failures = sum(result.status == "fail" for result in results)
    print(f"Validation: {len(results) - failures} pass, {failures} fail, {len(results)} total")
    return failures == 0


def save_validation_report(
    results: list[ValidationResult],
    output: Path,
    *,
    env_file: Path,
    online: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a secret-free, machine-readable wizard verification artifact."""
    failures = sum(result.status == "fail" for result in results)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "setup/config wizard validation",
        "env_file": str(env_file),
        "secrets_included": False,
        "real_order_submission_supported": False,
        "validation": {
            "status": "verified-pass" if failures == 0 else "verified-fail",
            "pass_count": len(results) - failures,
            "fail_count": failures,
            "total_count": len(results),
            "checks": [asdict(result) for result in results],
        },
        "online_read_only": online,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(output)
    return report


async def online_check(values: Mapping[str, Any], output: Path) -> dict[str, Any]:
    settings = SimpleNamespace(
        private_key=str(values.get("PRIVATE_KEY", "")),
        proxy_wallet=str(values.get("PROXY_WALLET", "")),
        order_size=float(values.get("ORDER_SIZE", 10.0)),
        polygon_rpc_url=str(values.get("POLYGON_RPC_URL", "")),
    )
    report = await collect_wallet_status(settings)
    save_wallet_status(report, output)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=SUPPORTED_PROFILES)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--check", action="store_true", help="validate an existing env file")
    parser.add_argument("--online", action="store_true", help="also run real read-only wallet/geoblock checks")
    parser.add_argument("--force", action="store_true", help="replace an existing env file")
    parser.add_argument("--wallet-output", type=Path, default=ROOT / "state" / "wallet_status.json")
    parser.add_argument(
        "--report-output",
        type=Path,
        help="write a secret-free JSON validation report",
    )
    args = parser.parse_args()

    if args.check:
        if not args.env_file.is_file():
            parser.error(f"env file does not exist: {args.env_file}")
        values = dict(DEFAULTS)
        values.update(
            {key: str(value or "") for key, value in dotenv_values(args.env_file).items()}
        )
    else:
        profile = args.profile or _prompt("Profile (dry-run or paper)", "paper")
        if profile not in SUPPORTED_PROFILES:
            parser.error("profile must be dry-run or paper")
        values = interactive_values(profile)

    results = validate_config(values)
    valid = print_results(results)
    if not valid:
        if args.report_output:
            save_validation_report(results, args.report_output, env_file=args.env_file)
        print("Configuration was not written. Resolve the failed checks and retry.")
        return 1

    if not args.check:
        written = write_env(values, args.env_file, force=args.force)
        control = LiveTradingControl(
            ROOT / "state" / "live_control.json",
            ROOT / "state" / "live_control_audit.jsonl",
        )
        control.set_operator_enabled(
            False,
            actor="setup_wizard",
            reason="safe initialization after configuration",
        )
        print(f"Wrote {written} with permissions 0600; master live flag is disabled.")

    online_report = None
    if args.online:
        online_report = asyncio.run(online_check(values, args.wallet_output))
        print(
            "Read-only online check: "
            f"funding_ready={online_report['funding_ready']} "
            f"approvals_ready={online_report['approvals_ready']} "
            f"trading_ready={online_report['trading_ready']} "
            f"configured_rpc_healthy={online_report['configured_rpc_healthy']} "
            f"rpc_fallback_used={online_report['rpc_fallback_used']} "
            f"jurisdiction={online_report['jurisdiction']['status']} "
            f"hard_failures={online_report['hard_failure_count']} "
            f"unknown={online_report['unknown_count']}"
        )
    if args.report_output:
        save_validation_report(
            results,
            args.report_output,
            env_file=args.env_file,
            online=online_report,
        )
        print(f"Validation report: {args.report_output}")
    if online_report and (
        online_report["hard_failure_count"] or online_report["unknown_count"]
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
