from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import httpx

from .alerts import TelegramAlert
from .config import Config
from .db import Database
from .executor import Executor
from .exits import ExitManager
from .models import AgentStatus, ExitAction, NexwaveSignal, Position, RegimeData
from .risk import RiskManager
from .signals import fetch_regime, poll_signals
from .utils import utcnow

logger = logging.getLogger(__name__)


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.executor = Executor(config)
        self.risk = RiskManager(config)
        self.exit_manager = ExitManager(config)
        self.alerts = TelegramAlert(config)

        self._started_at = utcnow()
        self._paused = False
        self._paused_reason: str | None = None
        self._last_signal_at: datetime | None = None
        self._last_trade_at: datetime | None = None
        self._signals_today = 0
        self._trades_today = 0
        self._counter_date: str = utcnow().strftime("%Y-%m-%d")
        self._nexwave_status: str = "down"
        self._exchange_status: str = "down"
        self._regime_fetched_at: datetime | None = None
        self._running = False

    async def startup(self) -> None:
        logger.info("Nexagent starting up (paper=%s, exit_mode=%s)", self.config.paper_trading, self.config.exit_mode)
        await self.db.connect()
        await self.executor.connect()
        self._running = True
        self._exchange_status = "connected"
        await self._sync_exchange_positions()
        portfolio = await self.executor.get_portfolio_usd()
        if not self.config.paper_trading and portfolio == 0:
            logger.warning(
                "Hyperliquid balance is $0 — deposit USDC to your account before live trading. "
                "Wallet: %s", self.config.hyperliquid_wallet_address
            )
        logger.info(
            "Startup complete — mode=%s portfolio=$%.0f",
            "PAPER" if self.config.paper_trading else "LIVE",
            portfolio,
        )

    async def shutdown(self) -> None:
        self._running = False
        positions = await self.load_positions()
        logger.info(
            "Shutting down — open positions: %d, signals today: %d, trades today: %d",
            len(positions), self._signals_today, self._trades_today,
        )
        await self.executor.close()
        await self.db.close()

    # ── Main loops ────────────────────────────────────────────────────────────

    async def signal_loop(self) -> None:
        """Poll Nexwave for signals on an interval."""
        async with httpx.AsyncClient() as client:
            while self._running:
                try:
                    await self._poll_and_act(client)
                    await self._maybe_refresh_regime(client)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Signal loop error: %s", e, exc_info=True)
                await asyncio.sleep(self.config.nexwave_poll_interval)

    async def exit_loop(self) -> None:
        """Monitor open positions for exit conditions every 10s."""
        while self._running:
            try:
                await self._check_exits()
                await self._update_daily_loss_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Exit loop error: %s", e, exc_info=True)
            await asyncio.sleep(10)

    # ── Signal processing ─────────────────────────────────────────────────────

    async def _poll_and_act(self, client: httpx.AsyncClient) -> None:
        self._maybe_reset_daily_counters()
        signals = await poll_signals(client, self.config)

        if signals:
            self._nexwave_status = "connected"
            self._last_signal_at = utcnow()
            self._signals_today += len(signals)
        else:
            # Distinguish empty (no signals right now) from failure
            self._nexwave_status = "connected"

        status = await self._build_status()
        open_pos_symbols = {p.symbol for p in await self.load_positions()}

        for signal in signals:
            await self._process_signal(signal, status, open_pos_symbols)

    async def _process_signal(
        self, signal: NexwaveSignal, status: AgentStatus, open_pos_symbols: set[str]
    ) -> None:
        # Deduplicate (same symbol/type/direction in last hour)
        if await self.db.signal_seen(signal.symbol, signal.signal_type, signal.direction):
            await self.db.save_signal(signal, acted_on=False, skip_reason="duplicate_1h")
            return

        # Exit signal → route to exit handler
        if signal.exit_signal:
            await self._handle_exit_signal(signal)
            return

        # Risk check
        ok, reason = self.risk.check(signal, status)
        if not ok:
            logger.debug("Signal skipped: %s %s reason=%s", signal.symbol, signal.signal_type, reason)
            await self.db.save_signal(signal, acted_on=False, skip_reason=reason)
            return

        # Conflict: signal opposes existing position → close first
        if signal.symbol in open_pos_symbols:
            positions = await self.load_positions()
            for pos in positions:
                if pos.symbol == signal.symbol:
                    if pos.side != signal.direction:
                        logger.info("Conflicting position detected for %s — closing first", signal.symbol)
                        await self._execute_exit(pos, reason="reversal")

        # Size and execute
        _MIN_NOTIONAL = 11.0  # Hyperliquid rejects orders below $10 notional
        portfolio_usd = await self.executor.get_portfolio_usd()
        size_usd = self.risk.position_size_usd(portfolio_usd)
        if size_usd <= 0:
            await self.db.save_signal(signal, acted_on=False, skip_reason="size_zero_regime")
            return
        if size_usd < _MIN_NOTIONAL:
            await self.db.save_signal(signal, acted_on=False, skip_reason=f"size_below_min_notional ({size_usd:.2f})")
            return

        order = await self.executor.execute_signal(signal, size_usd)
        if order is None:
            await self.db.save_signal(signal, acted_on=False, skip_reason="execution_failed")
            return

        await self.db.save_signal(signal, acted_on=True)
        await self.db.save_order(order)

        position = Position(
            symbol=signal.symbol,
            side=signal.direction,
            size_usd=size_usd,
            entry_price=order.price or 0.0,
            high_water_mark=order.price,
            opened_at=utcnow(),
            signal_id=signal.id,
            order_id=order.id,
        )
        await self.db.save_position(position)

        self.risk.record_trade(signal.symbol)
        self._last_trade_at = utcnow()
        self._trades_today += 1

        await self.alerts.trade_opened(order, position)
        logger.info(
            "Trade opened: %s %s $%.2f signal=%s",
            signal.symbol, signal.direction.upper(), size_usd, signal.signal_type,
        )

    async def _handle_exit_signal(self, signal: NexwaveSignal) -> None:
        positions = await self.load_positions()
        for pos in positions:
            if pos.symbol == signal.symbol:
                await self._execute_exit(pos, reason="signal")
                await self.db.save_signal(signal, acted_on=True)
                return
        await self.db.save_signal(signal, acted_on=False, skip_reason="no_open_position")

    # ── Exit handling ─────────────────────────────────────────────────────────

    async def _check_exits(self) -> None:
        positions = await self.load_positions()
        if not positions:
            return

        positions = await self.executor.sync_positions(positions)

        # Update high water marks
        for pos in positions:
            if self.exit_manager.update_high_water_mark(pos):
                await self.db.save_position(pos)

        actions = self.exit_manager.check_exits(positions)
        for action in actions:
            await self._execute_exit(action.position, action.reason)

    async def _execute_exit(self, pos: Position, reason: str) -> None:
        order = await self.executor.close_position(pos, reason)
        if order is None:
            logger.error("Failed to close position %s", pos.symbol)
            return

        await self.db.save_order(order)
        await self.db.delete_position(pos.symbol)

        pnl = pos.unrealized_pnl or 0.0
        await self.db.add_realized_pnl(pnl)

        self._last_trade_at = utcnow()
        self._trades_today += 1

        await self.alerts.trade_closed(pos, pnl, reason)
        logger.info(
            "Position closed: %s reason=%s pnl=%.2f",
            pos.symbol, reason, pnl,
        )

    # ── Daily loss check ──────────────────────────────────────────────────────

    async def _update_daily_loss_check(self) -> None:
        pnl_data = await self.db.get_today_pnl()
        daily_realized = pnl_data.get("realized", 0.0)
        if daily_realized < -self.config.daily_loss_limit_usd and not self._paused:
            self._paused = True
            self._paused_reason = "daily_loss_limit_hit"
            logger.warning("Daily loss limit hit ($%.2f) — agent paused", daily_realized)
            await self.alerts.agent_paused("daily_loss_limit_hit")

    # ── Regime refresh ────────────────────────────────────────────────────────

    async def _maybe_refresh_regime(self, client: httpx.AsyncClient) -> None:
        if self._regime_fetched_at is None or (
            (utcnow() - self._regime_fetched_at).total_seconds() > 4 * 3600
        ):
            regime = await fetch_regime(client, self.config)
            if regime:
                self.risk.update_regime(regime)
                self._regime_fetched_at = utcnow()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _maybe_reset_daily_counters(self) -> None:
        today = utcnow().strftime("%Y-%m-%d")
        if today != self._counter_date:
            self._counter_date = today
            self._signals_today = 0
            self._trades_today = 0

    async def _sync_exchange_positions(self) -> None:
        """Cold-start recovery: fetch live exchange positions and add any missing from DB."""
        if self.config.paper_trading or self.executor.exchange is None:
            return
        try:
            exchange_positions = await self.executor.exchange.fetch_positions()
            db_symbols = {p.symbol for p in await self.load_positions()}
            recovered = 0
            for ep in exchange_positions:
                contracts = ep.get("contracts") or 0
                if float(contracts) == 0:
                    continue
                symbol = ep.get("info", {}).get("coin") or ep["symbol"].split("/")[0]
                if symbol in db_symbols:
                    continue
                side = "long" if ep.get("side") == "long" else "short"
                entry_price = float(ep.get("entryPrice") or ep.get("info", {}).get("entryPx") or 0)
                notional = float(ep.get("notional") or 0)
                size_usd = notional if notional > 0 else abs(float(contracts)) * entry_price
                pos = Position(
                    symbol=symbol,
                    side=side,  # type: ignore[arg-type]
                    size_usd=size_usd,
                    entry_price=entry_price,
                    high_water_mark=entry_price,
                    opened_at=utcnow(),
                    signal_id="recovered",
                )
                await self.db.save_position(pos)
                recovered += 1
            if recovered:
                logger.info("Cold-start recovery: reconciled %d exchange position(s)", recovered)
        except Exception as e:
            logger.warning("Startup position sync failed (non-critical): %s", e)

    async def load_positions(self) -> list[Position]:
        rows = await self.db.get_all_positions()
        positions = []
        for r in rows:
            try:
                positions.append(Position(
                    symbol=r["symbol"],
                    side=r["side"],
                    size_usd=r["size_usd"],
                    entry_price=r["entry_price"],
                    high_water_mark=r.get("high_water_mark"),
                    opened_at=datetime.fromisoformat(r["opened_at"]),
                    signal_id=r.get("signal_id") or "",
                    order_id=r.get("order_id"),
                ))
            except Exception as e:
                logger.warning("Could not deserialize position %s: %s", r.get("symbol"), e)
        return positions

    async def _build_status(self) -> AgentStatus:
        pnl_data = await self.db.get_today_pnl()
        positions = await self.load_positions()
        uptime = (utcnow() - self._started_at).total_seconds()
        return AgentStatus(
            running=self._running,
            paper_trading=self.config.paper_trading,
            exit_mode=self.config.exit_mode,
            open_positions=len(positions),
            daily_pnl_usd=pnl_data.get("realized", 0.0),
            daily_loss_limit_usd=self.config.daily_loss_limit_usd,
            paused=self._paused,
            paused_reason=self._paused_reason,
            last_signal_at=self._last_signal_at,
            last_trade_at=self._last_trade_at,
            signals_today=self._signals_today,
            trades_today=self._trades_today,
            uptime_seconds=uptime,
            nexwave_status=self._nexwave_status,  # type: ignore[arg-type]
            exchange_status=self._exchange_status,  # type: ignore[arg-type]
        )

    async def get_status(self) -> AgentStatus:
        return await self._build_status()

    async def pause(self, reason: str = "manual") -> None:
        self._paused = True
        self._paused_reason = reason
        await self.alerts.agent_paused(reason)

    async def resume(self) -> None:
        self._paused = False
        self._paused_reason = None
        await self.alerts.agent_resumed()
