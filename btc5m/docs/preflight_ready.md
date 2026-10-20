# Preflight Ready: Fund & Approval Requirements for First Live Test Order

## Wallet

| Field | Value |
|---|---|
| **PRIVATE_KEY** | (loaded from `.env`) |
| **Derived Address** | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` |
| **PROXY_WALLET** | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` (same as wallet in current `.env`) |

## Order Parameters (from `.env`)

| Parameter | Value |
|---|---|
| `ORDER_SIZE` | 10.0 shares per leg |
| `MIN_EDGE` | 0.01 (trade when YES + NO combined ask < 0.99) |
| Max price per share | $1.00 |

## Exact pUSD Amount Needed

**$20.00 pUSD minimum** for the first live test order.

Calculation:
- Each leg: `ORDER_SIZE * max_price = 10.0 * $1.00 = $10.00`
- Two legs (YES + NO): `2 * $10.00 = $20.00`
- With `MIN_EDGE=0.01`, combined ask < 0.99, so actual cost per pair ≈ $19.80
- Use **$20.00** as the buffer amount to fund

**Token to acquire:** USDC (pUSD is the Polymarket USDC contract)
- **Contract:** `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`
- **Decimals:** 6 (standard USDC)
- **Chain:** Polygon (MATIC)

## Exact Approval Steps

### Step 1: Fund the wallet

Send **at least $20.00 pUSD** (USDC on Polygon) to wallet `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` via any Polygon-supporting exchange or bridge.

Send **at least 0.5 MATIC** for gas to the same wallet address.

### Step 2: Submit trading approvals (4 on-chain transactions)

The wallet must approve **two token types for two exchange contracts**:

| # | Token | Type | Action | Spender (operator) | Amount |
|---|---|---|---|---|---|
| 1 | pUSD (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`) | ERC-20 | `approve(spender, max)` | Exchange V2 (`0xE111180000d2663C0091e4f400237545B87B996B`) | max uint256 |
| 2 | pUSD (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`) | ERC-20 | `approve(spender, max)` | Neg Risk Exchange V2 (`0xe2222d279d744050d28e00520010520000310F59`) | max uint256 |
| 3 | CTF (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`) | ERC-1155 | `setApprovalForAll(operator, True)` | Exchange V2 (`0xE111180000d2663C0091e4f400237545B87B996B`) | True |
| 4 | CTF (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`) | ERC-1155 | `setApprovalForAll(operator, True)` | Neg Risk Exchange V2 (`0xe2222d279d744050d28e00520010520000310F59`) | True |

These approvals are executed automatically by `TradingClient.setup_trading_approvals()` in `polymarket_arb/clients/trading_client.py` when the bot runs in LIVE or PAPER_TRADE mode (`DRY_RUN=false`).

**To execute approvals manually** (if needed outside the bot):
```bash
# Set DRY_RUN=false, PAPER_TRADE=true in .env, then start the bot:
.venv/bin/python -m polymarket_arb  # approvals run at startup via TradingClient.__init__()
```

### Step 3: Verify geoblock status

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

### Step 4: Verify LIVE_SIGNING_VERIFIED

The `.env` has `LIVE_SIGNING_VERIFIED=true`. This flag gates `settings.live` (see `config.py:174`). Without it, the bot stays in DRY_RUN/PAPER_TRADE and no orders are submitted.

Run the signing verifier before enabling live trading:
```bash
.venv/bin/python -m polymarket_arb.clients.verify_signing
```

### Step 5: Run preflight check

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
- CTF approved for Exchange V2 (may WARN — see note below)
- CTF approved for Neg Risk Exchange V2 (may WARN — see note below)
- Geoblock clear
- LIVE_SIGNING_VERIFIED = true

## Current Preflight Status (as of 2026-08-03)

| Check | Status | Value |
|---|---|---|
| PRIVATE_KEY set | PASS | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` |
| Wallet address | PASS | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` |
| MATIC for gas | FAIL | 0.0000 MATIC (need >= 0.5 MATIC) |
| pUSD balance | FAIL | $0.00 (need >= $20.00) |
| pUSD approved for Exchange V2 | FAIL | Allowance = 0 |
| pUSD approved for Neg Risk Exchange V2 | FAIL | Allowance = 0 |
| CTF approved for Exchange V2 | WARN | Cannot verify via eth_call (contract reverts isApprovalForAll) |
| CTF approved for Neg Risk Exchange V2 | WARN | Cannot verify via eth_call (contract reverts isApprovalForAll) |
| Geoblock | FAIL | blocked (CA) |
| LIVE_SIGNING_VERIFIED | PASS | true |

## Important Notes

### CTF isApprovalForAll Verification

The Polymarket CTF contract (`0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`) does NOT expose `isApprovalForAll` as a stateless view function. Calling `eth_call` on this function returns "execution reverted" with empty revert data. This is by design — the CTF uses a custom approval mechanism that requires an on-chain `setApprovalForAll` transaction.

**Verification:** CTF approval must be confirmed via the on-chain transaction receipt after calling `setApprovalForAll(operator, True)`, NOT via a pre-flight `eth_call` read. The bot's `setup_trading_approvals()` in `trading_client.py` sends these transactions; check the transaction hash on Polygonscan to confirm.

### RPC Endpoints

The preflight check tries the following Polygon RPC endpoints in order:
1. `https://polygon-rpc.com` (from `.env POLYGON_RPC_URL`) — public, no API key required
2. `https://polygon.drpc.org` — public, no API key required (verified working 2026-08-03)
3. `https://polygon.api.onfinality.io/public` — public, no key, rate-limited fallback
4. `https://rpc.ankr.com/polygon` — requires API key, last resort

If all endpoints fail, the check reports the error from each endpoint so you can diagnose the issue (geoblocking, API key requirement, rate limiting).

### Geoblock

Live order placement via the Polymarket CLOB API is **externally blocked from CA/BC jurisdiction**. This is enforced server-side by Polymarket's geoblock endpoint and cannot be bypassed programmatically. Trading must be conducted from a permitted jurisdiction.
