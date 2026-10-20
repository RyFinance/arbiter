"""Scrapers package — background data collection agents.

page_agent_runner.py is a standalone script that can also be imported as
``scrapers.page_agent_runner`` (main.py does ``from scrapers.page_agent_runner
import PageAgentRunner``). The package marker file makes both invocation paths
work: ``python scrapers/page_agent_runner.py`` (standalone) and
``python -m polymarket_arb.main`` (which imports via ``scrapers.page_agent_runner``).
"""
