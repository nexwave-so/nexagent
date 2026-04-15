# Nexagent Operator Guide

This guide walks through everything needed to run Nexagent in production — from account setup to live trading. Read it top to bottom before flipping `PAPER_TRADING=false`.

---

## Table of Contents

1. [What You Need](#what-you-need)
2. [Step 1 — Get Nexwave Access](#step-1--get-nexwave-access)
3. [Step 2 — Set Up Hyperliquid](#step-2--set-up-hyperliquid)
4. [Step 3 — Install and Configure](#step-3--install-and-configure)
5. [Step 4 — Run in Paper Mode](#step-4--run-in-paper-mode)
6. [Step 5 — Go Live](#step-5--go-live)
7. [Deployment Options](#deployment-options)
8. [Risk Management Reference](#risk-management-reference)
9. [Exit Modes](#exit-modes)
10. [CLI Reference](#cli-reference)
11. [Monitoring and Alerts](#monitoring-and-alerts)
12. [x402 Pay-Per-Signal Mode](#x402-pay-per-signal-mode)
13. [Troubleshooting](#troubleshooting)

---

## What You Need

| Requirement | Notes |
|---|---|
| Python 3.12+ | Or use Docker |
| Nexwave account | [nexwave.so/dashboard](https://nexwave.so/dashboard) |
| Hyperliquid account | [app.hyperliquid.xyz](https://app.hyperliquid.xyz) |
| Funded HL wallet | $100 minimum recommended to clear notional floors |
| (Optional) Telegram bot | For trade alerts |

---

## Step 1 — Get Nexwave Access

Nexwave is the signal oracle. Nexagent polls it every 30 seconds for live trading signals using **x402 pay-per-signal** — you pay micro-amounts of USDC on Solana per fetch. No subscription, no monthly commitment.

1. Create a Solana wallet ([Phantom](https://phantom.app) is easiest)
2. Fund it with USDC on Solana mainnet — $20–50 is a comfortable starting amount
3. Export your private key: Phantom → Settings → Security & Privacy → Export Private Key
4. Set in `.env`:
   ```
   NEXWAVE_X402_WALLET=<your Solana wallet address>
   NEXWAVE_X402_PRIVATE_KEY=<exported private key>
   ```

See [x402 Pay-Per-Signal Mode](#x402-pay-per-signal-mode) for details on how payments work and how to monitor your balance.

---

## Step 2 — Set Up Hyperliquid

Hyperliquid is a perpetuals DEX where Nexagent executes trades.

### Create an API wallet

Hyperliquid separates the **master wallet** (holds funds) from the **agent/API wallet** (signs orders). You should never put your master wallet's private key into Nexagent.

1. Go to [app.hyperliquid.xyz](https://app.hyperliquid.xyz)
2. Open **Settings → API Wallets**
3. Click **Generate Agent**  — this creates a fresh signing wallet
4. Approve the transaction to authorize it against your master account
5. Copy the **agent private key** (shown once — save it)
6. Your **master wallet address** is your funding account address (shown in the top-right)

In `.env`:
```
HYPERLIQUID_WALLET_ADDRESS=0x...   # master wallet — this is where your USDC lives
HYPERLIQUID_PRIVATE_KEY=0x...      # agent wallet private key — used for signing orders
```

### Fund your account

Deposit USDC into your master wallet via the Hyperliquid bridge. Minimum $100 recommended — Hyperliquid rejects orders below $10 notional and the agent skips rather than retries them, so very small accounts will see most signals skipped.

With `RISK_PER_TRADE_PCT=1.0` on a $100 portfolio, position sizes will be ~$1 (well below the $10 floor). You need roughly:

```
min_balance = $11 / (RISK_PER_TRADE_PCT/100) / regime_multiplier
```

For `ranging` regime (0.5x multiplier): `$11 / 0.01 / 0.5 = $2,200`  
For `trending_bull` regime (1.0x multiplier): `$11 / 0.01 = $1,100`

**Practical default**: set `RISK_PER_TRADE_PCT=5.0` and `MAX_POSITION_USD=100` on a $200 account. This gives $10 positions that reliably clear the floor.

---

## Step 3 — Install and Configure

### Install

```bash
git clone https://github.com/nexwave-so/nexagent
cd nexagent

# Standard (API key mode)
pip install -e ".[alerts]"

# With x402 pay-per-signal support
pip install -e ".[alerts,x402]"
```

Or with `uv` (faster):
```bash
uv pip install -e ".[alerts,x402,dev]"
```

### Configure

```bash
cp .env.example .env
$EDITOR .env
```

Minimum required fields:
```
NEXWAVE_API_KEY=nxw_...
HYPERLIQUID_WALLET_ADDRESS=0x...
HYPERLIQUID_PRIVATE_KEY=0x...
PAPER_TRADING=true
```

Review the full `.env.example` — every variable has a comment. The defaults are conservative and safe to start with.

---

## Step 4 — Run in Paper Mode

Paper mode fetches real signals and simulates fills at mid-price. No real orders are placed. Run this for at least 48 hours before going live.

```bash
uvicorn nexagent.server:app --host 127.0.0.1 --port 7070
```

In a second terminal, watch what the agent is doing:
```bash
nex status       # health, positions, daily PnL
nex signals      # last 20 signals: acted_on=1 means it would have traded
nex positions    # simulated open positions with exit levels
nex trades       # simulated fills
```

Look for:
- Signals flowing in (`nex signals` shows recent entries)
- `acted_on=1` on at least some signals — if all are `acted_on=0`, check skip reasons
- Position sizes making sense (not too small, not at `MAX_POSITION_USD`)
- Exits triggering correctly (trailing stop, take-profit, time stop)

### Common paper mode issues

**All signals skipped with `size_below_min_notional`**: Portfolio value is too small for the current `RISK_PER_TRADE_PCT`. Increase `RISK_PER_TRADE_PCT` or deposit more.

**All signals skipped with `regime_risk_off`**: The market regime is `risk_off` — Nexwave has flagged conditions as too volatile for new entries. This is expected behavior; wait for regime to shift.

**No signals at all**: Check `nex status` — if `nexwave_status` shows `error`, your API key may be invalid or x402 wallet has no USDC.

---

## Step 5 — Go Live

Only do this after paper mode has run for 48+ hours with behavior you're comfortable with.

1. Stop the agent: `Ctrl+C`
2. Set `PAPER_TRADING=false` in `.env`
3. Double-check `MAX_POSITION_USD` and `DAILY_LOSS_LIMIT_USD` — these are your blast radius limits
4. Restart: `uvicorn nexagent.server:app --host 127.0.0.1 --port 7070`
5. Watch the first few trades closely: `nex trades` and `nex positions`

On cold start in live mode, the agent syncs its database with current exchange positions so it can monitor existing trades after a restart.

**Telegram alerts** show `[LIVE]` badge on every fill, close, stop-loss, and daily loss limit breach. Set these up before going live — you want to know immediately if something goes wrong.

---

## Deployment Options

### Render.com (recommended for 24/7 uptime)

1. Fork this repo to your GitHub account
2. Go to [render.com](https://render.com) → New → Web Service → connect your fork
3. Render reads `render.yaml` automatically — no manual config needed
4. Add environment variables in the Render dashboard (paste from your `.env`)
5. Push to `main` — Render builds and deploys automatically

Cost: ~$7/month (Starter plan). Health check runs against `/health`.

> **Important**: Set `API_BIND=0.0.0.0` on Render so the health check can reach the agent.

### Docker

```bash
docker build -t nexagent .
docker run -d \
  --env-file .env \
  --restart unless-stopped \
  -p 7070:7070 \
  nexagent
```

View logs: `docker logs -f <container_id>`

### VPS / bare metal

```bash
# Run as a systemd service
sudo tee /etc/systemd/system/nexagent.service <<EOF
[Unit]
Description=Nexagent Trading Agent
After=network.target

[Service]
WorkingDirectory=/opt/nexagent
EnvironmentFile=/opt/nexagent/.env
ExecStart=/opt/nexagent/.venv/bin/uvicorn nexagent.server:app --host 127.0.0.1 --port 7070
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now nexagent
sudo journalctl -u nexagent -f
```

---

## Risk Management Reference

All risk parameters are in `.env`. Defaults are conservative.

| Variable | Default | What it controls |
|---|---|---|
| `MAX_POSITION_USD` | `500` | Hard cap on notional per trade |
| `RISK_PER_TRADE_PCT` | `1.0` | % of portfolio risked per signal; drives position size |
| `DAILY_LOSS_LIMIT_USD` | `200` | Agent pauses new entries when this is breached; does not close existing positions |
| `MAX_OPEN_POSITIONS` | `5` | Concurrent position cap |
| `MAX_DAILY_TRADES` | `20` | Trade count cap per day; `0` = unlimited |
| `COOLDOWN_SECONDS` | `300` | Minimum time between trades on the same asset |
| `MIN_SIGNAL_STRENGTH` | `0.7` | Nexwave score threshold (0–1); higher = fewer, higher-quality signals |
| `MIN_SIGNAL_CONFIDENCE` | `0.6` | Nexwave confidence threshold (0–1) |

### Position sizing formula

```
size_usd = portfolio_value * (RISK_PER_TRADE_PCT / 100) * regime_multiplier
size_usd = min(size_usd, MAX_POSITION_USD)
```

Regime multipliers applied by Nexwave:
- `trending_bull` → 1.0x (full size)
- `ranging` → 0.5x (half size)
- `high_volatility` → 0.25x (quarter size)
- `risk_off` → 0.0x (no new entries)

### What happens at the daily loss limit

When realized + unrealized PnL crosses `-DAILY_LOSS_LIMIT_USD`, the agent pauses new entries automatically. Existing positions continue to be monitored and exited by the exit manager. Resume manually with `nex resume` or wait for the next UTC midnight reset.

---

## Exit Modes

Set with `EXIT_MODE` in `.env`.

| Mode | Behavior |
|---|---|
| `signal` | Hold until Nexwave sends a reverse/close signal. No automatic exits. |
| `trailing_stop` | Close when price falls `TRAILING_STOP_PCT`% from the high-water-mark |
| `time` | Close after `TIME_STOP_HOURS` hours regardless of PnL |
| `hybrid` | All rules active simultaneously — whichever triggers first wins |

`hybrid` is recommended for most operators. It combines:
- **Hard stop-loss** (`STOP_LOSS_PCT`) — runs first, always
- **Trailing stop** (`TRAILING_STOP_PCT`) — locks in gains
- **Take-profit** (`TAKE_PROFIT_PCT`) — exits at target; `0` disables
- **Time stop** (`TIME_STOP_HOURS`) — prevents indefinite holds; `0` disables

---

## CLI Reference

CLI commands require the agent to be running (`uvicorn` or Docker). They call the FastAPI endpoints on port 7070.

```
nex status          Full agent state: positions, PnL, health, pause status
nex signals         Last 20 signals with acted_on flag and skip_reason
nex trades          Last 20 executed orders
nex positions       Open positions: entry, current price, unrealized PnL, exit levels
nex pause           Pause new entries (holds existing positions)
nex resume          Resume after pause
nex close BTC       Market-close a specific position by symbol
nex close-all       Emergency: market-close all open positions immediately
nex config          Print resolved config with secrets masked
```

If `API_KEY` is set in `.env`, the CLI reads it automatically. You can also pass it explicitly: `nex --api-key <key> status`.

---

## Monitoring and Alerts

### Telegram setup

1. Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → follow prompts → copy the token
2. Start a chat with your new bot (or add it to a group)
3. Get your chat ID: message [@userinfobot](https://t.me/userinfobot)
4. Set in `.env`:
   ```
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   TELEGRAM_CHAT_ID=<your chat ID>
   ```

The agent sends alerts for:
- Every trade entry (with symbol, side, size, entry price)
- Every position close (with reason and realized PnL)
- Stop-loss triggers
- Daily loss limit breach (with automatic pause notice)

### API monitoring

The `/health` endpoint is suitable for uptime monitoring:
```bash
curl http://localhost:7070/health
# → {"ok": true, "uptime": 86400}
```

Point any uptime monitor (UptimeRobot, Render health checks, etc.) at this endpoint.

### Log levels

Set `LOG_LEVEL=DEBUG` for verbose output including every signal evaluation and skip reason. `INFO` (default) logs trade activity and errors.

---

## x402 Pay-Per-Signal Mode

An alternative to API key subscriptions. You pay a small USDC amount on Solana for each signal fetch. No monthly commitment.

### Setup

1. Create a Solana wallet (Phantom recommended)
2. Fund it with USDC on Solana mainnet — $20–50 is a good starting amount
3. Export the private key from Phantom: **Settings → Security & Privacy → Export Private Key**
4. In `.env`, comment out `NEXWAVE_API_KEY` and set:
   ```
   NEXWAVE_X402_WALLET=<your Solana wallet address>
   NEXWAVE_X402_PRIVATE_KEY=<exported private key>
   ```
   The private key can be the base58 string from Phantom (88 chars) or a JSON array of 64 integers from Solana CLI keygen.

### Monitoring wallet balance

The agent logs a warning when payments fail. Check your balance:

```bash
python3 -c "
import urllib.request, json
wallet = '<your wallet address>'
usdc_mint = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
payload = json.dumps({'jsonrpc':'2.0','id':1,'method':'getTokenAccountsByOwner',
    'params':[wallet,{'mint':usdc_mint},{'encoding':'jsonParsed'}]}).encode()
req = urllib.request.Request('https://api.mainnet-beta.solana.com', data=payload,
    headers={'Content-Type':'application/json'})
resp = json.loads(urllib.request.urlopen(req).read())
accounts = resp.get('result',{}).get('value',[])
bal = int(accounts[0]['account']['data']['parsed']['info']['tokenAmount']['amount'])/1e6 if accounts else 0
print(f'USDC balance: \${bal:.4f}')
"
```

If balance hits zero, the agent keeps running but cannot fetch signals — all polls will log `x402 payment flow failed`. Top up the wallet and it resumes automatically with no restart needed.

### How payments work

When Nexwave returns HTTP 402, Nexagent:
1. Builds a Solana SPL USDC transfer transaction (partially signed — feePayer slot left for the Nexwave facilitator)
2. Base64-encodes it into a `X-Payment` header
3. Resends the signal request with that header
4. Nexwave's facilitator co-signs, broadcasts the tx, and returns the signal payload

The private key never leaves your machine — the Nexwave facilitator only adds their signature to your already-signed transaction.

---

## Troubleshooting

### Signals flowing but no trades

Check `nex signals` for `skip_reason` on each signal:

| Skip reason | Cause | Fix |
|---|---|---|
| `size_below_min_notional` | Position size < $11 | Increase `RISK_PER_TRADE_PCT` or deposit more |
| `regime_risk_off` | Nexwave flagged risk-off | Wait — this is intentional |
| `max_positions_reached` | At `MAX_OPEN_POSITIONS` | Increase limit or wait for exits |
| `daily_trade_limit` | Hit `MAX_DAILY_TRADES` | Increase limit or wait for reset |
| `asset_blocked` | Symbol is in `BLOCKED_ASSETS` | Remove from blocklist if desired |
| `strength_below_threshold` | Signal score < `MIN_SIGNAL_STRENGTH` | Lower threshold or wait for stronger signals |
| `already_open` | Position already open on this asset | Normal — prevents doubling |

### x402 payments failing

- Check USDC balance (see command above)
- Look for pattern: Solana POST returns 200 but second Nexwave GET still returns 402 → wallet is empty or Nexwave's facilitator is temporarily down
- Nexwave platform outages will also cause this — check [nexwave.so/status](https://nexwave.so/status)

### Hyperliquid order errors

- `Cannot increase position when open interest is at cap` — Hyperliquid has per-asset OI limits; the agent retries 3 times then skips. Normal during high-volume periods.
- `market orders require price` — should not occur in current code; if seen, report as a bug
- Orders below $10 notional are rejected silently by the exchange and skipped by the agent

### Agent not starting

- Missing env vars: run `nex config` to see what's loaded (secrets masked)
- Port 7070 in use: `lsof -ti:7070 | xargs kill` then restart
- Import errors: ensure you installed with the right extras: `pip install -e ".[alerts,x402]"`

### Recovering from a crash

On restart, the agent re-syncs its position database with the exchange (live mode only). Any positions opened before the crash will be detected and exit monitoring will resume. Orders placed while the agent was down will not have been tracked — close them manually with `nex close <symbol>` if needed.
