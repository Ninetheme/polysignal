"""Market scanner: fetches Price to Beat from Polymarket page + API.

PTB source priority:
1. Scrape polymarket.com/event/{slug} → __NEXT_DATA__ → past-results → last closePrice
2. Gamma Events API → eventMetadata.priceToBeat (available after resolution)
3. Binance BTC price as fallback
"""

import asyncio
import json
import re
import time
from typing import Optional

import requests

from utils.logger import setup_logger

log = setup_logger("scanner")

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"


def scrape_ptb_from_page(slug: str) -> float:
    """Polymarket sayfasindan Price to Beat cek.

    Sayfa __NEXT_DATA__ icindeki series events'ten en yakin
    zaman damgasina sahip marketteki priceToBeat'i alir.

    Aktif market henuz listede olmayabilir — bir onceki pencerenin
    PTB'si alinir (bu = aktif marketin sayfada gosterilen PTB).
    """
    try:
        slug_ts = int(slug.split("-")[-1])
    except (ValueError, IndexError):
        return 0.0

    try:
        url = f"https://polymarket.com/event/{slug}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return 0.0

        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            r.text,
            re.DOTALL,
        )
        if not m:
            return 0.0

        data = json.loads(m.group(1))
        queries = data.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])

        # Series events'ten tum PTB degerlerini topla
        candidates = []  # [(timestamp, ptb), ...]

        for q in queries:
            state_data = q.get("state", {}).get("data", {})
            if not isinstance(state_data, dict):
                continue

            events = state_data.get("events", [])
            if not isinstance(events, list):
                continue

            for ev in events:
                ticker = ev.get("ticker", "")
                if not ticker.startswith("btc-updown-5m-"):
                    continue
                meta = ev.get("eventMetadata")
                if not meta:
                    continue
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        continue
                ptb = float(meta.get("priceToBeat", 0))
                if ptb <= 1000:
                    continue

                try:
                    ev_ts = int(ticker.split("-")[-1])
                except (ValueError, IndexError):
                    continue

                candidates.append((ev_ts, ptb, ticker))

        if not candidates:
            log.warning("PTB sayfa: hic series PTB bulunamadi")
            return 0.0

        # En yakin zaman damgasina sahip PTB'yi sec
        # Aktif market (slug_ts) ile eslesen veya bir onceki
        candidates.sort(key=lambda x: abs(x[0] - slug_ts))
        best_ts, best_ptb, best_ticker = candidates[0]
        diff = slug_ts - best_ts

        log.info(
            "PTB sayfa: $%.2f (%s, fark:%ds)",
            best_ptb, best_ticker[-10:], diff,
        )
        return best_ptb

    except Exception as e:
        log.error("Page scrape error: %s", e)
        return 0.0


def fetch_ptb_from_api(slug: str) -> float:
    """Fetch PTB from Gamma Events API."""
    try:
        # Yontem 1: Events API — eventMetadata.priceToBeat
        r = requests.get(GAMMA_EVENTS, params={"slug": slug}, timeout=5)
        if r.status_code == 200 and r.json():
            meta = r.json()[0].get("eventMetadata")
            if meta:
                if isinstance(meta, str):
                    meta = json.loads(meta)
                ptb = float(meta.get("priceToBeat", 0))
                if ptb > 1000:
                    return ptb
    except Exception:
        pass

    try:
        # Yontem 2: Markets API → events[0] icindeki series'den PTB
        r = requests.get(GAMMA_MARKETS, params={"slug": slug}, timeout=5)
        if r.status_code == 200 and r.json():
            d = r.json()[0]
            events = d.get("events", [])
            if isinstance(events, list):
                for ev in events:
                    if ev.get("ticker") == slug or ev.get("slug") == slug:
                        meta = ev.get("eventMetadata")
                        if meta:
                            if isinstance(meta, str):
                                meta = json.loads(meta)
                            ptb = float(meta.get("priceToBeat", 0))
                            if ptb > 1000:
                                log.info("PTB markets/events: $%.2f", ptb)
                                return ptb
    except Exception:
        pass

    try:
        # Yontem 3: Onceki pencerenin kapanisi = bu pencerenin PTB'si
        slug_ts = int(slug.split("-")[-1])
        prev_ts = slug_ts - 300
        prev_slug = f"btc-updown-5m-{prev_ts}"
        r = requests.get(GAMMA_EVENTS, params={"slug": prev_slug}, timeout=5)
        if r.status_code == 200 and r.json():
            meta = r.json()[0].get("eventMetadata")
            if meta:
                if isinstance(meta, str):
                    meta = json.loads(meta)
                close = float(meta.get("closePrice", 0))
                if close > 1000:
                    log.info("PTB onceki kapanis (%s): $%.2f", prev_slug[-10:], close)
                    return close
    except Exception:
        pass

    return 0.0


