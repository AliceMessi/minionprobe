"""#352: write the run-status history with the installed openminion (0.1.10 or 0.1.11).

Writes the two sessions of `probe_352_lib.write_history` (the maintainer's
2,002-event window probe, and a ~3,300-event session with a long run spanning
the 2,000-event window, interleaved runs/threads, padded and JSON-token text,
NaN/Infinity/NUL) through the installed package's public storage API
(`build_runtime_storage`, `SessionStore.append_event`,
`append_run_state_event`), closes the writer, and records the migration
ledger, the tables and a sha256 of every stored event row.

With --verify-only it writes nothing: it opens an existing database (e.g. the
pre-upgrade backup, with 0.1.10) and records the same facts.

Usage: python -I probe_352_history.py <work_dir> <out.json> <sqlite:PATH|postgres:SCHEMA> [--verify-only]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

import probe_352_lib as lib  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
TARGET = sys.argv[3]
VERIFY_ONLY = "--verify-only" in sys.argv[4:]
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
kind, value = lib.parse_target(TARGET)
info = banner("probe_352_history", target=f"{kind}:{Path(value).name if kind == 'sqlite' else value}",
              mode="verify-only" if VERIFY_ONLY else "write")

from openminion.modules.task.run import append_run_state_event  # noqa: E402

c = Checks()
result: dict = {"info": info}
if kind == "postgres" and not VERIFY_ONLY:
    lib.pg_create_schema(value)

storage = lib.open_storage(TARGET, WORK)
try:
    result["migration_result"] = {"applied_versions": list(storage.migration_result.applied_versions),
                                  "current_version": storage.migration_result.current_version}
    if not VERIFY_ONLY:
        result["history"] = lib.write_history(storage.sessions, append_run_state_event)
finally:
    storage.close()  # the writer is stopped before anything else reads the database

rows = lib.stored_event_rows(TARGET)
result["ledger"] = lib.ledger(TARGET)
result["has_event_run_facts_table"] = lib.has_table(TARGET, "event_run_facts")
result["stored_events"] = len(rows)
result["events_digest_sha256"] = lib.digest(rows)
# Session ids are random per database; this digest leaves them out, so the same
# history written by 0.1.10 and by 0.1.11 can be compared.
result["events_content_digest_sha256"] = lib.digest([{k: v for k, v in r.items() if k != "session_id"} for r in rows])
per_session: dict[str, int] = {}
for r in rows:
    per_session[r["session_id"]] = per_session.get(r["session_id"], 0) + 1
result["events_per_session"] = sorted(per_session.values())
c.check("writer closed; events stored", len(rows) > 2000, {"stored_events": len(rows), "per_session": sorted(per_session.values())})
c.check("migration ledger head", True, {"ledger_max": max(result["ledger"]) if result["ledger"] else 0,
                                        "event_run_facts_table": result["has_event_run_facts_table"]})
lib.dispose_engines()
result["checks"] = c.items
result["summary"] = c.summary()
write_result(OUT, result)
print("events digest:", result["events_digest_sha256"], "| ledger max:", max(result["ledger"]))
