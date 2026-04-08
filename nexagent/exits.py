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

        # Hard stop-loss — always active regardless of exit mode
        if self._stop_loss_hit(pos, current):
            logger.info(
                "Stop-loss triggered for %s @ %.4f (entry=%.4f)",
                pos.symbol, current, pos.entry_price,
            )
            return ExitAction(position=pos, reason="stop_loss")

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
                if self.config.take_profit_pct > 0 and self._take_profit_hit(pos, current):
                    return ExitAction(position=pos, reason="take_profit")
                if self.config.time_stop_hours > 0 and self._time_stop_hit(pos):
                    return ExitAction(position=pos, reason="time_stop")

        return None

    def _stop_loss_hit(self, pos: Position, current: float) -> bool:
        if self.config.stop_loss_pct <= 0:
            return False
        sl = pos.stop_loss_price(self.config.stop_loss_pct)
        if pos.side == "long":
            return current <= sl
        return current >= sl

    def _trailing_stop_hit(self, pos: Position, current: float) -> bool:
        if self.config.trailing_stop_pct <= 0:
            return False
        tsl = pos.trailing_stop_price(self.config.trailing_stop_pct)
        if tsl is None:
            return False
        if pos.side == "long":
            return current <= tsl
        return current >= tsl

    def _take_profit_hit(self, pos: Position, current: float) -> bool:
        tp = pos.take_profit_price(self.config.take_profit_pct)
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
