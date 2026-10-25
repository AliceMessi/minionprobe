#!/bin/bash
# The consolidation cron recovery probe on dev and on the #397 branch head.
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=b670bbb6
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys;print("CPython", sys.version.split()[0])'); $(grep PRETTY_NAME /etc/os-release); sophiagraph $($PY -c 'import importlib.metadata as m;print(m.version("sophiagraph"))')"
sha256sum $EV/*.py $EV/run_cron.sh
echo; echo "== [dev sources: git checkout $DEV -- src] PYTHONPATH=src:. python probe_cron_capture_recovery.py"
git checkout -q $DEV -- src
PYTHONPATH=src:. $PY $EV/probe_cron_capture_recovery.py 2>&1
git checkout -q HEAD -- src
echo; echo "== [head: #397 branch] the same"
PYTHONPATH=src:. $PY $EV/probe_cron_capture_recovery.py 2>&1
