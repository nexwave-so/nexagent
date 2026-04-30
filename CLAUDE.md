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

### Two-Loop Design + Cold Path

The agent runs two concurrent async loops (the **hot path**):

- **Signal loop** (every 30s): Poll Nexwave API → apply risk filters → execute trades
- **Exit loop** (every 10s): Sync live prices → apply exit rules → close positions

Additional: daily loss check on every exit iteration; regime refresh every 4 hours.

The **cold path** runs asynchronously via `asyncio.create_task` (fire-and-forget) and never blocks trade execution:

- **Post-trade review** (per closed trade): LLM analyzes the completed trade and stores a verdict + recommendation in `llm_insights`
- **Regime analysis** (every 30 min, configurable): LLM classifies market regime from recent 6hr trade data; alerts on `risk_off`/`high_volatility`
- **Daily review** (once per day after 01:00 UTC): LLM produces a full strategy review with parameter change suggestions and an overall grade

Cold path is opt-in: set `OPENROUTER_API_KEY` to enable. Without it, all analyst methods return immediately.

### Signal Pipeline

`poll_signals()` → dedup via DB (`signal_seen()` in last 1hr, **only `acted_on=1` signals count**) → `RiskManager.check()` (10+ filters) → conflict check (if reverse position exists, close it first) → `RiskManager.position_size_usd()` → min-notional floor check → `Executor.execute_signal()` → persist to SQLite → Telegram alert.

`RiskManager.check()` filters include: paused state, daily loss limit, max open positions, per-asset-class position caps, directional caps, signal type allowlist, min strength/confidence, **crypto long strength boost** (+0.10 over baseline), daily trade cap, asset blocklist/allowlist, cooldown (standard + **loss cooldown per asset class**), and regime gate.

### Exit Logic (`exits.py`)

In `hybrid` mode, all of these are active per position: hard stop-loss (always runs first), trailing stop (% from high-water-mark), take-profit (% from entry), time stop (max hold hours). `ExitMode.SIGNAL` skips automatic exits entirely — manual close only.

**Asset-class-aware exits**: stop-loss %, trailing stop %, and take-profit % are all looked up per asset class (crypto/equity/commodity) via `config.asset_class(symbol)`. Defaults: crypto SL 0.9%/TSL 1.0%/TP 1.2%, equity SL 0.7%/TSL 0.8%/TP 0.5%, commodity SL 1.0%/TSL 0.8%/TP 0.5%. Crypto TP is higher than equity/commodity because its fees are ~5× larger.

**Trailing stop activation gate**: the trailing stop only arms once the position is `TRAILING_ACTIVATION_PCT` (default 1%) in profit from entry. Below that threshold only the hard stop fires, preventing premature exits on positions that haven't had a chance to move.

### Position Sizing

`portfolio_value * (risk_pct / 100) * regime_multiplier * conviction`, capped at `max_position_usd`. Regime multipliers: `trending_bull=1.0`, `ranging=0.5`, `high_volatility=0.25`, `risk_off=0.0` (blocks all new entries). Conviction = `max(strength * confidence, 0.5)` — floored at 50% so micro-sizing is avoided in ranging+weak-signal conditions.

After sizing, `agent.py` enforces a `_MIN_NOTIONAL = $11` floor — signals are skipped with `size_below_min_notional` rather than sent to the exchange where they would be rejected. With a `ranging` multiplier of 0.5, `RISK_PER_TRADE_PCT` must be ≥ ~22% to reliably clear this floor on a $100 portfolio.

### Circuit Breakers

- **Consecutive loss limit**: if `MAX_CONSECUTIVE_LOSSES` (default 6) losses occur in a row, the agent auto-pauses with reason `consecutive_loss_limit`. Resets on any winning trade. Set to 0 to disable.
- **Loss cooldown**: after a losing trade, all signals for the same asset class (crypto/equity/commodity) are blocked for `LOSS_COOLDOWN_SECONDS` (default 900 = 15 min).

### Asset Classification

`Config.asset_class(symbol)` classifies symbols used throughout exits and risk:
- No `:` prefix → **crypto** (e.g. `AXS`, `BLUR`)
- Venue prefix + known commodity → **commodity** (e.g. `xyz:BRENTOIL`, `vntl:NATGAS`)
- Venue prefix + anything else → **equity** (e.g. `xyz:SAMSUNG`, `xyz:DKNG`)

