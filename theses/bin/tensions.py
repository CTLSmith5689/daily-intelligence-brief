#!/usr/bin/env python3
"""Detect places where a name's own data disagrees with itself.

This does NOT change what gets covered. Slot allocation still runs off the
composite. What this adds is a QUESTION attached to each name.

The distinction matters. A composite of +0.84 is a conclusion with nothing to
investigate, and the natural response to being handed a conclusion is to write
prose justifying it. "98th percentile on quality and 96th on value inside its
own sub-industry" is a question: why is the market discounting a good business?
One of those is the start of a variant perception and the other is a ranking.

Each tension names the literature it comes from where there is any, because
several of these are documented anomalies rather than things noticed here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import num


def _pct(row, sleeve, pool):
    vals = sorted(r["_sleeves"][sleeve] for r in pool
                  if r["_sleeves"].get(sleeve) is not None)
    v = row["_sleeves"].get(sleeve)
    if v is None or not vals:
        return None
    return 100 * sum(1 for x in vals if x < v) // len(vals)


def detect(row, pool):
    """Return [{key, headline, question, evidence}] for every tension that fires."""
    out = []
    g = lambda f: num(row.get(f))
    qp, vp, mp, gp = (_pct(row, s, pool) for s in ("Quality", "Value", "Momentum", "Growth"))

    if (qp or 0) >= 75 and (vp or 0) >= 75:
        out.append({
            "key": "quality_vs_value",
            "headline": f"Quality {qp}th percentile, valuation {vp}th, inside its own sub-industry",
            "question": "A good business priced cheaply against its direct peers. Either the "
                        "market knows something the panel does not, or nobody is looking. "
                        "Which, and what would distinguish them?",
            "evidence": f"Quality z {row['_sleeves']['Quality']:+.2f}, Value z {row['_sleeves']['Value']:+.2f}",
        })

    if (g("return_12_2") or 0) > 0.30 and (g("revenue_acceleration") or 0) < -0.02:
        out.append({
            "key": "momentum_vs_fundamentals",
            "headline": "Price running well ahead of decelerating revenue",
            "question": "The shares are pricing something the reported fundamentals do not yet "
                        "show. Is the market anticipating an inflection, or has momentum "
                        "detached? Name the inflection if you think there is one.",
            "evidence": f"return_12_2 {g('return_12_2')*100:+.0f}%, "
                        f"revenue_acceleration {g('revenue_acceleration')*100:+.1f}%",
        })

    if (g("eps_growth_yoy") or 0) > 0.15 and (g("accruals_ratio") or -9) > 0.05:
        out.append({
            "key": "accruals_vs_earnings",
            "headline": "Earnings growing, but not backed by cash",
            "question": "Sloan (1996): the accrual component of earnings has weak persistence "
                        "and high-accrual firms underperform. Is this working capital "
                        "financing real growth, or is it revenue recognised ahead of cash?",
            "evidence": f"eps_growth_yoy {g('eps_growth_yoy')*100:+.0f}%, "
                        f"accruals_ratio {g('accruals_ratio'):+.3f}",
        })

    ac = g("analyst_count")
    if ac is not None and ac <= 6 and (qp or 0) >= 70:
        out.append({
            "key": "neglect_vs_quality",
            "headline": f"Quality {qp}th percentile with {ac:.0f} analyst"
                        f"{'s' if ac != 1 else ''} covering it",
            "question": "The Lynch case, and Hong-Lim-Stein found momentum strongest where "
                        "coverage is thinnest. But thin coverage usually means small, "
                        "illiquid and genuinely hard to research. Is the information "
                        "advantage real, or is it just the reason nobody bothers?",
            "evidence": f"analyst_count {ac:.0f}, market_cap "
                        f"${(g('market_cap') or 0)/1e9:,.1f}B",
        })

    if (g("gross_margin_trend") or -9) > 0.005 and (g("high52w_proximity") or 0) < -0.25:
        out.append({
            "key": "margin_vs_price",
            "headline": "Gross margin improving while the stock is disbelieved",
            "question": "Margins are going the right way and the shares are well off the high. "
                        "Is the market pricing a problem below the gross line, or has it "
                        "not noticed?",
            "evidence": f"gross_margin_trend {g('gross_margin_trend')*100:+.1f}pp, "
                        f"{g('high52w_proximity')*100:.0f}% off the 52-week high",
        })

    if (g("insider_cluster_max_30d") or 0) >= 3 and (g("high52w_proximity") or 0) < -0.20:
        out.append({
            "key": "insider_vs_price",
            "headline": "Insiders buying as a cluster into price weakness",
            "question": "Seyhun: clustered open-market buying by multiple insiders is among the "
                        "more informative insider signals, and it is strongest against a "
                        "falling price. What do they think is mispriced?",
            "evidence": f"{g('insider_cluster_max_30d'):.0f} distinct buyers in 30d, "
                        f"net ${(g('insider_net_buy_90d') or 0)/1e6:,.1f}M over 90d",
        })

    if (g("benford_mad") or 0) > 0.012 and (g("revenue_growth_yoy") or 0) > 0.20:
        out.append({
            "key": "benford_vs_growth",
            "headline": "Reported figures fit Benford poorly while growth is high",
            "question": "Weak evidence on its own and it must not be reported as fraud. But "
                        "poor first-digit conformity alongside fast growth is a reason to "
                        "read the filing rather than the summary.",
            "evidence": f"benford_mad {g('benford_mad'):.4f} (above 0.012 is a poor fit), "
                        f"revenue_growth_yoy {g('revenue_growth_yoy')*100:+.0f}%",
        })

    if (g("net_debt_ebitda") or 0) > 4 and (g("op_margin_stability") or 0) > 0.04:
        out.append({
            "key": "leverage_vs_volatility",
            "headline": "Heavy leverage against unstable operating margins",
            "question": "Debt is serviced out of operating cash. A levered balance sheet on a "
                        "margin that swings is a different risk from the same leverage on a "
                        "stable one. How much cushion is there at the trough?",
            "evidence": f"net_debt_ebitda {g('net_debt_ebitda'):.1f}x, "
                        f"op_margin_stability {g('op_margin_stability'):.3f} (std dev)",
        })

    return out
