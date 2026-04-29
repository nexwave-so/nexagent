from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .utils import utcnow


class NexwaveSignal(BaseModel):
    id: str
    symbol: str
    signal_type: str  # funding_rate | oi_divergence | volume_anomaly
    direction: Literal["long", "short"]
    strength: float  # 0.0–1.0
    confidence: float  # 0.0–1.0
    z_score: float | None = None
    source: str  # hydromancer | hyperliquid
    exit_signal: bool = False
    timestamp: datetime


class Order(BaseModel):
    id: str
    symbol: str
    side: Literal["buy", "sell"]
    size_usd: float
    price: float | None = None
    order_type: Literal["entry", "exit", "stop_loss", "take_profit", "time_stop", "manual"]
    exchange_order_id: str | None = None
    status: Literal["pending", "filled", "failed", "cancelled"]
    signal_id: str | None = None
    created_at: datetime
    filled_at: datetime | None = None


class Position(BaseModel):
    symbol: str
    side: Literal["long", "short"]
    size_usd: float
    entry_price: float
    current_price: float | None = None
    unrealized_pnl: float | None = None
    high_water_mark: float | None = None
    opened_at: datetime
    signal_id: str
    order_id: str | None = None

    def stop_loss_price(self, stop_loss_pct: float) -> float:
        if self.side == "long":
            return self.entry_price * (1 - stop_loss_pct / 100)
        return self.entry_price * (1 + stop_loss_pct / 100)

    def take_profit_price(self, take_profit_pct: float) -> float:
        if self.side == "long":
            return self.entry_price * (1 + take_profit_pct / 100)
        return self.entry_price * (1 - take_profit_pct / 100)

    def trailing_stop_price(self, trailing_stop_pct: float) -> float | None:
        hwm = self.high_water_mark or self.entry_price
        if self.side == "long":
            return hwm * (1 - trailing_stop_pct / 100)
        return hwm * (1 + trailing_stop_pct / 100)


class ExitAction(BaseModel):
    position: Position
    reason: Literal["stop_loss", "trailing_stop", "take_profit", "time_stop", "signal", "reversal", "manual"]


class AgentStatus(BaseModel):
    running: bool
    paper_trading: bool
    exit_mode: str
    open_positions: int
    open_long_positions: int = 0
    open_short_positions: int = 0
    open_crypto_positions: int = 0
    open_equity_positions: int = 0
    open_commodity_positions: int = 0
    consecutive_losses: int = 0
    daily_pnl_usd: float
    daily_loss_limit_usd: float
    paused: bool
    paused_reason: str | None = None
    last_signal_at: datetime | None = None
    last_trade_at: datetime | None = None
    signals_today: int
    trades_today: int
    uptime_seconds: float
    nexwave_status: Literal["connected", "degraded", "down"]
    exchange_status: Literal["connected", "degraded", "down"]


class RegimeData(BaseModel):
    state: str
    confidence: float
    breadth: float | None = None
    avg_return: float | None = None
    funding_skew: float | None = None
    vol_dispersion: float | None = None
    fetched_at: datetime = Field(default_factory=utcnow)
