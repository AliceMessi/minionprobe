#!/bin/bash
# Native Windows evidence for #420: focused tests and the probes on the branch head and on dev sources.
W=/c/Users/User/Downloads/agentprize/projects/cortensor-hackathon-5/om-420
EV=/c/Users/User/AppData/Local/Temp/claude/C--Users-User-Downloads-agentprize/c1e19ae3-71ba-4e30-8f18-0c01cd7b59a2/scratchpad/w420
P=/c/Users/User/Downloads/agentprize/projects/cortensor-hackathon-5/om-352/.venv/Scripts/python.exe
DEV=8c0568f7
FOCUSED="tests/runtime/test_cron_capture_recovery.py tests/runtime/test_cron_turn_executor.py tests/memory/runtime/test_capture_recovery.py tests/memory/test_external_backend_capture.py tests/services/agent/test_execution_resources_memory.py"
cd $W || exit 1
echo "$(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse HEAD) (worktree clean: $([ -z "$(git status --short)" ] && echo True || echo False)) and dev @ $(git rev-parse $DEV)"
echo "native Windows 11 Pro $(cmd //c ver | tr -d '\r' | grep -o '[0-9.]*[0-9]'), $($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'), PYTHONPATH=\"src;.\""
(cd $EV && sha256sum probe_cron_capture_recovery.py probe_cron_recovery_turn.py)
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; echo; echo "== dev sources from here on: git checkout $DEV -- src ($(git status --short | wc -l) path differs)"; fi
  echo; echo "== [$tree] pytest -q -p no:cacheprovider -p no:logging --show-capture=no -rfE --tb=line <focused: $FOCUSED>"
  PYTHONPATH="src;." $P -m pytest -q -p no:cacheprovider -p no:logging --show-capture=no -rfE --tb=line $FOCUSED 2>&1 | grep -E "^FAILED|^ERROR|passed|failed| Error|Error:" | sed -E "s/Accesso negato/(localized message trimmed)/" | cut -c1-300
  for probe in probe_cron_capture_recovery.py probe_cron_recovery_turn.py; do
    echo; echo "== [$tree] python $probe"
    PYTHONPATH="src;." $P $EV/$probe 2>&1 | grep -v "^20[0-9][0-9]-"
  done
done
git checkout -q HEAD -- src; echo; echo "== back to the branch head: $(git status --short | wc -l) paths differ"
