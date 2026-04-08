from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx

from .config import Config
from .models import NexwaveSignal, RegimeData
from .utils import utcnow

logger = logging.getLogger(__name__)


def _auth_headers(config: Config) -> dict[str, str]:
    if config.nexwave_api_key:
        return {"X-API-Key": config.nexwave_api_key}
    return {}  # x402: no auth header needed; server returns 402 to unauthenticated requests


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

        if resp.status_code == 401:
            www_auth = resp.headers.get("www-authenticate", "").lower()
            if config.use_x402 and "x402" in www_auth:
                # Server explicitly advertises x402 — try it
                return await _x402_fetch(client, config, resp)
            # Plain API-key 401 — surface a clear message
            logger.error(
                "Nexwave requires an API key (www-authenticate: %s). "
                "Get one at https://nexwave.so/dashboard?tab=API+Usage or set NEXWAVE_API_KEY in .env.",
                resp.headers.get("www-authenticate", ""),
            )
            return []

        resp.raise_for_status()
        return _parse_signals_response(resp.json())
    except httpx.HTTPStatusError as e:
        logger.error("Nexwave API error %s: %s", e.response.status_code, e.response.text[:200])
        return []
    except Exception as e:
        logger.error("Signal poll failed: %s", e)
        return []


def _parse_signals_response(data: Any) -> list[NexwaveSignal]:
    """
    Convert a Nexwave API response to NexwaveSignal objects.

    Handles both the Nexwave v2 nested format:
        { updatedAt, signals: { data: [{asset, type, venue, direction, ...}] } }
    and the flat legacy format:
        [ {symbol, signal_type, source, direction, ...} ]
    """
    if isinstance(data, dict):
        updated_at_str = data.get("updatedAt")
        try:
            ts = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00")) if updated_at_str else utcnow()
        except Exception:
            ts = utcnow()

        signals_section = data.get("signals", {})
        if isinstance(signals_section, dict):
            raw = signals_section.get("data", [])
        else:
            raw = signals_section if isinstance(signals_section, list) else []
    elif isinstance(data, list):
        raw = data
        ts = utcnow()
    else:
        return []

    results: list[NexwaveSignal] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        try:
            # Map Nexwave API field names → NexwaveSignal field names
            symbol = s.get("symbol") or s.get("asset", "")
            signal_type = s.get("signal_type") or s.get("type", "unknown")
            source = s.get("source") or s.get("venue", "nexwave")
            confidence = float(s.get("confidence", 0.0))
            strength = float(s.get("strength", confidence))
            direction = s.get("direction", "long")
            key = f"{symbol}:{signal_type}:{direction}:{ts.isoformat()}"
            sig_id = s.get("id") or hashlib.sha1(key.encode()).hexdigest()[:16]
            results.append(NexwaveSignal(
                id=sig_id,
                symbol=symbol,
                signal_type=signal_type,
                direction=direction,  # type: ignore[arg-type]
                strength=strength,
                confidence=confidence,
                z_score=s.get("z_score"),
                source=source,
                exit_signal=s.get("exit_signal", False),
                timestamp=ts,
            ))
        except Exception as e:
            logger.warning("Failed to parse signal: %s — %s", s, e)
    return results


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
            headers={**_auth_headers(config), "PAYMENT-SIGNATURE": payment_header},
            timeout=15.0,
        )
        resp.raise_for_status()
        return _parse_signals_response(resp.json())
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
