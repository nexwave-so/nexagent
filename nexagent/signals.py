from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

from .config import Config
from .models import NexwaveSignal, RegimeData
from .utils import utcnow

logger = logging.getLogger(__name__)


def _auth_headers(config: Config) -> dict[str, str]:
    if config.nexwave_api_key:
        return {"X-API-Key": config.nexwave_api_key}
    return {}


async def poll_signals(client: httpx.AsyncClient, config: Config) -> list[NexwaveSignal]:
    """Fetch current signals from Nexwave REST endpoint."""
    try:
        resp = await client.get(
            config.nexwave_signals_url,
            headers=_auth_headers(config),
            timeout=15.0,
        )

        if resp.status_code == 402 and config.use_x402:
            return await _x402_fetch(client, config, resp)

        resp.raise_for_status()
        data = resp.json()
        raw_signals = data.get("signals", data) if isinstance(data, dict) else data
        signals = []
        for s in raw_signals:
            try:
                signals.append(NexwaveSignal(**s))
            except Exception as e:
                logger.warning("Failed to parse signal: %s — %s", s, e)
        return signals
    except httpx.HTTPStatusError as e:
        logger.error("Nexwave API error %s: %s", e.response.status_code, e.response.text[:200])
        return []
    except Exception as e:
        logger.error("Signal poll failed: %s", e)
        return []


async def _x402_fetch(
    client: httpx.AsyncClient, config: Config, payment_required_resp: httpx.Response
) -> list[NexwaveSignal]:
    """Handle x402 pay-per-signal flow (Solana USDC micro-payment)."""
    try:
        from .x402 import sign_and_pay  # optional dep
    except ImportError:
        logger.error("x402 auth configured but 'solders' package not installed. "
                     "Run: pip install nexagent[x402]")
        return []

    try:
        payment_header = await sign_and_pay(payment_required_resp, config)
        resp = await client.get(
            config.nexwave_signals_url,
            headers={**_auth_headers(config), "X-Payment": payment_header},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        raw_signals = data.get("signals", data) if isinstance(data, dict) else data
        return [NexwaveSignal(**s) for s in raw_signals]
    except Exception as e:
        logger.error("x402 payment flow failed: %s", e)
        return []


async def fetch_regime(client: httpx.AsyncClient, config: Config) -> RegimeData | None:
    """Fetch current market regime from Nexwave."""
    try:
        resp = await client.get(
            config.nexwave_regime_url,
            headers=_auth_headers(config),
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return RegimeData(
            state=data.get("state", "ranging"),
            confidence=data.get("confidence", 0.5),
            breadth=data.get("breadth"),
            avg_return=data.get("avg_return"),
            funding_skew=data.get("funding_skew"),
            vol_dispersion=data.get("vol_dispersion"),
        )
    except Exception as e:
        logger.warning("Regime fetch failed: %s", e)
        return None


async def stream_signals(
    client: httpx.AsyncClient, config: Config
) -> AsyncIterator[NexwaveSignal]:
    """SSE signal stream (v0.2+). URL must end in /stream."""
    sse_url = config.nexwave_signals_url.rstrip("/") + "/stream"
    async with client.stream("GET", sse_url, headers=_auth_headers(config)) as resp:
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                import json
                yield NexwaveSignal(**json.loads(raw))
            except Exception as e:
                logger.warning("Failed to parse SSE signal: %s", e)
