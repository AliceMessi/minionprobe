#!/bin/bash
# Linux gates for #395 (PostgreSQL 16 service available; these tests do not use it).
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=b670bbb6
FOCUSED="tests/brain/test_freshness_entry_retention.py tests/api/test_freshness_continuation.py tests/architecture/test_marker_discipline.py"
RELATED="tests/brain/test_freshness_routing.py tests/brain/test_freshness_enforcement.py tests/brain/test_unified_turn_entry.py tests/services/brain/test_current_date_evidence_grounding.py tests/brain/test_runner_context.py tests/test_cted_regression.py tests/brain/test_integration_brain_v1.py tests/modules/brain/loop/tools/test_engine_characterization.py tests/api/test_provider_failure.py"
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys;print("CPython", sys.version.split()[0])'); $(grep PRETTY_NAME /etc/os-release)"
sha256sum $EV/*.py $EV/run395.sh
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src"; git status --short | grep -v '^?? .deps' | head; fi
  sec "[$tree] pytest -q -p no:cacheprovider -p no:logging -rf --tb=line <focused>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -p no:logging -rf --tb=line $FOCUSED 2>&1 | grep -v "^20[0-9][0-9]-" | tail -16
  sec "[$tree] pytest -q -p no:cacheprovider -p no:logging -rf --tb=line <related>"
  PYTHONPATH=src:. $PY -m pytest -q -p no:cacheprovider -p no:logging -rf --tb=line $RELATED 2>&1 | grep -v "^20[0-9][0-9]-" | tail -6
  sec "[$tree] the issue's probe: PYTHONPATH=src:. python probe_freshness_continuation.py"
  PYTHONPATH=src:. $PY $EV/probe_freshness_continuation.py 2>&1
done
git checkout -q HEAD -- src; sec "back to the branch head"; git status --short | grep -v '^?? .deps' | head
sec "ruff check ."; $PY -m ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -16; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
