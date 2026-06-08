#!/usr/bin/env bash
# live_watchdog.sh -- keep the live_feed publisher + the event-driven paper bots
# (bot_live.py) alive, one writer per state DB. Run via cron (*/5). The reset-vs-
# keep comparison runs as two variants of relwave_bad_veto_21.
set -u
ROOT="/root/crash-collector"
cd "$ROOT" || exit 1
PY="$ROOT/venv/bin/python"
LOCK="/tmp/live_watchdog.lock"
exec 9>"$LOCK"
flock -n 9 || { echo "$(date -u +%FT%TZ) another watchdog running, skip"; exit 0; }
ts() { date -u +%FT%TZ; }

# "strategy:variant" -> state DB data/bot_live_<strategy>_<variant>.duckdb
# Keep-only mirrors real bot late-bet behavior (no ladder reset on suspend).
BOTS="relwave_bad_veto_21:keep"

# 1) publisher (exactly one)
mapfile -t fpids < <(pgrep -f 'live_feed\.py')
if [ "${#fpids[@]}" -eq 0 ]; then
    nohup "$PY" -B -u live_feed.py >> logs/live_feed.log 2>&1 &
    echo "$(ts) STARTED live_feed pid=$!"
elif [ "${#fpids[@]}" -gt 1 ]; then
    for p in "${fpids[@]:1}"; do kill "$p" 2>/dev/null && echo "$(ts) trimmed dup live_feed $p"; done
fi

# 2) bots (exactly one per variant)
for spec in $BOTS; do
    strat="${spec%%:*}"
    var="${spec##*:}"
    db="data/bot_live_${strat}_${var}.duckdb"
    mapfile -t pids < <(pgrep -f -- "bot_live\.py.*--bot-db ${db}")
    if [ "${#pids[@]}" -eq 0 ]; then
        nohup "$PY" -B -u bot_live.py --strategy "$strat" --bot-db "$db" \
            --cooldown-scale "$var" >> "logs/bot_live_${var}.log" 2>&1 &
        echo "$(ts) STARTED bot_live $strat $var pid=$!"
    elif [ "${#pids[@]}" -gt 1 ]; then
        for p in "${pids[@]:1}"; do
            kill "$p" 2>/dev/null && echo "$(ts) trimmed dup bot_live $var $p"
        done
    fi
done
echo "$(ts) live_watchdog done"
