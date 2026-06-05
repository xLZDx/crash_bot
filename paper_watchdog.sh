#!/usr/bin/env bash
# paper_watchdog.sh -- keep exactly ONE bot.py per intended paper strategy.
# Intended set = strategies that have a data/bot_state_<name>.duckdb file,
# minus EXCLUDE. Kills duplicate writers, restarts dead bots.
# Set DRY_RUN=1 to print actions without changing anything.
#
# Touches ONLY processes matching 'bot.py ... --strategy <name>$'.
# Never touches bot_realbet.py (real money), main.py collect, dashboard, run_bot.py.
set -u

ROOT="/root/crash-collector"
cd "$ROOT" || exit 1
PY="$ROOT/venv/bin/python"
EXCLUDE=" turbo "          # space-padded for word match
DRY="${DRY_RUN:-0}"
LOCK="/tmp/paper_watchdog.lock"

# single-instance lock
exec 9>"$LOCK"
flock -n 9 || { echo "$(date -u +%FT%TZ) another watchdog running, skip"; exit 0; }

ts() { date -u +%FT%TZ; }

start_bot() {
    local name="$1"
    if [ "$DRY" = "1" ]; then
        echo "$(ts) DRY would START $name"
        return
    fi
    nohup "$PY" -u bot.py --db data/crash.duckdb \
        --bot-db "data/bot_state_${name}.duckdb" --strategy "${name}" \
        >> "logs/bot_${name}.log" 2>&1 &
    echo "$(ts) STARTED $name pid=$!"
}

trimmed=0; started=0; ok=0
for db in data/bot_state_*.duckdb; do
    [ -e "$db" ] || continue
    base="$(basename "$db" .duckdb)"
    name="${base#bot_state_}"
    [ -z "$name" ] && continue
    case "$EXCLUDE" in *" $name "*) continue;; esac

    # exact-match pids: --strategy <name> must be the final token
    mapfile -t pids < <(pgrep -f -- "bot\.py .*--strategy ${name}\$")
    n=${#pids[@]}

    if [ "$n" -eq 0 ]; then
        start_bot "$name"; started=$((started+1))
    elif [ "$n" -eq 1 ]; then
        ok=$((ok+1))
    else
        keep="${pids[0]}"
        for p in "${pids[@]:1}"; do
            if [ "$DRY" = "1" ]; then
                echo "$(ts) DRY would KILL dup pid=$p ($name) keep=$keep"
            else
                kill "$p" 2>/dev/null && echo "$(ts) KILLED dup pid=$p ($name) keep=$keep"
            fi
            trimmed=$((trimmed+1))
        done
    fi
done

# cleanup: kill any excluded strategy that somehow runs
for ex in $EXCLUDE; do
    mapfile -t expids < <(pgrep -f -- "bot\.py .*--strategy ${ex}\$")
    for p in "${expids[@]}"; do
        if [ "$DRY" = "1" ]; then
            echo "$(ts) DRY would KILL excluded $ex pid=$p"
        else
            kill "$p" 2>/dev/null && echo "$(ts) KILLED excluded $ex pid=$p"
        fi
    done
done

echo "$(ts) watchdog done: ok=$ok started=$started trimmed=$trimmed dry=$DRY"
