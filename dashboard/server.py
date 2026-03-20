"""Limit order bot dashboard — BUY/SELL buttons, orderbook, positions."""

import asyncio
import hashlib
import json
import os
import time
from aiohttp import web
from utils.logger import setup_logger

log = setup_logger("dashboard")

# Dashboard sifresi: .env'den DASHBOARD_PASSWORD, yoksa local kullanim icin "1"
_DASH_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
if not _DASH_PASSWORD:
    _DASH_PASSWORD = "1"
    log.info("DASHBOARD_PASSWORD ayarlanmamis! Varsayilan local sifre: %s", _DASH_PASSWORD)

# Token: sifrenin hash'i — cookie olarak saklanir
_AUTH_TOKEN = hashlib.sha256(_DASH_PASSWORD.encode()).hexdigest()[:32]


class DashboardServer:
    def __init__(self, strategy, host="0.0.0.0", port=8898):
        self.strategy = strategy
        self.host = host
        self.port = port
        self._ws_clients: list[web.WebSocketResponse] = []
        self._app = web.Application()
        self._app.router.add_get("/", self._serve_html)
        self._app.router.add_post("/login", self._handle_login)
        self._app.router.add_get("/ws", self._ws_handler)
        self._push_task = None

    def _check_auth(self, request) -> bool:
        """Cookie'den auth token kontrolu."""
        token = request.cookies.get("ps_auth", "")
        return token == _AUTH_TOKEN

    async def start(self):
        runner = web.AppRunner(self._app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        self._push_task = asyncio.create_task(self._push_loop())
        log.info("Dashboard: http://localhost:%d", self.port)

    def _state(self) -> dict:
        s = self.strategy
        now = time.time()
        books = {}
        feed_state = {"status": "offline", "ws_connected": False, "rx_age_sec": None, "book_age_sec": None, "stale_assets": []}
        if s.market_feed:
            feed_state = s.market_feed.health()
            for aid in s._current_asset_ids:
                book = s.market_feed.get_book(aid)
                if book:
                    bid_vol = sum(l.size for l in book.bids[:10])
                    ask_vol = sum(l.size for l in book.asks[:10])
                    books[aid[:12]] = {
                        "best_bid": book.best_bid, "best_ask": book.best_ask,
                        "mid": book.mid_price, "spread": book.spread,
                        "bids_top5": [{"p": l.price, "s": l.size} for l in book.bids[:10]],
                        "asks_top5": [{"p": l.price, "s": l.size} for l in book.asks[:10]],
                        "bid_vol": round(bid_vol, 0), "ask_vol": round(ask_vol, 0),
                    }

        up_p, dn_p = s._get_prices()
        pf = s.portfolio.state(up_p, dn_p)
        loss = s.limit_engine.check_loss_limit(up_p, dn_p)

        up_shares = pf["up_shares"]
        dn_shares = pf["down_shares"]
        up_cost = round(pf["up_cost"], 2)
        dn_cost = round(pf["down_cost"], 2)
        up_rev = round(pf["up_revenue"], 2)
        dn_rev = round(pf["down_revenue"], 2)
        total_cost = pf["total_cost"]

        up_current_val = round(up_p * up_shares, 2) if up_p else 0
        dn_current_val = round(dn_p * dn_shares, 2) if dn_p else 0
        up_max_payout = round(up_shares, 2)
        dn_max_payout = round(dn_shares, 2)

        up_wins_pnl = pf.get("up_wins_pnl", 0)
        dn_wins_pnl = pf.get("dn_wins_pnl", 0)
        up_roi = round(up_wins_pnl / total_cost * 100, 1) if total_cost > 0 else 0
        dn_roi = round(dn_wins_pnl / total_cost * 100, 1) if total_cost > 0 else 0
        signal_state = s.signal_tracker.current() if s.signal_tracker else {
            "state": "NEUTRAL", "up_mid": 0, "dn_mid": 0,
            "spread_pct": 0, "confidence": 0, "checks": 0, "signals_fired": 0,
        }
        spread_pct = signal_state.get("spread_pct", 0) or 0
        vol_label = "LOW" if spread_pct < 40 else "HIGH" if spread_pct > 120 else "MID"

        return {
            "ts": now,
            "bot_active": s.bot_active,
            "busy": s.limit_engine.is_busy,
            "budget": s.budget,
            "market_question": s._market_question,
            "time_remaining": max(0, s._market_end_time - now) if s._market_end_time else None,
            "btc_price": None,
            "up_price": up_p, "down_price": dn_p,
            "up_shares": up_shares, "down_shares": dn_shares,
            "up_cost": up_cost, "down_cost": dn_cost,
            "up_revenue": up_rev, "down_revenue": dn_rev,
            "up_bought": pf["up_bought"], "up_sold": pf["up_sold"],
            "down_bought": pf["down_bought"], "down_sold": pf["down_sold"],
            "up_avg_buy": pf.get("up_avg_buy", 0), "down_avg_buy": pf.get("down_avg_buy", 0),
            "up_current_value": up_current_val,
            "dn_current_value": dn_current_val,
            "up_max_payout": up_max_payout,
            "dn_max_payout": dn_max_payout,
            "up_payout": pf.get("up_payout", 0), "dn_payout": pf.get("dn_payout", 0),
            "up_wins_pnl": up_wins_pnl, "dn_wins_pnl": dn_wins_pnl,
            "up_roi": up_roi, "dn_roi": dn_roi,
            "worst_pnl": pf.get("worst_pnl", 0), "best_pnl": pf.get("best_pnl", 0),
            "total_pnl": pf["total_pnl"], "total_cost": total_cost,
            "total_revenue": pf.get("total_revenue", 0),
            "realized_pnl": pf["realized_pnl"],
            "loss_pct": loss["loss_pct"], "loss_exceeded": loss["exceeded"],
            "books": books,
            "feed": feed_state,
            "orders": [
                {"id": o.order_id, "asset": o.asset_id[:12], "side": o.side.value,
                 "price": o.price, "size": o.size,
                 "ttl": max(0, o.expiration - int(now)) if o.expiration else 0}
                for o in s.order_mgr.active_orders.values()
            ],
            "fill_log": s.portfolio.all_fills(),
            "order_history": s.limit_engine.history[-10:],
            "stats": s.stats,
            "process_log": s.process_log[-20:],
            "order_archive": (s.order_archive or [])[-50:],
            "market_history": s.market_history[-100:],
            "cumulative_pnl": round(sum(h.get("resolved_pnl", 0) for h in s.market_history), 2),
            "auto_mode": s._auto_mode,
            "signal": signal_state,
            "signal_state": s._signal_state if hasattr(s, '_signal_state') else "NEUTRAL",
            "current_phase": (s._current_phase + 1) if hasattr(s, '_current_phase') else 0,
            "phase_role": s._strategy_state if hasattr(s, '_strategy_state') else "IZLE",
            "active_direction": s._active_direction if hasattr(s, '_active_direction') else None,
            "direction_changes": s._direction_changes if hasattr(s, '_direction_changes') else 0,
            "ai_agent": s.ai_agent.stats if hasattr(s, 'ai_agent') else {"enabled": False},
            "ai_action": s._ai_last_action if hasattr(s, '_ai_last_action') else "HOLD",
            "ai_reasoning": s._ai_reasoning if hasattr(s, '_ai_reasoning') else "",
            "btc_change_pct": 0,
            "btc_direction": 0,
            "btc_ptb": 0,
            "btc_current": 0,
            "vol_label": vol_label,
            "izle_signal": s._izle_signal if hasattr(s, '_izle_signal') else "NEUTRAL",
            "izle_avg": round(s._izle_avg, 4) if hasattr(s, '_izle_avg') else 0,
            "izle_samples": len(s._izle_samples) if hasattr(s, '_izle_samples') else 0,
        }

    async def _handle_login(self, request):
        """POST /login — sifre dogrulama."""
        try:
            data = await request.json()
            password = data.get("password", "")
        except Exception:
            return web.json_response({"ok": False}, status=400)

        if password == _DASH_PASSWORD:
            resp = web.json_response({"ok": True})
            resp.set_cookie("ps_auth", _AUTH_TOKEN, httponly=True, max_age=86400, samesite="Strict")
            return resp
        return web.json_response({"ok": False}, status=401)

    async def _ws_handler(self, request):
        if not self._check_auth(request):
            return web.Response(status=401, text="Unauthorized")
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.append(ws)
        try:
            await ws.send_json(self._state())
            async for msg in ws:
                if msg.type == 1:
                    try:
                        await self.strategy.handle_command(json.loads(msg.data))
                    except Exception as e:
                        log.error("Cmd: %s", e)
        finally:
            if ws in self._ws_clients:
                self._ws_clients.remove(ws)
        return ws

    async def _push_loop(self):
        while True:
            await asyncio.sleep(1)
            if not self._ws_clients:
                continue
            payload = json.dumps(self._state())
            dead = []
            for ws in self._ws_clients:
                try:
                    await ws.send_str(payload)
                except:
                    dead.append(ws)
            for ws in dead:
                if ws in self._ws_clients:
                    self._ws_clients.remove(ws)

    async def _serve_html(self, request):
        if self._check_auth(request):
            return web.Response(text=HTML, content_type="text/html")
        return web.Response(text=LOGIN_HTML, content_type="text/html")


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="tr" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PolySignal — Giris</title>
<style>
body{background:#080c14;color:#f1f5f9;font-family:system-ui,sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}
.box{background:#0f1522;border:1px solid #1c2840;border-radius:12px;padding:40px;width:320px;text-align:center}
h1{font-size:18px;margin-bottom:24px;color:#06b6d4}
input{width:100%;padding:10px 14px;background:#0b1018;border:1px solid #1c2840;border-radius:8px;color:#f1f5f9;font-size:14px;margin-bottom:16px;box-sizing:border-box}
input:focus{outline:none;border-color:#06b6d4}
button{width:100%;padding:10px;background:#10b981;border:none;border-radius:8px;color:#000;font-weight:700;font-size:14px;cursor:pointer}
button:hover{background:#34d399}
.err{color:#ef4444;font-size:12px;margin-top:8px;display:none}
</style>
</head>
<body>
<div class="box">
  <h1>PolySignal</h1>
  <form onsubmit="return doLogin(event)">
    <input type="password" id="pw" placeholder="Sifre" autofocus>
    <button type="submit">Giris</button>
  </form>
  <div id="err" class="err">Yanlis sifre</div>
</div>
<script>
async function doLogin(e){
  e.preventDefault();
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
  if(r.ok){location.reload()}else{document.getElementById('err').style.display='block'}
  return false;
}
</script>
</body>
</html>"""

HTML = r"""<!DOCTYPE html>
<html lang="tr" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PolySignal</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>
tailwind.config = {
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'SF Mono', 'monospace'],
      },
      colors: {
        base: { DEFAULT: '#080c14', card: '#0f1522', surface: '#161e2e', input: '#0b1018' },
        brd: { DEFAULT: '#1c2840', light: '#253352' },
      }
    }
  }
}
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
body { background: #080c14; }
.book-bar-bid { background: rgba(16,185,129,.12); }
.book-bar-ask { background: rgba(239,68,68,.12); }
.log-scroll::-webkit-scrollbar { width: 4px; }
.log-scroll::-webkit-scrollbar-track { background: transparent; }
.log-scroll::-webkit-scrollbar-thumb { background: #253352; border-radius: 2px; }
</style>
</head>
<body class="font-sans text-slate-200 text-sm antialiased">

<!-- Header -->
<header class="flex items-center justify-between px-6 py-3 bg-base-card border-b border-brd">
  <h1 class="text-base font-extrabold tracking-tight">
    <span class="text-cyan-400">&#9670;</span> Poly<span class="text-cyan-400">Signal</span>
  </h1>
  <div class="flex items-center gap-4 text-xs text-slate-400">
    <span id="mktLabel" class="text-cyan-400 font-semibold truncate max-w-xs">Baglanmadi</span>
    <span id="busyLabel" class="hidden px-2 py-0.5 bg-amber-500 text-black rounded text-[10px] font-bold uppercase">Emir Isleniyor</span>
    <span class="flex items-center gap-1.5">
      <span id="cDot" class="w-2 h-2 rounded-full bg-red-500"></span>
      <span id="cLbl" class="text-slate-400">Baglanti yok</span>
    </span>
    <span class="flex items-center gap-1.5">
      <span id="feedDot" class="w-2 h-2 rounded-full bg-slate-600"></span>
      <span id="feedLbl" class="text-slate-400">Feed yok</span>
    </span>
  </div>
</header>

<!-- Metrics Bar -->
<div class="grid grid-cols-6 border-b border-brd bg-base-card">
  <div class="flex flex-col items-center py-2.5 border-r border-brd">
    <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">OB Mid</span>
    <span id="btcNow" class="font-mono text-sm font-bold">—</span>
  </div>
  <div class="flex flex-col items-center py-2.5 border-r border-brd">
    <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Referans</span>
    <span id="btcPtb" class="font-mono text-sm font-bold text-amber-400">—</span>
  </div>
  <div class="flex flex-col items-center py-2.5 border-r border-brd">
    <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">UP Mid</span>
    <span id="upMid" class="font-mono text-sm font-bold text-emerald-400">—</span>
  </div>
  <div class="flex flex-col items-center py-2.5 border-r border-brd">
    <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">DN Mid</span>
    <span id="dnMid" class="font-mono text-sm font-bold text-red-400">—</span>
  </div>
  <div class="flex flex-col items-center py-2.5 border-r border-brd">
    <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Kalan</span>
    <span id="timer" class="font-mono text-sm font-bold text-amber-400">—</span>
  </div>
  <div class="flex flex-col items-center py-2.5">
    <span class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Toplam K/Z</span>
    <span id="cumPnl" class="font-mono text-sm font-bold text-slate-500">$0</span>
  </div>
</div>

<!-- Main Grid -->
<div class="grid grid-cols-2 gap-4 p-4 h-[calc(100vh-100px)]">

  <!-- Left Column -->
  <div class="flex flex-col gap-4 overflow-y-auto min-h-0">

    <!-- Controls -->
    <div class="bg-base-card border border-brd rounded-xl">
      <div class="px-5 py-2.5 border-b border-brd text-[10px] font-bold uppercase tracking-widest text-slate-500">Islem Kontrol</div>
      <div class="p-4">
        <div class="flex items-end gap-3 flex-wrap">
          <div>
            <label class="block text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-1">Butce ($)</label>
            <input type="number" id="inBudget" value="1000" min="10"
              class="w-24 px-3 py-2 bg-base-input border border-brd rounded-lg font-mono text-sm font-medium text-slate-200 focus:outline-none focus:border-cyan-500 transition">
          </div>
          <button onclick="sendCmd({action:'start',budget:+$('inBudget').value})"
            class="px-7 py-2.5 bg-emerald-500 hover:bg-emerald-400 text-black font-bold text-sm uppercase tracking-wider rounded-lg transition-all hover:-translate-y-0.5">Start</button>
          <button id="btnAuto" onclick="sendCmd({action:'auto',budget:+$('inBudget').value})"
            class="px-5 py-2.5 bg-blue-500 hover:bg-blue-400 text-white font-bold text-xs uppercase tracking-wider rounded-lg transition-all hover:-translate-y-0.5">Auto</button>
          <button onclick="sendCmd({action:'sell',token:'UP',shares:0})"
            class="px-4 py-2.5 bg-red-500 hover:bg-red-400 text-white font-bold text-xs uppercase tracking-wider rounded-lg transition-all hover:-translate-y-0.5">Sell UP</button>
          <button onclick="sendCmd({action:'sell',token:'DOWN',shares:0})"
            class="px-4 py-2.5 bg-red-500 hover:bg-red-400 text-white font-bold text-xs uppercase tracking-wider rounded-lg transition-all hover:-translate-y-0.5">Sell DN</button>
          <button onclick="sendCmd({action:'stop'})"
            class="px-4 py-2.5 bg-slate-700 hover:bg-slate-600 text-slate-300 font-bold text-xs uppercase tracking-wider rounded-lg transition-all hover:-translate-y-0.5">Stop</button>
        </div>
      </div>
    </div>

    <!-- Signal Panel -->
    <div class="bg-base-card border border-brd rounded-xl">
      <div class="px-5 py-2.5 border-b border-brd text-[10px] font-bold uppercase tracking-widest text-slate-500">Sinyal Takibi</div>
      <div class="px-4 py-3 flex flex-col gap-2.5">
        <!-- Satir 1: Sinyal + Faz -->
        <div class="flex items-center gap-3 font-mono text-xs flex-wrap">
          <span id="sigState" class="px-3 py-1 rounded-full text-[11px] font-bold bg-slate-700 text-slate-300">NEUTRAL</span>
          <span id="phaseRole" class="px-3 py-1 rounded-full text-[11px] font-bold bg-slate-700 text-slate-400">—</span>
          <div class="w-px h-5 bg-brd shrink-0"></div>
          <span class="text-[10px] text-slate-500 font-sans font-semibold">FAZ</span>
          <span id="sigPhase" class="text-cyan-400 font-bold">1/10</span>
          <span class="text-[10px] text-slate-500 font-sans font-semibold">VOL</span>
          <span id="volLabel" class="font-bold text-slate-400">MID</span>
          <div class="w-px h-5 bg-brd shrink-0"></div>
          <span class="text-[10px] text-slate-500 font-sans font-semibold">MAKAS</span>
          <span id="sigSpread" class="font-bold">%0</span>
          <span class="text-[10px] text-slate-500 font-sans font-semibold">GUVEN</span>
          <div class="w-16 h-1.5 bg-base-surface rounded-full overflow-hidden">
            <div id="sigConfBar" class="h-full rounded-full bg-slate-500 transition-all" style="width:0%"></div>
          </div>
          <span id="sigConf" class="text-slate-500">%0</span>
          <div class="w-px h-5 bg-brd shrink-0"></div>
          <span id="sigFired" class="text-amber-400 font-bold">0</span><span class="text-[10px] text-slate-500">sin</span>
          <span id="sigHedge" class="text-amber-400 font-bold">0</span><span class="text-[10px] text-slate-500">hed</span>
          <span id="sigChecks" class="text-slate-500">0</span><span class="text-[10px] text-slate-500">chk</span>
          <span id="sigRapid" class="hidden px-2 py-0.5 bg-red-500/20 text-red-400 border border-red-500/30 rounded text-[10px] font-bold">HIZLI</span>
        </div>
        <!-- Satir 2: Orderbook referans metrikleri -->
        <div class="flex items-center gap-3 font-mono text-xs flex-wrap border-t border-brd/50 pt-2.5">
          <span class="text-[10px] text-slate-500 font-sans font-semibold">OB MID</span>
          <span id="sigBtcNow" class="font-bold">$0</span>
          <span class="text-[10px] text-slate-500 font-sans font-semibold">REF</span>
          <span id="sigBtcPtb" class="font-bold text-amber-400">$0</span>
          <div class="w-px h-5 bg-brd shrink-0"></div>
          <span class="text-[10px] text-slate-500 font-sans font-semibold">FARK</span>
          <span id="sigBtcDiff" class="font-bold text-slate-400">$0</span>
          <span id="sigBtcPct" class="font-bold text-slate-400">%0</span>
          <div class="w-px h-5 bg-brd shrink-0"></div>
          <span class="text-[10px] text-slate-500 font-sans font-semibold">OB SKOR</span>
          <span id="sigBtcDir" class="font-bold text-slate-400">0</span>
          <span id="sigBtcLabel" class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-700 text-slate-400">NEUTRAL</span>
        </div>
        <!-- Satir 3: IZLE progress -->
        <div id="izleRow" class="flex items-center gap-3 font-mono text-xs flex-wrap border-t border-brd/50 pt-2.5">
          <span class="text-[10px] text-slate-500 font-sans font-semibold">IZLE</span>
          <div class="w-24 h-1.5 bg-base-surface rounded-full overflow-hidden">
            <div id="izleBar" class="h-full rounded-full bg-blue-400 transition-all" style="width:0%"></div>
          </div>
          <span id="izleSamples" class="text-slate-500">0/60</span>
          <div class="w-px h-5 bg-brd shrink-0"></div>
          <span class="text-[10px] text-slate-500 font-sans font-semibold">ORT</span>
          <span id="izleAvg" class="font-bold text-slate-400">%0</span>
          <span id="izleResult" class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-700 text-slate-400">—</span>
        </div>
      </div>
    </div>

    <!-- Orderbook -->
    <div class="bg-base-card border border-brd rounded-xl flex-1 min-h-0 flex flex-col">
      <div class="px-5 py-2.5 border-b border-brd flex items-center justify-between gap-3">
        <span class="text-[10px] font-bold uppercase tracking-widest text-slate-500">Emir Defteri</span>
        <span id="feedMeta" class="text-[10px] font-semibold uppercase tracking-wider text-slate-500">Feed bekleniyor</span>
      </div>
      <div class="p-4 overflow-y-auto flex-1">
        <div class="grid grid-cols-2 gap-4">
          <div>
            <div class="text-center text-xs font-bold text-emerald-400 uppercase tracking-wider mb-2">UP</div>
            <div id="bU"><div class="text-center text-slate-500 py-5 text-xs">—</div></div>
          </div>
          <div>
            <div class="text-center text-xs font-bold text-red-400 uppercase tracking-wider mb-2">DOWN</div>
            <div id="bD"><div class="text-center text-slate-500 py-5 text-xs">—</div></div>
          </div>
        </div>
      </div>
    </div>

  </div>

  <!-- Right Column -->
  <div class="flex flex-col gap-4 overflow-y-auto min-h-0">

    <!-- K/Z -->
    <div class="bg-base-card border border-brd rounded-xl">
      <div class="px-5 py-2.5 border-b border-brd text-[10px] font-bold uppercase tracking-widest text-slate-500">Polymarket K/Z</div>
      <div class="p-4">
        <table class="w-full font-mono text-xs">
          <thead>
            <tr class="text-[10px] uppercase tracking-wider text-slate-500 font-sans font-semibold">
              <th class="text-left pb-2 font-semibold">Senaryo</th>
              <th class="text-right pb-2 font-semibold">K/Z</th>
              <th class="text-right pb-2 font-semibold">ROI</th>
              <th class="text-right pb-2 font-semibold">Odeme</th>
              <th class="text-right pb-2 font-semibold">Formul</th>
            </tr>
          </thead>
          <tbody class="border-t border-brd">
            <tr>
              <td class="py-2.5 text-left font-sans font-extrabold text-xs text-emerald-400">UP Kazanir</td>
              <td class="py-2.5 text-right text-base font-bold" id="upWinsPnl">$0</td>
              <td class="py-2.5 text-right font-semibold" id="upRoi">%0</td>
              <td class="py-2.5 text-right text-slate-500"><span id="upPayout">0</span>sh x $1 = $<span id="upPayAmt">0</span></td>
              <td class="py-2.5 text-right text-slate-500">$<span id="upPayAmt2">0</span> - $<span id="upTotCost">0</span></td>
            </tr>
            <tr class="border-t border-white/5">
              <td class="py-2.5 text-left font-sans font-extrabold text-xs text-red-400">DN Kazanir</td>
              <td class="py-2.5 text-right text-base font-bold" id="dnWinsPnl">$0</td>
              <td class="py-2.5 text-right font-semibold" id="dnRoi">%0</td>
              <td class="py-2.5 text-right text-slate-500"><span id="dnPayout">0</span>sh x $1 = $<span id="dnPayAmt">0</span></td>
              <td class="py-2.5 text-right text-slate-500">$<span id="dnPayAmt2">0</span> - $<span id="dnTotCost">0</span></td>
            </tr>
          </tbody>
        </table>
        <div class="flex items-center gap-4 flex-wrap mt-3 pt-3 border-t border-brd text-xs text-slate-400 font-semibold">
          <span>En kotu: <span id="worstPnl" class="text-red-400">$0</span></span>
          <span>En iyi: <span id="bestPnl" class="text-emerald-400">$0</span></span>
          <span>Maliyet: $<span id="totalCost">0</span></span>
          <span>Gelir: $<span id="totalRev">0</span></span>
          <span class="flex items-center gap-2">
            <span id="lossLbl">%0</span>
            <span class="w-16 h-1 bg-base-surface rounded-full overflow-hidden"><span id="lossFill" class="block h-full rounded-full bg-emerald-500 transition-all" style="width:0%"></span></span>
          </span>
        </div>
      </div>
    </div>

    <!-- Positions -->
    <div class="bg-base-card border border-brd rounded-xl">
      <div class="px-5 py-2.5 border-b border-brd text-[10px] font-bold uppercase tracking-widest text-slate-500">Pozisyonlar</div>
      <div class="px-4 py-3">
        <div class="flex items-center font-mono text-xs gap-0">
          <div class="flex items-baseline gap-2 flex-1 px-3 py-2 rounded-lg bg-emerald-500/[.06]">
            <span class="font-sans font-extrabold text-[11px] text-emerald-400">UP</span>
            <span class="text-emerald-400 font-bold text-base" id="upSh">0</span>
            <span class="text-slate-500">&cent;<span id="upMktPrice">—</span></span>
            <span class="text-slate-400 font-semibold">$<span id="upCost">0</span></span>
            <span class="text-slate-500 text-[10px] uppercase tracking-wider font-semibold">Ort</span>
            <span class="text-cyan-400 font-semibold">&cent;<span id="upAvg">0</span></span>
          </div>
          <div class="w-px h-6 bg-brd mx-2 shrink-0"></div>
          <div class="flex items-baseline gap-2 flex-1 px-3 py-2 rounded-lg bg-red-500/[.06]">
            <span class="font-sans font-extrabold text-[11px] text-red-400">DN</span>
            <span class="text-red-400 font-bold text-base" id="dnSh">0</span>
            <span class="text-slate-500">&cent;<span id="dnMktPrice">—</span></span>
            <span class="text-slate-400 font-semibold">$<span id="dnCost">0</span></span>
            <span class="text-slate-500 text-[10px] uppercase tracking-wider font-semibold">Ort</span>
            <span class="text-cyan-400 font-semibold">&cent;<span id="dnAvg">0</span></span>
          </div>
          <div class="w-px h-6 bg-brd mx-2 shrink-0"></div>
          <div class="flex items-baseline gap-2 px-3 py-2 text-cyan-400 font-bold shrink-0">
            <span class="font-sans text-[10px] uppercase tracking-wider text-slate-500 font-semibold">Top</span>
            <span id="totalSh">0</span>
            <span class="text-slate-400 font-semibold">$<span id="totalCostPos">0</span></span>
          </div>
        </div>
        <div class="hidden">
          <span id="upRev"></span><span id="dnRev"></span>
          <span id="upBought"></span><span id="upSold"></span>
          <span id="dnBought"></span><span id="dnSold"></span>
          <span id="upCurVal"></span><span id="dnCurVal"></span>
          <span id="upMaxPay"></span><span id="dnMaxPay"></span>
          <span id="totalValPos"></span>
        </div>
      </div>
    </div>

    <!-- Log -->
    <div class="bg-base-card border border-brd rounded-xl">
      <div class="px-5 py-2.5 border-b border-brd text-[10px] font-bold uppercase tracking-widest text-slate-500">Islem Gunlugu</div>
      <div class="p-4">
        <div id="logPanel" class="log-scroll font-mono text-xs leading-7 max-h-44 overflow-y-auto px-3 py-2 bg-base rounded-lg border border-brd">START butonuna basin.</div>
      </div>
    </div>

    <!-- Fills (hidden, keeps JS working) -->
    <div class="hidden"><span id="fillCount"></span><span id="fillPanel"></span></div>

    <!-- Market History -->
    <div class="bg-base-card border border-brd rounded-xl">
      <div class="flex items-center justify-between px-5 py-2.5 border-b border-brd">
        <span class="text-[10px] font-bold uppercase tracking-widest text-slate-500">Market Gecmisi <span id="histCount" class="text-cyan-400"></span></span>
        <button onclick="sendCmd({action:'clear_history'})"
          class="px-3 py-1 bg-red-500/80 hover:bg-red-500 text-white text-[10px] font-bold uppercase rounded-full transition">Temizle</button>
      </div>
      <div class="p-4">
        <div id="histPanel" class="log-scroll max-h-72 overflow-y-auto text-xs">Henuz kapanan market yok</div>
      </div>
    </div>

  </div>
</div>

<script>
const $=id=>document.getElementById(id);
let ws;

function conn(){
  ws=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/ws`);
  ws.onopen=()=>{$('cDot').className='w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_8px_theme(colors.emerald.400)]';$('cLbl').textContent='Bagli'};
  ws.onclose=()=>{$('cDot').className='w-2 h-2 rounded-full bg-red-500';$('cLbl').textContent='Yeniden baglaniliyor...';setTimeout(conn,2000)};
  ws.onmessage=e=>render(JSON.parse(e.data));
}
function sendCmd(o){if(ws&&ws.readyState===1)ws.send(JSON.stringify(o))}

function render(d){
  $('mktLabel').textContent=d.market_question||'Baglanmadi';
  $('busyLabel').className=d.busy?'px-2 py-0.5 bg-amber-500 text-black rounded text-[10px] font-bold uppercase':'hidden';

  const feed=d.feed||{};
  const feedStatus=feed.status||'offline';
  const feedRx=feed.rx_age_sec;
  const feedBook=feed.book_age_sec;
  const staleAssets=feed.stale_assets||[];
  const feedDot=$('feedDot');
  const feedLbl=$('feedLbl');
  const feedMeta=$('feedMeta');
  if(feedStatus==='live'){
    feedDot.className='w-2 h-2 rounded-full bg-emerald-400 shadow-[0_0_8px_theme(colors.emerald.400)]';
    feedLbl.textContent='Feed canli';
    feedLbl.className='text-emerald-400';
    feedMeta.textContent='Son book '+(feedBook!=null?feedBook.toFixed(1)+'s':'—');
    feedMeta.className='text-[10px] font-semibold uppercase tracking-wider text-emerald-400';
  }else if(feedStatus==='stale'){
    feedDot.className='w-2 h-2 rounded-full bg-amber-400 shadow-[0_0_8px_theme(colors.amber.400)]';
    feedLbl.textContent='Feed stale';
    feedLbl.className='text-amber-400';
    feedMeta.textContent=staleAssets.length?'Stale '+staleAssets.join(','):'Rx '+(feedRx!=null?feedRx.toFixed(1)+'s':'—');
    feedMeta.className='text-[10px] font-semibold uppercase tracking-wider text-amber-400';
  }else if(feedStatus==='reconnecting'){
    feedDot.className='w-2 h-2 rounded-full bg-red-500';
    feedLbl.textContent='Feed reconnect';
    feedLbl.className='text-red-400';
    feedMeta.textContent='Yeniden baglaniyor';
    feedMeta.className='text-[10px] font-semibold uppercase tracking-wider text-red-400';
  }else{
    feedDot.className='w-2 h-2 rounded-full bg-slate-600';
    feedLbl.textContent='Feed yok';
    feedLbl.className='text-slate-400';
    feedMeta.textContent='Feed bekleniyor';
    feedMeta.className='text-[10px] font-semibold uppercase tracking-wider text-slate-500';
  }

  // Auto
  const ab=$('btnAuto');
  if(d.auto_mode){ab.className='px-5 py-2.5 bg-emerald-500 hover:bg-emerald-400 text-black font-bold text-xs uppercase tracking-wider rounded-lg transition-all hover:-translate-y-0.5 shadow-[0_0_12px_theme(colors.emerald.500/40%)]';ab.textContent='AUTO ON'}
  else{ab.className='px-5 py-2.5 bg-blue-500 hover:bg-blue-400 text-white font-bold text-xs uppercase tracking-wider rounded-lg transition-all hover:-translate-y-0.5';ab.textContent='AUTO'}

  // K/Z
  const uwp=d.up_wins_pnl||0,dwp=d.dn_wins_pnl||0;
  const uw=$('upWinsPnl');uw.textContent='$'+(uwp>=0?'+':'')+uwp.toFixed(2);uw.className='py-2.5 text-right text-base font-bold '+(uwp>=0?'text-emerald-400':'text-red-400');
  const dw=$('dnWinsPnl');dw.textContent='$'+(dwp>=0?'+':'')+dwp.toFixed(2);dw.className='py-2.5 text-right text-base font-bold '+(dwp>=0?'text-emerald-400':'text-red-400');

  const ur=d.up_roi||0,dr=d.dn_roi||0;
  $('upRoi').textContent='%'+(ur>=0?'+':'')+ur.toFixed(1);$('upRoi').style.color=ur>=0?'#10b981':'#ef4444';
  $('dnRoi').textContent='%'+(dr>=0?'+':'')+dr.toFixed(1);$('dnRoi').style.color=dr>=0?'#10b981':'#ef4444';

  const wp=d.worst_pnl||0,bp=d.best_pnl||0;
  $('worstPnl').textContent='$'+(wp>=0?'+':'')+wp.toFixed(2);$('worstPnl').style.color=wp>=0?'#10b981':'#ef4444';
  $('bestPnl').textContent='$'+(bp>=0?'+':'')+bp.toFixed(2);$('bestPnl').style.color=bp>=0?'#10b981':'#ef4444';
  $('totalCost').textContent=(d.total_cost||0).toFixed(2);
  $('totalRev').textContent=(d.total_revenue||0).toFixed(2);

  const upSh=d.up_shares||0,dnSh=d.down_shares||0,tc=d.total_cost||0,trev=d.total_revenue||0;
  $('upPayout').textContent=upSh.toFixed(0);$('upPayAmt').textContent=(upSh+trev).toFixed(0);
  $('upPayAmt2').textContent=(upSh+trev).toFixed(0);$('upTotCost').textContent=tc.toFixed(0);
  $('dnPayout').textContent=dnSh.toFixed(0);$('dnPayAmt').textContent=(dnSh+trev).toFixed(0);
  $('dnPayAmt2').textContent=(dnSh+trev).toFixed(0);$('dnTotCost').textContent=tc.toFixed(0);

  const cp=d.cumulative_pnl||0;
  $('cumPnl').textContent='$'+(cp>=0?'+':'')+cp.toFixed(2);$('cumPnl').style.color=cp>=0?'#10b981':'#ef4444';

  const lp=d.loss_pct||0;
  $('lossLbl').textContent='%'+lp.toFixed(1)+' / %7';
  const lf=$('lossFill');lf.style.width=Math.min(100,lp/7*100)+'%';
  lf.className='block h-full rounded-full transition-all '+(lp>5?'bg-red-500':lp>3?'bg-amber-500':'bg-emerald-500');

  // Positions
  $('upSh').textContent=upSh.toFixed(0);$('dnSh').textContent=dnSh.toFixed(0);
  $('upCost').textContent=(d.up_cost||0).toFixed(2);$('dnCost').textContent=(d.down_cost||0).toFixed(2);
  $('upRev').textContent=(d.up_revenue||0).toFixed(2);$('dnRev').textContent=(d.down_revenue||0).toFixed(2);
  $('upAvg').textContent=d.up_avg_buy?(d.up_avg_buy*100).toFixed(1):'0';
  $('dnAvg').textContent=d.down_avg_buy?(d.down_avg_buy*100).toFixed(1):'0';
  $('upCurVal').textContent=(d.up_current_value||0).toFixed(2);
  $('dnCurVal').textContent=(d.dn_current_value||0).toFixed(2);
  $('upMaxPay').textContent=(d.up_max_payout||0).toFixed(0);
  $('dnMaxPay').textContent=(d.dn_max_payout||0).toFixed(0);
  $('totalSh').textContent=(upSh+dnSh).toFixed(0);
  $('totalCostPos').textContent=(d.total_cost||0).toFixed(2);
  $('totalValPos').textContent=((d.up_current_value||0)+(d.dn_current_value||0)).toFixed(2);
  $('upMktPrice').textContent=d.up_price?(d.up_price*100).toFixed(1):'—';
  $('dnMktPrice').textContent=d.down_price?(d.down_price*100).toFixed(1):'—';
  $('upBought').textContent=(d.up_bought||0).toFixed(0);$('upSold').textContent=(d.up_sold||0).toFixed(0);
  $('dnBought').textContent=(d.down_bought||0).toFixed(0);$('dnSold').textContent=(d.down_sold||0).toFixed(0);

  // Referans + Timer
  const btc=d.btc_price;
  $('btcNow').textContent=btc&&btc.current?'$'+btc.current.toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
  $('btcPtb').textContent=btc&&btc.price_to_beat?'$'+btc.price_to_beat.toLocaleString('tr-TR',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';
  $('upMid').textContent=d.up_price?'\u00A2'+(d.up_price*100).toFixed(1):'—';
  $('dnMid').textContent=d.down_price?'\u00A2'+(d.down_price*100).toFixed(1):'—';
  const tr=d.time_remaining;
  if(tr!=null&&tr>0){const m=Math.floor(tr/60),s=Math.floor(tr%60);$('timer').textContent=m+':'+String(s).padStart(2,'0');$('timer').style.color=tr<30?'#ef4444':'#f59e0b'}
  else{$('timer').textContent='—'}

  // Books
  const bk=Object.keys(d.books||{});
  if(bk.length>=1)renderBook($('bU'),d.books[bk[0]]);
  if(bk.length>=2)renderBook($('bD'),d.books[bk[1]]);

  // Log
  const pl=d.process_log;
  if(pl&&pl.length){const el=$('logPanel');el.innerHTML=pl.map(l=>{
    const c=l.includes('HATA')?'text-red-400':l.includes('DOLUM')||l.includes('SONUC')?'text-emerald-400':l.includes('ALIS')||l.includes('SATIS')?'text-cyan-400':'text-slate-400';
    return '<div class="'+c+'">'+l+'</div>'}).join('');el.scrollTop=el.scrollHeight}

  renderHistory(d);

  // Signal
  const sig=d.signal||{};const ss=d.signal_state||'NEUTRAL';
  const se=$('sigState');
  if(ss==='STRONG_UP'){se.textContent='UP';se.className='px-3 py-1 rounded-full text-[11px] font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'}
  else if(ss==='STRONG_DOWN'){se.textContent='DOWN';se.className='px-3 py-1 rounded-full text-[11px] font-bold bg-red-500/20 text-red-400 border border-red-500/30'}
  else{se.textContent='NEUTRAL';se.className='px-3 py-1 rounded-full text-[11px] font-bold bg-slate-700 text-slate-300'}
  const sp=sig.spread_pct||0;
  $('sigSpread').textContent='%'+sp.toFixed(1);$('sigSpread').style.color=sp>=15?'#f59e0b':sp>=10?'#94a3b8':'#64748b';
  const sc=sig.confidence||0;
  $('sigConf').textContent='%'+(sc*100).toFixed(0);
  const scb=$('sigConfBar');scb.style.width=(sc*100)+'%';
  scb.className='h-full rounded-full transition-all '+(sc>0.6?'bg-amber-400':sc>0.3?'bg-blue-400':'bg-slate-500');
  $('sigChecks').textContent=sig.checks||0;
  $('sigFired').textContent=sig.signals_fired||0;
  $('sigPhase').textContent=(d.current_phase||1)+'/10';
  $('sigHedge').textContent=sig.hedges||0;
  $('sigRapid').className=sig.rapid_move?'px-2 py-0.5 bg-red-500/20 text-red-400 border border-red-500/30 rounded text-[10px] font-bold':'hidden';

  // Faz rolu badge
  const pr=d.phase_role||'AL';
  const pe=$('phaseRole');
  if(pr==='IZLE'){pe.textContent='IZLE';pe.className='px-3 py-1 rounded-full text-[11px] font-bold bg-blue-500/20 text-blue-400 border border-blue-500/30'}
  else if(pr==='BEKLE'){pe.textContent='BEKLE';pe.className='px-3 py-1 rounded-full text-[11px] font-bold bg-amber-500/20 text-amber-400 border border-amber-500/30'}
  else{pe.textContent='AL';pe.className='px-3 py-1 rounded-full text-[11px] font-bold bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'}

  // Volatilite
  const vl=d.vol_label||'MID';
  const ve=$('volLabel');
  ve.textContent=vl;
  ve.style.color=vl==='LOW'?'#10b981':vl==='HIGH'?'#ef4444':'#94a3b8';

  // Referans farki / orderbook skoru
  const btcC=d.btc_current||0,btcP=d.btc_ptb||0;
  const btcDiff=btcC-btcP;const btcPct=d.btc_change_pct||0;const btcDir=d.btc_direction||0;
  $('sigBtcNow').textContent=btcC>0?'$'+btcC.toLocaleString('en-US',{maximumFractionDigits:2}):'—';
  $('sigBtcPtb').textContent=btcP>0?'$'+btcP.toLocaleString('en-US',{maximumFractionDigits:2}):'—';
  const diffEl=$('sigBtcDiff');diffEl.textContent=(btcDiff>=0?'+':'')+btcDiff.toFixed(2);
  diffEl.style.color=btcDiff>=0?'#10b981':'#ef4444';
  const pctEl=$('sigBtcPct');pctEl.textContent='%'+(btcPct>=0?'+':'')+btcPct.toFixed(3);
  pctEl.style.color=btcPct>=0?'#10b981':'#ef4444';
  $('sigBtcDir').textContent=btcDir.toFixed(2);$('sigBtcDir').style.color=btcDir>0.3?'#10b981':btcDir<-0.3?'#ef4444':'#94a3b8';
  const bl=$('sigBtcLabel');
  if(btcDir>0.6){bl.textContent='STRONG UP';bl.className='px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-400'}
  else if(btcDir>0.2){bl.textContent='UP';bl.className='px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 text-emerald-400/70'}
  else if(btcDir>-0.2){bl.textContent='NEUTRAL';bl.className='px-2 py-0.5 rounded text-[10px] font-bold bg-slate-700 text-slate-400'}
  else if(btcDir>-0.6){bl.textContent='DOWN';bl.className='px-2 py-0.5 rounded text-[10px] font-bold bg-red-500/10 text-red-400/70'}
  else{bl.textContent='STRONG DN';bl.className='px-2 py-0.5 rounded text-[10px] font-bold bg-red-500/20 text-red-400'}

  // IZLE progress
  const izSam=d.izle_samples||0;const izAvg=d.izle_avg||0;const izSig=d.izle_signal||'NEUTRAL';const izPhase=d.phase_role||'AL';
  $('izleBar').style.width=Math.min(100,izSam/60*100)+'%';
  $('izleSamples').textContent=izSam+'/60';
  const ia=$('izleAvg');ia.textContent='%'+(izAvg>=0?'+':'')+izAvg.toFixed(4);ia.style.color=izAvg>0.03?'#10b981':izAvg<-0.03?'#ef4444':'#94a3b8';
  const ir=$('izleResult');
  if(izSig==='STRONG_UP'){ir.textContent='UP';ir.className='px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-400'}
  else if(izSig==='STRONG_DOWN'){ir.textContent='DN';ir.className='px-2 py-0.5 rounded text-[10px] font-bold bg-red-500/20 text-red-400'}
  else{ir.textContent=izSam>0?'...':'—';ir.className='px-2 py-0.5 rounded text-[10px] font-bold bg-slate-700 text-slate-400'}
  $('izleRow').style.opacity=izPhase==='IZLE'?'1':'0.5'

  // Fills
  const fills=d.fill_log||[];
  if(fills.length){$('fillCount').textContent=fills.length+' fill';
    const rev=[...fills].reverse();
    $('fillPanel').innerHTML=rev.map(f=>{
      const isBuy=f.side==='ALIS';const c=isBuy?'text-emerald-400':'text-red-400';const sl=isBuy?'BUY':'SELL';
      const t=new Date(f.ts*1000).toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      return '<div class="'+c+'">'+t+' '+f.token+' '+sl+' '+f.shares.toFixed(0)+'sh @\u00A2'+(f.price*100).toFixed(0)+' = $'+f.cost.toFixed(2)+'</div>'}).join('')}
}

function renderBook(el,bk){
  if(!bk){el.innerHTML='<div class="text-center text-slate-500 py-5 text-xs">—</div>';return}
  const asks=(bk.asks_top5||[]).slice().reverse(),bids=bk.bids_top5||[];
  const mx=Math.max(...asks.map(a=>a.s),...bids.map(b=>b.s),1);
  let h='<div class="flex justify-between px-2 text-[10px] uppercase tracking-wider text-slate-500 mb-1"><span>Ask</span><span>'+(bk.ask_vol||0)+'</span></div>';
  asks.forEach((a,i)=>{const op=0.4+((asks.length-i)/asks.length)*0.6;
    h+='<div class="flex items-center px-2 py-0.5 rounded font-mono text-[11px]" style="opacity:'+op+'"><span class="w-12 font-semibold text-red-400">\u00A2'+(a.p*100).toFixed(1)+'</span><span class="w-10 text-right text-slate-500 text-[10px]">'+a.s.toFixed(0)+'</span><div class="flex-1 h-2.5 ml-2 rounded-sm book-bar-ask" style="width:'+(a.s/mx*100).toFixed(0)+'%"></div></div>'});
  h+='<div class="text-center py-1.5 my-1 border-y border-dashed border-brd font-mono text-xs font-semibold text-amber-400">ORT \u00A2'+(bk.mid!=null?(bk.mid*100).toFixed(1):'—')+'<span class="text-[10px] text-slate-500 font-normal ml-2">Makas '+(bk.spread!=null?(bk.spread*100).toFixed(1)+'\u00A2':'—')+'</span></div>';
  bids.forEach((b,i)=>{const op=1.0-(i/bids.length)*0.6;
    h+='<div class="flex items-center px-2 py-0.5 rounded font-mono text-[11px]" style="opacity:'+op+'"><span class="w-12 font-semibold text-emerald-400">\u00A2'+(b.p*100).toFixed(1)+'</span><span class="w-10 text-right text-slate-500 text-[10px]">'+b.s.toFixed(0)+'</span><div class="flex-1 h-2.5 ml-2 rounded-sm book-bar-bid" style="width:'+(b.s/mx*100).toFixed(0)+'%"></div></div>'});
  h+='<div class="flex justify-between px-2 text-[10px] uppercase tracking-wider text-slate-500 mt-1"><span>Bid</span><span>'+(bk.bid_vol||0)+'</span></div>';
  el.innerHTML=h;
}

function renderHistory(d){
  const hist=d.market_history||[];
  if(!hist.length)return;
  $('histCount').textContent=hist.length+' market';
  let h='<table class="w-full font-mono text-[11px]"><thead><tr class="text-[10px] uppercase tracking-wider text-slate-500 font-sans font-semibold">';
  h+='<th class="text-left pb-2">Saat</th><th class="text-center pb-2">Kzn</th>';
  h+='<th class="text-right pb-2">UP</th><th class="text-right pb-2">DN</th><th class="text-right pb-2">Odeme</th><th class="text-right pb-2">Maliyet</th><th class="text-right pb-2">K/Z</th></tr></thead><tbody class="border-t border-brd">';
  const rev=[...hist].reverse();
  rev.forEach(m=>{
    const pnl=m.resolved_pnl||0;const pc=pnl>=0?'text-emerald-400':'text-red-400';
    const wc=m.winner==='UP'?'text-emerald-400':'text-red-400';const wl=m.winner==='UP'?'YKR':'ASG';
    const t=m.ts?new Date(m.ts*1000).toLocaleTimeString('tr-TR',{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'—';
    h+='<tr class="border-t border-white/[.03]">';
    h+='<td class="py-1.5 text-left text-cyan-400">'+t+'</td>';
    h+='<td class="py-1.5 text-center font-bold '+wc+'">'+wl+'</td>';
    h+='<td class="py-1.5 text-right text-emerald-400/70">'+(m.up_shares||0).toFixed(0)+'</td>';
    h+='<td class="py-1.5 text-right text-red-400/70">'+(m.down_shares||0).toFixed(0)+'</td>';
    h+='<td class="py-1.5 text-right">$'+(m.payout||0).toFixed(0)+'</td>';
    h+='<td class="py-1.5 text-right">$'+(m.total_cost||0).toFixed(0)+'</td>';
    h+='<td class="py-1.5 text-right font-bold '+pc+'">$'+(pnl>=0?'+':'')+pnl.toFixed(2)+'</td>';
    h+='</tr>'});
  h+='</tbody></table>';$('histPanel').innerHTML=h;
}

// Budget localStorage
const savedBudget=localStorage.getItem('polysignal_budget');
if(savedBudget)$('inBudget').value=savedBudget;
$('inBudget').addEventListener('change',()=>{
  const v=+$('inBudget').value;
  localStorage.setItem('polysignal_budget',v);
  sendCmd({action:'set_budget',budget:v});
});
conn();
</script>
</body>
</html>"""
