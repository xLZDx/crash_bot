#!/usr/bin/env bash
# nuke_and_start_live.sh -- one-time clean reset of the live ZMQ paper bots:
# kill ALL existing live_feed + bot_live python processes (matched by python
# comm, so this script's own shell can't self-match), wipe state, then start
# exactly one publisher + the reset/keep bots. After this, live_watchdog.sh
# (fixed dedup) maintains them.
cd /root/crash-collector || exit 1
PY=venv/bin/python

echo "killing existing live procs..."
for pid in $(ps -eo pid,comm,args | awk '$2 ~ /python/ && ($0 ~ /live_feed\.py/ || $0 ~ /bot_live\.py/){print $1}'); do
    kill -9 "$pid" 2>/dev/null && echo "  killed $pid"
done
sleep 1
rm -rf __pycache__
rm -f data/bot_live_relwave_bad_veto_21_*.duckdb*
: > logs/bot_live_reset.log
: > logs/bot_live_keep.log

echo "starting publisher..."
setsid sh -c "cd /root/crash-collector && exec $PY -u live_feed.py >> logs/live_feed.log 2>&1" </dev/null &
sleep 4
echo "starting bots..."
for v in reset keep; do
    setsid sh -c "cd /root/crash-collector && exec $PY -u bot_live.py --strategy relwave_bad_veto_21 --bot-db data/bot_live_relwave_bad_veto_21_${v}.duckdb --cooldown-scale ${v} >> logs/bot_live_${v}.log 2>&1" </dev/null &
done
sleep 2

echo "publishers: $(ps -eo comm,args | awk '$1 ~ /python/ && /live_feed\.py/' | grep -vc awk)"
echo "bots:       $(ps -eo comm,args | awk '$1 ~ /python/ && /bot_live\.py/' | grep -vc awk)"
