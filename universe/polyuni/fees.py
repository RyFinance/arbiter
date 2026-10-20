"""Polymarket fee math used by paper execution.

The protocol charges takers ``shares * rate * (p * (1-p)) ** exponent`` and
rounds the USDC fee to five decimal places. Makers pay zero, but PolyUni's
paper simulator crosses the current book and is therefore modeled as a taker.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

_FEE_QUANTUM = Decimal("0.00001")


def taker_fee(
    shares: float,
    price: float,
    rate: float,
    exponent: float = 1.0,
) -> float:
    """Return the official fee amount in USDC, rounded to five decimals."""
    if shares <= 0 or price <= 0 or price >= 1 or rate <= 0:
        return 0.0
    quantity = Decimal(str(shares))
    p = Decimal(str(price))
    r = Decimal(str(rate))
    component = p * (Decimal("1") - p)
    if float(exponent) == 1.0:
        curved = component
    else:
        # Fee schedules currently use exponent=1, but Gamma exposes the field.
        # Decimal has no portable fractional power; converting only this curve
        # component keeps the final protocol rounding deterministic.
        curved = Decimal(str(float(component) ** float(exponent)))
    raw = quantity * r * curved
    return float(raw.quantize(_FEE_QUANTUM, rounding=ROUND_HALF_UP))


def effective_fee_rate(
    *,
    fees_enabled: bool | None,
    market_rate: float,
    is_crypto: bool,
    crypto_fallback: float,
    default_fallback: float,
) -> float:
    """Resolve Gamma fee metadata with a conservative category fallback."""
    if fees_enabled is False:
        return 0.0
    if market_rate > 0:
        return float(market_rate)
    return float(crypto_fallback if is_crypto else default_fallback)

