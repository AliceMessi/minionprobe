#!/bin/bash
# windows419b.sh OM_WORKTREE PYTHON TMPDIR : the #430 round-2 tests on native Windows.
# Runs on clean copies (git archive), never on the worktree itself.
set -u
WT="$1"; PY="$2"; T="$3"
HEAD=$(git -C "$WT" rev-parse HEAD); DEV=$(git -C "$WT" rev-parse upstream/dev)
F="tests/api/test_text_answer_freshness.py tests/api/test_freshness_continuation.py tests/brain/test_freshness_entry_retention.py"
echo "head: $(git -C "$WT" log --oneline -1 $HEAD)"; echo "dev:  $(git -C "$WT" log --oneline -1 $DEV)"
"$PY" -c 'import sys,sqlite3,platform;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version, platform.platform())'
pyt() { "$PY" -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{200}).*/\1 .../'; }
tree() { rm -rf "$T/$1"; mkdir -p "$T/$1"; git -C "$WT" archive "$2" | tar -x -C "$T/$1"; }
echo; echo "== [head $HEAD] pytest <focused>"; tree head $HEAD; (cd "$T/head" && pyt $F)
echo; echo "== [dev $DEV] the head's tests/api/test_text_answer_freshness.py on a dev tree, pytest <focused>"
tree dev $DEV; cp "$T/head/tests/api/test_text_answer_freshness.py" "$T/dev/tests/api/"; (cd "$T/dev" && pyt $F)
echo; echo "== [ablation A] head without the trace check in _same_turn_evidence (dispatch.py)"
tree abl_a $HEAD
"$PY" - "$T/abl_a/src/openminion/modules/brain/execution/dispatch.py" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(encoding="utf-8")
old = '    if not trace_id or getattr(state, "turn_evidence_trace_id", None) != trace_id:\n        return None\n'
assert s.count(old) == 1; p.write_text(s.replace(old, ""), encoding="utf-8"); print("removed:", old.strip().splitlines()[0])
PYEOF
(cd "$T/abl_a" && pyt tests/api/test_text_answer_freshness.py)
echo; echo "== [ablation B] head without the record in _autonomous_seeded_result (seeded.py)"
tree abl_b $HEAD
"$PY" - "$T/abl_b/src/openminion/modules/brain/loop/adaptive/seeded.py" <<'PYEOF'
import sys, pathlib
p = pathlib.Path(sys.argv[1]); s = p.read_text(encoding="utf-8")
old = "        ctx.state.turn_evidence_trace_id = ctx.state.trace_id\n        ctx.state.turn_evidence = action_result\n"
assert s.count(old) == 1; p.write_text(s.replace(old, ""), encoding="utf-8"); print("removed: the two turn_evidence assignments")
PYEOF
(cd "$T/abl_b" && pyt tests/api/test_text_answer_freshness.py)
echo; echo "== ruff check / format --check on the changed Python files (head)"
(cd "$T/head" && "$PY" -m ruff check src/openminion/modules/brain/execution/dispatch.py src/openminion/modules/brain/loop/adaptive/seeded.py src/openminion/modules/brain/schemas/state/working.py tests/api/test_text_answer_freshness.py && "$PY" -m ruff format --check src/openminion/modules/brain/execution/dispatch.py src/openminion/modules/brain/loop/adaptive/seeded.py src/openminion/modules/brain/schemas/state/working.py tests/api/test_text_answer_freshness.py) 2>&1 | tail -3
