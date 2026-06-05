import asyncio
import logging
import sys
from pathlib import Path

import typer

from config import DB_PATH

app = typer.Typer(help="Crash-game data collector & analytics.", add_completion=False)


@app.command()
def collect(
    hours: float = typer.Option(8760.0, "-t", "--hours", help="Duration (hours)"),
    db:    str   = typer.Option(DB_PATH, "--db"),
):
    """Collect crash-point data via persistent Playwright session. ~200 MB RAM."""
    from playwright_collector import run as pw_run

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)-8s %(message)s")
    try:
        asyncio.run(pw_run(db_path=db, duration_hours=hours))
    except KeyboardInterrupt:
        print("\nStopped.")


@app.command(name="inspect-ws")
def inspect_ws(
    minutes: int = typer.Option(5, "-m", "--minutes", help="Capture duration (minutes)"),
    db:      str = typer.Option(DB_PATH, "--db"),
):
    """
    Record raw WebSocket frames to data/ws_debug.log WITHOUT writing to the DB.

    Run this first to identify the event type and key names the game uses,
    then update ROUND_END_EVENTS and MULTIPLIER_KEYS in config.py.
    """
    from collector import CrashCollector
    from storage import CrashStorage

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    storage = CrashStorage(db)
    collector = CrashCollector(storage, debug_ws=True,
                               headless=False, login_timeout=30,
                               dry_run=True)  # never writes to DB
    try:
        asyncio.run(collector.run(duration_hours=minutes / 60))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        storage.close()
    print("WS frames saved to data/ws_debug.log")
    print("Open it and find the crash-point field, then update config.py.")


@app.command()
def dashboard(
    db:   str = typer.Option(DB_PATH, "--db"),
    port: int = typer.Option(8501,    "--port"),
):
    """Open the analytics dashboard in the browser (Streamlit)."""
    import subprocess

    dashboard_path = str(Path(__file__).parent / "dashboard.py")
    cmd = [
        sys.executable, "-m", "streamlit", "run", dashboard_path,
        "--server.port", str(port),
        "--server.address", "127.0.0.1",
        "--server.headless", "false",
        "--server.enableCORS", "true",
        "--server.enableXsrfProtection", "true",
        "--browser.gatherUsageStats", "false",
    ]
    print(f"Dashboard: http://localhost:{port}")
    print("Press Ctrl-C to stop.")
    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\nDashboard stopped.")


@app.command()
def train(
    db:       str  = typer.Option(DB_PATH, "--db"),
    once:     bool = typer.Option(False,   "--once",     help="Train once then exit"),
    interval: int  = typer.Option(60,      "--interval", help="Poll interval (seconds)"),
):
    """Run the continuous ML training pipeline."""
    from training import TrainingPipeline
    pipeline = TrainingPipeline(db_path=db)
    if once:
        pipeline.run_once()
    else:
        pipeline.run_forever(interval_s=interval)


@app.command()
def analyze(
    db:         str = typer.Option(DB_PATH, "--db"),
    min_rounds: int = typer.Option(100,     "--min"),
):
    """Print a statistical report on collected data."""
    from analysis import analyze as run_analysis
    run_analysis(db, min_rounds=min_rounds)


@app.command()
def export(
    db:  str = typer.Option(DB_PATH, "--db"),
    out: str = typer.Option("data/crash_rounds.csv", "-o", "--out"),
):
    """Export all collected rounds to a CSV file."""
    from storage import CrashStorage
    storage = CrashStorage(db)
    try:
        n = storage.count()
        storage.export_csv(out)
        print(f"Exported {n:,} rounds -> {out}")
    finally:
        storage.close()


if __name__ == "__main__":
    app()
