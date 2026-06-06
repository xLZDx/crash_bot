"""Test db_util.connect_ro (read-only DuckDB connect helper)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
from db_util import connect_ro


def test_connect_ro_reads(tmp_path):
    p = str(tmp_path / "t.duckdb")
    con = duckdb.connect(p)
    con.execute("CREATE TABLE t(x INT)")
    con.execute("INSERT INTO t VALUES (1),(2),(3)")
    con.close()
    with connect_ro(p) as c:
        assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 3


def test_connect_ro_nonlock_error_raises_fast(tmp_path):
    # a non-existent file in read-only mode raises (not a lock error) without 10x retry spin
    import time
    t0 = time.time()
    raised = False
    try:
        connect_ro(str(tmp_path / "nope.duckdb"), retries=10, backoff=0.3)
    except Exception:
        raised = True
    assert raised
    assert time.time() - t0 < 1.0   # did NOT spin 10x0.3s on a non-lock error
