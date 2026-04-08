from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .agent import Agent
from .config import Config
from .utils import setup_logging

logger = logging.getLogger(__name__)

_agent: Agent | None = None
_start_time = time.time()
_config: Config | None = None

bearer_scheme = HTTPBearer(auto_error=False)


def get_agent() -> Agent:
    assert _agent is not None, "Agent not initialized"
    return _agent


def require_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    if not _config or not _config.api_key:
        return  # Auth not configured → open
    if credentials is None or credentials.credentials != _config.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _config
    config = Config()
    _config = config
    setup_logging(config.log_level)
    _agent = Agent(config)
    await _agent.startup()

    task_signals = asyncio.create_task(_agent.signal_loop())
    task_exits = asyncio.create_task(_agent.exit_loop())
    logger.info("Agent loops started")

    yield

    task_signals.cancel()
    task_exits.cancel()
    try:
        await asyncio.gather(task_signals, task_exits, return_exceptions=True)
    except Exception:
        pass
    await _agent.shutdown()
    logger.info("Agent shutdown complete")


app = FastAPI(title="Nexagent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "uptime": round(time.time() - _start_time)}


@app.get("/status", dependencies=[Depends(require_auth)])
async def status_endpoint():
    return (await get_agent().get_status()).model_dump()


@app.get("/signals", dependencies=[Depends(require_auth)])
async def signals_endpoint():
    return await get_agent().db.get_recent_signals(50)


@app.get("/trades", dependencies=[Depends(require_auth)])
async def trades_endpoint():
    return await get_agent().db.get_recent_orders(50)


@app.get("/positions", dependencies=[Depends(require_auth)])
async def positions_endpoint():
    agent = get_agent()
    positions = await agent.load_positions()
    positions = await agent.executor.sync_positions(positions)
    return [
        {
            "symbol": p.symbol,
            "side": p.side,
            "size_usd": p.size_usd,
            "entry_price": p.entry_price,
            "current_price": p.current_price,
            "unrealized_pnl": p.unrealized_pnl,
            "stop_loss": p.stop_loss_price(agent.config.stop_loss_pct),
            "take_profit": p.take_profit_price(agent.config.take_profit_pct) if agent.config.take_profit_pct > 0 else None,
            "opened_at": p.opened_at.isoformat(),
        }
        for p in positions
    ]


@app.post("/pause", dependencies=[Depends(require_auth)])
async def pause_endpoint():
    await get_agent().pause("api_request")
    return {"paused": True}


@app.post("/resume", dependencies=[Depends(require_auth)])
async def resume_endpoint():
    await get_agent().resume()
    return {"paused": False}


@app.post("/close/{symbol}", dependencies=[Depends(require_auth)])
async def close_symbol(symbol: str):
    agent = get_agent()
    positions = await agent.load_positions()
    for pos in positions:
        if pos.symbol.upper() == symbol.upper():
            await agent._execute_exit(pos, reason="manual")
            return {"closed": symbol}
    raise HTTPException(status_code=404, detail=f"No open position for {symbol}")


@app.post("/close-all", dependencies=[Depends(require_auth)])
async def close_all():
    agent = get_agent()
    positions = await agent.load_positions()
    closed = []
    for pos in positions:
        await agent._execute_exit(pos, reason="manual")
        closed.append(pos.symbol)
    return {"closed": closed}
