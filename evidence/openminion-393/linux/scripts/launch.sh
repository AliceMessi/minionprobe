#!/bin/sh
# launch.sh LABEL REF CMDNAME [--postgres] : one runner job, waits and downloads logs into logs/LABEL
cd "$(dirname "$0")"
mkdir -p logs
python -I <LINUX_RUNNER_KIT>/run.py --repo AliceMessi/openminion --ref "$2" --preset openminion $4 --label "$1" --out "logs/$1" -- "$(cat out/$3.cmd)" > "run-$1.out" 2>&1
grep -E "^run |^conclusion" "run-$1.out"
