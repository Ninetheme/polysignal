"""Gerçek market_history.json verisiyle backtest analizi."""

import json
import pathlib
import statistics
from datetime import datetime

BASE = pathlib.Path(__file__).resolve().parent.parent
HISTORY_FILE = BASE / "data" / "market_history.json"


def run():
    with open(HISTORY_FILE) as f:
        history = json.load(f)

    if not history:
        print("Veri yok!")
        return

    print("=" * 60)
    print("  POLYSIGNAL — GERCEK VERI BACKTEST")
    print(f"  {len(history)} market | {HISTORY_FILE.name}")
    print("=" * 60)

    pnls = [h.get("resolved_pnl", 0) for h in history]
    costs = [h.get("total_cost", 0) for h in history]
    payouts = [h.get("payout", 0) for h in history]
    fills_list = [h.get("fills", 0) for h in history]
    paired_list = [h.get("paired", 0) for h in history]

    total_pnl = sum(pnls)
    total_cost = sum(costs)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    zero = sum(1 for p in pnls if p == 0)
    win_rate = wins / len(history) * 100

    # Zaman aralığı
    ts_start = history[0].get("ts", 0)
    ts_end = history[-1].get("ts", 0)
    dt_start = datetime.fromtimestamp(ts_start).strftime("%Y-%m-%d %H:%M") if ts_start else "?"
    dt_end = datetime.fromtimestamp(ts_end).strftime("%Y-%m-%d %H:%M") if ts_end else "?"
    duration_h = (ts_end - ts_start) / 3600 if ts_end > ts_start else 0

    print(f"\n  Tarih araligi: {dt_start} — {dt_end}")
    print(f"  Sure: {duration_h:.1f} saat")

    print(f"\n{'─'*60}")
    print(f"  KARLILILIK")
    print(f"{'─'*60}")
    print(f"  Toplam K/Z:               ${total_pnl:+.2f}")
    print(f"  Toplam maliyet:           ${total_cost:,.2f}")
    print(f"  Toplam ROI:               %{total_pnl/total_cost*100:+.2f}" if total_cost > 0 else "  ROI: N/A")
    print(f"\n  Ortalama K/Z/market:      ${statistics.mean(pnls):+.2f}")
    print(f"  Medyan K/Z/market:        ${statistics.median(pnls):+.2f}")
    if len(pnls) > 1:
        print(f"  Std sapma:                ${statistics.stdev(pnls):.2f}")

    print(f"\n  En iyi market:            ${max(pnls):+.2f}")
    print(f"  En kotu market:           ${min(pnls):+.2f}")

    print(f"\n{'─'*60}")
    print(f"  KAZANMA ANALIZI")
    print(f"{'─'*60}")
    print(f"  Kazanma:                  {wins} / {len(history)} (%{win_rate:.0f})")
    print(f"  Kaybetme:                 {losses}")
    print(f"  Sifir:                    {zero}")

    # Kazanç/kayıp büyüklüğü
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]
    if win_pnls:
        print(f"\n  Ort kazanc:               ${statistics.mean(win_pnls):+.2f}")
    if loss_pnls:
        print(f"  Ort kayip:                ${statistics.mean(loss_pnls):+.2f}")
    if win_pnls and loss_pnls:
        ratio = statistics.mean(win_pnls) / abs(statistics.mean(loss_pnls))
        print(f"  Kazanc/Kayip orani:       {ratio:.2f}x")

    print(f"\n{'─'*60}")
    print(f"  ISLEM ISTATISTIKLERI")
    print(f"{'─'*60}")
    print(f"  Toplam fill:              {sum(fills_list)}")
    print(f"  Ort fill/market:          {statistics.mean(fills_list):.0f}")
    print(f"  Ort paired shares:        {statistics.mean(paired_list):.0f}")
    print(f"  Ort maliyet/market:       ${statistics.mean(costs):.2f}")

    # İşlem yapılmayan marketler (cost ≈ 0)
    no_trade = sum(1 for c in costs if c < 1)
    print(f"  Islem yapilmayan:         {no_trade} / {len(history)}")

    print(f"\n{'─'*60}")
    print(f"  DRAWDOWN ANALIZI")
    print(f"{'─'*60}")

    # Kümülatif PnL ve drawdown
    cumulative = 0
    peak = 0
    max_dd = 0
    max_dd_market = 0
    for i, p in enumerate(pnls):
        cumulative += p
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
            max_dd_market = i

    print(f"  Max drawdown:             ${max_dd:.2f}")
    print(f"  Drawdown market#:         {max_dd_market + 1}")

    # Ardışık kayıplar
    current_streak = 0
    max_streak = 0
    for p in pnls:
        if p < 0:
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0

    print(f"  Max ardisik kayip:        {max_streak}")

    # Shares dengesizliği analizi
    print(f"\n{'─'*60}")
    print(f"  SHARES DENGESIZLIGI")
    print(f"{'─'*60}")

    imbalances = []
    for h in history:
        up_sh = h.get("up_shares", 0)
        dn_sh = h.get("down_shares", 0)
        mx = max(up_sh, dn_sh)
        if mx > 0:
            imb = abs(up_sh - dn_sh) / mx * 100
            imbalances.append(imb)

    if imbalances:
        print(f"  Ort dengesizlik:          %{statistics.mean(imbalances):.1f}")
        print(f"  Max dengesizlik:          %{max(imbalances):.1f}")

    # Garantili kar vs gerçek kar
    guaranteed_profits = []
    for h in history:
        tc = h.get("total_cost", 0)
        paired = h.get("paired", 0)
        if tc > 0 and paired > 0:
            # Garantili kar = paired * (1 - pair_cost_per_share)
            pair_cost_per = tc / paired if paired > 0 else 1
            gp = paired * (1.0 - pair_cost_per) if pair_cost_per < 1 else 0
            guaranteed_profits.append(gp)

    print(f"\n{'─'*60}")
    print(f"  KAZANAN TARAF ANALIZI")
    print(f"{'─'*60}")
    up_wins = sum(1 for h in history if h.get("winner") == "UP")
    dn_wins = sum(1 for h in history if h.get("winner") == "DOWN")
    print(f"  UP kazanan:               {up_wins} (%{up_wins/len(history)*100:.0f})")
    print(f"  DOWN kazanan:             {dn_wins} (%{dn_wins/len(history)*100:.0f})")

    # Saatlik performans
    print(f"\n{'─'*60}")
    print(f"  PERFORMANS METRIKLERI")
    print(f"{'─'*60}")
    if duration_h > 0:
        print(f"  K/Z / saat:               ${total_pnl/duration_h:+.2f}")
        print(f"  Market / saat:            {len(history)/duration_h:.1f}")

    # Sharpe
    if len(pnls) > 1:
        sharpe = statistics.mean(pnls) / statistics.stdev(pnls) if statistics.stdev(pnls) > 0 else 0
        print(f"  Sharpe (per market):      {sharpe:.2f}")

    print(f"\n{'='*60}")
    if total_pnl > 0:
        print(f"  SONUC: KARLI — ${total_pnl:+.2f} ({len(history)} market, %{win_rate:.0f} win rate)")
    else:
        print(f"  SONUC: ZARARDA — ${total_pnl:+.2f} ({len(history)} market)")
    print(f"{'='*60}")


if __name__ == "__main__":
    run()
