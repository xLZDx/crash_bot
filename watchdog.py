"""watchdog.py -- health monitor for crash_collector processes.

Checks every 30 s:
- collector:  last round in DB older than 60 s -> kill + restart
- dashboard:  port 8501 not responding          -> kill + restart
- training:   process not alive                 -> restart

Logs to logs/watchdog.log.
"""
import os
import sys
import time
import socket
import logging
import subprocess
from pathlib import Path
from typing import Optional

import duckdb
import psutil

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE           = Path(r"D:\crash_collector")
DB_PATH        = str(BASE / "data" / "crash.duckdb")
PYTHON         = r"C:\Python314\python.exe"
STALE_SEC           = 240   # collector: no new round for this long -> restart
CHECK_SEC           = 30    # loop interval
DASHBOARD_PORT      = 8501
LOCK_GRACE_SEC      = 90
TRAINING_WARN_SEC   = 600   # training: no prediction written for 10 min -> WARNING
TRAINING_RESTART_SEC = 1800  # training: no prediction written for 30 min -> restart
_LOCK_STARTED_AT = None

PROCS = {
    "collector": {
        "match": ["main.py", "collect"],
        "cmd": [PYTHON, str(BASE / "main.py"), "collect", "--hours", "8760"],
    },
    "training": {
        "match": ["training.py"],
        "cmd": [PYTHON, str(BASE / "training.py")],
    },
    "dashboard": {
        "match": ["streamlit", "dashboard.py"],
        "cmd": [
            PYTHON, "-m", "streamlit", "run", str(BASE / "dashboard.py"),
            "--server.port", str(DASHBOARD_PORT),
            "--server.address", "127.0.0.1",
            "--server.headless", "true",
            "--server.enableCORS", "true",
            "--server.enableXsrfProtection", "true",
            "--server.fileWatcherType", "none",
        ],
    },
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_PATH = BASE / "logs" / "watchdog.log"
LOG_PATH.parent.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watchdog")


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def find_procs(match_tokens: list) -> list:
    """Return psutil.Process objects whose cmdline contains all match_tokens."""
    result = []
    my_pid = os.getpid()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.pid == my_pid:
                continue
            if p.info["name"] and "python" in p.info["name"].lower():
                cmd = " ".join(p.info["cmdline"] or [])
                if all(tok in cmd for tok in match_tokens):
                    result.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return result


def kill_proc_tree(proc: psutil.Process) -> None:
    try:
        children = proc.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        proc.kill()
        proc.wait(timeout=10)
        log.info("Killed PID %d (+%d children)", proc.pid, len(children))
    except (psutil.NoSuchProcess, psutil.TimeoutExpired) as exc:
        log.warning("Kill failed: %s", exc)


def start_proc(name: str) -> None:
    cfg = PROCS[name]
    # Guard: re-check right before spawn to prevent duplicate processes
    if find_procs(cfg["match"]):
        log.info("start_proc(%s): already running, skipping spawn", name)
        return
    stderr_log = open(BASE / "logs" / f"{name}.stderr.log", "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cfg["cmd"],
            cwd=str(BASE),
            stdout=subprocess.DEVNULL,
            stderr=stderr_log,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        log.info("Started %-10s PID=%d", name, proc.pid)
    except Exception as exc:
        log.error("Failed to start %s: %s", name, exc, exc_info=True)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

def collector_age() -> float:
    """Seconds since the last round was stored.
    Returns -1.0 if DB is write-locked (collector actively writing — healthy).
    Returns inf on genuine error or empty table.
    """
    try:
        con = duckdb.connect(DB_PATH, read_only=True)
        try:
            row = con.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(ts))) FROM rounds"
            ).fetchone()
            return float(row[0]) if (row and row[0] is not None) else float("inf")
        finally:
            con.close()
    except Exception as exc:
        msg = str(exc)
        if "being used by another process" in msg or "Cannot open file" in msg:
            # Write-lock held by collector = actively inserting = healthy
            log.debug("DB write-locked by collector (healthy)")
            return -1.0
        log.warning("DB age check failed: %s", exc)
        return float("inf")


def _component_heartbeat_age(component: str) -> Optional[float]:
    """Seconds since system_health was last updated for the given component.

    Returns:
        float  -- seconds since last heartbeat (0 .. inf)
        inf    -- component has never written a heartbeat row (benign first-run)
        None   -- DB could not be read (error); caller must NOT treat as healthy
    """
    try:
        con = duckdb.connect(DB_PATH, read_only=True)
        try:
            row = con.execute("""
                SELECT EXTRACT(EPOCH FROM (NOW() - last_ok_at))
                FROM system_health WHERE component=?
            """, [component]).fetchone()
            return float(row[0]) if (row and row[0] is not None) else float("inf")
        finally:
            con.close()
    except Exception as exc:
        log.warning("heartbeat check failed for %s: %s", component, exc)
        return None


