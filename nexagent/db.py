from __future__ import annotations

import aiosqlite

from .models import NexwaveSignal, Order, Position
from .utils import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id          TEXT PRIMARY KEY,
    symbol      TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    direction   TEXT NOT NULL,
    strength    REAL NOT NULL,
    confidence  REAL NOT NULL,
    z_score     REAL,
    source      TEXT NOT NULL,
    exit_signal INTEGER DEFAULT 0,
    acted_on    INTEGER DEFAULT 0,
    skip_reason TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    size_usd          REAL NOT NULL,
    order_type        TEXT NOT NULL,
    price             REAL,
    exchange_order_id TEXT,
    status            TEXT NOT NULL,
    signal_id         TEXT REFERENCES signals(id),
    created_at        TEXT NOT NULL,
    filled_at         TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    symbol          TEXT PRIMARY KEY,
    side            TEXT NOT NULL,
    size_usd        REAL NOT NULL,
    entry_price     REAL NOT NULL,
    high_water_mark REAL,
    opened_at       TEXT NOT NULL,
    signal_id       TEXT,
    order_id        TEXT REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    date        TEXT PRIMARY KEY,
    realized    REAL DEFAULT 0,
    unrealized  REAL DEFAULT 0,
    trades      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
CREATE INDEX IF NOT EXISTS idx_orders_type ON orders(order_type);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self._db.close()

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database not connected"
        return self._db

    # ── Signals ──────────────────────────────────────────────────────────────

    async def save_signal(
        self, signal: NexwaveSignal, acted_on: bool = False, skip_reason: str | None = None
    ) -> None:
        await self.db.execute(
            """INSERT OR IGNORE INTO signals
               (id, symbol, signal_type, direction, strength, confidence,
                z_score, source, exit_signal, acted_on, skip_reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal.id,
                signal.symbol,
                signal.signal_type,
                signal.direction,
                signal.strength,
                signal.confidence,
                signal.z_score,
                signal.source,
                int(signal.exit_signal),
                int(acted_on),
                skip_reason,
                signal.timestamp.isoformat(),
            ),
        )
        await self.db.commit()

    async def get_recent_signals(self, limit: int = 50) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def signal_seen(self, symbol: str, signal_type: str, direction: str) -> bool:
        """Check if a (symbol, type, direction) combo was seen in the last hour."""
        async with self.db.execute(
            """SELECT 1 FROM signals
               WHERE symbol=? AND signal_type=? AND direction=?
               AND created_at > datetime('now', '-1 hour')
               LIMIT 1""",
            (symbol, signal_type, direction),
        ) as cur:
            return await cur.fetchone() is not None

    # ── Orders ────────────────────────────────────────────────────────────────

    async def save_order(self, order: Order) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO orders
               (id, symbol, side, size_usd, order_type, price, exchange_order_id,
                status, signal_id, created_at, filled_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                order.id,
                order.symbol,
                order.side,
                order.size_usd,
                order.order_type,
                order.price,
                order.exchange_order_id,
                order.status,
                order.signal_id,
                order.created_at.isoformat(),
                order.filled_at.isoformat() if order.filled_at else None,
            ),
        )
        await self.db.commit()

    async def get_recent_orders(self, limit: int = 50) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Positions ─────────────────────────────────────────────────────────────

    async def save_position(self, pos: Position) -> None:
        await self.db.execute(
            """INSERT OR REPLACE INTO positions
               (symbol, side, size_usd, entry_price, high_water_mark, opened_at, signal_id, order_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                pos.symbol,
                pos.side,
                pos.size_usd,
                pos.entry_price,
                pos.high_water_mark,
                pos.opened_at.isoformat(),
                pos.signal_id,
                pos.order_id,
            ),
        )
        await self.db.commit()

    async def delete_position(self, symbol: str) -> None:
        await self.db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
        await self.db.commit()

    async def get_all_positions(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM positions") as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Daily PnL ─────────────────────────────────────────────────────────────

    async def add_realized_pnl(self, pnl_usd: float) -> None:
        today = utcnow().strftime("%Y-%m-%d")
        await self.db.execute(
            """INSERT INTO daily_pnl(date, realized, trades) VALUES(?,?,1)
               ON CONFLICT(date) DO UPDATE SET
               realized = realized + excluded.realized,
               trades = trades + 1""",
            (today, pnl_usd),
        )
        await self.db.commit()

    async def get_today_pnl(self) -> dict:
        today = utcnow().strftime("%Y-%m-%d")
        async with self.db.execute(
            "SELECT * FROM daily_pnl WHERE date=?", (today,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
            return {"date": today, "realized": 0.0, "unrealized": 0.0, "trades": 0}

    async def get_trades_today(self) -> int:
        today = utcnow().strftime("%Y-%m-%d")
        async with self.db.execute(
            "SELECT COALESCE(trades, 0) FROM daily_pnl WHERE date=?", (today,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0
