"""Signal threshold backtest — %5, %10, %15, %20, %25 karsilastirmasi.

Senaryo: 100 market, her markette UP/DOWN fiyatlari rastgele hareket eder.
Sinyal threshold'u asildiranda guclu tarafa agresif girilir, zayif kapatilir.
Threshold ne kadar dusukse o kadar erken sinyal alir ama false positive riski artar.
"""

import random
import statistics

random.seed(42)

NUM_SIMS = 5_000
MARKETS = 100
BUDGET = 100.0

THRESHOLDS = [5, 8, 10, 12, 15, 18, 20, 25]

# Market parametreleri
SPREAD_AVAILABLE_PCT = 0.42
FILL_RATE_MEAN = 0.72
FILL_RATE_STD = 0.15
PAIR_COST_MEAN = 0.965
PAIR_COST_STD = 0.012


def simulate_market_with_signal(budget, threshold_pct):
    """Tek market: grid + sinyal sistemi simülasyonu."""

    has_spread = random.random() < SPREAD_AVAILABLE_PCT
    if not has_spread:
        return {"pnl": 0, "traded": False, "signal_fired": False}

    pair_cost = max(0.85, min(0.99, random.gauss(PAIR_COST_MEAN, PAIR_COST_STD)))
    spread = 1.0 - pair_cost
    spread_pct = spread / pair_cost * 100

    if spread_pct < 3.0:
        return {"pnl": 0, "traded": False, "signal_fired": False}

    fill_rate = max(0.2, min(1.0, random.gauss(FILL_RATE_MEAN, FILL_RATE_STD)))

    # Baslangic: 50-50 grid emirleri
    up_ratio = 0.5
    dn_ratio = 0.5

    # Market ici fiyat hareketi simülasyonu
    # UP ve DOWN fiyatlari bagimsiz hareket eder
    up_price = pair_cost * random.uniform(0.45, 0.55)
    dn_price = pair_cost - up_price

    # Zaman icinde fiyat degisimi (5 dakika = 300 saniye, 5x/s = 1500 kontrol)
    # Rastgele walk ile fiyat ayrismasi
    volatility = random.uniform(0.001, 0.008)
    max_divergence = 0
    signal_fired = False
    signal_direction = None  # "UP" or "DOWN"
    signal_time_ratio = 1.0  # sinyalden sonra kalan zaman orani

    for tick in range(300):  # 300 saniye
        up_price += random.gauss(0, volatility)
        dn_price += random.gauss(0, volatility)
        up_price = max(0.05, min(0.95, up_price))
        dn_price = max(0.05, min(0.95, dn_price))

        if min(up_price, dn_price) > 0:
            divergence = abs(up_price - dn_price) / min(up_price, dn_price) * 100
        else:
            divergence = 0

        max_divergence = max(max_divergence, divergence)

        if not signal_fired and divergence >= threshold_pct:
            signal_fired = True
            signal_direction = "UP" if up_price > dn_price else "DOWN"
            signal_time_ratio = (300 - tick) / 300  # kalan zaman orani

    effective_budget = budget * fill_rate

    if signal_fired:
        # Sinyal sonrasi: agresif tarafa yonelme
        # Zayif tarafin %70'ini iptal et, gucluye aktar
        # Agresif gecis sinyalin zamanlmasina bagli
        if signal_direction == "UP":
            up_ratio = 0.7 + 0.1 * signal_time_ratio  # %70-80
            dn_ratio = 1.0 - up_ratio
        else:
            dn_ratio = 0.7 + 0.1 * signal_time_ratio
            up_ratio = 1.0 - dn_ratio

        # False positive riski: dusuk threshold → daha sik yanlis sinyal
        # Gercek yonle eslesmeme olasiligi
        false_positive_rate = max(0, 0.5 - threshold_pct / 100)  # %5 th → %45 FP, %25 th → %25 FP
        if random.random() < false_positive_rate:
            # Yanlis yone gitmis — swap
            up_ratio, dn_ratio = dn_ratio, up_ratio

    up_alloc = effective_budget * up_ratio
    dn_alloc = effective_budget * dn_ratio

    up_bid = pair_cost * random.uniform(0.45, 0.55)
    dn_bid = pair_cost - up_bid

    up_shares = up_alloc / max(up_bid, 0.01)
    dn_shares = dn_alloc / max(dn_bid, 0.01)

    # Dengesizlik
    imbalance = max(0, random.gauss(0.03, 0.02))
    if random.random() < 0.5:
        up_shares *= (1 - imbalance)
    else:
        dn_shares *= (1 - imbalance)

    total_cost = up_shares * up_bid + dn_shares * dn_bid

    # Resolution
    winner_is_up = random.random() < 0.5
    payout = up_shares if winner_is_up else dn_shares
    pnl = payout - total_cost

    return {
        "pnl": pnl,
        "traded": True,
        "signal_fired": signal_fired,
        "signal_direction": signal_direction,
        "false_positive_rate": false_positive_rate if signal_fired else 0,
    }


