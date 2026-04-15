from datetime import datetime, timezone

import pytest

from nexagent.config import Config
from nexagent.models import AgentStatus, NexwaveSignal, RegimeData
from nexagent.risk import RiskManager


def _signal(**kwargs) -> NexwaveSignal:
    defaults = dict(
        id="sig-001",
        symbol="BTC",
        signal_type="funding_rate",
        direction="long",
        strength=0.8,
        confidence=0.75,
        source="hyperliquid",
        exit_signal=False,
        timestamp=datetime.now(timezone.utc),
    )
    defaults.update(kwargs)
    return NexwaveSignal(**defaults)


def _status(**kwargs) -> AgentStatus:
    defaults = dict(
        running=True,
        paper_trading=True,
        exit_mode="hybrid",
        open_positions=0,
        open_long_positions=0,
        open_short_positions=0,
        daily_pnl_usd=0.0,
        daily_loss_limit_usd=200.0,
        paused=False,
        signals_today=0,
        trades_today=0,
        uptime_seconds=60.0,
        nexwave_status="connected",
        exchange_status="connected",
    )
    defaults.update(kwargs)
    return AgentStatus(**defaults)


def _config(**kwargs) -> Config:
    defaults = dict(
        hyperliquid_wallet_address="0xabc",
        hyperliquid_private_key="0xdef",
        paper_trading=True,
        max_position_usd=500.0,
        risk_per_trade_pct=1.0,
        daily_loss_limit_usd=200.0,
        max_open_positions=5,
        cooldown_seconds=300,
        min_signal_strength=0.7,
        min_signal_confidence=0.6,
        blocked_assets="FARTCOIN,PENGU",
        allowed_assets="",
        allowed_signal_types="funding_rate,oi_divergence,volume_anomaly",
    )
    defaults.update(kwargs)
    return Config(**defaults)


def test_passes_valid_signal():
    rm = RiskManager(_config())
    ok, reason = rm.check(_signal(), _status())
    assert ok
    assert reason == ""


def test_blocks_paused():
    rm = RiskManager(_config())
    ok, reason = rm.check(_signal(), _status(paused=True, paused_reason="manual"))
    assert not ok
    assert "paused" in reason


def test_blocks_daily_loss_limit():
    rm = RiskManager(_config())
    ok, reason = rm.check(_signal(), _status(daily_pnl_usd=-250.0))
    assert not ok
    assert reason == "daily_loss_limit_hit"


def test_blocks_max_positions():
    rm = RiskManager(_config(max_open_positions=3))
    ok, reason = rm.check(_signal(), _status(open_positions=3))
    assert not ok
    assert reason == "max_positions_reached"


def test_blocks_low_strength():
    rm = RiskManager(_config(min_signal_strength=0.8))
    ok, reason = rm.check(_signal(strength=0.6), _status())
    assert not ok
    assert "strength_below_threshold" in reason


def test_blocks_low_confidence():
    rm = RiskManager(_config(min_signal_confidence=0.8))
    ok, reason = rm.check(_signal(confidence=0.5), _status())
    assert not ok
    assert "confidence_below_threshold" in reason


def test_blocks_blocked_asset():
    rm = RiskManager(_config())
    ok, reason = rm.check(_signal(symbol="FARTCOIN"), _status())
    assert not ok
    assert reason == "asset_blocked"


def test_blocks_allowlist_miss():
    rm = RiskManager(_config(allowed_assets="BTC,ETH"))
    ok, reason = rm.check(_signal(symbol="SOL"), _status())
    assert not ok
    assert reason == "asset_not_in_allowlist"


def test_allows_allowlist_hit():
    rm = RiskManager(_config(allowed_assets="BTC,ETH"))
    ok, reason = rm.check(_signal(symbol="BTC"), _status())
    assert ok


def test_cooldown():
    rm = RiskManager(_config(cooldown_seconds=300))
    rm.record_trade("BTC")
    ok, reason = rm.check(_signal(symbol="BTC"), _status())
    assert not ok
    assert reason == "cooldown_active"


def test_blocks_max_daily_trades():
    rm = RiskManager(_config(max_daily_trades=5))
    ok, reason = rm.check(_signal(), _status(trades_today=5))
    assert not ok
    assert reason == "max_daily_trades_reached"


def test_unlimited_daily_trades():
    rm = RiskManager(_config(max_daily_trades=0))
    ok, _ = rm.check(_signal(), _status(trades_today=9999))
    assert ok


def test_regime_risk_off():
    rm = RiskManager(_config())
    rm.update_regime(RegimeData(state="risk_off", confidence=0.9))
    ok, reason = rm.check(_signal(), _status())
    assert not ok
    assert reason == "regime_risk_off"


def test_position_size_regime_scaling():
    sig = _signal(strength=1.0, confidence=1.0)  # conviction=1.0 to isolate regime scaling
    rm = RiskManager(_config(risk_per_trade_pct=1.0, max_position_usd=1000))
    rm.update_regime(RegimeData(state="ranging", confidence=0.8))
    size = rm.position_size_usd(10_000, sig)
    assert size == 50.0  # 10000 * 1% * 0.5 * 1.0

    rm.update_regime(RegimeData(state="high_volatility", confidence=0.8))
    size = rm.position_size_usd(10_000, sig)
    assert size == 25.0  # 10000 * 1% * 0.25 * 1.0

    rm.update_regime(RegimeData(state="trending_bull", confidence=0.8))
    size = rm.position_size_usd(10_000, sig)
    assert size == 100.0  # 10000 * 1% * 1.0 * 1.0


def test_position_size_conviction_scaling():
    sig = _signal(strength=0.8, confidence=0.5)  # conviction = 0.40
    rm = RiskManager(_config(risk_per_trade_pct=1.0, max_position_usd=1000))
    rm.update_regime(RegimeData(state="trending_bull", confidence=0.9))
    size = rm.position_size_usd(10_000, sig)
    assert size == pytest.approx(40.0)  # 10000 * 1% * 1.0 * 0.40


def test_blocks_max_long_positions():
    rm = RiskManager(_config(max_long_positions=2))
    ok, reason = rm.check(_signal(direction="long"), _status(open_long_positions=2))
    assert not ok
    assert reason == "max_long_positions_reached"


def test_allows_short_when_long_cap_hit():
    rm = RiskManager(_config(max_long_positions=2, max_short_positions=5))
    ok, _ = rm.check(_signal(direction="short"), _status(open_long_positions=2, open_short_positions=1))
    assert ok


def test_blocks_max_short_positions():
    rm = RiskManager(_config(max_short_positions=1))
    ok, reason = rm.check(_signal(direction="short"), _status(open_short_positions=1))
    assert not ok
    assert reason == "max_short_positions_reached"


def test_blocks_crypto():
    rm = RiskManager(_config(block_crypto=True))
    ok, reason = rm.check(_signal(symbol="BTC"), _status())
    assert not ok
    assert reason == "crypto_blocked"


def test_allows_venue_prefixed_when_crypto_blocked():
    rm = RiskManager(_config(block_crypto=True))
    ok, _ = rm.check(_signal(symbol="xyz:CL"), _status())
    assert ok


def test_crypto_block_disabled_by_default():
    rm = RiskManager(_config())
    ok, _ = rm.check(_signal(symbol="BTC"), _status())
    assert ok
