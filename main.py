"""PolySignal dashboard launcher.

Run:   ./.venv/bin/python main.py
Open:  http://localhost:<DASHBOARD_PORT>
"""

import asyncio
import signal
import subprocess
import sys

sys.path.insert(0, ".")

from config.settings import BotConfig, __version__
from core.strategy import LimitBotStrategy
from dashboard.server import DashboardServer
from utils.logger import setup_logger

log = setup_logger("main")


def _port_owner(port: int) -> str:
    """Best-effort lookup for the process currently listening on a port."""
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return ""

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return ""
    return lines[1]


async def run():
    config = BotConfig()
    strategy = LimitBotStrategy(config=config)
    dashboard = DashboardServer(
        strategy=strategy,
        host=config.dashboard_host,
        port=config.dashboard_port,
    )
    strategy.dashboard = dashboard

    try:
        await dashboard.start()
    except OSError as exc:
        if exc.errno in {48, 98}:
            owner = _port_owner(config.dashboard_port)
            detail = f" Port sahibi: {owner}" if owner else ""
            msg = (
                f"Dashboard portu {config.dashboard_port} zaten dolu.{detail} "
                f".env icinde DASHBOARD_PORT'u benzersiz bir degere cekin "
                "ve nginx proxy_pass ayarini ayni portla esleyin."
            )
            log.error(msg)
            raise SystemExit(msg) from exc
        raise

    print("\n" + "=" * 50)
    print(f"  PolySignal v{__version__} — Asymmetric Jackpot")
    print(f"  Dashboard: http://localhost:{config.dashboard_port}")
    print("=" * 50)
    print("  IZLE → GUCLU YON ALIM → UCUZ TARAF BIRIKTIR → JACKPOT")
    print("=" * 50 + "\n")

    # Bot acilir acilmaz otomatik basla
    await strategy.auto_start()

    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    await strategy._disconnect()


if __name__ == "__main__":
    asyncio.run(run())