class MarketScanner:
    """Scans for active BTC 5M markets and resolves their Price to Beat."""

    def __init__(self):
        self.current_slug: str = ""
        self.price_to_beat: float = 0.0
        self.market_info: dict = {}

    async def find_active_market_with_ptb(self, timeout_sec: int = 60) -> dict:
        """Find the current active market and get its PTB.

        Uses page scrape as primary source (available immediately),
        falls back to API polling.
        """
        start = time.time()

        while time.time() - start < timeout_sec:
            now = int(time.time())
            current_window = now - (now % 300)
            elapsed = now - current_window
            slug = f"btc-updown-5m-{current_window}"

            # Get market data (tokens, prices)
            info = self._fetch_market(current_window)
            if not info:
                log.info("No active market at %s, retrying...", slug)
                await asyncio.sleep(3)
                continue

            # Try to get PTB — Events API is the authoritative source
            ptb = 0.0

            # Method 1: Events API (exact Chainlink value, available ~30-120s after window opens)
            ptb = fetch_ptb_from_api(slug)
            if ptb > 0:
                log.info("PTB Events API (kesin): $%.2f", ptb)

            # Method 2: Page scrape (may return approximate value from past-results)
            if ptb <= 0:
                ptb = scrape_ptb_from_page(slug)
                if ptb > 0:
                    log.info("PTB sayfa scrape: $%.2f", ptb)

            if ptb > 0:
                info["price_to_beat"] = ptb
                self.market_info = info
                self.price_to_beat = ptb
                self.current_slug = slug
                log.info(
                    "Market ready: %s | PTB=$%.2f | elapsed=%ds in window",
                    info["question"], ptb, elapsed,
                )
                return info

            log.info(
                "Waiting for PTB on %s (elapsed %ds, attempt %d)...",
                slug[-10:], elapsed, int((time.time() - start) / 3) + 1,
            )
            await asyncio.sleep(5)

        # Timeout — return without PTB
        if info:
            log.warning("PTB timeout after %ds", timeout_sec)
            self.market_info = info
            return info

        return {}

    def _fetch_market(self, window_ts: int) -> Optional[dict]:
        """Fetch market info for a specific window timestamp."""
        slug = f"btc-updown-5m-{window_ts}"
        try:
            r = requests.get(GAMMA_MARKETS, params={"slug": slug}, timeout=5)
            if r.status_code != 200 or not r.json():
                return None

            d = r.json()[0]
            if not d.get("acceptingOrders", False):
                return None

            outcomes = json.loads(d.get("outcomes", "[]"))
            tokens = json.loads(d.get("clobTokenIds", "[]"))
            prices = json.loads(d.get("outcomePrices", "[]"))

            up_token = down_token = None
            for i, o in enumerate(outcomes):
                if o.lower() == "up":
                    up_token = tokens[i]
                elif o.lower() == "down":
                    down_token = tokens[i]

            if not (up_token and down_token):
                return None

            from datetime import datetime, timezone

            end_date = d.get("endDate", "")
            end_time = 0.0
            if end_date:
                dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                end_time = dt.timestamp()

            return {
                "up_token_id": up_token,
                "down_token_id": down_token,
                "condition_id": d.get("conditionId", ""),
                "question": d.get("question", ""),
                "end_time": end_time,
                "tick_size": d.get("orderPriceMinTickSize", 0.01),
                "slug": slug,
                "prices": dict(zip(outcomes, prices)),
                "accepting_orders": True,
                "price_to_beat": 0.0,
            }
        except Exception as e:
            log.error("Fetch error for %s: %s", slug, e)
            return None
