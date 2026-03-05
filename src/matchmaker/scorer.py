"""
src/matchmaker/scorer.py — Rule-based urgency scoring for property signals.

Score components (total max 100):
  - Price drop severity: 0–50 pts
  - Days on market severity: 0–30 pts
  - Signal type combo bonus: 0–20 pts
"""


def score_signal(
    price_drop_pct: float | None,
    days_on_market: int | None,
    signal_type: str,
) -> float:
    score = 0.0
    price_drop_pct = price_drop_pct or 0
    days_on_market = days_on_market or 0

    # Price drop component (10% = 20pts, 20% = 40pts, 25%+ = 50pts cap)
    if price_drop_pct >= 10:
        score += min((price_drop_pct - 10) * 3 + 20, 50)

    # DOM component (90 days = 10pts, 120 days = 19pts, 180+ days = 30pts cap)
    if days_on_market >= 90:
        score += min((days_on_market - 90) * 0.2 + 10, 30)

    # Combo bonus — both signals present = more urgent
    if signal_type == "BOTH":
        score += 20

    return round(min(score, 100), 1)
