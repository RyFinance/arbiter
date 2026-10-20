# Fund & Approval Readiness — First Live Test Order

> **See also:** `docs/preflight_ready.md` for the latest preflight results and RPC endpoint details.

## Wallet

| Field | Value |
|---|---|
| **PRIVATE_KEY** | `ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80` (from `.env`) |
| **Derived Address** | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` |
| **PROXY_WALLET** | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` (same as wallet in current `.env`) |

## Order Parameters (from `.env` / `config.py`)

| Parameter | Value |
|---|---|
| `ORDER_SIZE` | 10.0 shares per leg |
| `MIN_EDGE` | 0.01 (trade when YES + NO < 0.99) |
| Max price per share | $1.00 |

## Exact Steps for First Live Test Order

### Step 1: Fund the wallet

**Required pUSD: ~$20.00**

Calculation:
- Each leg: `ORDER_SIZE * max_price = 10.0 * $1.00 = $10.00`
- Two legs (YES + NO): `2 * $10.00 = $20.00`
- In practice, with `MIN_EDGE=0.01`, combined ask < 0.99, so actual cost per pair ≈ $19.80
- Use **$20.00** as the buffer amount to fund

**Token to acquire:** USDC (pUSD is the Polymarket USDC contract)
- **Contract:** `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`
- **Decimals:** 6 (standard USDC)
- **Chain:** Polygon (MATIC)

Send USDC to wallet `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` via any Polygon-supporting exchange or bridge.

### Step 2: Ensure sufficient MATIC for gas

**Required: 0.5 MATIC minimum** (covers 4 approval transactions + order placement at ~1 gwei).

Current gas estimate per approval txn:
- ERC-20 `approve`: ~80,000 gas
- ERC-1155 `setApprovalForAll`: ~80,000 gas
- With 4 approval txns + 1 order post: ~400,000 gas total
- At 50 gwei: ~0.0021 MATIC for approvals alone
- Buffer of 0.5 MATIC is conservative and sufficient.

Check MATIC balance via:
```bash
.venv/bin/python scripts/preflight_check.py
```

### Step 3: Submit trading approvals (4 on-chain transactions)

Per `docs.polymarket.com/trading/wallets-auth` and `trading_client.py` lines 128-193, the wallet must approve **two token types for two exchange contracts**:

| # | Token | Type | Action | Spender (operator) | Amount |
|---|---|---|---|---|---|
| 1 | pUSD (`0xC011a7...`) | ERC-20 | `approve` | Exchange V2 (`0xE1111...`) | max uint256 |
| 2 | pUSD (`0xC011a7...`) | ERC-20 | `approve` | Neg Risk (`0xe2222...`) | max uint256 |
| 3 | CTF (`0x4D97DC...`) | ERC-1155 | `setApprovalForAll` | Exchange V2 (`0xE1111...`) | True |
| 4 | CTF (`0x4D97DC...`) | ERC-1155 | `setApprovalForAll` | Neg Risk (`0xe2222...`) | True |

These are executed by `TradingClient.setup_trading_approvals()` in `trading_client.py`. In LIVE mode, the transactions are signed and broadcast to Polygon RPC.

**To execute approvals manually** (if needed outside the bot):
```python
# This is done automatically by TradingClient.__init__() when settings.live is True
# and is NOT done in PAPER_TRADE or DRY_RUN mode.
```

### Step 4: Verify geoblock status

Check from the machine that will run the bot:
```
GET https://polymarket.com/api/geoblock
```

Expected response (if allowed):
```json
{"blocked": false, "ip": "...", "country": "..."}
```

If `blocked: true`, trading is not permitted from that jurisdiction. The current environment returns:
```json
{"blocked": true, "country": "CA", "region": "BC"}
```

**Action:** Geoblock must be clear from the trading machine. This cannot be bypassed programmatically.

### Step 5: Verify LIVE_SIGNING_VERIFIED

The `.env` has `LIVE_SIGNING_VERIFIED=true`. This flag gates `settings.live` (see `config.py:174`). Without it, the bot stays in DRY_RUN/PAPER_TRADE and no orders are submitted.

Run the signing verifier before enabling live trading:
```bash
.venv/bin/python -m polymarket_arb.clients.verify_signing
```

### Step 6: Run preflight check

```bash
.venv/bin/python scripts/preflight_check.py
```

All checks must PASS (or only WARN) before live trading:
- PRIVATE_KEY set
- Wallet address derivable
- MATIC for gas (>= 0.5 MATIC)
- pUSD balance (>= $20.00)
- pUSD approved for Exchange V2
- pUSD approved for Neg Risk Exchange V2
- CTF approved for Exchange V2
- CTF approved for Neg Risk Exchange V2
- Geoblock clear
- LIVE_SIGNING_VERIFIED = true

## Summary

The minimum pUSD to fund for a first live test: **$20.00**.
The minimum MATIC for gas: **0.5 MATIC**.
The minimum required approvals: **4 transactions** (2 ERC-20 approve + 2 ERC-1155 setApprovalForAll).
