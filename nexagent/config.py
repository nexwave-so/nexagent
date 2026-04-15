from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Nexwave connection
    nexwave_signals_url: str = "https://nexwave.so/api/signals"
    nexwave_regime_url: str = ""
    nexwave_poll_interval: int = 30
    nexwave_x402_wallet: str = ""
    nexwave_x402_private_key: str = ""

    # Exchange
    exchange: str = "hyperliquid"
    hyperliquid_wallet_address: str = ""
    hyperliquid_private_key: str = ""
    paper_trading: bool = True

    # Risk
    max_position_usd: float = 500.0
    risk_per_trade_pct: float = 1.0
    daily_loss_limit_usd: float = 200.0
    max_open_positions: int = 5
    max_daily_trades: int = 20          # 0 = unlimited
    cooldown_seconds: int = 300
    min_signal_strength: float = 0.7
    min_signal_confidence: float = 0.6

    # Exits
    exit_mode: Literal["signal", "trailing_stop", "time", "hybrid"] = "hybrid"
    stop_loss_pct: float = 3.0
    trailing_stop_pct: float = 2.0
    take_profit_pct: float = 5.0
    time_stop_hours: float = 72.0

    # Signal filters
    allowed_signal_types: str = "funding_rate,oi_divergence,volume_anomaly"
    allowed_assets: str = ""
    blocked_assets: str = "FARTCOIN,PENGU"

    # Alerts
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Agent — api_port reads API_PORT or PORT (Render injects PORT)
    log_level: str = "INFO"
    db_path: str = "./nexagent.db"
    api_port: int = Field(
        default=7070,
        validation_alias=AliasChoices("API_PORT", "PORT"),
    )
    api_bind: str = "127.0.0.1"
    api_key: str = ""

    @property
    def allowed_signal_types_set(self) -> set[str]:
        return {t.strip() for t in self.allowed_signal_types.split(",") if t.strip()}

    @property
    def allowed_assets_set(self) -> set[str]:
        return {a.strip().upper() for a in self.allowed_assets.split(",") if a.strip()}

    @property
    def blocked_assets_set(self) -> set[str]:
        return {a.strip().upper() for a in self.blocked_assets.split(",") if a.strip()}

    def __repr__(self) -> str:
        masked_pk = "***" if self.hyperliquid_private_key else "(not set)"
        masked_x402 = "***" if self.nexwave_x402_private_key else "(not set)"
        return (
            f"Config(paper_trading={self.paper_trading}, exchange={self.exchange}, "
            f"exit_mode={self.exit_mode}, hl_private_key={masked_pk}, x402_key={masked_x402})"
        )
