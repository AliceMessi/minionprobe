"""#352: open a run-status history with installed 0.1.11 and check the readers.

--mode fresh    the history was written by 0.1.11 (`probe_352_history.py`).
--mode upgrade  the history was written by 0.1.10 (runtime schema head 11; 0.1.10
                has neither #363's migration 12 nor #372's migration 13). The
                0.1.10 writer is closed before this runs (coordinated upgrade:
                stop writers). On SQLite this probe first copies the database
                to a pre-upgrade backup; on PostgreSQL the steps file takes a
                pg_dump of the schema before this probe runs.
  --step12      first apply only 0.1.11's runtime migrations <= 12 with the
                package's own runner (migrate_database / migrate_record_store),
                giving a genuine migration-12 database, then open it normally
                (build_runtime_storage), which applies migration 13. Without
                --step12 the normal open applies 12 and 13 together.

Then, for both sessions of the history ("window": the maintainer's probe;
"mixed": a long run spanning the 2,000-event window, interleaved runs/threads,
NaN/Infinity/NUL/padded/token values):
- every stored event row is byte-identical before and after (sha256);
- event_run_facts has exactly one row per event, equal to 0.1.11's
  event_run_facts() and to the 0.1.10 expressions;
- list_session_runs, list_session_run_events and resolve_thread_lifecycle equal
  a plain event-by-event reduction over all stored events (probe_352_lib.Oracle);
- the maintainer's probe values; the long runs' start/end/count/state/step/error;
- the rows each reader reads at the store's backend boundary are bounded.
The reader outputs (session ids removed) are hashed so fresh and upgraded
databases can be compared (probe_352_compare.py).

Usage: python -I probe_352_check.py <work_dir> <out.json> <sqlite:PATH|postgres:SCHEMA> --mode fresh|upgrade [--step12] [--seed <history.json>]
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

import probe_352_lib as lib  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
TARGET = sys.argv[3]
args = sys.argv[4:]
MODE = args[args.index("--mode") + 1]
STEP12 = "--step12" in args
SEED = Path(args[args.index("--seed") + 1]) if "--seed" in args else None
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
kind, value = lib.parse_target(TARGET)
label = f"{kind}:{Path(value).name if kind == 'sqlite' else value}"
info = banner("probe_352_check", target=label, mode=MODE, step12=STEP12)

from openminion.modules.storage.runtime.session_store.backend import RuntimeSessionStoreBackend  # noqa: E402
from openminion.modules.task.run import list_session_run_events, list_session_runs, resolve_thread_lifecycle  # noqa: E402

c = Checks()
result: dict = {"info": info}

pre_rows = lib.stored_event_rows(TARGET)
pre_digest = lib.digest(pre_rows)
pre_ledger = lib.ledger(TARGET)
result["before"] = {"ledger": pre_ledger, "stored_events": len(pre_rows), "events_digest_sha256": pre_digest,
                    "event_run_facts_table": lib.has_table(TARGET, "event_run_facts")}
if SEED is not None:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    c.check("stored events equal the writer's record (sha256 of every event row)",
            seed["events_digest_sha256"] == pre_digest,
            {"writer_version": seed["info"]["openminion.__version__"], "digest": pre_digest, "events": len(pre_rows)})

if MODE == "upgrade":
    c.check("pre-upgrade database is the 0.1.10 schema: ledger 1..11, no event_run_facts table",
            pre_ledger == list(range(1, 12)) and not result["before"]["event_run_facts_table"],
            {"ledger": pre_ledger, "event_run_facts_table": result["before"]["event_run_facts_table"]})
    if kind == "sqlite":
        backup = WORK / "backup-pre-upgrade.db"
        for suffix in ("-wal", "-shm"):
            if Path(value + suffix).exists():
                c.check(f"no {suffix} file after the writer stopped", False, str(Path(value + suffix).name))
        shutil.copy2(value, backup)
        result["backup"] = {"file": backup.name, "sha256": lib.file_sha256(backup)}
        c.check("pre-upgrade backup taken (file copy, sha256 equal to the database)",
                lib.file_sha256(backup) == lib.file_sha256(Path(value)), result["backup"])
    else:
        result["backup"] = "pg_dump of the schema taken by steps_352.sh before this probe (see the run log)"
    if STEP12:
        res = lib.migrate_target(TARGET, lib.migrations_upto(12))
        rows12 = lib.stored_event_rows(TARGET)
        led12 = lib.ledger(TARGET)
        result["migration_12_only"] = {"applied_versions": list(res.applied_versions), "ledger": led12,
                                       "event_run_facts_table": lib.has_table(TARGET, "event_run_facts")}
        c.check("0.1.11 migrations <= 12 applied alone: a migration-12 database (ledger 1..12, no event_run_facts), events unchanged",
                list(res.applied_versions) == [12] and led12 == list(range(1, 13))
                and not result["migration_12_only"]["event_run_facts_table"] and lib.digest(rows12) == pre_digest,
                result["migration_12_only"])

storage = lib.open_storage(TARGET, WORK)
try:
    applied = list(storage.migration_result.applied_versions)
    expected_applied = [] if MODE == "fresh" else ([13] if STEP12 else [12, 13])
    post_rows = lib.stored_event_rows(TARGET)
    post_ledger = lib.ledger(TARGET)
    result["after_open"] = {"applied_versions": applied, "current_version": storage.migration_result.current_version,
                            "ledger": post_ledger, "stored_events": len(post_rows)}
    c.check("normal open (build_runtime_storage) applies the expected migrations; ledger 1..13",
            applied == expected_applied and post_ledger == list(range(1, 14)),
            {"applied_versions": applied, "expected": expected_applied, "ledger_max": max(post_ledger)})
    c.check("every stored event row unchanged by the open/upgrade (sha256 of all event rows)",
            lib.digest(post_rows) == pre_digest, {"events": len(post_rows), "digest": lib.digest(post_rows)})
    facts = lib.check_facts_table(TARGET)
    c.check("event_run_facts: one row per event, equal to 0.1.11 event_run_facts() and to the 0.1.10 expressions; ASCII keys without NUL",
            facts["ok"], facts)

    sessions = storage.sessions
    sids = {r["target"]: r["id"] for r in lib.raw_query(TARGET, "SELECT id, target FROM sessions")}
    window, mixed = sids["window"], sids["mixed"]

    def record(name, ok, observed=None):
        return c.check(name, ok, observed)

    dump = {
        "window": lib.compare_readers(sessions, TARGET, window, "window", record=record,
                                      thread_requests=[("conv", "thread"), ("<session>", "thread"), ("", "thread"), ("conv", "missing")]),
        "mixed": lib.compare_readers(sessions, TARGET, mixed, "mixed", record=record,
                                     thread_requests=[("conv", "thread-a"), ("conv", "thread-b"), ("conv2", "thread-c"),
                                                      ("<session>", "thread-a"), ("", "thread-b"), ("conv", "missing"),
                                                      ("trace: 1e999", "[NaN]"), (f" conv{lib.PADDING}", f"{lib.PADDING}thread-a")]),
    }

    # The maintainer's probe (issue body), on this database.
    runs = [(r.run_id, r.state) for r in list_session_runs(sessions, session_id=window)]
    new_events = len(list_session_run_events(sessions, session_id=window, run_id="new"))
    p = resolve_thread_lifecycle(sessions, session_id=window, conversation_id="conv", thread_id="thread")
    hw = sessions.event_high_water(session_id=window)
    probe = {"runs": runs, "new_run_events": new_events,
             "projection": {k: getattr(p, k) for k in ("latest_run_id", "latest_run_state", "invocation_id", "latest_event_id")},
             "high_water": hw}
    result["maintainer_probe"] = probe
    c.check("maintainer's probe: runs [new failed, old completed], 2 new-run events, projection new/failed/new-inv at the high water",
            runs == [("new", "failed"), ("old", "completed")] and new_events == 2
            and probe["projection"] == {"latest_run_id": "new", "latest_run_state": "failed", "invocation_id": "new-inv", "latest_event_id": hw},
            probe)

    # The long runs of "mixed", reported over all of their events.
    oracle = lib.Oracle(TARGET, mixed)
    listed = {r.run_id: r for r in list_session_runs(sessions, session_id=mixed, limit=20, scan_limit=20)}
    long_events = [i for i in oracle.items if i["run_id"] == "long"]
    long2_events = [i for i in oracle.items if i["run_id"] == "long2"]
    lr, lr2 = listed.get("long"), listed.get("long2")
    first_mixed = oracle.all[0]
    lr_obs = None if lr is None else {k: getattr(lr, k) for k in ("state", "current_step", "started_at", "ended_at", "event_count", "error")}
    window_ids = [e["id"] for e in oracle.run_rows[-2000:]]
    c.check("mixed: run 'long' starts at the session's first event (below the 2,000 run-event window) and is reported over all its events",
            lr is not None and lr.started_at == first_mixed["created_at"] and lr.event_count == len(long_events)
            and lr.state == "running" and lr.ended_at == "" and lr.current_step == "final-step"
            and long_events[0]["id"] < window_ids[0],
            {"long": lr_obs, "long_events": len(long_events), "first_long_event_id": long_events[0]["id"],
             "oldest_event_id_in_default_window": window_ids[0], "run_events_in_session": len(oracle.run_rows)})
    lr2_obs = None if lr2 is None else {k: getattr(lr2, k) for k in ("state", "started_at", "ended_at", "event_count")}
    c.check("mixed: run 'long2' (started below the window, completed at the end) reports its first start, last end and full count",
            lr2 is not None and lr2.state == "completed" and lr2.started_at == long2_events[0]["created_at"]
            and lr2.ended_at == long2_events[-1]["created_at"] and lr2.event_count == len(long2_events),
            {"long2": lr2_obs, "long2_events": len(long2_events)})

    # Bounded reads on this history, counted at the backend boundary.
    reads: list[int] = []
    real_query = RuntimeSessionStoreBackend.query_dicts

    def counting(self, sql, params=None):
        rows = real_query(self, sql, params)
        reads.append(len(rows))
        return rows

    def measured(fn):
        reads.clear()
        with patch.object(RuntimeSessionStoreBackend, "query_dicts", counting):
            fn()
        return list(reads)

    bounds = {
        "list_session_runs(limit=5, scan_limit=10)": measured(lambda: list_session_runs(sessions, session_id=mixed, limit=5, scan_limit=10)),
        "list_session_runs(defaults)": measured(lambda: list_session_runs(sessions, session_id=mixed)),
        "list_session_run_events(long, limit=5)": measured(lambda: list_session_run_events(sessions, session_id=mixed, run_id="long", limit=5)),
        "resolve_thread_lifecycle(conv, thread-a)": measured(lambda: resolve_thread_lifecycle(sessions, session_id=mixed, conversation_id="conv", thread_id="thread-a")),
    }
    result["bounded_reads_rows_per_query"] = bounds
    b = list(bounds.values())
    c.check("bounded returned rows: runs = 1 aggregate (<= window rows) + 1 read by id (<= 4 per listed run); run events = [limit]; thread = messages + 1 aggregate row + <= 4 rows",
            len(b[0]) == 2 and b[0][0] <= 10 and b[0][1] <= 20 and len(b[1]) == 2 and b[1][1] <= 80
            and b[2] == [5] and len(b[3]) == 3 and b[3][1] == 1 and b[3][2] <= 4,
            bounds)

    result["readers_digest_sha256"] = lib.readers_digest(dump)
    result["events_content_digest_sha256"] = lib.digest([{k: v for k, v in r.items() if k != "session_id"} for r in post_rows])
finally:
    storage.close()
    lib.dispose_engines()

result["checks"] = c.items
result["summary"] = c.summary()
write_result(OUT, result)
print("summary:", result["summary"], "| readers digest:", result.get("readers_digest_sha256"))