Known commodities: `BRENTOIL`, `WTIOIL`, `NATGAS`, `GOLD`, `SILVER`, `COPPER`, `WHEAT`, `CORN`, `SOYBEAN`.

### Trade Log

Every closed round-trip is recorded in the `trade_log` SQLite table with: symbol, asset_class, direction, signal_type, entry/exit price, size_usd, pnl_usd, hold_minutes, exit_reason, opened_at, closed_at. Query via `GET /performance`.

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
| `db.py` | `Database` — aiosqlite; 6 tables: signals, orders, positions, daily_pnl, trade_log, llm_insights |
| `signals.py` | `poll_signals()`, `fetch_regime()` — HTTP client to Nexwave |
| `x402.py` | `sign_and_pay()` — builds partially-signed Solana tx for x402 pay-per-signal |
| `alerts.py` | `TelegramAlert` — optional bot notifications |
| `llm.py` | `LLMClient` — async OpenRouter client; `complete()` / `complete_json()`; best-effort |
| `analyst.py` | `Analyst` — cold path brain; post-trade review, regime analysis, daily review |
| `server.py` | FastAPI app with lifespan startup/shutdown |
| `cli.py` | Typer CLI; delegates all commands to the FastAPI endpoints |
| `models.py` | Pydantic data models (NexwaveSignal, Order, Position, etc.) |

### API (port 7070)

`GET /health`, `/status`, `/signals`, `/trades`, `/positions`, `/performance`, `/insights`
`POST /pause`, `/resume`, `/close/{symbol}`, `/close-all`

`/performance` returns per-asset-class, per-direction win rates, profit factors, and average hold time from the `trade_log` table.

`/insights` returns LLM analyst results from the `llm_insights` table. Optional query params: `insight_type` (filter to `trade_review`, `regime_analysis`, or `daily_review`) and `limit` (default 20). CLI: `nex insights --type trade_review -n 5`.

Optional bearer token auth via `API_KEY` env var. The CLI (`nex status`, `nex close BTC`, etc.) is a thin client over these endpoints.

## Configuration

Copy `.env.example` to `.env`. Required variables:

```
HYPERLIQUID_WALLET_ADDRESS=0x...
HYPERLIQUID_PRIVATE_KEY=0x...
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
STOP_LOSS_PCT_LONG=1.5           # fallback; per-class overrides below take precedence
STOP_LOSS_PCT_SHORT=1.5
TRAILING_STOP_PCT=0.8            # fallback
TAKE_PROFIT_PCT=0.5              # fallback
TIME_STOP_HOURS=1.0              # 1-hour hard kill; raised from 0.5h (avg winning hold exceeds 55 min)
MIN_HOLD_MINUTES=3               # skip trailing/TP for first 3 min (noise zone)
ALLOWED_ASSETS=                  # empty = all
BLOCKED_ASSETS=FARTCOIN,PENGU

# Per-asset-class exit overrides
# Crypto: TP lowered 3.0%→1.2% (3% never triggered; actual wins avg ~0.65%); SL tightened to cut losers faster
STOP_LOSS_PCT_LONG_CRYPTO=0.9
STOP_LOSS_PCT_SHORT_CRYPTO=0.9
TRAILING_STOP_PCT_CRYPTO=1.0
TAKE_PROFIT_PCT_CRYPTO=1.2
# Equity: SL tightened to match the 0.5% TP target
STOP_LOSS_PCT_LONG_EQUITY=0.7
STOP_LOSS_PCT_SHORT_EQUITY=0.7
TRAILING_STOP_PCT_EQUITY=0.8
TAKE_PROFIT_PCT_EQUITY=0.5
# Commodity: moderate SL tighten
STOP_LOSS_PCT_LONG_COMMODITY=1.0
STOP_LOSS_PCT_SHORT_COMMODITY=1.0
TRAILING_STOP_PCT_COMMODITY=0.8
TAKE_PROFIT_PCT_COMMODITY=0.5
TRAILING_ACTIVATION_PCT=1.0      # trailing stop only arms once position is this % in profit

# Circuit breakers
MAX_CONSECUTIVE_LOSSES=6         # pause after N consecutive losses (0 = disabled)
LOSS_COOLDOWN_SECONDS=900        # extra cooldown per asset class after a loss (15 min)

# Per-asset-class position caps — crypto capped at 1; equity/commodity get more room
MAX_CRYPTO_POSITIONS=1
MAX_EQUITY_POSITIONS=3
MAX_COMMODITY_POSITIONS=3

# Signal quality
CRYPTO_LONG_STRENGTH_BOOST=0.15  # extra min-strength required for crypto longs (needs ≥ 0.85)

# LLM cold path (optional — OpenRouter)
OPENROUTER_API_KEY=              # sk-or-v1-... — enables AI-powered trade analysis
LLM_MODEL_FAST=deepseek/deepseek-chat-v3-0324   # per-trade review & regime analysis
LLM_MODEL_REASONING=deepseek/deepseek-r1        # daily strategy review
LLM_REGIME_INTERVAL_MINUTES=30  # 0 = disabled
LLM_DAILY_REVIEW_ENABLED=true
```

