from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import box

from .config import Config
from .utils import fmt_usd, fmt_pct, setup_logging, mask_key

app = typer.Typer(
    name="nex",
    help="Nexagent — autonomous trading agent powered by Nexwave signals.",
    add_completion=False,
)
console = Console()


def _get_config() -> Config:
    return Config()


def _run(coro):
    return asyncio.run(coro)


# ── start ─────────────────────────────────────────────────────────────────────

@app.command()
def start(
    daemon: bool = typer.Option(False, "--daemon", "-d", help="Run in background (PID → .nexagent.pid)"),
):
    """Start the agent (foreground). Ctrl+C to stop."""
    from .agent import Agent
    config = _get_config()
    setup_logging(config.log_level)

    if daemon:
        _start_daemon()
        return

    async def _run_agent():
        agent = Agent(config)
        await agent.startup()

        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        def _handle_signal():
            stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

        task_signals = asyncio.create_task(agent.signal_loop())
        task_exits = asyncio.create_task(agent.exit_loop())

        console.print(f"[bold green]Nexagent started[/] — mode={'[yellow]PAPER[/]' if config.paper_trading else '[red]LIVE[/]'} | exit_mode={config.exit_mode}")
        console.print("Press Ctrl+C to stop\n")

        try:
            await stop_event.wait()
        finally:
            task_signals.cancel()
            task_exits.cancel()
            await asyncio.gather(task_signals, task_exits, return_exceptions=True)
            await agent.shutdown()
            console.print("\n[bold]Agent stopped.[/]")

    _run(_run_agent())


def _start_daemon():
    pid = os.fork()
    if pid > 0:
        Path(".nexagent.pid").write_text(str(pid))
        console.print(f"[green]Agent started in background (PID {pid})[/]")
        console.print("Use [bold]nex stop[/] to stop, [bold]nex status[/] to check.")
        sys.exit(0)
    # Child process
    os.setsid()
    from .agent import Agent
    config = _get_config()
    setup_logging(config.log_level)
    asyncio.run(_daemon_loop(Agent(config)))


async def _daemon_loop(agent):
    await agent.startup()
    t1 = asyncio.create_task(agent.signal_loop())
    t2 = asyncio.create_task(agent.exit_loop())
    await asyncio.gather(t1, t2)


# ── stop ──────────────────────────────────────────────────────────────────────

@app.command()
def stop():
    """Stop the background agent process."""
    pid_file = Path(".nexagent.pid")
    if not pid_file.exists():
        console.print("[yellow]No .nexagent.pid found — is the agent running?[/]")
        raise typer.Exit(1)
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, signal.SIGTERM)
        pid_file.unlink()
        console.print(f"[green]Sent SIGTERM to PID {pid}[/]")
    except ProcessLookupError:
        pid_file.unlink()
        console.print("[yellow]Process not found — removed stale PID file.[/]")


# ── status ────────────────────────────────────────────────────────────────────

