"""Runtime governance gates for the crash ML system."""

SYSTEM_STATUS = "MANUAL_REVIEW"
PROMOTION_ALLOWED = False
LIVE_EXECUTION_ALLOWED = False

ALLOWED_STATES = {
    "TRAINING",
    "VALIDATING",
    "AUTO_REJECT",
    "MANUAL_REVIEW",
    "SHADOW_READY",
    "DEFERRED_DEPENDENCY",
}


def operator_signal(raw_signal: str, would_bet: bool) -> str:
    if PROMOTION_ALLOWED and LIVE_EXECUTION_ALLOWED and raw_signal == "BET" and would_bet:
        return "BET"
    return "SKIP"


def governance_payload() -> dict:
    return {
        "system_status": SYSTEM_STATUS,
        "promotion_allowed": PROMOTION_ALLOWED,
        "live_execution_allowed": LIVE_EXECUTION_ALLOWED,
        "allowed_states": sorted(ALLOWED_STATES),
        "mode": "observation_only",
    }
