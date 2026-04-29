from __future__ import annotations

import logging
from datetime import datetime

from .config import Config
from .models import AgentStatus, NexwaveSignal, RegimeData
from .utils import utcnow

logger = logging.getLogger(__name__)

# Regime position-size multipliers (matches Nexwave regime states)
_REGIME_MULTIPLIERS: dict[str, float] = {
    "trending_bull": 1.0,
    "alt_season": 1.0,
    "ranging": 0.5,
    "high_volatility": 0.25,
    "risk_off": 0.0,
}


class RiskManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._cooldowns: dict[str, datetime] = {}
        self._loss_cooldowns: dict[str, datetime] = {}  # asset_class → last loss time
        self._current_regime: str = "ranging"

    def update_regime(self, regime: RegimeData) -> None:
        self._current_regime = regime.state
        logger.info("Regime updated: %s (confidence=%.2f)", regime.state, regime.confidence)

    def check(
        self, signal: NexwaveSignal, state: AgentStatus
    ) -> tuple[bool, str]:
        if state.paused:
            return False, f"paused: {state.paused_reason}"

        if state.daily_pnl_usd < -self.config.daily_loss_limit_usd:
            return False, "daily_loss_limit_hit"

        if state.open_positions >= self.config.max_open_positions:
            return False, "max_positions_reached"

        if signal.direction == "long":
            cap = self.config.max_long_positions
            if cap > 0 and state.open_long_positions >= cap:
                return False, "max_long_positions_reached"
        else:
            cap = self.config.max_short_positions
            if cap > 0 and state.open_short_positions >= cap:
                return False, "max_short_positions_reached"

        ac = self.config.asset_class(signal.symbol)
        if ac == "crypto" and self.config.max_crypto_positions > 0:
            if state.open_crypto_positions >= self.config.max_crypto_positions:
                return False, "max_crypto_positions_reached"
        elif ac == "equity" and self.config.max_equity_positions > 0:
            if state.open_equity_positions >= self.config.max_equity_positions:
                return False, "max_equity_positions_reached"
        elif ac == "commodity" and self.config.max_commodity_positions > 0:
            if state.open_commodity_positions >= self.config.max_commodity_positions:
                return False, "max_commodity_positions_reached"

        if signal.signal_type not in self.config.allowed_signal_types_set:
            return False, f"signal_type_filtered ({signal.signal_type})"

        if signal.strength < self.config.min_signal_strength:
            return False, f"strength_below_threshold ({signal.strength:.2f})"

        if signal.direction == "long" and ":" not in signal.symbol:
            boosted = self.config.min_signal_strength + self.config.crypto_long_strength_boost
            if signal.strength < boosted:
                return False, f"crypto_long_strength_below_boosted ({signal.strength:.2f} < {boosted:.2f})"

        if signal.confidence < self.config.min_signal_confidence:
            return False, f"confidence_below_threshold ({signal.confidence:.2f})"

        if self.config.max_daily_trades > 0 and state.trades_today >= self.config.max_daily_trades:
            return False, "max_daily_trades_reached"

        if signal.symbol.upper() in self.config.blocked_assets_set:
            return False, "asset_blocked"

        allowed = self.config.allowed_assets_set
        if allowed and signal.symbol.upper() not in allowed:
            return False, "asset_not_in_allowlist"

        if self.config.block_crypto and ":" not in signal.symbol:
            return False, "crypto_blocked"

        if self._in_cooldown(signal.symbol):
            return False, "cooldown_active"

        if self._current_regime == "risk_off":
            return False, "regime_risk_off"

        return True, ""

    def record_trade(self, symbol: str) -> None:
        self._cooldowns[symbol.upper()] = utcnow()

    def record_loss(self, symbol: str) -> None:
        ac = self.config.asset_class(symbol)
        self._loss_cooldowns[ac] = utcnow()

    def _in_cooldown(self, symbol: str) -> bool:
        last = self._cooldowns.get(symbol.upper())
        if last is not None and (utcnow() - last).total_seconds() < self.config.cooldown_seconds:
            return True
        if self.config.loss_cooldown_seconds > 0:
            ac = self.config.asset_class(symbol)
            last_loss = self._loss_cooldowns.get(ac)
            if last_loss is not None and (utcnow() - last_loss).total_seconds() < self.config.loss_cooldown_seconds:
                return True
        return False

    def position_size_usd(self, portfolio_usd: float, signal: NexwaveSignal) -> float:
        raw = portfolio_usd * (self.config.risk_per_trade_pct / 100)
        capped = min(raw, self.config.max_position_usd)
        multiplier = _REGIME_MULTIPLIERS.get(self._current_regime, 1.0)
        # Floor conviction at 0.5 so regime+signal scaling never shrinks size below half
        conviction = max(signal.strength * signal.confidence, 0.5)
        return capped * multiplier * conviction
