"""`memctl inspect`: the history summary with the store memctl builds.

Seeds a SQLite memory database with a keyed fact that was updated once
(store.put, then store.upsert on the same scope/type/key), so the key has a
history of depth 2. Then:
1. `memctl history` for that key, as a separate process, to show the history
   the CLI sees;
2. `memctl inspect --scope agent:a --db ... --json` as a separate process
   (`python -m openminion.modules.memory`, the `memctl` entry point), and the
   same command without `--json`;
3. build_inspect_payload (the function `memctl inspect` calls) with the service
   that memctl's `_get_service` builds, and with a MemoryService over the bare
   SQLiteMemoryStore.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_inspect_history.py
(PYTHONPATH=src:. on Linux)
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from openminion.modules.memory import cli as memory_cli
from openminion.modules.memory.diagnostics.cli import build_inspect_payload
from openminion.modules.memory.models import MemoryRecord
from openminion.modules.memory.service import MemoryService
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore


def memctl(root, *args):
    completed = subprocess.run(
        [sys.executable, "-m", "openminion.modules.memory", *args],
        env={**os.environ, "OPENMINION_HOME": str(root / "home"),
             "OPENMINION_DATA_ROOT": str(root / "data")},
        capture_output=True,
        text=True,
        timeout=600,
    )
    return completed.returncode, completed.stdout.replace(str(root), "<tmp>")


def main():
    report = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db_path = root / "memory.db"
        trace_file = root / "trace.jsonl"
        trace_file.write_text("", encoding="utf-8")
        store = SQLiteMemoryStore(db_path, artifactctl=None)
        store.put(
            MemoryRecord(
                id="v1",
                scope="agent:a",
                type="fact",
                key="pref:indent",
                title="Prefer tabs",
                content="I prefer tabs.",
                created_at="2026-10-01T00:00:00+00:00",
                updated_at="2026-10-01T00:00:00+00:00",
            )
        )
        store.upsert(
            "agent:a", "fact", "pref:indent", {"title": "Prefer spaces", "content": "I prefer spaces."}
        )

        code, out = memctl(root, "history", "--scope", "agent:a", "--type", "fact",
                           "--key", "pref:indent", "--db", str(db_path), "--json")
        history = json.loads(out) if code == 0 else out
        report["memctl history --scope agent:a --type fact --key pref:indent --json"] = {
            "exit code": code,
            "record titles": [r.get("title") if isinstance(r, dict) else r for r in history]
            if isinstance(history, list) else history,
        }

        common = ("inspect", "--scope", "agent:a", "--db", str(db_path), "--trace-file", str(trace_file))
        code, out = memctl(root, *common, "--json")
        payload = json.loads(out) if code == 0 else {}
        report["memctl inspect --scope agent:a --db <tmp>/memory.db --json"] = {
            "exit code": code,
            "stats.active_record_count": (payload.get("stats") or {}).get("active_record_count"),
            "stats.supersession_chain_count": (payload.get("stats") or {}).get(
                "supersession_chain_count"
            ),
            "history_summary": payload.get("history_summary", "(missing)"),
        }
        code, out = memctl(root, *common)
        report["memctl inspect --scope agent:a --db <tmp>/memory.db"] = {
            "exit code": code,
            "prints a 'Deepest history' line": "Deepest history" in out,
        }

        service = memory_cli._get_service(str(db_path))
        bare = MemoryService(store=SQLiteMemoryStore(db_path, artifactctl=None))
        for label, svc in (
            ("memctl's _get_service(db)", service),
            ("MemoryService over the bare SQLiteMemoryStore", bare),
        ):
            store_type = type(svc._store).__name__
            if hasattr(svc._store, "_sink"):
                store_type += f" wrapping {type(svc._store._store).__name__}"
            payload = build_inspect_payload(
                service=svc, scope="agent:a", db_path=db_path, trace_file=trace_file
            )
            summary = payload["history_summary"]
            report[f"build_inspect_payload with {label}"] = {
                "service store": store_type,
                "history_summary": None
                if summary is None
                else {k: summary.get(k) for k in ("scope", "type", "key", "depth")},
            }
        service._store._sink.close()
    print(json.dumps(report, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
