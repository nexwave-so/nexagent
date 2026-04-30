"""Cold-path LLM analyst — learns from trades without touching execution."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from .config import Config
from .db import Database
from .llm import LLMClient
from .alerts import TelegramAlert
from .utils import utcnow

logger = logging.getLogger(__name__)

# ── System prompts ───────────────────────────────────────────────────────────

_SYSTEM_TRADE_REVIEW = """You are a quantitative trading analyst reviewing completed trades for an autonomous scalping agent on Hyperliquid perp futures.

The agent trades three asset classes:
- Crypto perps (e.g., AXS, BLUR, BTC) — high volatility, higher fees (~0.085% of notional)
- Equity perps (e.g., xyz:SAMSUNG, xyz:DKNG) — medium volatility, lower fees (~0.017%)
- Commodity perps (e.g., xyz:BRENTOIL, xyz:WTIOIL) — trending, lowest fees (~0.017%)

The agent's sweet spot is 5-15 minute holds. Positions held 30min-6hr historically bleed money.

Your job is to analyze each completed trade and provide:
1. What went right or wrong
2. Whether the exit was optimal
3. A concrete, actionable recommendation

Be concise. No fluff. Think like a prop desk risk manager."""

_SYSTEM_REGIME = """You are a market microstructure analyst. Given recent trading performance data and market metrics, classify the current market regime and recommend position sizing adjustments.

Regimes: trending_bull, alt_season, ranging, high_volatility, risk_off

Output JSON with:
- regime: one of the regime labels above
- confidence: 0.0-1.0
- reasoning: 1-2 sentences
- size_multiplier: 0.0-1.0 recommended position size multiplier
- recommendations: list of 1-3 specific parameter suggestions"""

_SYSTEM_DAILY = """You are a senior quant reviewing an autonomous trading agent's daily performance. You have access to all trades from the past 24 hours grouped by asset class and direction.

Provide:
1. Performance summary with key metrics
2. What's working and what isn't
3. Specific parameter change recommendations (with exact values)
4. Risk concerns if any

Be direct and data-driven. The operator is technical and prefers tables and numbers over narrative."""


class Analyst:
    """Async LLM analyst — all methods are fire-and-forget safe."""

    def __init__(self, config: Config, db: Database, llm: LLMClient, alerts: TelegramAlert) -> None:
        self.config = config
        self.db = db
        self.llm = llm
        self.alerts = alerts
        self._last_regime_at: datetime | None = None
        self._last_daily_at: str | None = None  # date string

    # ── Post-trade review (called after every exit) ──────────────────────────

    async def review_trade(
        self,
        symbol: str,
        asset_class: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        size_usd: float,
        pnl_usd: float,
        hold_minutes: float,
        exit_reason: str,
        signal_type: str | None,
    ) -> None:
        """Analyze a single completed trade. Fire-and-forget safe."""
        if not self.llm.enabled:
            return
        try:
            perf = await self.db.get_performance()
            ac_perf = [p for p in perf if p["asset_class"] == asset_class]

            pnl_pct = (pnl_usd / size_usd * 100) if size_usd > 0 else 0
            prompt = f"""Analyze this completed trade:

Symbol: {symbol}
Asset class: {asset_class}
Direction: {direction}
Signal type: {signal_type or 'unknown'}
Entry: {entry_price:.6f}
Exit: {exit_price:.6f}
Size: ${size_usd:.2f}
PnL: ${pnl_usd:.4f} ({pnl_pct:+.2f}%)
Hold time: {hold_minutes:.1f} minutes
Exit reason: {exit_reason}

Recent {asset_class} performance:
{json.dumps(ac_perf, indent=2) if ac_perf else 'No prior trades in this asset class.'}

