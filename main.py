"""Polymarket Limit Order Bot — Dashboard launcher.

Run:   python main.py
Open:  http://localhost:8897
"""

import asyncio
import signal
import sys

sys.path.insert(0, ".")

from config.settings import BotConfig
from core.strategy import LimitBotStrategy
from dashboard.server import DashboardServer
from utils.logger import setup_logger

log = setup_logger("main")


async def run():
    config = BotConfig()
    strategy = LimitBotStrategy(config=config)
    dashboard = DashboardServer(strategy=strategy, port=8897)
    strategy.dashboard = dashboard

    await dashboard.start()

    print("\n" + "=" * 50)
    print("  PolySignal — Otomatik Sniper")
    print("  Dashboard: http://localhost:8897")
    print("=" * 50)
    print("  Otomatik calisir — ¢75-80 + BTC>=$65 + son 120s")
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
