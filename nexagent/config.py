from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_COMMODITY_ASSETS: frozenset[str] = frozenset({
    "BRENTOIL", "WTIOIL", "NATGAS", "GOLD", "SILVER",
    "COPPER", "WHEAT", "CORN", "SOYBEAN",
})


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
    max_long_positions: int = 0         # 0 = no per-direction cap
    max_short_positions: int = 0        # 0 = no per-direction cap
    max_daily_trades: int = 20          # 0 = unlimited
    cooldown_seconds: int = 120
    min_signal_strength: float = 0.7
    min_signal_confidence: float = 0.6

    # Exits
    exit_mode: Literal["signal", "trailing_stop", "time", "hybrid"] = "hybrid"
    stop_loss_pct_long: float = 1.5     # hard stop for long positions
    stop_loss_pct_short: float = 1.5    # hard stop for short positions
    trailing_stop_pct: float = 0.8      # fallback trailing stop (commodity/equity default)
    take_profit_pct: float = 0.5        # fallback TP (commodity/equity default)
    time_stop_hours: float = 0.5        # 30-min hard kill: scalp thesis is dead if not paid in 30 min
    min_hold_minutes: float = 3.0       # skip trailing/TP exits for first 3 min (noise zone)

    # Per-asset-class exit overrides
    stop_loss_pct_long_crypto: float = 1.5
    stop_loss_pct_short_crypto: float = 1.5
    trailing_stop_pct_crypto: float = 1.5
    take_profit_pct_crypto: float = 3.0     # crypto needs bigger move to clear higher fees
    stop_loss_pct_long_equity: float = 1.5
    stop_loss_pct_short_equity: float = 1.5
    trailing_stop_pct_equity: float = 0.8
    take_profit_pct_equity: float = 0.5
    stop_loss_pct_long_commodity: float = 1.5
    stop_loss_pct_short_commodity: float = 1.5
    trailing_stop_pct_commodity: float = 0.8
    take_profit_pct_commodity: float = 0.5
    trailing_activation_pct: float = 1.0   # trailing stop only arms once position is this % in profit (0 = always active)

    # Signal filters
    allowed_signal_types: str = "funding_rate,oi_divergence,volume_anomaly"
    allowed_assets: str = ""
    blocked_assets: str = "FARTCOIN,PENGU"
    block_crypto: bool = False          # block plain symbols (no venue prefix = crypto perp)

    # Risk — per-asset-class position limits (0 = no limit) and circuit breakers
    max_crypto_positions: int = 2
    max_equity_positions: int = 2
    max_commodity_positions: int = 2
    max_consecutive_losses: int = 6          # pause after N consecutive losses (0 = disabled)
    loss_cooldown_seconds: int = 900         # extra cooldown per asset class after a loss
    crypto_long_strength_boost: float = 0.10 # additional min-strength required for crypto longs

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

    def asset_class(self, symbol: str) -> str:
        """Classify a symbol as crypto, equity, or commodity."""
        if ":" not in symbol:
            return "crypto"
        asset_part = symbol.upper().split(":", 1)[1]
        if asset_part in _COMMODITY_ASSETS:
            return "commodity"
        return "equity"

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
