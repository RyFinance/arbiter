"""Fail-closed live-trading control state and append-only audit trail.

Live submission needs three independent switches, all fail-closed:
  1. ``ARBITER_LIVE_TRADING=enabled`` in the operator's own environment/.env
     (never settable from the dashboard),
  2. ``LIVE_EXECUTION=true`` for this bot process (set by the dashboard only
     when the operator picked LIVE for the venue with a typed confirmation),
  3. the audited operator flag in this module's state file.
Without all three, ``live_endpoint_enabled()`` is False and every live check
is denied with ``live_exchange_order_endpoint_unbuilt``.

Paper and dry-run submissions are allowed because they cannot place exchange
orders; every check is still recorded so the audit trail covers every call.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_PATH = ROOT / "state" / "live_control.json"
DEFAULT_AUDIT_PATH = ROOT / "state" / "live_control_audit.jsonl"
def live_endpoint_enabled() -> bool:
    """Env-level opt-in for real orders; read at call time, fail-closed."""
    return (
        os.getenv("ARBITER_LIVE_TRADING", "").strip().lower() == "enabled"
        and os.getenv("LIVE_EXECUTION", "").strip().lower() == "true"
    )
SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ControlState:
    schema_version: int
    operator_enabled: bool
    effective_live_submission_enabled: bool
    hard_block_reason: str
    revision: int
    updated_at: str
    updated_by: str
    change_reason: str


@dataclass(frozen=True)
class SubmissionDecision:
    allowed: bool
    mode: str
    call_site: str
    reason: str
    operator_enabled: bool
    endpoint_built: bool
    checked_at: str


class LiveTradingControl:
    """Persist operator intent while enforcing an independent hard block."""

    def __init__(
        self,
        state_path: Path | str = DEFAULT_STATE_PATH,
        audit_path: Path | str = DEFAULT_AUDIT_PATH,
    ) -> None:
        self.state_path = Path(state_path)
        self.audit_path = Path(audit_path)
        self._lock = threading.RLock()

    @staticmethod
    def _default_state() -> ControlState:
        return ControlState(
            schema_version=SCHEMA_VERSION,
            operator_enabled=False,
            effective_live_submission_enabled=False,
            hard_block_reason="live_exchange_order_endpoint_unbuilt",
            revision=0,
            updated_at="never",
            updated_by="system_default",
            change_reason="fail_closed_default",
        )

    def read_state(self) -> ControlState:
        """Read state fail-closed; missing, corrupt, or invalid means disabled."""
        with self._lock:
            if not self.state_path.is_file():
                return self._default_state()
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
                if payload.get("schema_version") != SCHEMA_VERSION:
                    raise ValueError("unsupported schema version")
                operator_enabled = payload.get("operator_enabled")
                if not isinstance(operator_enabled, bool):
                    raise ValueError("operator_enabled must be boolean")
                return ControlState(
                    schema_version=SCHEMA_VERSION,
                    operator_enabled=operator_enabled,
                    effective_live_submission_enabled=(
                        operator_enabled and live_endpoint_enabled()
                    ),
                    hard_block_reason=(
                        "" if live_endpoint_enabled()
                        else "live_exchange_order_endpoint_unbuilt"
                    ),
                    revision=max(0, int(payload.get("revision", 0))),
                    updated_at=str(payload.get("updated_at", "unknown")),
                    updated_by=str(payload.get("updated_by", "unknown")),
                    change_reason=str(payload.get("change_reason", "unspecified")),
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return self._default_state()

    def _append_audit(self, record: dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), sort_keys=True, default=str)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def set_operator_enabled(
        self,
        enabled: bool,
        *,
        actor: str,
        reason: str,
    ) -> ControlState:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        actor = actor.strip()
        reason = reason.strip()
        if not actor:
            raise ValueError("actor is required for the audit trail")
        if not reason:
            raise ValueError("reason is required for the audit trail")

        with self._lock:
            before = self.read_state()
            changed_at = _now()
            after = ControlState(
                schema_version=SCHEMA_VERSION,
                operator_enabled=enabled,
                effective_live_submission_enabled=(enabled and live_endpoint_enabled()),
                hard_block_reason=(
                    "" if live_endpoint_enabled()
                    else "live_exchange_order_endpoint_unbuilt"
                ),
                revision=before.revision + (1 if before.operator_enabled != enabled else 0),
                updated_at=changed_at,
                updated_by=actor,
                change_reason=reason,
            )
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(asdict(after), handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.state_path)
            self._append_audit(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "live_control_state_change",
                    "occurred_at": changed_at,
                    "actor": actor,
                    "reason": reason,
                    "changed": before.operator_enabled != enabled,
                    "before_operator_enabled": before.operator_enabled,
                    "after_operator_enabled": after.operator_enabled,
                    "effective_live_submission_enabled": (
                        after.effective_live_submission_enabled
                    ),
                    "hard_block_reason": after.hard_block_reason,
                    "revision": after.revision,
                }
            )
            return after

    def check_submission(
        self,
        *,
        mode: str,
        call_site: str,
        side: str = "",
        token_suffix: str = "",
    ) -> SubmissionDecision:
        """Return and durably audit the effective permission for one call site."""
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {"dry_run", "paper", "live"}:
            normalized_mode = "unknown"
        state = self.read_state()
        if normalized_mode in {"dry_run", "paper"}:
            allowed = True
            reason = "simulation_mode_no_exchange_submission"
        elif normalized_mode == "live" and not state.operator_enabled:
            allowed = False
            reason = "master_live_trading_flag_disabled"
        elif normalized_mode == "live" and not live_endpoint_enabled():
            allowed = False
            reason = "live_exchange_order_endpoint_unbuilt"
        elif normalized_mode == "live":
            allowed = True
            reason = "live_enabled_by_env_process_and_operator"
        else:
            allowed = False
            reason = "fail_closed_unknown_mode"

        decision = SubmissionDecision(
            allowed=allowed,
            mode=normalized_mode,
            call_site=str(call_site),
            reason=reason,
            operator_enabled=state.operator_enabled,
            endpoint_built=live_endpoint_enabled(),
            checked_at=_now(),
        )
        with self._lock:
            self._append_audit(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "live_control_submission_check",
                    **asdict(decision),
                    "side": str(side).upper(),
                    "token_suffix": str(token_suffix)[-8:],
                    "state_revision": state.revision,
                }
            )
        return decision

    def read_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        if limit <= 0 or not self.audit_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with self.audit_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return rows[-limit:]


_DEFAULT_CONTROL: LiveTradingControl | None = None
_DEFAULT_LOCK = threading.Lock()


def default_live_control() -> LiveTradingControl:
    global _DEFAULT_CONTROL
    with _DEFAULT_LOCK:
        if _DEFAULT_CONTROL is None:
            state_path = os.getenv("LIVE_CONTROL_STATE_PATH", str(DEFAULT_STATE_PATH))
            audit_path = os.getenv("LIVE_CONTROL_AUDIT_PATH", str(DEFAULT_AUDIT_PATH))
            _DEFAULT_CONTROL = LiveTradingControl(state_path, audit_path)
        return _DEFAULT_CONTROL
