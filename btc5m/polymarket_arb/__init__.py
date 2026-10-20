"""Polymarket latency-first arbitrage bot.

The only strategy: buy YES + NO together when best_ask(YES) + best_ask(NO) < 1.00.
Everything in this package is built to make that decision-and-submit path as short
as physically possible. See README.md ("Speed First").
"""

__version__ = "0.1.0"
