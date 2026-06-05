from pathlib import Path

GAME_URL = "https://bcgame61.com/game/crash"

_ROOT = Path(__file__).parent
DB_PATH          = str(_ROOT / "data"  / "crash.duckdb")
DEBUG_WS_LOG     = str(_ROOT / "data"  / "ws_debug.log")
COLLECTOR_LOG    = str(_ROOT / "logs"  / "collector.log")

LOGIN_TIMEOUT_SECONDS = 90

# WS event types that mean "round just ended" — only these frames are processed.
# If a frame has NO event/type field at all, it is still processed (some protocols
# send only the final message without a wrapper).
# Add / remove after running: python main.py inspect-ws
ROUND_END_EVENTS = [
    "round_end", "roundEnd",
    "crash",     "crashed",
    "bust",      "busted",
    "game_over", "gameOver",
    "result",    "final",
    "end",       "complete",
]

# Keys searched recursively for the crash multiplier value
MULTIPLIER_KEYS = [
    "crash_point",   "crashPoint",
    "multiplier",    "finalMultiplier", "final_multiplier",
    "bust_at",       "bustAt",
    "crash_rate",    "crashRate",
]

# Keys searched for total bet amount (float)
BET_AMOUNT_KEYS = [
    "total_bets",    "totalBets",
    "total_bet",     "totalBet",
    "total_wagered", "totalWagered",
    "wagered",       "total_amount", "totalAmount",
]

# Keys searched for number of bettors (int)
BET_COUNT_KEYS = [
    "player_count", "playerCount",
    "num_players",  "numPlayers",
    "bettors",      "betters",
]

# CSS selectors tried in order for DOM fallback
DOM_SELECTORS = [
    "[data-testid*='multiplier']",
    "[data-testid*='crash']",
    ".crash-multiplier",
    "[class*='crash-result']",
    "[class*='crashResult']",
    "[class*='CrashResult']",
    "[class*='multiplier']",
    "[class*='Multiplier']",
    "[class*='crash-point']",
    "[class*='crashPoint']",
]
