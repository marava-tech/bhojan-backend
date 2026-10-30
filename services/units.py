"""Weights are stored in kg and heights in cm. Units only change how they are shown to the user,
including the few server-written messages that print a weight."""

KG_PER_LB = 0.45359237


def fmt_weight(kg: float, profile: dict | None, decimals: int = 1) -> str:
    """'72.5 kg' or '159.8 lb', per the user's saved units."""
    if ((profile or {}).get("units") or {}).get("weight") == "lb":
        return f"{kg / KG_PER_LB:.{decimals}f} lb"
    return f"{kg:.{decimals}f} kg"