Respond with JSON:
{{
  "verdict": "good" | "bad" | "neutral",
  "analysis": "1-2 sentence analysis",
  "recommendation": "specific actionable recommendation or 'none'",
  "suggested_filters": ["list of filter suggestions if the trade should have been avoided, or empty"]
}}"""

            result = await self.llm.complete_json(prompt, system=_SYSTEM_TRADE_REVIEW)
            if result:
                await self.db.save_insight(
                    insight_type="trade_review",
                    symbol=symbol,
                    content=result,
                )
                if result.get("verdict") == "bad" and result.get("recommendation", "none") != "none":
                    await self.alerts.llm_insight(
                        f"📊 *Trade Review: {symbol}*\n"
                        f"PnL: `${pnl_usd:+.4f}` ({pnl_pct:+.2f}%) · {hold_minutes:.0f}min\n"
                        f"💡 {result['recommendation']}"
                    )
        except Exception as e:
            logger.warning("Post-trade review failed for %s: %s", symbol, e)

    # ── Regime analysis (every N min) ────────────────────────────────────────

    async def maybe_analyze_regime(self) -> None:
        """Run regime analysis if enough time has passed. Fire-and-forget safe."""
        if not self.llm.enabled:
            return

        interval = self.config.llm_regime_interval_minutes
        if interval <= 0:
            return

        now = utcnow()
        if self._last_regime_at and (now - self._last_regime_at).total_seconds() < interval * 60:
            return

        try:
            self._last_regime_at = now

            recent = await self.db.get_recent_trade_log(hours=6)
            if not recent:
                return

            total_pnl = sum(t.get("pnl_usd", 0) for t in recent)
            wins = sum(1 for t in recent if t.get("pnl_usd", 0) > 0)
            losses = len(recent) - wins
            by_class: dict = {}
            for t in recent:
                ac = t.get("asset_class", "unknown")
                if ac not in by_class:
                    by_class[ac] = {"trades": 0, "pnl": 0.0, "wins": 0}
                by_class[ac]["trades"] += 1
                by_class[ac]["pnl"] += t.get("pnl_usd", 0)
                if t.get("pnl_usd", 0) > 0:
                    by_class[ac]["wins"] += 1

            prompt = f"""Analyze current market regime based on recent trading data.

Last 6 hours: {len(recent)} trades, PnL=${total_pnl:.4f}, W/L={wins}/{losses}

By asset class:
{json.dumps(by_class, indent=2)}

Recent trade details (last 10):
{json.dumps(recent[-10:], indent=2, default=str)}

Current agent config:
- Exit mode: {self.config.exit_mode}
- Max positions: {self.config.max_open_positions}
- Consecutive losses allowed: {self.config.max_consecutive_losses}

