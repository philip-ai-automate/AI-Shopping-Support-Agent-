"""
cart_scorer.py  — Intent scoring for the Intelligent Cart Revenue Recovery feature.

Produces:
  score    int  0–100   — cumulative intent signal from all cart events in a session
  priority str          — 'LOW' | 'MEDIUM' | 'HIGH'

Score thresholds:
  HIGH    70–100  →  strong purchase intent / clear abandonment
  MEDIUM  40–69   →  engaged, cart populated
  LOW      0–39   →  browsing, low commitment

Recovery is only triggered for MEDIUM and HIGH sessions.
"""

# ── Per-event point values ────────────────────────────────────────────────────
# Positive values signal purchase intent; negative signals disengagement.
_EVENT_SCORES: dict[str, int] = {
    "add_to_cart":            25,
    "cart_updated":            5,
    "remove_cart_item":      -15,
    "checkout_started":       30,
    "checkout_abandoned":     40,
    "exit_intent":            20,
    "page_idle":              10,
    # Neutral events (already in recovery pipeline — do not inflate score)
    "recovery_popup_shown":    0,
    "recovery_popup_closed":   0,
    "recovery_popup_clicked":  0,
    # Positive outcome — should mark queue recovered, but include for completeness
    "checkout_completed":    100,
}

# ── Minimum score required to enter the recovery queue ───────────────────────
RECOVERY_THRESHOLD = 40   # MEDIUM or higher only


def _cart_value_bonus(cart_value: float | None) -> int:
    """
    Higher-value carts earn additional score points so they are prioritised
    ahead of low-value sessions when both are at the same raw event score.
    """
    if not cart_value:
        return 0
    v = float(cart_value)
    if v >= 200:
        return 15
    if v >= 100:
        return 10
    if v >= 50:
        return 5
    return 2


def compute_intent_score(
    events: list[dict],
    cart_value: float | None = None,
) -> tuple[int, str]:
    """
    Compute intent score from a list of event dicts.
    Each dict must contain at least {"event_type": "<name>"}.

    Args:
        events:      list of event dicts from cart_events table
        cart_value:  most recent cart value in GBP (float or None)

    Returns:
        (score, priority)
        score    — clamped integer 0–100
        priority — 'LOW', 'MEDIUM', or 'HIGH'
    """
    raw = 0
    for event in events:
        event_type = (event.get("event_type") or "").lower().strip()
        raw += _EVENT_SCORES.get(event_type, 0)

    raw += _cart_value_bonus(cart_value)

    # Clamp to valid range
    score = max(0, min(100, raw))

    # Priority tier
    if score >= 70:
        priority = "HIGH"
    elif score >= RECOVERY_THRESHOLD:
        priority = "MEDIUM"
    else:
        priority = "LOW"

    return score, priority
