"""Tests for reconcile_rounds pure core (gap detection, classification, orphan bets)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reconcile_rounds import find_gaps, classify_gaps, find_orphan_bets, coverage_pct


def test_no_gaps():
    assert find_gaps([1, 2, 3, 4, 5]) == []


def test_single_gap():
    # 3 and 4 missing between 2 and 5
    assert find_gaps([1, 2, 5, 6]) == [(2, 2, 5)]


def test_multiple_gaps():
    g = find_gaps([10, 11, 20, 21, 30])
    assert g == [(8, 11, 20), (8, 21, 30)]


def test_classify_outage_vs_drop():
    gaps = [(5136, 1, 5138), (3, 10, 14), (60, 100, 161)]
    outages, drops = classify_gaps(gaps, threshold=50)
    assert {x[0] for x in outages} == {5136, 60}
    assert {x[0] for x in drops} == {3}


def test_orphan_bets_detected():
    rounds = {100, 101, 102, 103}
    bets = [100, 101, 999, 102, 888]
    assert sorted(find_orphan_bets(rounds, bets)) == [888, 999]


def test_no_orphans_when_all_present():
    assert find_orphan_bets({1, 2, 3}, [1, 2, 3, 2, 1]) == []


def test_orphan_accepts_list_or_set():
    assert find_orphan_bets([1, 2, 3], [4]) == [4]


def test_coverage_pct():
    assert coverage_pct(78, 100) == 78.0
    assert coverage_pct(0, 0) == 0.0
    assert abs(coverage_pct(41871, 53749) - 77.9) < 0.1


def test_gap_missing_count_matches_total():
    # the real-data shape: total missing = sum of gap missing counts
    gids = [1, 2, 3, 8, 9, 100]  # gaps: (4,3,8)->4 missing, (90,9,100)->90 missing
    gaps = find_gaps(gids)
    assert sum(g[0] for g in gaps) == (gids[-1] - gids[0] + 1) - len(gids)
