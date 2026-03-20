"""AI Trading Agent — Claude API ile otonom trading kararlari.

Market verisini analiz eder, yon belirler, alim/satis/hedge karari verir.
Tam yetki: strateji kararlari tamamen AI agent'a ait.
"""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from utils.logger import setup_logger

log = setup_logger("ai_agent")

# API anahtari
_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

SYSTEM_PROMPT = """Sen bir Polymarket BTC 5-dakika UP/DOWN market trading AI agent'isin.

## STRATEJI: ASIMETRIK JACKPOT

Temel fikir: Guclu yondeki pahali tokendan AZ al, ucuz taraftaki tokendan COK al.
Binary markette her token $1 oder — ucuz token cok daha fazla share/$ verir.
Pahali tarafta kayip minimal, ucuz taraf kazanirsa JACKPOT.

## MATEMATIGI

Ornek: UP guclu (¢65), DOWN ucuz (¢24)
- $100'luk UP = 154 share → UP kazanirsa $154 odeme (maliyet $100, az kayip)
- $60'lik DOWN = 250 share → DOWN kazanirsa $250 odeme (maliyet $60, dev kar)
- Toplam: $160 maliyet, min(154,250)=154 → worst case -$6 (-%3.75)
- Jackpot: +$90 (+%56)
- Risk/Odul: 1:15
- Basabas: DOWN %6.25 kazanma orani yeter

## MARKET YAPISI
- Polymarket'te BTC 5dk marketleri var
- UP token: orderbook'ta kapanista baskin taraf → $1, diger → $0
- UP + DOWN fiyat toplami ≈ $1
- Sadece orderbook verisine bak, dis veri kullanma

## KARAR KURALLARI

### FAZ 1: IZLE (0-30s)
- Orderbook'u izle, hangi taraf guclu (pahali) belirle
- action: HOLD
- Guclu taraf = pahali token (¢55+), zayif taraf = ucuz token (¢45-)

### FAZ 2: GUCLU YON ALIMI (30-120s)
- Guclu yone (pahali tarafa) butcenin %0.4'u/saniye hizinda alim
- action: BUY_UP veya BUY_DOWN (hangisi gucluyse)
- budget_pct: 0.3-0.5
- use_taker: false (maker emir, sabirl bekle)

### FAZ 3: UCUZ TARAF BIRIKTIRME (120-240s)
- KRITIK FAZA: Ucuz tarafi biriktirmeye basla
- Ucuz token ¢30 altindaysa → AGRESIF al (budget_pct: 1.0-2.0)
- Ucuz token ¢20 altindaysa → COKAGRESIF al (budget_pct: 2.0-3.0, use_taker: true)
- Hedef: ucuz taraf shares > toplam maliyet (jackpot pozisyonu)
- Guclu tarafta da ALMAya devam et ama yavs (budget_pct: 0.2)

### FAZ 4: POZISYON OPTIMIZASYONU (240-290s)
- min(UP_sh, DN_sh) >= total_cost * 0.90 kontrolu yap
- Eksik taraf varsa tamamla
- risk% > 8 ise → HEDGE (eksik tarafa taker alim)
- risk% < 4 ise → ucuz tarafta biriktirmeye devam
- action: HOLD eger her sey yolundaysa

### FAZ 5: BEKLE (son 10s)
- action: HOLD — hicbir sey yapma

## YON DEGISIMI
Guclu yon degisirse (ornegin UP→DOWN):
1. Eski "guclu" taraf artik ucuz olacak → BIRIKTIR
2. Yeni "guclu" tarafa kucuk alimlar yap
3. Her zaman ucuz tarafta shares >> cost hedefle

## RISK KURALLARI (KIRMIZI COK ONEMLI)
- ¢90+ fiyattan ASLA alim yapma
- max risk: toplam butcenin %10'u → min(UP_sh, DN_sh) >= total_cost * 0.90
- Her kararda risk kontrolu yap: worst_pnl kontrol et
- risk% > 10 ise → CANCEL_ALL ve HEDGE

## KARAR FORMATI
SADECE JSON dondur:
{
  "action": "BUY_UP" | "BUY_DOWN" | "HEDGE_UP" | "HEDGE_DOWN" | "HOLD" | "CANCEL_ALL",
  "budget_pct": 0.0-5.0,
  "use_taker": false,
  "reasoning": "kisa aciklama"
}

## HEDEF
Her markette "kucuk kayip VEYA buyuk kar" profili olustur.
16 marketten 15'ini kaybetsen bile 1 jackpot tum kayiplari karsilar.
Asla buyuk kayip olmasin — ucuz taraf biriktirme ile asimetri koru.
"""


@dataclass
class AgentDecision:
    """AI agent'in verdigi karar."""
    action: str         # BUY_UP, BUY_DOWN, HEDGE_UP, HEDGE_DOWN, HOLD, CANCEL_ALL
    budget_pct: float   # butcenin %'si
    use_taker: bool     # taker mi maker mi
    reasoning: str      # neden
    raw_response: str   # ham API yaniti
    latency_ms: float   # API cagri suresi