def collector_heartbeat_age() -> Optional[float]:
    return _component_heartbeat_age("collector")


def training_heartbeat_age() -> Optional[float]:
    return _component_heartbeat_age("training")


def port_alive(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Per-process checks
# ---------------------------------------------------------------------------

def check_collector() -> None:
    global _LOCK_STARTED_AT
    age   = collector_age()
    procs = find_procs(PROCS["collector"]["match"])

    if age == -1.0:
        now = time.time()
        if _LOCK_STARTED_AT is None:
            _LOCK_STARTED_AT = now
        lock_age = now - _LOCK_STARTED_AT
        heartbeat_age = collector_heartbeat_age()
        if not procs:
            # Stale lock file left by a crashed collector — restart
            log.warning("collector DB locked but NO process found -- stale lock, restarting")
            start_proc("collector")
            return
        if lock_age > LOCK_GRACE_SEC and heartbeat_age is not None and heartbeat_age > STALE_SEC:
            log.warning(
                "collector DB locked for %.0fs with stale heartbeat %.0fs -- restarting",
                lock_age, heartbeat_age,
            )
            for p in procs:
                kill_proc_tree(p)
            time.sleep(3)
            _LOCK_STARTED_AT = None
            start_proc("collector")
            return
        log.info(
            "collector OK  PIDs=%s  (DB locked %.0fs, heartbeat %.0fs)",
            [p.pid for p in procs], lock_age, heartbeat_age,
        )
        return
    _LOCK_STARTED_AT = None

    if age > STALE_SEC:
        log.warning("collector STUCK: last_round=%.0fs ago, PIDs=%s -- restarting",
                    age, [p.pid for p in procs])
        for p in procs:
            kill_proc_tree(p)
        time.sleep(3)
        start_proc("collector")
    elif not procs:
        log.warning("collector NOT RUNNING -- starting")
        start_proc("collector")
    else:
        log.info("collector OK  PIDs=%s  last_round=%.0fs ago",
                 [p.pid for p in procs], age)


def check_dashboard() -> None:
    alive = port_alive(DASHBOARD_PORT)
    procs = find_procs(PROCS["dashboard"]["match"])

    if not alive:
        log.warning("dashboard NOT RESPONDING on :%d, PIDs=%s -- restarting",
                    DASHBOARD_PORT, [p.pid for p in procs])
        for p in procs:
            kill_proc_tree(p)
        time.sleep(3)
        start_proc("dashboard")
    elif not procs:
        log.warning("dashboard proc GONE -- starting")
        start_proc("dashboard")
    else:
        log.info("dashboard OK  PIDs=%s  port:%d alive",
                 [p.pid for p in procs], DASHBOARD_PORT)


def check_training() -> None:
    procs = find_procs(PROCS["training"]["match"])

    if not procs:
        # Double-check after a short pause to guard against momentary psutil misses
        time.sleep(2)
        procs = find_procs(PROCS["training"]["match"])

    if not procs:
        log.warning("training NOT RUNNING -- starting")
        start_proc("training")
        return

    hb_age = training_heartbeat_age()
    if hb_age is None:
        # DB unreadable -- skip this tick rather than killing a healthy process
        log.warning("training heartbeat DB unreadable -- skipping restart decision, PIDs=%s",
                    [p.pid for p in procs])
        return
    if hb_age == float("inf"):
        # No heartbeat row yet -- training hasn't completed a cycle since last restart
        log.info("training  OK  PIDs=%s  (no heartbeat yet -- first cycle in progress)",
                 [p.pid for p in procs])
        return

    if hb_age > TRAINING_RESTART_SEC:
        log.warning(
            "training STUCK: last prediction=%.0fs ago, PIDs=%s -- restarting",
            hb_age, [p.pid for p in procs],
        )
        for p in procs:
            kill_proc_tree(p)
        time.sleep(3)
        start_proc("training")
    elif hb_age > TRAINING_WARN_SEC:
        log.warning(
            "training SLOW: last prediction=%.0fs ago (threshold=%ds), PIDs=%s",
            hb_age, TRAINING_WARN_SEC, [p.pid for p in procs],
        )
    else:
        log.info("training  OK  PIDs=%s  last_prediction=%.0fs ago",
                 [p.pid for p in procs], hb_age)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== Watchdog started  stale=%ds  interval=%ds ===", STALE_SEC, CHECK_SEC)
    while True:
        try:
            check_collector()
            check_dashboard()
            check_training()
        except Exception as exc:
            log.error("Loop error: %s", exc, exc_info=True)
        time.sleep(CHECK_SEC)


if __name__ == "__main__":
    main()
