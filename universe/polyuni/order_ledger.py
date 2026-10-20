"""Durable append-only audit log for submitted paired orders.

The ledger is deliberately small: an fsynced ``pending`` record is written after
both submit calls return and *before* risk is committed or an in-memory pending
entry is installed.  That ordering makes a process crash recoverable.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ReconciliationResult:
    """Outcome of replaying the append-only order ledger on cold-start recovery.

    ``restored_amount`` is the USD-equivalent of in-flight orders the recovery
    loop was able to reattach to live bot state. ``resolved`` lists order ids we
    matched to a fill (handled normally); ``manual_review`` lists ids we could not
    classify automatically and require an operator to inspect before next start.
    """

    restored_amount: float = 0.0
    resolved: list[str] = field(default_factory=list)
    manual_review: list[str] = field(default_factory=list)
    sequential_manual_review: list[dict[str, Any]] = field(default_factory=list)


class OrderLedger:
    """Append-only JSONL ledger for every paired YES+NO submission + recovery.

    Each entry is written with fsync BEFORE the bot commits risk or in-memory
    pending state (module docstring): a crash at any point can be replayed by
    ``reconcile_pending()`` to restore in-flight orders without silent loss. The
    ledger is intentionally small — only order ids and paired-leg references,
    no PnL or book data. Path is configurable so tests isolate via tmp_path.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        # O_APPEND makes each single write an append; fsync makes the acknowledgement
        # meaningful across a process death / power-loss boundary.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def append_pending(self, *, market_id: str, yes_id: str | None, no_id: str | None,
                       yes_token_id: str, no_token_id: str, reserved: float,
                       mode: str = "live") -> None:
        self._append({
            "type": "order", "status": "pending", "timestamp": time.time(),
            "market_id": market_id, "yes_id": yes_id, "no_id": no_id,
            "yes_token_id": yes_token_id, "no_token_id": no_token_id,
            "reserved": float(reserved),
            # TASK 2: explicit mode so paper-vs-live is visible in the ledger.
            "mode": mode,
        })

    def append_resolved(self, pending: dict[str, Any], *, status: str,
                        final_committed_amount: float, note: str | None = None) -> None:
        record = {
            "type": "resolution", "status": status, "timestamp": time.time(),
            "market_id": pending["market_id"], "yes_id": pending.get("yes_id"),
            "no_id": pending.get("no_id"),
            "final_committed_amount": float(final_committed_amount),
        }
        if note:
            record["note"] = note
        self._append(record)

    def append_sequence_event(
        self,
        *,
        sequence_id: str,
        market_id: str,
        status: str,
        side: str,
        token_id: str,
        order_id: str | None,
        price: float,
        size: float,
        fee: float,
        note: str | None = None,
    ) -> None:
        """Durable audit event for a deliberately time-separated pair."""
        record: dict[str, Any] = {
            "type": "sequential",
            "status": status,
            "timestamp": time.time(),
            "sequence_id": sequence_id,
            "market_id": market_id,
            "side": side,
            "token_id": token_id,
            "order_id": order_id,
            "price": float(price),
            "size": float(size),
            "fee": float(fee),
        }
        if note:
            record["note"] = note
        self._append(record)

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        result = []
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    result.append(json.loads(line))
                except json.JSONDecodeError:
                    # A torn final line is never treated as proof an order is safe.
                    continue
        return result

    def pending_latest(self) -> list[dict[str, Any]]:
        latest: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
        for r in self.records():
            key = (r.get("market_id"), r.get("yes_id"), r.get("no_id"))
            if r.get("type") == "order" and r.get("status") == "pending":
                latest[key] = r
            elif r.get("type") == "resolution":
                latest.pop(key, None)
        return list(latest.values())

    def unresolved_sequential_first_legs(self) -> list[dict[str, Any]]:
        """Return first fills with no durable second-fill or exit event."""
        unresolved: dict[str, dict[str, Any]] = {}
        for record in self.records():
            if record.get("type") != "sequential":
                continue
            sequence_id = str(record.get("sequence_id") or "")
            if not sequence_id:
                continue
            status = str(record.get("status") or "")
            if status == "first_filled":
                unresolved[sequence_id] = record
            elif status in {"second_filled", "exited"}:
                unresolved.pop(sequence_id, None)
        return list(unresolved.values())

    async def reconcile(self, client: Any, risk: Any) -> ReconciliationResult:
        """Resolve ledger-only orders before normal trading starts.

        A positive fill observation restores the entire reservation, deliberately
        conservative when exact executed quantity cannot be known.  A probe error is
        manual-review and also reserves the full amount; it is never assumed safe.
        """
        outcome = ReconciliationResult()
        for entry in self.unresolved_sequential_first_legs():
            amount = (
                float(entry.get("price") or 0.0) * float(entry.get("size") or 0.0)
                + float(entry.get("fee") or 0.0)
            )
            risk.restore_deployed(str(entry["market_id"]), amount)
            risk.halt(
                f"unresolved sequential first leg {entry['sequence_id']} requires manual review"
            )
            restored = dict(entry)
            restored["cost"] = amount
            outcome.restored_amount += amount
            outcome.manual_review.append(str(entry["sequence_id"]))
            outcome.sequential_manual_review.append(restored)
        for entry in self.pending_latest():
            try:
                async def probe(order_id: str | None, token_id: str) -> bool:
                    return bool(order_id) and await client.is_filled(order_id, token_id, gap_ms=0)
                yes, no = await asyncio.gather(
                    probe(entry.get("yes_id"), entry["yes_token_id"]),
                    probe(entry.get("no_id"), entry["no_token_id"]),
                )
            except Exception as exc:
                amount = float(entry["reserved"])
                risk.restore_deployed(entry["market_id"], amount)
                self.append_resolved(entry, status="manual_review",
                                     final_committed_amount=amount, note=repr(exc))
                outcome.restored_amount += amount
                outcome.manual_review.append(entry["market_id"])
                continue
            amount = float(entry["reserved"]) if (yes or no) else 0.0
            if amount:
                risk.restore_deployed(entry["market_id"], amount)
                outcome.restored_amount += amount
            self.append_resolved(entry, status="resolved", final_committed_amount=amount,
                                 note="recovered_filled" if amount else "recovered_unfilled")
            outcome.resolved.append(entry["market_id"])
        return outcome