Classify the regime and recommend adjustments. Respond with JSON:
{{
  "regime": "trending_bull" | "alt_season" | "ranging" | "high_volatility" | "risk_off",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation",
  "size_multiplier": 0.0-1.0,
  "recommendations": ["list of specific suggestions"]
}}"""

            result = await self.llm.complete_json(prompt, system=_SYSTEM_REGIME)
            if result:
                await self.db.save_insight(
                    insight_type="regime_analysis",
                    symbol=None,
                    content=result,
                )
                regime = result.get("regime", "unknown")
                conf = result.get("confidence", 0)
                logger.info("LLM regime analysis: %s (confidence=%.2f)", regime, conf)

                if regime in ("risk_off", "high_volatility"):
                    recs = result.get("recommendations", [])
                    rec_text = "\n".join(f"  • {r}" for r in recs[:3]) if recs else ""
                    await self.alerts.llm_insight(
                        f"⚠️ *Regime Alert: {regime}*\n"
                        f"Confidence: {conf:.0%}\n"
                        f"{result.get('reasoning', '')}\n{rec_text}"
                    )
        except Exception as e:
            logger.warning("Regime analysis failed: %s", e)

    # ── Daily review (once per day) ──────────────────────────────────────────

    async def maybe_daily_review(self) -> None:
        """Run daily review if it hasn't been done today. Fire-and-forget safe."""
        if not self.llm.enabled:
            return
        if not self.config.llm_daily_review_enabled:
            return

        today = utcnow().strftime("%Y-%m-%d")
        if self._last_daily_at == today:
            return

        # Only run after at least 1 hour into the day (avoid running at midnight with no data)
        if utcnow().hour < 1:
            return

        try:
            self._last_daily_at = today

            trades = await self.db.get_recent_trade_log(hours=24)
            if len(trades) < 3:
                return

            total_pnl = sum(t.get("pnl_usd", 0) for t in trades)
            wins = sum(1 for t in trades if t.get("pnl_usd", 0) > 0)
            win_rate = wins / len(trades) if trades else 0

            gross_profit = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
            gross_loss = abs(sum(t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0))
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

            groups: dict = {}
            for t in trades:
                key = f"{t.get('asset_class', '?')}_{t.get('direction', '?')}"
                if key not in groups:
                    groups[key] = {"trades": 0, "pnl": 0.0, "wins": 0, "avg_hold": 0.0}
                groups[key]["trades"] += 1
                groups[key]["pnl"] += t.get("pnl_usd", 0)
                if t.get("pnl_usd", 0) > 0:
                    groups[key]["wins"] += 1
                groups[key]["avg_hold"] += t.get("hold_minutes", 0)
            for g in groups.values():
                if g["trades"] > 0:
                    g["avg_hold"] = round(g["avg_hold"] / g["trades"], 1)

            exit_reasons: dict = {}
            for t in trades:
                r = t.get("exit_reason", "unknown")
                exit_reasons[r] = exit_reasons.get(r, 0) + 1

            prompt = f"""Daily performance review for {today}.

Summary: {len(trades)} trades, PnL=${total_pnl:.4f}, Win rate={win_rate:.1%}, Profit factor={profit_factor:.2f}

By asset class + direction:
{json.dumps(groups, indent=2)}

Exit reason distribution:
{json.dumps(exit_reasons, indent=2)}

Current config snapshot:
- Stop loss (crypto): {self.config.stop_loss_pct_long_crypto}%
- Stop loss (equity): {self.config.stop_loss_pct_long_equity}%
- Trailing stop (crypto): {self.config.trailing_stop_pct_crypto}%
- Trailing stop (equity): {self.config.trailing_stop_pct_equity}%
- Trailing stop (commodity): {self.config.trailing_stop_pct_commodity}%
- Take profit (crypto): {self.config.take_profit_pct_crypto}%
- Take profit (equity/commodity): {self.config.take_profit_pct_equity}%
- Trailing activation: {self.config.trailing_activation_pct}%
- Time stop: {self.config.time_stop_hours}h
- Min hold: {self.config.min_hold_minutes}min
- Cooldown: {self.config.cooldown_seconds}s
- Loss cooldown: {self.config.loss_cooldown_seconds}s
- Max consecutive losses: {self.config.max_consecutive_losses}

All trades (chronological):
{json.dumps(trades, indent=2, default=str)}

Provide a daily review with specific parameter recommendations. Respond with JSON:
{{
  "summary": "2-3 sentence performance summary",
  "whats_working": ["list of things working well"],
  "whats_not": ["list of things not working"],
  "parameter_changes": [
    {{"param": "config_field_name", "current": "current_value", "suggested": "new_value", "reason": "why"}}
  ],
  "risk_alerts": ["any risk concerns"],
  "overall_grade": "A" | "B" | "C" | "D" | "F"
}}"""

            result = await self.llm.complete_json(
                prompt,
                system=_SYSTEM_DAILY,
                model=self.config.llm_model_reasoning,
                max_tokens=4000,
                temperature=0.2,
            )
            if result:
                await self.db.save_insight(
                    insight_type="daily_review",
                    symbol=None,
                    content=result,
                )
                grade = result.get("overall_grade", "?")
                summary = result.get("summary", "No summary")
                params = result.get("parameter_changes", [])
                param_text = ""
                if params:
                    param_text = "\n*Suggested changes:*\n"
                    for p in params[:5]:
                        param_text += f"  `{p.get('param')}`: {p.get('current')} → {p.get('suggested')}\n"

                await self.alerts.llm_insight(
                    f"📈 *Daily Review — Grade: {grade}*\n"
                    f"Trades: {len(trades)} · PnL: ${total_pnl:+.4f} · WR: {win_rate:.0%}\n\n"
                    f"{summary}{param_text}"
                )
                logger.info(
                    "Daily review complete: grade=%s, %d parameter suggestions",
                    grade, len(params),
                )
        except Exception as e:
            logger.warning("Daily review failed: %s", e)
