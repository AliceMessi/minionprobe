#!/bin/bash
set -u
DEV="8c0568f70bd54190411da9926861543f166ebcb4"
F="tests/memory/test_memctl_inspect_scope.py tests/memory/test_operability.py tests/memory/test_cli_read_commands.py tests/memory/test_memctl_service_close.py tests/memory/test_memory_review_cli.py"
RM="tests/memory tests/integration/test_operability.py"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
PGX="tests/memory/test_operability_postgres.py"
PROBE="/tmp/ev/probe_inspect_scope.py"
P=.venv/bin/python; DEV=${DEV:-a41487ab1b51f7fd526ff29cd526f0b98dc01fc7}
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph|typer) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
mkdir -p /tmp/ev
cat > /tmp/ev/probe_inspect_scope.py <<'PYEOF_X'
"""`memctl inspect` without `--scope`: the command as a user runs it.

Seeds a SQLite memory database with one fact in scope `agent:a` (the store is
closed before the commands run) and an empty trace file. Then runs, each as its
own process (`python -m openminion.modules.memory`, the `memctl` entry point),
with OPENMINION_HOME and OPENMINION_DATA_ROOT in a temporary directory:
- `memctl inspect --db ... --trace-file ... --json` with no `--scope`, with
  `--scope ""`, with `--scope agent:a` (control) and with `--scope not-a-scope`;
- the same four without `--json`;
- `memctl stats --db ... --json` with no `--scope` (control: another command
  whose scope is optional).
For each it prints the exit code, the last stderr line and, for JSON output,
the `scope` and `stats.active_record_count` it reports.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_inspect_scope.py
(PYTHONPATH=src:. on Linux)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from openminion.modules.memory.models import MemoryRecord
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore

SCOPES = {
    "no --scope": [],
    '--scope ""': ["--scope", ""],
    "--scope agent:a (control)": ["--scope", "agent:a"],
    "--scope not-a-scope": ["--scope", "not-a-scope"],
}


def memctl(root, *args):
    env = {
        **os.environ,
        "OPENMINION_HOME": str(root / "home"),
        "OPENMINION_DATA_ROOT": str(root / "data"),
        "_TYPER_STANDARD_TRACEBACK": "1",
    }
    done = subprocess.run(
        [sys.executable, "-m", "openminion.modules.memory", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    lines = [line for line in done.stderr.splitlines() if line.strip()]
    return done.returncode, done.stdout, lines[-1] if lines else ""


def main():
    report = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db_path, trace_file = root / "memory.db", root / "trace.jsonl"
        trace_file.write_text("", encoding="utf-8")
        store = SQLiteMemoryStore(db_path, artifactctl=None)
        store.put(
            MemoryRecord(
                id="r1",
                scope="agent:a",
                type="fact",
                title="Prefer tabs",
                content="I prefer tabs.",
                created_at="2026-10-01T00:00:00+00:00",
                updated_at="2026-10-01T00:00:00+00:00",
            )
        )
        store.close()
        common = ["--db", str(db_path), "--trace-file", str(trace_file)]
        for label, scope_args in SCOPES.items():
            code, out, err = memctl(root, "inspect", *scope_args, *common, "--json")
            row = {"exit code": code, "last stderr line": err}
            if code == 0:
                payload = json.loads(out)
                row["scope"] = payload.get("scope")
                row["stats.active_record_count"] = (payload.get("stats") or {}).get(
                    "active_record_count"
                )
            report[f"memctl inspect {label} --json"] = row
        for label, scope_args in SCOPES.items():
            code, out, err = memctl(root, "inspect", *scope_args, *common)
            report[f"memctl inspect {label}"] = {
                "exit code": code,
                "last stderr line": err,
                "prints 'Memory inspect'": "Memory inspect" in out,
            }
        code, out, err = memctl(root, "stats", "--db", str(db_path), "--json")
        report["memctl stats --json, no --scope (control)"] = {
            "exit code": code,
            "last stderr line": err,
            "active_record_count": json.loads(out).get("active_record_count")
            if code == 0
            else None,
        }
    print(json.dumps(report, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
PYEOF_X
[ -n "${PROBE:-}" ] && sha256sum /tmp/ev/*
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{240}).*/\1 .../'; }
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"; fi
  sec "[$tree] pytest <focused>"; pyt $F
  if [ -n "${R:-}" ]; then sec "[$tree] pytest <related>"; pyt $R; fi
  if [ -n "${RM:-}" ]; then sec "[$tree] pytest -m \"$RMARK\" $RM"; pyt -m "$RMARK" $RM; fi
  if [ -n "${PGX:-}" ]; then sec "[$tree] pytest -m postgres <postgres>"; pyt -m postgres $PGX; fi
  if [ -n "${PROBE:-}" ]; then sec "[$tree] the issue's probe: PYTHONPATH=src:. python $(basename $PROBE)"; PYTHONPATH=src:. $P $PROBE 2>&1 | grep -v "^20[0-9][0-9]-"; fi
  if [ -n "${PROBE2:-}" ]; then sec "[$tree] PYTHONPATH=src:. python $(basename $PROBE2)"; PYTHONPATH=src:. $P $PROBE2 2>&1 | grep -v "^20[0-9][0-9]-"; fi
done
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -40; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