@app.command()
def status():
    """Current agent state: positions, PnL, health."""
    try:
        import httpx
        config = _get_config()
        resp = httpx.get(f"http://{config.api_bind}:{config.api_port}/status", timeout=5.0)
        data = resp.json()
    except Exception as e:
        console.print(f"[red]Cannot reach agent API: {e}[/]\nIs the agent running?")
        raise typer.Exit(1)

    mode = "[yellow]PAPER[/]" if data["paper_trading"] else "[red]LIVE[/]"
    paused = " · [red]PAUSED[/]" if data["paused"] else ""
    uptime_m = int(data["uptime_seconds"] // 60)
    uptime_h = uptime_m // 60

    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    table.add_column(style="bold cyan", width=22)
    table.add_column()

    table.add_row("Open Positions", f"{data['open_positions']} / {data.get('max_open_positions', '?')}")
    table.add_row("Daily PnL", fmt_usd(data["daily_pnl_usd"]))
    table.add_row("Daily Loss Limit", fmt_usd(data["daily_loss_limit_usd"]))
    table.add_row("Signals Today", f"{data['signals_today']} received · {data['trades_today']} acted on")
    if data.get("last_signal_at"):
        table.add_row("Last Signal", data["last_signal_at"])
    if data.get("last_trade_at"):
        table.add_row("Last Trade", data["last_trade_at"])
    table.add_row("Nexwave", f"● {data['nexwave_status']}")
    table.add_row("Hyperliquid", f"● {data['exchange_status']}")

    console.print(f"\n[bold]Nexagent v0.1  ·  {mode}  ·  {data['exit_mode']} exits  ·  up {uptime_h}h {uptime_m % 60}m{paused}[/]")
    console.print(table)
    console.print()


# ── signals ───────────────────────────────────────────────────────────────────

@app.command()
def signals():
    """Last 20 signals with acted_on and skip_reason."""
    try:
        import httpx
        config = _get_config()
        resp = httpx.get(f"http://{config.api_bind}:{config.api_port}/signals", timeout=5.0)
        rows = resp.json()[:20]
    except Exception as e:
        console.print(f"[red]Cannot reach agent API: {e}[/]")
        raise typer.Exit(1)

    table = Table(box=box.SIMPLE_HEAD)
    table.add_column("Time", style="dim")
    table.add_column("Symbol")
    table.add_column("Type")
    table.add_column("Dir")
    table.add_column("Str")
    table.add_column("Acted")
    table.add_column("Skip Reason", style="dim")

    for r in rows:
        acted = "[green]✓[/]" if r["acted_on"] else "[dim]✗[/]"
        table.add_row(
            r["created_at"][:19],
            r["symbol"],
            r["signal_type"],
            r["direction"],
            f"{r['strength']:.2f}",
            acted,
            r.get("skip_reason") or "",
        )
    console.print(table)


# ── trades ────────────────────────────────────────────────────────────────────

@app.command()
def trades():
    """Last 20 executed orders."""
    try:
        import httpx
        config = _get_config()
        resp = httpx.get(f"http://{config.api_bind}:{config.api_port}/trades", timeout=5.0)
        rows = resp.json()[:20]
    except Exception as e:
        console.print(f"[red]Cannot reach agent API: {e}[/]")
        raise typer.Exit(1)

    table = Table(box=box.SIMPLE_HEAD)
    table.add_column("Time", style="dim")
    table.add_column("Symbol")
    table.add_column("Side")
    table.add_column("Type")
    table.add_column("Size")
    table.add_column("Price")
    table.add_column("Status")

    for r in rows:
        table.add_row(
            r["created_at"][:19],
            r["symbol"],
            r["side"],
            r["order_type"],
            f"${r['size_usd']:,.2f}",
            f"{r['price']:.4f}" if r.get("price") else "-",
            r["status"],
        )
    console.print(table)


# ── positions ─────────────────────────────────────────────────────────────────

@app.command()
def positions():
    """Open positions with unrealized PnL and exit levels."""
    try:
        import httpx
        config = _get_config()
        resp = httpx.get(f"http://{config.api_bind}:{config.api_port}/positions", timeout=5.0)
        rows = resp.json()
    except Exception as e:
        console.print(f"[red]Cannot reach agent API: {e}[/]")
        raise typer.Exit(1)

    if not rows:
        console.print("[dim]No open positions.[/]")
        return

    table = Table(box=box.ROUNDED)
    table.add_column("Symbol")
    table.add_column("Side")
    table.add_column("Size")
    table.add_column("Entry")
    table.add_column("Current")
    table.add_column("PnL")
    table.add_column("SL")
    table.add_column("TP")

    for r in rows:
        pnl = r.get("unrealized_pnl") or 0
        pnl_str = f"[green]{fmt_usd(pnl)}[/]" if pnl >= 0 else f"[red]{fmt_usd(pnl)}[/]"
        table.add_row(
            r["symbol"],
            r["side"].upper(),
            f"${r['size_usd']:,.2f}",
            f"{r['entry_price']:.4f}",
            f"{r['current_price']:.4f}" if r.get("current_price") else "-",
            pnl_str,
            f"{r['stop_loss']:.4f}" if r.get("stop_loss") else "-",
            f"{r['take_profit']:.4f}" if r.get("take_profit") else "-",
        )
    console.print(table)


# ── pause / resume ────────────────────────────────────────────────────────────

@app.command()
def pause():
    """Pause trading (holds open positions)."""
    import httpx
    config = _get_config()
    httpx.post(f"http://{config.api_bind}:{config.api_port}/pause", timeout=5.0)
    console.print("[yellow]Agent paused.[/]")


@app.command()
def resume():
    """Resume trading after pause."""
    import httpx
    config = _get_config()
    httpx.post(f"http://{config.api_bind}:{config.api_port}/resume", timeout=5.0)
    console.print("[green]Agent resumed.[/]")


# ── config ────────────────────────────────────────────────────────────────────

@app.command("config")
def show_config():
    """Print resolved config (secrets masked)."""
    config = _get_config()
    console.print(repr(config))
    console.print(f"\nSignal URL: {config.nexwave_signals_url}")
    console.print(f"Exchange:   {config.exchange}")
    console.print(f"Paper:      {config.paper_trading}")
    console.print(f"Exit mode:  {config.exit_mode}")
    console.print(f"Max pos:    ${config.max_position_usd:,.2f}")
    console.print(f"Risk %:     {config.risk_per_trade_pct}%")
    console.print(f"Daily limit: ${config.daily_loss_limit_usd:,.2f}")
    console.print(f"API key:    {mask_key(config.nexwave_api_key)}")
    console.print(f"HL wallet:  {config.hyperliquid_wallet_address or '(not set)'}")


# ── close ─────────────────────────────────────────────────────────────────────

@app.command()
def close(symbol: str = typer.Argument(..., help="Symbol to close, e.g. BTC")):
    """Market-close a specific position."""
    import httpx
    config = _get_config()
    resp = httpx.post(f"http://{config.api_bind}:{config.api_port}/close/{symbol.upper()}", timeout=10.0)
    if resp.status_code == 404:
        console.print(f"[red]No open position for {symbol}[/]")
    else:
        console.print(f"[green]Closed {symbol}[/]")


@app.command("close-all")
def close_all():
    """Market-close all positions (emergency)."""
    confirm = typer.confirm("Close ALL open positions?", abort=True)
    import httpx
    config = _get_config()
    resp = httpx.post(f"http://{config.api_bind}:{config.api_port}/close-all", timeout=30.0)
    data = resp.json()
    console.print(f"[green]Closed: {', '.join(data.get('closed', []))}[/]")


# ── init ──────────────────────────────────────────────────────────────────────

@app.command()
def init():
    """Interactive setup wizard — writes .env and verifies connectivity."""
    console.print("[bold]Nexagent Setup Wizard[/]\n")

    env_path = Path(".env")
    if env_path.exists():
        overwrite = typer.confirm(".env already exists. Overwrite?", default=False)
        if not overwrite:
            raise typer.Exit()

    wallet = typer.prompt("Hyperliquid wallet address (0x...)")
    private_key = typer.prompt("Hyperliquid private key (0x...)", hide_input=True)
    api_key = typer.prompt("Nexwave API key (nxw_...)", default="")
    paper = typer.confirm("Start in paper trading mode?", default=True)
    max_pos = typer.prompt("Max position size USD", default="500")
    daily_limit = typer.prompt("Daily loss limit USD", default="200")
    exit_mode = typer.prompt("Exit mode (signal/trailing_stop/time/hybrid)", default="hybrid")
    tg_token = typer.prompt("Telegram bot token (optional, Enter to skip)", default="")
    tg_chat = typer.prompt("Telegram chat ID (optional, Enter to skip)", default="") if tg_token else ""

    lines = [
        f"HYPERLIQUID_WALLET_ADDRESS={wallet}",
        f"HYPERLIQUID_PRIVATE_KEY={private_key}",
        f"NEXWAVE_API_KEY={api_key}",
        f"PAPER_TRADING={'true' if paper else 'false'}",
        f"MAX_POSITION_USD={max_pos}",
        f"DAILY_LOSS_LIMIT_USD={daily_limit}",
        f"EXIT_MODE={exit_mode}",
    ]
    if tg_token:
        lines.append(f"TELEGRAM_BOT_TOKEN={tg_token}")
    if tg_chat:
        lines.append(f"TELEGRAM_CHAT_ID={tg_chat}")

    env_path.write_text("\n".join(lines) + "\n")
    console.print(f"\n[green]✓ .env written[/]")
    console.print("Run [bold]nex start[/] to begin trading.")


if __name__ == "__main__":
    app()
