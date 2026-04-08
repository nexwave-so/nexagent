import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexagent.config import Config
from nexagent.models import NexwaveSignal
from nexagent.signals import poll_signals


def _config(**kwargs) -> Config:
    defaults = dict(
        nexwave_api_key="nxw_test",
        nexwave_signals_url="https://nexwave.so/api/v1/signals",
        paper_trading=True,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def _raw_signal() -> dict:
    return {
        "id": "sig-001",
        "symbol": "BTC",
        "signal_type": "funding_rate",
        "direction": "long",
        "strength": 0.82,
        "confidence": 0.75,
        "z_score": 2.1,
        "source": "hyperliquid",
        "exit_signal": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@pytest.mark.asyncio
async def test_poll_returns_signals():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"signals": [_raw_signal()]}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    config = _config()
    signals = await poll_signals(mock_client, config)

    assert len(signals) == 1
    assert signals[0].symbol == "BTC"
    assert signals[0].strength == 0.82


@pytest.mark.asyncio
async def test_poll_returns_empty_on_error():
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))

    config = _config()
    signals = await poll_signals(mock_client, config)
    assert signals == []


@pytest.mark.asyncio
async def test_poll_skips_malformed_signals():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "signals": [
            _raw_signal(),
            {"id": "bad", "symbol": "ETH"},  # missing required fields
        ]
    }
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    signals = await poll_signals(mock_client, _config())
    assert len(signals) == 1


@pytest.mark.asyncio
async def test_auth_header_set():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"signals": []}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    await poll_signals(mock_client, _config(nexwave_api_key="nxw_mykey"))
    call_kwargs = mock_client.get.call_args
    assert call_kwargs[1]["headers"]["X-API-Key"] == "nxw_mykey"
