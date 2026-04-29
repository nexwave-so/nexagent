from __future__ import annotations

import logging
from datetime import timedelta

from .config import Config
from .models import ExitAction, Position
from .utils import utcnow

logger = logging.getLogger(__name__)


class ExitManager:
    def __init__(self, config: Config) -> None:
        self.config = config

    def check_exits(self, positions: list[Position]) -> list[ExitAction]:
        actions: list[ExitAction] = []
        for pos in positions:
            action = self._check_position(pos)
            if action:
                actions.append(action)
        return actions

    def _check_position(self, pos: Position) -> ExitAction | None:
        current = pos.current_price
        if current is None:
            return None

        # Hard stop-loss — always active regardless of exit mode or hold time
        if self._stop_loss_hit(pos, current):
            logger.info(
                "Stop-loss triggered for %s @ %.4f (entry=%.4f)",
                pos.symbol, current, pos.entry_price,
            )
            return ExitAction(position=pos, reason="stop_loss")

        # Skip soft exits (trailing stop, TP, time) until min hold has elapsed
        if self._in_min_hold(pos):
            return None

        match self.config.exit_mode:
            case "signal":
                return None  # exits handled by signal loop only

            case "trailing_stop":
                if self._trailing_stop_hit(pos, current):
                    return ExitAction(position=pos, reason="trailing_stop")

            case "time":
                if self._time_stop_hit(pos):
                    return ExitAction(position=pos, reason="time_stop")

            case "hybrid":
                if self._trailing_stop_hit(pos, current):
                    return ExitAction(position=pos, reason="trailing_stop")
                if self._take_profit_hit(pos, current):
                    return ExitAction(position=pos, reason="take_profit")
                if self.config.time_stop_hours > 0 and self._time_stop_hit(pos):
                    return ExitAction(position=pos, reason="time_stop")

        return None

    def _in_min_hold(self, pos: Position) -> bool:
        if self.config.min_hold_minutes <= 0:
            return False
        elapsed = (utcnow() - pos.opened_at).total_seconds()
        return elapsed < self.config.min_hold_minutes * 60

    def _stop_loss_pct(self, pos: Position) -> float:
        ac = self.config.asset_class(pos.symbol)
        if pos.side == "long":
            return {"crypto": self.config.stop_loss_pct_long_crypto,
                    "equity": self.config.stop_loss_pct_long_equity,
                    "commodity": self.config.stop_loss_pct_long_commodity}.get(ac, self.config.stop_loss_pct_long)
        return {"crypto": self.config.stop_loss_pct_short_crypto,
                "equity": self.config.stop_loss_pct_short_equity,
                "commodity": self.config.stop_loss_pct_short_commodity}.get(ac, self.config.stop_loss_pct_short)

    def _trailing_stop_pct(self, pos: Position) -> float:
        ac = self.config.asset_class(pos.symbol)
        return {"crypto": self.config.trailing_stop_pct_crypto,
                "equity": self.config.trailing_stop_pct_equity,
                "commodity": self.config.trailing_stop_pct_commodity}.get(ac, self.config.trailing_stop_pct)

    def _take_profit_pct(self, pos: Position) -> float:
        ac = self.config.asset_class(pos.symbol)
        return {"crypto": self.config.take_profit_pct_crypto,
                "equity": self.config.take_profit_pct_equity,
                "commodity": self.config.take_profit_pct_commodity}.get(ac, self.config.take_profit_pct)

    def _stop_loss_hit(self, pos: Position, current: float) -> bool:
        sl_pct = self._stop_loss_pct(pos)
        if sl_pct <= 0:
            return False
        sl = pos.stop_loss_price(sl_pct)
        if pos.side == "long":
            return current <= sl
        return current >= sl

    def _trailing_stop_hit(self, pos: Position, current: float) -> bool:
        tsl_pct = self._trailing_stop_pct(pos)
        if tsl_pct <= 0:
            return False

        # Only arm the trailing stop once the position is sufficiently in profit.
        # Hard stop remains active at all times regardless.
        if self.config.trailing_activation_pct > 0:
            pnl_pct = (
                (current - pos.entry_price) / pos.entry_price * 100
                if pos.side == "long"
                else (pos.entry_price - current) / pos.entry_price * 100
            )
            if pnl_pct < self.config.trailing_activation_pct:
                return False

        tsl = pos.trailing_stop_price(tsl_pct)
        if tsl is None:
            return False
        if pos.side == "long":
            return current <= tsl
        return current >= tsl

    def _take_profit_hit(self, pos: Position, current: float) -> bool:
        tp_pct = self._take_profit_pct(pos)
        if tp_pct <= 0:
            return False
        tp = pos.take_profit_price(tp_pct)
        if pos.side == "long":
            return current >= tp
        return current <= tp

    def _time_stop_hit(self, pos: Position) -> bool:
        if self.config.time_stop_hours <= 0:
            return False
        deadline = pos.opened_at + timedelta(hours=self.config.time_stop_hours)
        return utcnow() > deadline

    def update_high_water_mark(self, pos: Position) -> bool:
        """Update HWM if current price is better. Returns True if updated."""
        current = pos.current_price
        if current is None:
            return False
        hwm = pos.high_water_mark or pos.entry_price
        if pos.side == "long" and current > hwm:
            pos.high_water_mark = current
            return True
        if pos.side == "short" and current < hwm:
            pos.high_water_mark = current
            return True
        return False
