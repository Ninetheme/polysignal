"""Market geçmişini arsiv.html olarak oluşturur."""

import json
import os
import pathlib
from datetime import datetime

BASE = pathlib.Path(__file__).resolve().parent.parent
HISTORY_FILE = BASE / "data" / "market_history.json"
ARCHIVE_HTML = BASE / "arsiv.html"


def generate():
    if not HISTORY_FILE.exists():
        print("market_history.json bulunamadi")
        return

    with open(HISTORY_FILE) as f:
        history = json.load(f)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_pnl = sum(h.get("resolved_pnl", 0) for h in history)
    total_cost = sum(h.get("total_cost", 0) for h in history)
    wins = sum(1 for h in history if h.get("resolved_pnl", 0) > 0)
    losses = len(history) - wins
    win_rate = (wins / len(history) * 100) if history else 0

    rows = ""
    cumulative = 0.0
    for i, m in enumerate(history, 1):
        ts = m.get("ts", 0)
        dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "—"
        date_only = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "—"
        time_only = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"
        question = m.get("question", "")
        winner = m.get("winner", "")
        w_cls = "up" if winner == "UP" else "dn"
        w_label = "YKR" if winner == "UP" else "ASG"
        pnl = m.get("resolved_pnl", 0)
        cumulative += pnl
        pnl_cls = "pos" if pnl >= 0 else "neg"
        cum_cls = "pos" if cumulative >= 0 else "neg"
        cost = m.get("total_cost", 0)
        payout = m.get("payout", 0)
        up_sh = m.get("up_shares", 0)
        dn_sh = m.get("down_shares", 0)
        paired = m.get("paired", 0)
        fills = m.get("fills", 0)
        btc = m.get("btc_close", 0)
        ptb = m.get("ptb", 0)
        roi = (pnl / cost * 100) if cost > 0 else 0

        rows += f"""      <tr>
        <td>{i}</td>
        <td>{date_only}</td>
        <td>{time_only}</td>
        <td class="{w_cls}">{w_label}</td>
        <td>${btc:,.2f}</td>
        <td>${ptb:,.2f}</td>
        <td>{up_sh:.0f}</td>
        <td>{dn_sh:.0f}</td>
        <td>{paired:.0f}</td>
        <td>${cost:.2f}</td>
        <td>${payout:.2f}</td>
        <td class="{pnl_cls}">${pnl:+.2f}</td>
        <td>{roi:+.1f}%</td>
        <td class="{cum_cls}">${cumulative:+.2f}</td>
        <td>{fills}</td>
      </tr>
"""

    html = f"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PolySignal Arsiv</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #080c14;
  --card: #0f1522;
  --surface: #161e2e;
  --border: #1c2840;
  --t1: #f1f5f9;
  --t2: #94a3b8;
  --t3: #5a6a82;
  --green: #10b981;
  --red: #ef4444;
  --cyan: #06b6d4;
  --yellow: #f59e0b;
  --sans: 'Inter', -apple-system, system-ui, sans-serif;
  --mono: 'JetBrains Mono', 'SF Mono', monospace;
}}
*, *::before, *::after {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: var(--sans);
  background: var(--bg);
  color: var(--t1);
  font-size: 14px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  padding: 24px;
}}
.header {{
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 24px;
  flex-wrap: wrap;
  gap: 16px;
}}
.header h1 {{
  font-size: 22px;
  font-weight: 800;
  letter-spacing: -0.02em;
}}
.header h1 em {{ color: var(--cyan); font-style: normal; }}
.header .updated {{
  font-size: 12px;
  color: var(--t3);
}}
.stats {{
  display: flex;
  gap: 24px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}}
.stat {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 20px;
  text-align: center;
  min-width: 120px;
}}
.stat-label {{
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--t3);
  margin-bottom: 4px;
}}
.stat-value {{
  font-family: var(--mono);
  font-size: 20px;
  font-weight: 700;
}}
.pos {{ color: var(--green); }}
.neg {{ color: var(--red); }}
.up {{ color: var(--green); font-weight: 700; }}
.dn {{ color: var(--red); font-weight: 700; }}
.table-wrap {{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
}}
table {{
  width: 100%;
  border-collapse: collapse;
  font-family: var(--mono);
  font-size: 12px;
}}
thead th {{
  position: sticky;
  top: 0;
  background: var(--surface);
  font-family: var(--sans);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--t3);
  padding: 10px 10px;
  text-align: right;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}}
thead th:nth-child(1),
thead th:nth-child(2),
thead th:nth-child(3),
thead th:nth-child(4) {{ text-align: left; }}
tbody td {{
  padding: 8px 10px;
  text-align: right;
  border-bottom: 1px solid rgba(255,255,255,.03);
}}
tbody td:nth-child(1),
tbody td:nth-child(2),
tbody td:nth-child(3),
tbody td:nth-child(4) {{ text-align: left; }}
tbody tr:hover {{ background: rgba(255,255,255,.02); }}
tbody td:nth-child(1) {{ color: var(--t3); font-size: 11px; }}
tbody td:nth-child(2) {{ color: var(--cyan); }}
tbody td:nth-child(3) {{ color: var(--t2); }}
</style>
</head>
<body>

<div class="header">
  <h1><em>&#9670;</em> Poly<em>Hard</em> — Arsiv</h1>
  <span class="updated">Son guncelleme: {now}</span>
</div>

<div class="stats">
  <div class="stat">
    <div class="stat-label">Toplam K/Z</div>
    <div class="stat-value {'pos' if total_pnl >= 0 else 'neg'}">${total_pnl:+.2f}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Market Sayisi</div>
    <div class="stat-value" style="color:var(--cyan)">{len(history)}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Kazanma</div>
    <div class="stat-value pos">{wins}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Kaybetme</div>
    <div class="stat-value neg">{losses}</div>
  </div>
  <div class="stat">
    <div class="stat-label">Win Rate</div>
    <div class="stat-value" style="color:var(--yellow)">{win_rate:.0f}%</div>
  </div>
  <div class="stat">
    <div class="stat-label">Toplam Maliyet</div>
    <div class="stat-value" style="color:var(--t2)">${total_cost:,.0f}</div>
  </div>
</div>

<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Tarih</th>
        <th>Saat</th>
        <th>Kazanan</th>
        <th>BTC Kapanis</th>
        <th>PTB</th>
        <th>UP Sh</th>
        <th>DN Sh</th>
        <th>Cift</th>
        <th>Maliyet</th>
        <th>Odeme</th>
        <th>K/Z</th>
        <th>ROI</th>
        <th>Kumulatif</th>
        <th>Fills</th>
      </tr>
    </thead>
    <tbody>
{rows}    </tbody>
  </table>
</div>

</body>
</html>"""

    with open(ARCHIVE_HTML, "w") as f:
        f.write(html)

    print(f"arsiv.html olusturuldu: {len(history)} market, K/Z: ${total_pnl:+.2f}")


if __name__ == "__main__":
    generate()
