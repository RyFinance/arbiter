"""Execution package: the split of polymarket_arb's execution.py god-object into
OrderRouter (submission/cancellation), FillTracker (deferred fill resolution),
PartialFillRecovery (hedge-or-exit), and PnlLedger (accounting) -- coordinated by
the thin Execution class. Import `Execution`/`OrderTemplate` from here, matching
the flat `from .execution import Execution` shape main.py expects.
"""
from .execution import Execution
from .fill_tracker import FillTracker
from .order_router import OrderRouter, OrderTemplate
from .partial_fill_recovery import PartialFillRecovery
from .pnl_ledger import PnlLedger

__all__ = [
    "Execution",
    "FillTracker",
    "OrderRouter",
    "OrderTemplate",
    "PartialFillRecovery",
    "PnlLedger",
]
