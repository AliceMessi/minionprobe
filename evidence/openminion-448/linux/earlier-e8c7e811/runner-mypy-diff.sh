#!/bin/bash
# The mypy ratchet's own invocation on the branch head and on dev sources; the new error lines.
set -u
DEV="042e0a2c6d9efed679ec825ba621e1ee055be6af"
P=.venv/bin/python
echo "head: $(git log --oneline -1)"
CFG=/tmp/ratchet.ini
$P - <<'EOF'
import scripts.validate.mypy_error_budget as m
open("/tmp/ratchet.ini", "w").write(m._RATCHET_CONFIG)
EOF
run() { $P -m mypy src/openminion --config-file $CFG --explicit-package-bases --hide-error-context --no-error-summary --show-error-codes 2>&1 | grep ": error:" | sort > "$1"; wc -l < "$1"; }
echo "head errors: $(run /tmp/head.txt)"
git checkout -q $DEV -- src
echo "dev errors: $(run /tmp/dev.txt)"
git checkout -q HEAD -- src
strip() { sed -E 's/^([^:]+):[0-9]+(:[0-9]+)?:/\1:/' "$1" | sort; }
echo "== error lines only on head (line numbers stripped)"
comm -13 <(strip /tmp/dev.txt) <(strip /tmp/head.txt)
echo "== error lines only on dev"
comm -23 <(strip /tmp/dev.txt) <(strip /tmp/head.txt)
echo "== head errors in the changed files"
grep -E "post_execution/mixin.py|policy/adapters/memory.py|runner/coordinator.py|tick/input_processing.py" /tmp/head.txt
