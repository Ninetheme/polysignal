"""100 market simülasyonu — gerçek orderbook dinamikleriyle kar olasılığı analizi.

Parametreler gerçek Polymarket BTC 5M marketlerinden alınmıştır:
- Bid fiyatları: UP ¢40-60, DOWN ¢40-60 (toplam ¢90-100 arası)
- Spread >= %3 olasılığı: ~%40 (marketlerin ~%40'ında spread yeterli)
- Fill oranı: %60-90 (maker emirlerin dolma olasılığı)
- Shares dengesizliği: %0-15 (UP/DOWN shares farkı)
"""

import random
import statistics

random.seed(42)

NUM_SIMS = 10_000       # Monte Carlo tekrar sayısı
MARKETS_PER_SIM = 100   # Her simülasyonda 100 market
BUDGET = 100.0          # Market başına bütçe ($)
MIN_PROFIT_PCT = 3.0    # Minimum spread %3
MIN_ORDER_SIZE = 5      # Polymarket minimum

# Gerçek market parametreleri (data/market_history.json'dan türetilmiş)
SPREAD_AVAILABLE_PCT = 0.42    # Marketlerin %42'sinde spread >= %3
FILL_RATE_MEAN = 0.72          # Ortalama fill oranı
FILL_RATE_STD = 0.15           # Fill oranı std
PAIR_COST_MEAN = 0.965         # Ortalama pair cost (up_bid + dn_bid)
PAIR_COST_STD = 0.012          # Pair cost std
IMBALANCE_MEAN = 0.05          # Ortalama shares dengesizliği
IMBALANCE_STD = 0.04           # Dengesizlik std


def simulate_market(budget: float) -> dict:
    """Tek bir 5M market simüle et."""

    # Spread var mı?
    has_spread = random.random() < SPREAD_AVAILABLE_PCT
    if not has_spread:
        return {"traded": False, "pnl": 0, "cost": 0, "paired": 0, "spread_pct": 0}

    # Pair cost (UP bid + DOWN bid)
    pair_cost = max(0.85, min(0.99, random.gauss(PAIR_COST_MEAN, PAIR_COST_STD)))
    spread = 1.0 - pair_cost
    spread_pct = spread / pair_cost * 100

    if spread_pct < MIN_PROFIT_PCT:
        return {"traded": False, "pnl": 0, "cost": 0, "paired": 0, "spread_pct": spread_pct}

    # UP/DOWN bid dağılımı
    up_ratio = random.uniform(0.4, 0.6)
    up_bid = pair_cost * up_ratio
    dn_bid = pair_cost * (1 - up_ratio)

    # Fill oranı (kaçı dolacak)
    fill_rate = max(0.2, min(1.0, random.gauss(FILL_RATE_MEAN, FILL_RATE_STD)))
    effective_budget = budget * fill_rate

    # Bütçeyi fiyat oranına göre dağıt (eşit share için)
    total_bid = up_bid + dn_bid
    up_alloc = effective_budget * (up_bid / total_bid)
    dn_alloc = effective_budget * (dn_bid / total_bid)

    up_shares = up_alloc / up_bid
    dn_shares = dn_alloc / dn_bid

    # Shares dengesizliği (gerçek dünyada mükemmel eşitlik yok)
    imbalance = max(0, random.gauss(IMBALANCE_MEAN, IMBALANCE_STD))
    if random.random() < 0.5:
        up_shares *= (1 - imbalance)
    else:
        dn_shares *= (1 - imbalance)

    up_cost = up_shares * up_bid
    dn_cost = dn_shares * dn_bid
    total_cost = up_cost + dn_cost
    paired = min(up_shares, dn_shares)

    # Market resolution: %50 UP, %50 DOWN (rastgele)
    winner_is_up = random.random() < 0.5

    if winner_is_up:
        payout = up_shares * 1.0  # UP shares × $1
    else:
        payout = dn_shares * 1.0  # DOWN shares × $1

    pnl = payout - total_cost

    return {
        "traded": True,
        "pnl": pnl,
        "cost": total_cost,
        "paired": paired,
        "spread_pct": spread_pct,
        "up_shares": up_shares,
        "dn_shares": dn_shares,
        "imbalance_pct": abs(up_shares - dn_shares) / max(up_shares, dn_shares) * 100 if max(up_shares, dn_shares) > 0 else 0,
        "winner": "UP" if winner_is_up else "DOWN",
        "fill_rate": fill_rate,
    }


