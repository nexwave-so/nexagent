from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexagent.config import Config
from nexagent.executor import Executor
from nexagent.models import NexwaveSignal, Position


def _config(**kwargs) -> Config:
    defaults = dict(
        paper_trading=True,
        exchange="hyperliquid",
        hyperliquid_wallet_address="0xabc",
        hyperliquid_private_key="0xdef",
        stop_loss_pct=3.0,
        take_profit_pct=5.0,
    )
    defaults.update(kwargs)
    return Config(**defaults)


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


def _position(**kwargs) -> Position:
    defaults = dict(
        symbol="BTC",
        side="long",
        size_usd=500.0,
        entry_price=100.0,
        current_price=105.0,
        high_water_mark=105.0,
        opened_at=datetime.now(timezone.utc),
        signal_id="sig-001",
    )
    defaults.update(kwargs)
    return Position(**defaults)


@pytest.mark.asyncio
async def test_paper_execute_returns_filled_order():
    config = _config(paper_trading=True)
    ex = Executor(config)

    with patch.object(ex, "_get_mid_price", AsyncMock(return_value=68000.0)):
        order = await ex.execute_signal(_signal(), 500.0)

    assert order is not None
    assert order.status == "filled"
    assert order.price == 68000.0
    assert order.exchange_order_id.startswith("paper-")
    assert order.order_type == "entry"


@pytest.mark.asyncio
async def test_paper_close_position():
    config = _config(paper_trading=True)
    ex = Executor(config)
    pos = _position(current_price=110.0)

    with patch.object(ex, "_get_mid_price", AsyncMock(return_value=110.0)):
        order = await ex.close_position(pos, "trailing_stop")

    assert order is not None
    assert order.status == "filled"
    assert order.side == "sell"  # closing long = sell
    assert order.order_type == "trailing_stop"


@pytest.mark.asyncio
async def test_paper_close_short_position():
    config = _config(paper_trading=True)
    ex = Executor(config)
    pos = _position(side="short")

    with patch.object(ex, "_get_mid_price", AsyncMock(return_value=95.0)):
        order = await ex.close_position(pos, "take_profit")

    assert order is not None
    assert order.side == "buy"  # closing short = buy


def test_calc_pnl_long():
    pnl = Executor._calc_pnl(_position(entry_price=100.0, size_usd=1000.0), 110.0)
    # 10 contracts * (110 - 100) = 100
    assert pnl == pytest.approx(100.0, rel=0.01)


def test_calc_pnl_short():
    pnl = Executor._calc_pnl(
        _position(side="short", entry_price=100.0, size_usd=1000.0), 90.0
    )
    # price dropped 10 → profit
    assert pnl == pytest.approx(100.0, rel=0.01)
