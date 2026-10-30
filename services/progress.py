"""Pure helpers for the Progress screen: weight trend and plain-language insights."""
from datetime import date


def weight_trend(entries: list[tuple[str, float]], alpha: float = 0.1) -> list[tuple[str, float]]:
    """Smoothed weight, one point per weigh-in (entries sorted by ISO date, one per day).
    Exponential moving average, adjusted for gaps so a week without weighing moves the line more than a day.
    ponytail: MacroFactor-style trend without its full algorithm; swap in a Kalman filter if this proves too lagging."""
    out: list[tuple[str, float]] = []
    for d, kg in entries:
        if not out:
            out.append((d, kg))
            continue
        gap = max((date.fromisoformat(d) - date.fromisoformat(out[-1][0])).days, 1)
        a = 1 - (1 - alpha) ** gap
        out.append((d, out[-1][1] + a * (kg - out[-1][1])))
    return out


def weekly_rate(trend: list[tuple[str, float]]) -> float | None:
    """Trend change per week, or None until there are two weigh-ins at least a week apart."""
    if len(trend) < 2:
        return None
    days = (date.fromisoformat(trend[-1][0]) - date.fromisoformat(trend[0][0])).days
    return None if days < 7 else (trend[-1][1] - trend[0][1]) / days * 7


if __name__ == "__main__":
    t = weight_trend([("2026-10-01", 80.0), ("2026-10-02", 82.0), ("2026-10-09", 80.0)])
    assert t[0][1] == 80.0 and 80.0 < t[1][1] < 80.3, t       # one noisy day barely moves the trend
    assert 80.0 < t[2][1] < 80.2, t                           # back to normal after a week
    assert weekly_rate(t[:2]) is None                         # too short to call a rate
    assert abs(weekly_rate([("2026-10-01", 80.0), ("2026-10-15", 79.0)]) + 0.5) < 1e-9
    print("ok")