def run():
    print("=" * 70)
    print("  SIGNAL THRESHOLD BACKTEST — %5 → %25 karsilastirmasi")
    print(f"  {NUM_SIMS:,} sim x {MARKETS} market | Butce: ${BUDGET}")
    print("=" * 70)

    header = f"{'Threshold':>10} | {'Ort K/Z':>10} | {'Medyan':>10} | {'%95 VaR':>10} | {'Win%':>6} | {'Sinyal%':>8} | {'Sharpe':>7}"
    print(f"\n{header}")
    print("─" * len(header))

    best_threshold = 0
    best_pnl = -999999

    for threshold in THRESHOLDS:
        sim_pnls = []
        sim_win_rates = []
        sim_signal_rates = []

        for _ in range(NUM_SIMS):
            total_pnl = 0
            wins = 0
            traded = 0
            signals = 0

            for _ in range(MARKETS):
                r = simulate_market_with_signal(BUDGET, threshold)
                total_pnl += r["pnl"]
                if r["traded"]:
                    traded += 1
                    if r["pnl"] > 0:
                        wins += 1
                    if r.get("signal_fired"):
                        signals += 1

            sim_pnls.append(total_pnl)
            sim_win_rates.append(wins / traded * 100 if traded > 0 else 0)
            sim_signal_rates.append(signals / traded * 100 if traded > 0 else 0)

        mean_pnl = statistics.mean(sim_pnls)
        median_pnl = statistics.median(sim_pnls)
        std_pnl = statistics.stdev(sim_pnls) if len(sim_pnls) > 1 else 0
        var95 = sorted(sim_pnls)[int(NUM_SIMS * 0.05)]
        win_rate = statistics.mean(sim_win_rates)
        signal_rate = statistics.mean(sim_signal_rates)
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0

        marker = " <-- BEST" if mean_pnl > best_pnl else ""
        if mean_pnl > best_pnl:
            best_pnl = mean_pnl
            best_threshold = threshold

        print(
            f"  %{threshold:>7} | ${mean_pnl:>+9.2f} | ${median_pnl:>+9.2f} | "
            f"${var95:>+9.2f} | {win_rate:>5.1f} | {signal_rate:>7.1f} | "
            f"{sharpe:>6.2f}{marker}"
        )

    print(f"\n{'='*70}")
    print(f"  EN IYI THRESHOLD: %{best_threshold} — Ort K/Z: ${best_pnl:+.2f}")
    print(f"{'='*70}")

    # Detaylı analiz: en iyi threshold
    print(f"\n  DETAYLI ANALIZ: %{best_threshold} threshold")
    print(f"{'─'*70}")

    detail_pnls = []
    for _ in range(10_000):
        total = 0
        for _ in range(MARKETS):
            r = simulate_market_with_signal(BUDGET, best_threshold)
            total += r["pnl"]
        detail_pnls.append(total)

    profitable = sum(1 for p in detail_pnls if p > 0)
    sorted_pnls = sorted(detail_pnls)

    print(f"  Karli olma olasiligi:     %{profitable/len(detail_pnls)*100:.1f}")
    print(f"  Ortalama K/Z:             ${statistics.mean(detail_pnls):+.2f}")
    print(f"  Medyan K/Z:               ${statistics.median(detail_pnls):+.2f}")
    print(f"  Std sapma:                ${statistics.stdev(detail_pnls):.2f}")
    print(f"  %5 percentil (VaR):       ${sorted_pnls[int(len(detail_pnls)*0.05)]:+.2f}")
    print(f"  %95 percentil:            ${sorted_pnls[int(len(detail_pnls)*0.95)]:+.2f}")
    print(f"  En kotu:                  ${min(detail_pnls):+.2f}")
    print(f"  En iyi:                   ${max(detail_pnls):+.2f}")


if __name__ == "__main__":
    run()
