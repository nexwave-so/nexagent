from __future__ import annotations

import logging
from datetime import datetime

from .config import Config
from .models import AgentStatus, NexwaveSignal, RegimeData

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

        if signal.signal_type not in self.config.allowed_signal_types_set:
            return False, f"signal_type_filtered ({signal.signal_type})"

        if signal.strength < self.config.min_signal_strength:
            return False, f"strength_below_threshold ({signal.strength:.2f})"

        if signal.confidence < self.config.min_signal_confidence:
            return False, f"confidence_below_threshold ({signal.confidence:.2f})"

        if signal.symbol.upper() in self.config.blocked_assets_set:
            return False, "asset_blocked"

        allowed = self.config.allowed_assets_set
        if allowed and signal.symbol.upper() not in allowed:
            return False, "asset_not_in_allowlist"

        if self._in_cooldown(signal.symbol):
            return False, "cooldown_active"

        if self._current_regime == "risk_off":
            return False, "regime_risk_off"

        return True, ""

    def record_trade(self, symbol: str) -> None:
        self._cooldowns[symbol.upper()] = datetime.utcnow()

    def _in_cooldown(self, symbol: str) -> bool:
        last = self._cooldowns.get(symbol.upper())
        if last is None:
            return False
        elapsed = (datetime.utcnow() - last).total_seconds()
        return elapsed < self.config.cooldown_seconds

    def position_size_usd(self, portfolio_usd: float) -> float:
        raw = portfolio_usd * (self.config.risk_per_trade_pct / 100)
        capped = min(raw, self.config.max_position_usd)
        multiplier = _REGIME_MULTIPLIERS.get(self._current_regime, 1.0)
        return capped * multiplier