## Deployment

- **Render.com**: `render.yaml` defines the service. Set env vars in the dashboard; auto-deploys on `git push main`. Health check at `/health`.
- **Docker**: `Dockerfile` + `Procfile` are present. Expose port 7070.
- **Cold start**: In live mode, the agent syncs its DB with current exchange positions on startup to recover from restarts.

## x402 Pay-per-Signal

Nexwave is accessed exclusively via x402 micro-payments. `NEXWAVE_X402_WALLET` + `NEXWAVE_X402_PRIVATE_KEY` are required.

Flow: Nexwave returns HTTP 402 → `signals.py` calls `x402.sign_and_pay()` → builds a partially-signed Solana tx (SPL USDC TransferChecked + Memo + Compute Budget) with the facilitator as feePayer → tx is base64-encoded inside a `PaymentPayload` JSON → sent as `PAYMENT-SIGNATURE` header → facilitator co-signs and broadcasts.

**Payment header name**: The `@x402/next` library reads `PAYMENT-SIGNATURE` (or `payment-signature`), NOT `X-PAYMENT`. Do not change this header name.

**Facilitator**: Nexwave uses `https://facilitator.payai.network`. This facilitator has a **free tier quota**. When the quota is exhausted, `/settle` returns `403 free_tier_exhausted` and every signal request fails with a bare `402 {}` (no `payment-required` header). This is distinct from a normal payment failure — the agent logs show `x402 payment flow failed` but the underlying cause is invisible without calling `/settle` directly. To restore operation: top up credits at `https://merchant.payai.network`.

Spec: https://github.com/coinbase/x402/blob/main/specs/schemes/exact/scheme_exact_svm.md  
Overview: https://solana.com/x402

### x402 signing subtleties

- **Wire format**: Sign `bytes([0x80]) + bytes(msg)`, NOT `bytes(msg)` alone. `bytes(MessageV0)` in solders uses `0x02` prefix (internal bincode); the Solana runtime verifies against the `0x80`-prefixed wire format.
- **Partial signing**: Use `VersionedTransaction.populate(msg, sigs)` with `Signature.default()` placeholders for any signer slots not controlled locally (feePayer slot). `VersionedTransaction(msg, [keypair])` fails when feePayer ≠ authority keypair.
- **ATA program**: The deployed mainnet Associated Token Account program is `ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL`. The address ending in `...LJe1bRS` seen in older Python docs is not deployed on mainnet.

### Hyperliquid / Executor subtleties

- **Symbol mapping**: Nexwave uses venue-prefixed symbols (`xyz:CL`, `vntl:WHEAT`). `Executor._to_ccxt_symbol()` maps these to CCXT market IDs (`XYZ-CL/USDC:USDC`, `VNTL-WHEAT/USDH:USDH`). Plain symbols map to `SYMBOL/USDC:USDC`.
- **Market order price**: CCXT's Hyperliquid driver requires a `price` argument on `create_market_order()` to compute max-slippage price. Omitting it raises `"market orders require price to calculate the max slippage price"`.
- **Minimum notional**: Hyperliquid rejects orders below $10 notional. The agent skips rather than retries these.
- **Master wallet vs agent wallet**: `walletAddress` in CCXT config is the master wallet that holds funds; `privateKey` is the agent/signing wallet. These may differ on Hyperliquid — ensure `HYPERLIQUID_WALLET_ADDRESS` points to the funded account.