def run_simulation():
    """Monte Carlo: NUM_SIMS kez 100 market çalıştır."""
    sim_results = []

    for _ in range(NUM_SIMS):
        total_pnl = 0
        total_cost = 0
        markets_traded = 0
        wins = 0
        losses = 0

        for _ in range(MARKETS_PER_SIM):
            result = simulate_market(BUDGET)
            total_pnl += result["pnl"]
            total_cost += result["cost"]
            if result["traded"]:
                markets_traded += 1
                if result["pnl"] > 0:
                    wins += 1
                else:
                    losses += 1

        roi = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        sim_results.append({
            "total_pnl": total_pnl,
            "total_cost": total_cost,
            "roi": roi,
            "traded": markets_traded,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / markets_traded * 100 if markets_traded > 0 else 0,
        })

    return sim_results


def analyze(results):
    """Sonuçları analiz et."""
    pnls = [r["total_pnl"] for r in results]
    rois = [r["roi"] for r in results]
    win_rates = [r["win_rate"] for r in results]
    traded = [r["traded"] for r in results]

    profitable = sum(1 for p in pnls if p > 0)
    breakeven = sum(1 for p in pnls if p == 0)

    print("=" * 60)
    print("  POLYSIGNAL — 100 MARKET SIMULASYONU")
    print(f"  {NUM_SIMS:,} Monte Carlo iterasyonu")
    print("=" * 60)

    print(f"\n  Butce: ${BUDGET:.0f}/market | Min spread: %{MIN_PROFIT_PCT:.0f}")
    print(f"  Spread bulma olasiligi: %{SPREAD_AVAILABLE_PCT*100:.0f}")
    print(f"  Fill orani: %{FILL_RATE_MEAN*100:.0f} (std %{FILL_RATE_STD*100:.0f})")

    print(f"\n{'─'*60}")
    print(f"  KARLILILIK")
    print(f"{'─'*60}")
    print(f"  Karli olma olasiligi:     %{profitable/NUM_SIMS*100:.1f}")
    print(f"  Zararda olma olasiligi:   %{(NUM_SIMS-profitable-breakeven)/NUM_SIMS*100:.1f}")

    print(f"\n  Ortalama K/Z (100 mkt):   ${statistics.mean(pnls):+.2f}")
    print(f"  Medyan K/Z:               ${statistics.median(pnls):+.2f}")
    print(f"  Std sapma:                ${statistics.stdev(pnls):.2f}")

    print(f"\n  En iyi senaryo:           ${max(pnls):+.2f}")
    print(f"  En kotu senaryo:          ${min(pnls):+.2f}")

    pnls_sorted = sorted(pnls)
    p5 = pnls_sorted[int(NUM_SIMS * 0.05)]
    p25 = pnls_sorted[int(NUM_SIMS * 0.25)]
    p75 = pnls_sorted[int(NUM_SIMS * 0.75)]
    p95 = pnls_sorted[int(NUM_SIMS * 0.95)]
    print(f"\n  %5 percentil (VaR):       ${p5:+.2f}")
    print(f"  %25 percentil:            ${p25:+.2f}")
    print(f"  %75 percentil:            ${p75:+.2f}")
    print(f"  %95 percentil:            ${p95:+.2f}")

    print(f"\n{'─'*60}")
    print(f"  ROI")
    print(f"{'─'*60}")
    print(f"  Ortalama ROI:             %{statistics.mean(rois):+.2f}")
    print(f"  Medyan ROI:               %{statistics.median(rois):+.2f}")

    print(f"\n{'─'*60}")
    print(f"  ISLEM ISTATISTIKLERI")
    print(f"{'─'*60}")
    print(f"  Ort islem yapilan market: {statistics.mean(traded):.0f} / 100")
    print(f"  Ort kazanma orani:        %{statistics.mean(win_rates):.1f}")

    print(f"\n{'─'*60}")
    print(f"  RISK ANALIZI")
    print(f"{'─'*60}")

    # Drawdown analizi
    max_drawdowns = []
    for _ in range(1000):
        cumulative = 0
        peak = 0
        max_dd = 0
        for _ in range(MARKETS_PER_SIM):
            result = simulate_market(BUDGET)
            cumulative += result["pnl"]
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        max_drawdowns.append(max_dd)

    print(f"  Ort max drawdown:         ${statistics.mean(max_drawdowns):.2f}")
    print(f"  %95 max drawdown:         ${sorted(max_drawdowns)[int(len(max_drawdowns)*0.95)]:.2f}")

    # Ardışık kayıp
    consec_losses = []
    for _ in range(1000):
        current_streak = 0
        max_streak = 0
        for _ in range(MARKETS_PER_SIM):
            result = simulate_market(BUDGET)
            if result["traded"] and result["pnl"] < 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            elif result["traded"]:
                current_streak = 0
        consec_losses.append(max_streak)

    print(f"  Ort max ardisik kayip:    {statistics.mean(consec_losses):.0f}")
    print(f"  %95 max ardisik kayip:    {sorted(consec_losses)[int(len(consec_losses)*0.95)]}")

    # Sharpe benzeri oran (günlük varsayım: 100 market ≈ ~8 saat)
    mean_pnl = statistics.mean(pnls)
    std_pnl = statistics.stdev(pnls)
    sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0
    print(f"\n  Sharpe (100mkt/session):  {sharpe:.2f}")

    print(f"\n{'='*60}")

    # Dengesizlik etkisi analizi
    print(f"\n  DENGESIZLIK ETKISI")
    print(f"{'─'*60}")

    # Mükemmel denge vs gerçekçi denge karşılaştırması
    perfect_pnls = []
    for _ in range(5000):
        total = 0
        for _ in range(MARKETS_PER_SIM):
            has_spread = random.random() < SPREAD_AVAILABLE_PCT
            if not has_spread:
                continue
            pair_cost = max(0.85, min(0.99, random.gauss(PAIR_COST_MEAN, PAIR_COST_STD)))
            spread_pct = (1.0 - pair_cost) / pair_cost * 100
            if spread_pct < MIN_PROFIT_PCT:
                continue
            fill_rate = max(0.2, min(1.0, random.gauss(FILL_RATE_MEAN, FILL_RATE_STD)))
            eff = BUDGET * fill_rate
            shares = eff / pair_cost  # mükemmel eşit
            total += shares * (1.0 - pair_cost)  # garantili kar
        perfect_pnls.append(total)

    print(f"  Mukemmel denge K/Z:       ${statistics.mean(perfect_pnls):+.2f}")
    print(f"  Gercekci denge K/Z:       ${statistics.mean(pnls):+.2f}")
    print(f"  Dengesizlik maliyeti:     ${statistics.mean(perfect_pnls) - statistics.mean(pnls):.2f}")

    print(f"\n{'='*60}")
    verdict = "KARLI" if profitable / NUM_SIMS > 0.6 else "RISKLI"
    confidence = profitable / NUM_SIMS * 100
    print(f"  SONUC: {verdict} — %{confidence:.1f} olasilikla karli")
    print(f"{'='*60}")


if __name__ == "__main__":
    results = run_simulation()
    analyze(results)
