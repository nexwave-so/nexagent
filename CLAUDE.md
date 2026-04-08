# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Nexagent is an autonomous trading agent that polls Nexwave for ML-powered trading signals and executes trades on Hyperliquid (a perpetuals DEX). It is an async Python FastAPI application backed by SQLite, designed to run 24/7 with no external database or broker infrastructure.

## Commands

```bash
# Install (dev includes pytest; x402 adds Solana pay-per-signal support)
uv pip install -e ".[alerts,x402,dev]"

# Run the agent — uvicorn starts BOTH the agent loops AND the HTTP API (port 7070)
uvicorn nexagent.server:app --host 127.0.0.1 --port 7070

# nex start runs the agent loops ONLY (no HTTP API — nex status etc. won't work)
nex start                     # foreground, no API server
nex start --daemon            # background (PID → .nexagent.pid)
nex stop                      # stop background process

# CLI commands (require uvicorn to be running)
nex status
nex signals
nex trades
nex positions
nex pause / nex resume
nex close BTC
nex close-all

# Tests
pytest                        # all tests
pytest tests/test_risk.py     # single file
pytest tests/test_risk.py::test_passes_valid_signal  # single test

# Docker
docker build -t nexagent .
docker run -d --env-file .env -p 7070:7070 nexagent
```

All tests use `asyncio_mode = "auto"` (pytest-asyncio).

## Architecture

### Two-Loop Design

The agent runs two concurrent async loops:

- **Signal loop** (every 30s): Poll Nexwave API → apply risk filters → execute trades
- **Exit loop** (every 10s): Sync live prices → apply exit rules → close positions

Additional: daily loss check on every exit iteration; regime refresh every 4 hours.

### Signal Pipeline

`poll_signals()` → dedup via DB (`signal_seen()` in last 1hr) → `RiskManager.check()` (10+ filters) → conflict check (if reverse position exists, close it first) → `RiskManager.position_size_usd()` → `Executor.execute_signal()` → persist to SQLite → Telegram alert.

### Exit Logic (`exits.py`)

In `hybrid` mode, all of these are active per position: hard stop-loss (always runs first), trailing stop (% from high-water-mark), take-profit (% from entry), time stop (max hold hours). `ExitMode.SIGNAL` skips automatic exits entirely — manual close only.

### Position Sizing

`portfolio_value * (risk_pct / 100) * regime_multiplier`, capped at `max_position_usd`. Regime multipliers: `trending_bull=1.0`, `ranging=0.5`, `high_volatility=0.25`, `risk_off=0.0` (blocks all new entries).

### Paper Trading

Default mode (`PAPER_TRADING=true`). Fills are simulated at mid-price via Hyperliquid REST ticker; no real orders are placed. Full exit monitoring and PnL tracking still run. Telegram alerts show `[PAPER]` badge.

### Key Modules

| File | Class/Role |
|------|------------|
| `agent.py` | `Agent` — orchestrates both loops, holds shared state |
| `config.py` | `Config` — Pydantic `BaseSettings`, reads `.env` |
| `executor.py` | `Executor` — CCXT wrapper; paper vs. live dispatch |
| `risk.py` | `RiskManager` — pre-trade filtering and position sizing |
| `exits.py` | `ExitManager` — stop-loss, trailing stop, TP, time |
| `db.py` | `Database` — aiosqlite; 4 tables: signals, orders, positions, daily_pnl |
| `signals.py` | `poll_signals()`, `fetch_regime()` — HTTP client to Nexwave |
| `x402.py` | `sign_and_pay()` — builds partially-signed Solana tx for x402 pay-per-signal |
| `alerts.py` | `TelegramAlert` — optional bot notifications |
| `server.py` | FastAPI app with lifespan startup/shutdown |
| `cli.py` | Typer CLI; delegates all commands to the FastAPI endpoints |
| `models.py` | Pydantic data models (NexwaveSignal, Order, Position, etc.) |

### API (port 7070)

`GET /health`, `/status`, `/signals`, `/trades`, `/positions`
`POST /pause`, `/resume`, `/close/{symbol}`, `/close-all`

Optional bearer token auth via `API_KEY` env var. The CLI (`nex status`, `nex close BTC`, etc.) is a thin client over these endpoints.

## Configuration

Copy `.env.example` to `.env`. Required variables:

```
HYPERLIQUID_WALLET_ADDRESS=0x...
HYPERLIQUID_PRIVATE_KEY=0x...
NEXWAVE_API_KEY=nxw_...          # OR x402 Solana pay-per-signal mode (below)

# x402 pay-per-signal (alternative to API key):
NEXWAVE_X402_WALLET=<base58 Solana pubkey, 44 chars>
NEXWAVE_X402_PRIVATE_KEY=<base58 keypair, 88 chars — Phantom "Export Private Key">
# Also accepted: JSON array of 64 ints (Solana CLI keygen format)
```

Key risk/behavior variables (all have defaults in `config.py`):

```
PAPER_TRADING=true
MAX_POSITION_USD=500
RISK_PER_TRADE_PCT=1.0
DAILY_LOSS_LIMIT_USD=200
MAX_OPEN_POSITIONS=5
EXIT_MODE=hybrid                 # signal | trailing_stop | time | hybrid
STOP_LOSS_PCT=3.0
TRAILING_STOP_PCT=2.0
TAKE_PROFIT_PCT=5.0
TIME_STOP_HOURS=72
ALLOWED_ASSETS=                  # empty = all
BLOCKED_ASSETS=FARTCOIN,PENGU
```

## Deployment

- **Render.com**: `render.yaml` defines the service. Set env vars in the dashboard; auto-deploys on `git push main`. Health check at `/health`.
- **Docker**: `Dockerfile` + `Procfile` are present. Expose port 7070.
- **Cold start**: In live mode, the agent syncs its DB with current exchange positions on startup to recover from restarts.

## x402 Pay-per-Signal

When `NEXWAVE_X402_WALLET` + `NEXWAVE_X402_PRIVATE_KEY` are set (and `NEXWAVE_API_KEY` is absent), Nexwave is accessed via x402 micro-payments instead of a subscription key.

Flow: Nexwave returns HTTP 402 → `signals.py` calls `x402.sign_and_pay()` → builds a partially-signed Solana tx (SPL USDC TransferChecked + Memo + Compute Budget) with the facilitator as feePayer → tx is base64-encoded inside a `PaymentPayload` JSON → sent as `X-Payment` header → facilitator co-signs and broadcasts.

Spec: https://github.com/coinbase/x402/blob/main/specs/schemes/exact/scheme_exact_svm.md  
Overview: https://solana.com/x402
