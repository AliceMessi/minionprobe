#!/bin/bash
# The text-answer freshness probe on dev and on the #395 branch head.
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=b670bbb6
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys;print("CPython", sys.version.split()[0])'); $(grep PRETTY_NAME /etc/os-release)"
sha256sum $EV/*.py $EV/run_text.sh
echo; echo "== [head: #395 branch] PYTHONPATH=src:. python probe_text_answer_freshness.py"
PYTHONPATH=src:. $PY $EV/probe_text_answer_freshness.py 2>&1
git checkout -q $DEV -- src
echo; echo "== [dev sources: git checkout $DEV -- src] the same"
PYTHONPATH=src:. $PY $EV/probe_text_answer_freshness.py 2>&1
git checkout -q HEAD -- src
