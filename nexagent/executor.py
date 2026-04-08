from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

import ccxt.async_support as ccxt
import httpx

from .config import Config
from .models import NexwaveSignal, Order, Position

logger = logging.getLogger(__name__)

_DEFAULT_LEVERAGE = 3


class Executor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.exchange: ccxt.Exchange | None = None
        self._portfolio_usd: float = 10_000.0  # updated on sync

    async def connect(self) -> None:
        exchange_id = self.config.exchange.lower()
        if exchange_id == "hyperliquid":
            self.exchange = ccxt.hyperliquid({
                "walletAddress": self.config.hyperliquid_wallet_address,
                "privateKey": self.config.hyperliquid_private_key,
                "options": {"defaultType": "swap"},
            })
        else:
            exchange_cls = getattr(ccxt, exchange_id, None)
            if exchange_cls is None:
                raise ValueError(f"Unknown exchange: {exchange_id}")
            self.exchange = exchange_cls()

        if not self.config.paper_trading:
            await self.exchange.load_markets()
            logger.info("Exchange connected: %s", exchange_id)

    async def close(self) -> None:
        if self.exchange:
            await self.exchange.close()

    async def get_portfolio_usd(self) -> float:
        if self.config.paper_trading:
            return self._portfolio_usd
        try:
            balance = await self.exchange.fetch_balance()
            usdc = balance.get("USDC", {}).get("free", 0) or balance.get("total", {}).get("USDC", 0)
            self._portfolio_usd = float(usdc)
            return self._portfolio_usd
        except Exception as e:
            logger.warning("Could not fetch balance: %s", e)
            return self._portfolio_usd

    @staticmethod
    def _to_ccxt_symbol(symbol: str) -> str:
        """Map Nexwave venue-prefixed symbols to CCXT market IDs.

        Nexwave format  →  CCXT Hyperliquid format
        xyz:CL          →  XYZ-CL/USDC:USDC
        vntl:WHEAT      →  VNTL-WHEAT/USDH:USDH
        AXS             →  AXS/USDC:USDC
        """
        if ":" in symbol:
            venue, asset = symbol.split(":", 1)
            venue_upper = venue.upper()
            asset_upper = asset.upper()
            if venue_upper == "VNTL":
                return f"VNTL-{asset_upper}/USDH:USDH"
            return f"{venue_upper}-{asset_upper}/USDC:USDC"
        return f"{symbol.upper()}/USDC:USDC"

    async def execute_signal(self, signal: NexwaveSignal, size_usd: float) -> Order | None:
        symbol_ccxt = self._to_ccxt_symbol(signal.symbol)
        side = "buy" if signal.direction == "long" else "sell"
        order_id = f"nex-{uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)

        if self.config.paper_trading:
            price = await self._get_mid_price(signal.symbol)
            logger.info(
                "[PAPER] Order placed: %s %s $%.2f @ %.4f",
                side.upper(), signal.symbol, size_usd, price or 0,
            )
            return Order(
                id=order_id,
                symbol=signal.symbol,
                side=side,  # type: ignore[arg-type]
                size_usd=size_usd,
                price=price,
                order_type="entry",
                exchange_order_id=f"paper-{uuid4().hex[:8]}",
                status="filled",
                signal_id=signal.id,
                created_at=now,
                filled_at=now,
            )

        for attempt in range(3):
            try:
                ticker = await self.exchange.fetch_ticker(symbol_ccxt)
                price = ticker["last"]
                size_contracts = size_usd / price

                order = await self.exchange.create_market_order(
                    symbol_ccxt, side, size_contracts, price
                )
                logger.info(
                    "Order placed: %s %s $%.2f @ %.4f (id=%s)",
                    side.upper(), signal.symbol, size_usd, price, order["id"],
                )
                return Order(
                    id=order_id,
                    symbol=signal.symbol,
                    side=side,  # type: ignore[arg-type]
                    size_usd=size_usd,
                    price=price,
                    order_type="entry",
                    exchange_order_id=str(order["id"]),
                    status="filled",
                    signal_id=signal.id,
                    created_at=now,
                    filled_at=now,
                )
            except ccxt.RateLimitExceeded:
                wait = 2 ** attempt
                logger.warning("Rate limit hit; retrying in %ds (attempt %d/3)", wait, attempt + 1)
                await asyncio.sleep(wait)
            except Exception as e:
                logger.error("Order failed (attempt %d/3): %s", attempt + 1, e, exc_info=True)
                if attempt == 2:
                    return None
                await asyncio.sleep(1)

        return None

    async def close_position(self, pos: Position, reason: str) -> Order | None:
        side = "sell" if pos.side == "long" else "buy"
        symbol_ccxt = self._to_ccxt_symbol(pos.symbol)
        order_id = f"nex-exit-{uuid4().hex[:10]}"
        now = datetime.now(timezone.utc)

        if self.config.paper_trading:
            price = await self._get_mid_price(pos.symbol)
            pnl = self._calc_pnl(pos, price or pos.entry_price)
            logger.info(
                "[PAPER] Exit %s %s @ %.4f reason=%s pnl=%.2f",
                pos.symbol, side.upper(), price or 0, reason, pnl,
            )
            return Order(
                id=order_id,
                symbol=pos.symbol,
                side=side,  # type: ignore[arg-type]
                size_usd=pos.size_usd,
                price=price,
                order_type=reason if reason in ("stop_loss", "take_profit", "time_stop") else "exit",  # type: ignore[arg-type]
                exchange_order_id=f"paper-exit-{uuid4().hex[:8]}",
                status="filled",
                signal_id=None,
                created_at=now,
                filled_at=now,
            )

        try:
            ticker = await self.exchange.fetch_ticker(symbol_ccxt)
            price = ticker["last"]

            # Fetch actual position size from exchange — avoids partial/over-close
            # if entry price moved since we opened.
            size_contracts: float | None = None
            try:
                exchange_positions = await self.exchange.fetch_positions([symbol_ccxt])
                for ep in exchange_positions:
                    if ep.get("contracts") and ep.get("contracts") != 0:
                        size_contracts = abs(float(ep["contracts"]))
                        break
            except Exception:
                pass
            if size_contracts is None:
                size_contracts = pos.size_usd / pos.entry_price
                logger.warning(
                    "Could not fetch live position size for %s — using estimate", pos.symbol
                )

            order = await self.exchange.create_market_order(
                symbol_ccxt, side, size_contracts, price, params={"reduceOnly": True}
            )
            pnl = self._calc_pnl(pos, price)
            logger.info(
                "Exit %s %s @ %.4f reason=%s pnl=%.2f",
                pos.symbol, side.upper(), price, reason, pnl,
            )
            return Order(
                id=order_id,
                symbol=pos.symbol,
                side=side,  # type: ignore[arg-type]
                size_usd=pos.size_usd,
                price=price,
                order_type=reason if reason in ("stop_loss", "take_profit", "time_stop") else "exit",  # type: ignore[arg-type]
                exchange_order_id=str(order["id"]),
                status="filled",
                signal_id=None,
                created_at=now,
                filled_at=now,
            )
        except Exception as e:
            logger.error("Close position failed for %s: %s", pos.symbol, e, exc_info=True)
            return None

    async def sync_positions(self, db_positions: list[Position]) -> list[Position]:
        """Update current_price and high_water_mark for all open positions."""
        if not db_positions:
            return []

        updated = []
        for pos in db_positions:
            price = await self._get_mid_price(pos.symbol)
            if price:
                pos.current_price = price
                pos.unrealized_pnl = self._calc_pnl(pos, price)
            updated.append(pos)
        return updated

    async def _get_mid_price(self, symbol: str) -> float | None:
        # Try CCXT first (works in both paper and live — paper skips load_markets
        # but fetch_ticker still works for Hyperliquid).
        if self.exchange is not None:
            try:
                ticker = await self.exchange.fetch_ticker(self._to_ccxt_symbol(symbol))
                return ticker["last"]
            except Exception:
                pass  # fall through to REST fallback

        # Direct Hyperliquid REST fallback (no exchange object or ticker failed)
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.hyperliquid.fi/info",
                    json={"type": "allMids"},
                    timeout=5.0,
                )
                mids = resp.json()
                if isinstance(mids, dict):
                    val = mids.get(symbol)
                    return float(val) if val else None
        except Exception:
            return None
        return None

    @staticmethod
    def _calc_pnl(pos: Position, current_price: float) -> float:
        size_contracts = pos.size_usd / pos.entry_price
        if pos.side == "long":
            return (current_price - pos.entry_price) * size_contracts
        return (pos.entry_price - current_price) * size_contracts
