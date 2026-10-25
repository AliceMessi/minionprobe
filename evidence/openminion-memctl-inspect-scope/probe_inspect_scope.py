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
