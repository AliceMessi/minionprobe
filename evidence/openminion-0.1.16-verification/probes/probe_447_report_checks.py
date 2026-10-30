"""Checks over the #447 report's own probes, run unchanged on the installed package.

    python -I probe_447_report_checks.py <retention.json> <repro.txt> <out.json>

<retention.json> is the output of probe_447_report_retention.py (byte-identical to
the report's probe_failed_start_retention.py, sha256 6d556cd1...), <repro.txt> the
stdout of probe_447_report_repro.py (byte-identical to the report's
repro_failed_start.py, sha256 da974dfc...). This script only reads those outputs:
it imports nothing from openminion and adds no behavior of its own.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, write_result  # noqa: E402

RET, REPRO, OUT = (Path(p) for p in sys.argv[1:4])
report = json.loads(RET.read_text(encoding="utf-8"))
repro = REPRO.read_text(encoding="utf-8", errors="replace")
cases = report.get("cases", {})
windows = "windows" in str(report.get("versions", {}).get("platform", "")).lower()
checks = Checks()

ITEM = {
    "sqlite_memory_provider": 4,
    "sqlite_437_point": 2,
    "postgres_runtime_migration": 4,
    "postgres_437": 1,
    "sqlite_started_then_closed": 1,
    "postgres_storage_closed": 3,
}
MEANING = {
    "sqlite_memory_provider": "the unsupported-memory-provider start failure (ConfigError), SQLite",
    "sqlite_437_point": "an injected failure at the SQLite connection read in the constructor's arguments",
    "postgres_runtime_migration": "the real runtime migration failure (existing sessions table, MigrationError), PostgreSQL",
    "postgres_437": "the #437 PostgreSQL start (starts since 0.1.15), then close()",
    "sqlite_started_then_closed": "control: SQLite start, then close()",
    "postgres_storage_closed": "control: build_runtime_storage on PostgreSQL with the caller's telemetry service, then close()",
}
for case, result in cases.items():
    item = ITEM.get(case, 2)
    tag = f"item {item}: report probe {case} ({MEANING.get(case, case)})"
    first = result.get("at_failure") or result.get("after_close") or {}
    when = "right after the exception" if "at_failure" in result else "after close()"
    if "at_failure" in result:
        checks.check(f"{tag}: the caller gets the expected exception type", result.get("exception_type_as_expected") is True,
                     observed=result.get("exception"))
    else:
        checks.check(f"{tag}: the start (or build) succeeds", result.get("outcome") in ("started",) or
                     str(result.get("outcome", "")).startswith("built"), observed=result.get("outcome"))
    if case == "postgres_storage_closed":
        # The probe's own TelemetryService, which build_runtime_storage only borrows through the
        # storage hook, keeps its database open until the probe drops it: the storage must not close it.
        held = first.get("open_files_under_root") or []
        checks.check(f"{tag}: {when}, only the probe's borrowed telemetry database is still open (the storage "
                     f"closed what it owned and left the borrowed telemetry service to its owner)",
                     bool(held) and all(f.startswith(".openminion/telemetry/telemetry.db") for f in held),
                     observed=held)
    else:
        checks.check(f"{tag}: {when}, with gc off and before gc.collect(), no file under the case root is open",
                     first.get("open_files_under_root") == [], observed=first.get("open_files_under_root"))
    if windows:
        checks.check(f"{tag}: {when}, on native Windows every file under the case root can be renamed",
                     first.get("not_renamable_under_root") == [], observed=first.get("not_renamable_under_root"))
    threads = [t for t in first.get("new_threads_alive", []) if t.startswith(("runtimectl-", "storage-pool-health-emitter"))]
    checks.check(f"{tag}: {when}, no runtime-manager or pool-health-emitter thread is alive", threads == [],
                 observed=first.get("new_threads_alive"))
    if "pg_other_connections" in first:
        checks.check(f"{tag}: {when}, 0 other server connections to the case database",
                     first.get("pg_other_connections") == 0, observed=first.get("pg_other_connections"))
    if case != "postgres_storage_closed":
        checks.check(f"{tag}: {when}, the RuntimeStorageContext the attempt built was closed (close() ran once) or "
                     f"is gone", (first.get("context_close_calls") == 1) or (first.get("context_alive") is False),
                     observed={k: first.get(k) for k in ("contexts_built", "context_alive", "context_close_calls",
                                                         "close_runtime_components_calls", "sqlite_connection_open",
                                                         "pool_health_emitter_running")})
    checks.check(f"{tag}: the probe did not need to close anything itself", "probe_cleanup" not in result,
                 observed=result.get("probe_cleanup"))

m = re.search(r"after the exception: (\d+) open files under the root, databases: (\[[^\]]*\])", repro)
checks.check("item 4: report repro (OPENMINION_MEMORY_PROVIDER=not-a-provider, SQLite): the start fails with ConfigError",
             "start failed: ConfigError" in repro, observed=repro.splitlines()[0] if repro else None)
checks.check("item 4: report repro: after the exception, before gc.collect(), 0 open files under the root",
             bool(m) and m.group(1) == "0", observed=m.group(0) if m else repro[:300])
if windows:
    ren = re.search(r"after the exception: renaming state\.db[^\n]*", repro)
    checks.check("item 4: report repro on native Windows: state.db can be renamed right after the exception",
                 bool(ren) and ren.group(0).endswith("works"), observed=ren.group(0) if ren else None)

summary = checks.summary()
write_result(OUT, {"retention": RET.name, "repro": REPRO.name, "versions": report.get("versions"),
                   "summary": summary, "checks": checks.items})
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if checks.all_pass else 1)
