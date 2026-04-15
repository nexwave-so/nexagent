from datetime import datetime, timedelta, timezone

import pytest

from nexagent.config import Config
from nexagent.exits import ExitManager
from nexagent.models import Position


def _pos(**kwargs) -> Position:
    defaults = dict(
        symbol="BTC",
        side="long",
        size_usd=500.0,
        entry_price=100.0,
        current_price=100.0,
        high_water_mark=100.0,
        opened_at=datetime.now(timezone.utc),
        signal_id="sig-001",
    )
    defaults.update(kwargs)
    return Position(**defaults)


def _config(**kwargs) -> Config:
    defaults = dict(
        paper_trading=True,
        exit_mode="hybrid",
        stop_loss_pct=3.0,
        trailing_stop_pct=2.0,
        take_profit_pct=5.0,
        time_stop_hours=72.0,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def test_stop_loss_long():
    em = ExitManager(_config())
    pos = _pos(current_price=96.5)  # 3.5% below entry
    actions = em.check_exits([pos])
    assert len(actions) == 1
    assert actions[0].reason == "stop_loss"


def test_stop_loss_short():
    em = ExitManager(_config())
    pos = _pos(side="short", current_price=104.0)  # 4% above entry → stop
    actions = em.check_exits([pos])
    assert len(actions) == 1
    assert actions[0].reason == "stop_loss"


def test_no_stop_loss_when_fine():
    em = ExitManager(_config())
    pos = _pos(current_price=100.5)
    actions = em.check_exits([pos])
    assert not any(a.reason == "stop_loss" for a in actions)


def test_trailing_stop_long():
    em = ExitManager(_config(exit_mode="trailing_stop"))
    pos = _pos(current_price=107.0, high_water_mark=110.0)
    # 110 * (1 - 0.02) = 107.8 → price 107 < 107.8 → trigger
    actions = em.check_exits([pos])
    assert len(actions) == 1
    assert actions[0].reason == "trailing_stop"


def test_trailing_stop_not_triggered():
    em = ExitManager(_config(exit_mode="trailing_stop"))
    pos = _pos(current_price=109.0, high_water_mark=110.0)
    # 110 * (1 - 0.02) = 107.8 → price 109 > 107.8 → no trigger
    actions = em.check_exits([pos])
    assert not actions


def test_take_profit_hybrid():
    em = ExitManager(_config(exit_mode="hybrid"))
    pos = _pos(current_price=106.0)  # 6% > entry → TP at 5%
    actions = em.check_exits([pos])
    assert any(a.reason == "take_profit" for a in actions)


def test_time_stop():
    em = ExitManager(_config(exit_mode="time", time_stop_hours=1.0))
    pos = _pos(opened_at=datetime.now(timezone.utc) - timedelta(hours=2))
    actions = em.check_exits([pos])
    assert any(a.reason == "time_stop" for a in actions)


def test_signal_mode_no_exits():
    em = ExitManager(_config(exit_mode="signal"))
    pos = _pos(current_price=90.0)  # Would trigger trailing/TP but mode=signal
    # Only stop-loss should fire (always active)
    actions = em.check_exits([pos])
    # Price 90 = 10% drop, stop_loss_pct=3 → stop at 97 → triggers
    assert len(actions) == 1
    assert actions[0].reason == "stop_loss"


def test_update_high_water_mark():
    em = ExitManager(_config())
    pos = _pos(current_price=115.0, high_water_mark=110.0)
    updated = em.update_high_water_mark(pos)
    assert updated
    assert pos.high_water_mark == 115.0


def test_hwm_not_updated_below():
    em = ExitManager(_config())
    pos = _pos(current_price=108.0, high_water_mark=110.0)
    updated = em.update_high_water_mark(pos)
    assert not updated
    assert pos.high_water_mark == 110.0