class AITradingAgent:
    """Claude API ile otonom trading kararlari veren agent."""

    def __init__(self):
        self._client = None
        self._enabled = False
        self._call_count = 0
        self._total_latency = 0.0
        self._last_decision: Optional[AgentDecision] = None
        self._error_count = 0
        self._consecutive_errors = 0
        self._last_call_ts: float = 0
        self._min_interval: float = 2.0  # min 2s arayla API cagir
        self._history: list[dict] = []  # son 10 karar

        if not _API_KEY:
            log.warning("ANTHROPIC_API_KEY yok — AI agent devre disi")
            return

        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=_API_KEY)
            self._enabled = True
            log.info("AI Agent aktif — Claude API baglandi")
        except ImportError:
            log.error("anthropic paketi yuklu degil: pip install anthropic")
        except Exception as e:
            log.error("AI Agent baslatilamadi: %s", e)

    @property
    def is_enabled(self) -> bool:
        return self._enabled and self._client is not None

    @property
    def stats(self) -> dict:
        avg_latency = (self._total_latency / self._call_count) if self._call_count > 0 else 0
        return {
            "enabled": self.is_enabled,
            "calls": self._call_count,
            "errors": self._error_count,
            "avg_latency_ms": round(avg_latency, 0),
            "last_decision": self._last_decision.action if self._last_decision else None,
            "last_reasoning": self._last_decision.reasoning if self._last_decision else None,
        }

    def _build_snapshot(self, ctx: dict) -> str:
        """Market snapshot'ini AI agent icin ozetler."""
        return json.dumps({
            "timestamp": time.strftime("%H:%M:%S"),
            "time_remaining_sec": ctx.get("time_left", 0),
            "elapsed_sec": ctx.get("elapsed", 0),
            "budget_total": ctx.get("budget", 0),
            "budget_remaining": ctx.get("remaining", 0),
            "portfolio": {
                "up_shares": ctx.get("up_shares", 0),
                "down_shares": ctx.get("down_shares", 0),
                "up_cost": ctx.get("up_cost", 0),
                "down_cost": ctx.get("down_cost", 0),
                "total_cost": ctx.get("total_cost", 0),
                "worst_pnl": ctx.get("worst_pnl", 0),
                "risk_pct": ctx.get("risk_pct", 0),
            },
            "orderbook": {
                "up_bid": ctx.get("up_bid", 0),
                "up_ask": ctx.get("up_ask", 0),
                "dn_bid": ctx.get("dn_bid", 0),
                "dn_ask": ctx.get("dn_ask", 0),
                "up_bid_depth": ctx.get("up_bid_depth", 0),
                "dn_bid_depth": ctx.get("dn_bid_depth", 0),
            },
            "active_orders": ctx.get("active_orders", 0),
            "direction_changes": ctx.get("direction_changes", 0),
            "current_direction": ctx.get("current_direction"),
            "strategy_state": ctx.get("strategy_state", "IZLE"),
            "recent_fills": ctx.get("recent_fills", 0),
        }, indent=2)

    async def decide(self, ctx: dict) -> Optional[AgentDecision]:
        """Market snapshot'i analiz edip karar ver.

        ctx: strateji'den gelen market durumu dict'i
        Returns: AgentDecision veya None (hata/devre disi)
        """
        if not self.is_enabled:
            return None

        # Rate limiting
        now = time.time()
        if now - self._last_call_ts < self._min_interval:
            return self._last_decision

        # Ardisik hata kontrolu — 5+ ardisik hatada yavaslat
        if self._consecutive_errors >= 5:
            self._min_interval = min(30.0, self._min_interval * 2)
            log.warning("AI Agent yavaslatildi: %ds aralik", self._min_interval)
            self._consecutive_errors = 0

        self._last_call_ts = now
        snapshot = self._build_snapshot(ctx)

        # Son kararlari context olarak ekle
        history_text = ""
        if self._history:
            history_text = "\n\nSon kararlarim:\n" + "\n".join(
                f"- {h['time']}: {h['action']} ({h['reasoning']})"
                for h in self._history[-5:]
            )

        try:
            t0 = time.monotonic()
            response = await asyncio.to_thread(
                self._client.messages.create,
                model="claude-sonnet-4-20250514",
                max_tokens=256,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"Market snapshot:\n{snapshot}{history_text}\n\nKararini ver (sadece JSON):"
                }],
            )
            latency = (time.monotonic() - t0) * 1000

            self._call_count += 1
            self._total_latency += latency
            self._consecutive_errors = 0

            # Parse response
            text = response.content[0].text.strip()
            # JSON blogu cikar (```json ... ``` veya duz JSON)
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()

            data = json.loads(text)

            decision = AgentDecision(
                action=data.get("action", "HOLD"),
                budget_pct=min(5.0, max(0, float(data.get("budget_pct", 0)))),
                use_taker=bool(data.get("use_taker", False)),
                reasoning=data.get("reasoning", ""),
                raw_response=text,
                latency_ms=latency,
            )

            self._last_decision = decision
            self._history.append({
                "time": time.strftime("%H:%M:%S"),
                "action": decision.action,
                "reasoning": decision.reasoning[:50],
            })
            if len(self._history) > 10:
                self._history = self._history[-10:]

            log.info(
                "AI: %s | %s | %.0fms",
                decision.action, decision.reasoning[:60], latency
            )
            return decision

        except json.JSONDecodeError as e:
            self._error_count += 1
            self._consecutive_errors += 1
            log.error("AI JSON parse hatasi: %s", e)
            return None
        except Exception as e:
            self._error_count += 1
            self._consecutive_errors += 1
            log.error("AI API hatasi: %s", e)
            return None
