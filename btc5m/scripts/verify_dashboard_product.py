#!/usr/bin/env python3
"""Run the operator dashboard and sealed-session demo in-process."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "streamlit_app.py"
EVIDENCE_DATE = os.getenv(
    "PRODUCT_EVIDENCE_DATE",
    datetime.now(ZoneInfo("America/Vancouver")).date().isoformat(),
)
DEFAULT_OUTPUT = ROOT / "state" / f"dashboard_product_verification_{EVIDENCE_DATE}.json"


def _exceptions(app: AppTest) -> list[str]:
    return [str(getattr(item, "value", item)) for item in app.exception]


def _headless_http_probe(timeout_s: float = 20.0) -> dict:
    """Start the actual Streamlit server and probe its localhost health endpoint."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(APP),
        "--server.headless=true",
        f"--server.port={port}",
        "--server.address=127.0.0.1",
        "--browser.gatherUsageStats=false",
    ]
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false"},
    )
    health_body = None
    root_status = None
    error = None
    try:
        deadline = started + timeout_s
        while time.monotonic() < deadline:
            if process.poll() is not None:
                error = f"server exited early with code {process.returncode}"
                break
            try:
                with urlopen(
                    f"http://127.0.0.1:{port}/_stcore/health", timeout=1.0
                ) as response:
                    health_body = response.read().decode("utf-8", "replace").strip()
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2.0) as response:
                    root_status = int(response.status)
                break
            except (OSError, URLError):
                time.sleep(0.1)
        if health_body is None and error is None:
            error = f"health endpoint did not respond within {timeout_s}s"
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
    passed = health_body == "ok" and root_status == 200 and error is None
    return {
        "status": "verified-pass" if passed else "verified-fail",
        "localhost_only": True,
        "port": port,
        "health_body": health_body,
        "root_http_status": root_status,
        "startup_seconds": time.monotonic() - started,
        "server_exit_code_after_termination": process.returncode,
        "error": error,
    }


def verify_dashboard() -> dict:
    server_probe = _headless_http_probe()
    normal = AppTest.from_file(str(APP), default_timeout=30).run()
    normal_buttons = [item.label for item in normal.button]
    normal_toggles = [item.label for item in normal.toggle]
    normal_errors = _exceptions(normal)
    normal_pass = all(
        (
            not normal_errors,
            "Demo mode (sealed paper replay)" in normal_toggles,
            "Apply audited state change" in normal_buttons,
            "Refresh read-only wallet / jurisdiction" in normal_buttons,
        )
    )

    demo = AppTest.from_file(str(APP), default_timeout=30).run()
    if demo.toggle:
        demo.toggle[0].set_value(True).run()
    demo_errors = _exceptions(demo)
    warnings = [str(item.value) for item in demo.warning]
    metrics = {str(item.label): str(item.value) for item in demo.metric}
    visible_events = int(metrics.get("Visible events", "0"))
    demo_pass = all(
        (
            not demo_errors,
            any("network_used=false" in warning for warning in warnings),
            visible_events > 0,
            int(metrics.get("Fires", "0")) > 0,
            int(metrics.get("Trade events", "0")) > 0,
        )
    )
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "dashboard live-control surface and offline sealed-session demo",
        "status": (
            "verified-pass"
            if normal_pass and demo_pass and server_probe["status"] == "verified-pass"
            else "verified-fail"
        ),
        "real_orders_submitted": False,
        "normal": {
            "status": "verified-pass" if normal_pass else "verified-fail",
            "exceptions": normal_errors,
            "control_state_button_present": "Apply audited state change" in normal_buttons,
            "wallet_refresh_button_present": (
                "Refresh read-only wallet / jurisdiction" in normal_buttons
            ),
            "demo_toggle_present": "Demo mode (sealed paper replay)" in normal_toggles,
        },
        "headless_http_server": server_probe,
        "demo": {
            "status": "verified-pass" if demo_pass else "verified-fail",
            "exceptions": demo_errors,
            "network_used": False,
            "sealed_archive_warning_present": any(
                "network_used=false" in warning for warning in warnings
            ),
            "visible_events": visible_events,
            "fires": metrics.get("Fires"),
            "trade_events": metrics.get("Trade events"),
            "alerts": metrics.get("Alerts"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = verify_dashboard()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "verified-pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
