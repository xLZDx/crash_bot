import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import governance
import watchdog


ROOT = Path(__file__).resolve().parents[1]


def test_governance_is_observation_only_by_default():
    payload = governance.governance_payload()

    assert payload["system_status"] == "MANUAL_REVIEW"
    assert payload["promotion_allowed"] is False
    assert payload["live_execution_allowed"] is False
    assert governance.operator_signal("BET", would_bet=True) == "SKIP"
    assert governance.operator_signal("BET", would_bet=False) == "SKIP"


def test_watchdog_dashboard_streamlit_args_are_local_and_protected():
    cmd = watchdog.PROCS["dashboard"]["cmd"]

    assert "--server.address" in cmd
    assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"
    assert "--server.enableCORS" in cmd
    assert cmd[cmd.index("--server.enableCORS") + 1] == "true"
    assert "--server.enableXsrfProtection" in cmd
    assert cmd[cmd.index("--server.enableXsrfProtection") + 1] == "true"


def test_main_dashboard_launch_sets_localhost_and_browser_security_args():
    text = (ROOT / "main.py").read_text(encoding="utf-8")

    assert '"--server.address", "127.0.0.1"' in text
    assert '"--server.enableCORS", "true"' in text
    assert '"--server.enableXsrfProtection", "true"' in text


def test_dashboard_mutations_are_deny_by_default_and_observation_language():
    text = (ROOT / "dashboard.py").read_text(encoding="utf-8")

    assert 'os.environ.get("CRASH_DASHBOARD_MUTATIONS") == "1"' in text
    assert "disabled=not ALLOW_DASHBOARD_MUTATIONS" in text
    assert "SHADOW_CANDIDATE" in text
    assert "promotion_allowed" in text
    assert "Training process supervision is owned by watchdog.py or the CLI" in text
    forbidden = [
        "СТАВИТЬ",
        "CASH OUT",
        "Start Round",
        "Целься",
        "ставим",
        "ставить",
        "Selective betting",
    ]
    for phrase in forbidden:
        assert phrase not in text
