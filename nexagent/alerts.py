from __future__ import annotations

import logging
from typing import Any

from .config import Config
from .models import Order, Position

logger = logging.getLogger(__name__)

_PAPER_BADGE = "[PAPER] " if True else ""


class TelegramAlert:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._bot: Any = None
        self._paper = config.paper_trading

        if config.telegram_bot_token and config.telegram_chat_id:
            try:
                from telegram import Bot
                self._bot = Bot(token=config.telegram_bot_token)
                logger.info("Telegram alerts enabled (chat_id=%s)", config.telegram_chat_id)
            except ImportError:
                logger.warning(
                    "python-telegram-bot not installed — alerts disabled. "
                    "Run: pip install nexagent[alerts]"
                )

    @property
    def enabled(self) -> bool:
        return self._bot is not None

    async def trade_opened(self, order: Order, position: Position) -> None:
        badge = "[PAPER] " if self._paper else ""
        msg = (
            f"{badge}*Trade Opened*\n"
            f"• Symbol: `{order.symbol}`\n"
            f"• Direction: `{position.side.upper()}`\n"
            f"• Size: `${order.size_usd:,.2f}`\n"
            f"• Entry: `{order.price:.4f}`"
        )
        await self._send(msg)

    async def trade_closed(self, position: Position, pnl_usd: float, reason: str) -> None:
        badge = "[PAPER] " if self._paper else ""
        sign = "+" if pnl_usd >= 0 else ""
        msg = (
            f"{badge}*Trade Closed*\n"
            f"• Symbol: `{position.symbol}`\n"
            f"• Reason: `{reason}`\n"
            f"• PnL: `{sign}${pnl_usd:,.2f}`"
        )
        await self._send(msg)

    async def agent_paused(self, reason: str) -> None:
        await self._send(f"⚠️ *Agent Paused*\nReason: `{reason}`")

    async def agent_resumed(self) -> None:
        await self._send("✅ *Agent Resumed*")

    async def error(self, message: str) -> None:
        await self._send(f"🚨 *Agent Error*\n`{message}`")

    async def daily_summary(self, pnl_usd: float, win_rate: float, open_positions: int) -> None:
        badge = "[PAPER] " if self._paper else ""
        sign = "+" if pnl_usd >= 0 else ""
        msg = (
            f"{badge}*Daily Summary*\n"
            f"• PnL: `{sign}${pnl_usd:,.2f}`\n"
            f"• Win Rate: `{win_rate:.1%}`\n"
            f"• Open Positions: `{open_positions}`"
        )
        await self._send(msg)

    async def _send(self, text: str) -> None:
        if not self._bot:
            return
        try:
            await self._bot.send_message(
                chat_id=self.config.telegram_chat_id,
                text=text,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.debug("Telegram alert failed (non-critical): %s", e)
