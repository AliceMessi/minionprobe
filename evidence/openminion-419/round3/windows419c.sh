#!/bin/bash
# windows419c.sh OM_WORKTREE PYTHON TMPDIR : the #430 round-3 tests on native Windows.
# Runs on clean copies (git archive), never on the worktree itself.
set -u
WT="$1"; PY="$2"; T="$3"
HEAD=$(git -C "$WT" rev-parse HEAD); DEV=$(git -C "$WT" rev-parse upstream/dev)
F="tests/api/test_text_answer_freshness.py tests/api/test_freshness_continuation.py tests/brain/test_freshness_entry_retention.py"
F2="tests/brain/test_turn_evidence_persistence.py"
W=src/openminion/modules/brain/schemas/state/working.py
echo "head: $(git -C "$WT" log --oneline -1 $HEAD)"; echo "dev:  $(git -C "$WT" log --oneline -1 $DEV)"
"$PY" -c 'import sys,sqlite3,platform;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version, platform.platform())'
pyt() { "$PY" -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{200}).*/\1 .../'; }
tree() { rm -rf "$T/$1"; mkdir -p "$T/$1"; git -C "$WT" archive "$2" | tar -x -C "$T/$1"; }
edit() { "$PY" - "$1" "$2" "$3" <<'PYEOF'
import sys, pathlib
p, old, new = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
s = p.read_text(encoding="utf-8"); assert s.count(old) == 1, old
p.write_text(s.replace(old, new), encoding="utf-8"); print("edited:", old.strip(), "->", new.strip())
PYEOF
}
echo; echo "== [head $HEAD] pytest <focused> <persistence>"; tree head $HEAD; (cd "$T/head" && pyt $F $F2)
echo; echo "== [dev $DEV] the head's two test files on a dev tree, pytest <focused> <persistence>"
tree dev $DEV; cp "$T/head/tests/api/test_text_answer_freshness.py" "$T/dev/tests/api/"; cp "$T/head/$F2" "$T/dev/tests/brain/"
(cd "$T/dev" && pyt --continue-on-collection-errors $F $F2)
echo; echo "== [ablation C] head with both fields excluded from the saved state"
tree abl_c $HEAD
edit "$T/abl_c/$W" "    turn_evidence_trace_id: str | None = None" "    turn_evidence_trace_id: str | None = Field(default=None, exclude=True)"
edit "$T/abl_c/$W" "    turn_evidence: ActionResult | None = None" "    turn_evidence: ActionResult | None = Field(default=None, exclude=True)"
(cd "$T/abl_c" && pyt $F2)
echo; echo "== [ablation D] head with WorkingState ignoring unknown fields"
tree abl_d $HEAD
edit "$T/abl_d/$W" 'class WorkingState(BaseModel):
    model_config = ConfigDict(extra="forbid")' 'class WorkingState(BaseModel):
    model_config = ConfigDict(extra="ignore")'
(cd "$T/abl_d" && pyt $F2)
echo; echo "== ruff check / format --check on the new test file (head)"
(cd "$T/head" && "$PY" -m ruff check $F2 && "$PY" -m ruff format --check $F2) 2>&1 | tail -3
