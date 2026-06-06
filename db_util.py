"""
db_util.py
Shared DuckDB read helper for the crash tooling. The collector + 25 paper bots
each hold write-locks on their DuckDB files; a read-only connection can fail with
"Could not set lock ... Conflicting lock is held". connect_ro retries on that.
"""
import time

import duckdb


def connect_ro(path, retries=10, backoff=0.3):
    """Read-only connect with retry on transient write-lock contention."""
    last = None
    for i in range(retries):
        try:
            return duckdb.connect(path, read_only=True)
        except Exception as e:
            last = e
            msg = str(e).lower()
            # retry ONLY on transient write-lock contention -- NOT on a missing
            # file / permission error (those won't resolve by waiting).
            if ("conflicting lock" in msg or "set lock" in msg
                    or "database is locked" in msg) and i < retries - 1:
                time.sleep(backoff)
                continue
            raise
    raise last
