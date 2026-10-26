"""#369: upgrade a pre-migration-15 history (written by 0.1.11) with the installed 0.1.12.

    python -I probe_369_upgrade.py <case> <sqlite|postgres> <work> <out.json> (--db <path> | --schema <name>) --seed <seed.json>

The database was written by openminion==0.1.11 in its own process
(probe_369_seed.py), which has exited; the pre-upgrade backup was taken before
this process starts (steps_369*.sh). This is the first 0.1.12 process to open
it. Before opening, the ledger and stored metadata are read with plain
sqlite3 / SQLAlchemy, so nothing is migrated by the read.

cases:
  normal      13 -> open with 0.1.12: migrations 14 and 15 applied once; metadata unchanged;
              rows = json.loads-derived rows; filters; reopen applies nothing; writes after the upgrade
  via14       13 -> 14 with the package's own migration list stopped before 15 -> open: 15 applied alone
  malformed   a stored metadata_json that is not JSON (zz-bad, second backfill batch): migration 15 fails
              naming the session, rolls back with its ledger entry; repair; rerun applies 15 once
  extraction  a non-JSON error raised by the row extraction for zz-last (second batch): rolled back; rerun
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402
import probe_369_lib as L  # noqa: E402

CASE, BACKEND = sys.argv[1], sys.argv[2]
WORK = Path(sys.argv[3]).resolve()
OUT = Path(sys.argv[4])
ARGS = sys.argv[5:]


def arg(name: str) -> str | None:
    return ARGS[ARGS.index(name) + 1] if name in ARGS else None


PGURL = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
quiet()
clear_override_env()
checks = Checks()
info = banner(f"probe_369_upgrade {CASE}", backend=BACKEND)
assert L.IS_0112, f"run with openminion 0.1.12, not {L.VERSION}"
SEED = json.loads(Path(arg("--seed")).read_text(encoding="utf-8"))
DB = Path(arg("--db")).resolve() if arg("--db") else None
SCHEMA = arg("--schema")
URL = L.schema_url(PGURL, SCHEMA) if SCHEMA else None
if BACKEND == "postgres":
    info["postgres_server_version"] = L.pg_server_version(PGURL)


# ---------------------------------------------------------------- raw reads (no migration)
def raw_state() -> dict[str, Any]:
    if BACKEND == "sqlite":
        conn = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True)
        try:
            led = [(int(v), str(n)) for v, n in conn.execute("SELECT version, name FROM migrations ORDER BY version")]
            meta = {str(i): str(m) for i, m in conn.execute("SELECT id, metadata_json FROM sessions")}
            exists = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_metadata_strings'").fetchall())
            rows = (set(map(tuple, conn.execute("SELECT session_id, key, value, entry_digest FROM session_metadata_strings")))
                    if exists else set())
        finally:
            conn.close()
    else:
        import sqlalchemy as sa

        engine = sa.create_engine(URL)
        try:
            with engine.connect() as conn:
                led = [(int(v), str(n)) for v, n in conn.execute(sa.text("SELECT version, name FROM migrations ORDER BY version"))]
                meta = {str(i): str(m) for i, m in conn.execute(sa.text("SELECT id, metadata_json FROM sessions"))}
                exists = bool(conn.execute(sa.text(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = 'session_metadata_strings'")).fetchall())
                rows = (set(map(tuple, conn.execute(sa.text("SELECT session_id, key, value, entry_digest FROM session_metadata_strings"))))
                        if exists else set())
        finally:
            engine.dispose()
    return {"ledger": led, "metadata": meta, "table": exists, "rows": rows}


def raw_exec(sql: str, params: tuple) -> None:
    """A repair statement outside openminion (as an operator would run it)."""
    if BACKEND == "sqlite":
        conn = sqlite3.connect(str(DB))
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
    else:
        import sqlalchemy as sa

        engine = sa.create_engine(URL)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(sql.replace("?", "%s"), params)
        finally:
            engine.dispose()


def open_012():
    return L.open_storage("sqlite", db_path=DB) if BACKEND == "sqlite" else L.open_storage("postgres", url=URL, work=WORK)


def versions(led) -> list[int]:
    return [v for v, _ in led]


def check_upgraded(label: str, storage, pre: dict[str, Any]) -> None:
    led = L.ledger(storage)
    meta = L.stored_metadata(storage)
    checks.check(f"{label}: ledger has 14 and 15 exactly once (1..15)", versions(led) == list(range(1, 16)), observed=versions(led)[-4:])
    checks.check(f"{label}: stored metadata_json unchanged byte for byte ({len(meta)} sessions)",
                 meta == pre["metadata"] and L.metadata_digest(meta) == SEED["metadata_sha256"],
                 observed=(len(meta), L.metadata_digest(meta)[:16]))
    rows = L.stored_rows(storage)
    exp = L.expected_rows(meta)
    checks.check(f"{label}: backfilled rows = json.loads-derived rows ({len(exp)} rows, incl. NaN/Infinity/NUL/surrogate documents)",
                 rows == exp, observed=(len(rows), len(exp), sorted(exp - rows)[:2], sorted(rows - exp)[:2]))
    odd_ids = {r[0] for r in rows if r[0].startswith("odd-")}
    checks.check(f"{label}: sessions PostgreSQL cannot parse got their string rows",
                 {"odd-nan-other", "odd-inf-other", "odd-nul-other", "odd-surrogate-other", "odd-nul-key", "odd-long"} <= odd_ids,
                 observed=sorted(odd_ids))


def check_filters(label: str, storage) -> None:
    s = storage.sessions
    if BACKEND == "postgres":
        wrong = []
        for flt, expected in L.FILTER_EXPECTED:
            got = L.outcome_ids(s, flt, "legacy-matrix")
            if got != sorted(expected):
                wrong.append((L.show_filter(flt), got))
        for flt, expected in L.POSTGRES_ODD_EXPECTED:
            got = L.outcome_ids(s, flt, "legacy-odd")
            if got != sorted(expected):
                wrong.append((L.show_filter(flt), got))
        checks.check(f"{label} (postgres): 34 table filters + 23 odd filters select their sessions on the upgraded history",
                     not wrong, observed=wrong[:4] or "57/57")
    else:
        obs = SEED["observations"]
        wrong = []
        for key, value in obs.items():
            kind, _, _ = key.partition(" ")
            if kind == "matrix":
                flt = next(f for f, _ in L.FILTER_EXPECTED if f"matrix {L.show_filter(f)}" == key)
                got = L.outcome_ids(s, flt, "legacy-matrix")
            elif kind == "odd":
                flt = next(f for f, _ in L.POSTGRES_ODD_EXPECTED if f"odd {L.show_filter(f)}" == key)
                got = L.outcome_ids(s, flt, "legacy-odd")
            else:
                flt = next(f for f in L.LEGACY_FILTERS if f"legacy {L.show_filter(f)}" == key)
                got = L.outcome_ids(s, flt, "legacy")
            if got != value:
                wrong.append((key, value, got))
        checks.check(f"{label} (sqlite): every filter returns what 0.1.11 returned on this history before the upgrade",
                     not wrong, observed=wrong[:4] or f"{len(obs)} identical")
    for flt, limit in (({"surface": "acp"}, 25), ({"surface": "cli", "workspace_root": "/work/3"}, 10)):
        for newest_first in (True, False):
            got = L.selected(s, flt, "legacy", limit=limit, newest_first=newest_first)
            ref = L.reference_ids(storage, flt, "legacy", limit=limit, newest_first=newest_first)
            checks.check(f"{label}: legacy {L.show_filter(flt)} limit={limit} newest_first={newest_first} = reference order",
                         got == ref and len(got) == limit, observed=(len(got), got[:3]))
    if BACKEND == "postgres":
        tag = L.outcome_ids(s, {"tag": "s" + L.LONE_HIGH}, "legacy")
        checks.check(f"{label} (postgres): legacy lone-surrogate value filter selects the 24 sessions that carry it",
                     isinstance(tag, list) and len(tag) == 24, observed=tag if isinstance(tag, str) else len(tag))
        nul = L.outcome_ids(s, {"note": "n" + L.NUL + "ul"}, "legacy")
        checks.check(f"{label} (postgres): legacy escaped-NUL value filter selects the 24 sessions that carry it",
                     isinstance(nul, list) and len(nul) == 24, observed=nul if isinstance(nul, str) else len(nul))
    acp = s.list_sessions(limit=100, metadata_filter={"surface": "acp"}, target="local-client")
    checks.check(f"{label}: the pre-upgrade ACP-style session is found by the ACP surface filter",
                 [r.id for r in acp] == ["legacy-acp-client"], observed=[r.id for r in acp])
    focus = s.list_sessions(limit=20, agent_id="alpha", target="focus", metadata_filter={"working_dir": "/w/focus"})
    checks.check(f"{label}: the pre-upgrade Focus-style session is found by agent+target+working_dir",
                 [r.id for r in focus] == ["legacy-focus-client"], observed=[r.id for r in focus])


def reopen_and_write(label: str, storage_led: list) -> None:
    again = open_012()
    try:
        checks.check(f"{label}: reopening applies no migration (idempotent)", tuple(again.migration_result.applied_versions) == (),
                     observed=again.migration_result.applied_versions)
        checks.check(f"{label}: ledger unchanged after reopen", L.ledger(again) == storage_led)
        s = again.sessions
        s.update_session_metadata(session_id="legacy-0003", patch={"surface": "acp", "workspace_root": "/work/new"})
        s.resolve_session(agent_id="main", channel="console", target="legacy", session_id="after-upgrade",
                          metadata={"surface": "focus", "score": L.NAN})
        checks.check(f"{label}: writes after the upgrade keep rows = stored metadata",
                     L.stored_rows(again) == L.expected_rows(L.stored_metadata(again)))
        got = L.outcome_ids(s, {"workspace_root": "/work/new"}, "legacy")
        checks.check(f"{label}: an updated legacy session matches its new value", got == ["legacy-0003"], observed=got)
        assert s.delete_session("after-upgrade")
        checks.check(f"{label}: deleting a session after the upgrade removes its rows",
                     "after-upgrade" not in {r[0] for r in L.stored_rows(again)})
    finally:
        again.close()


pre = raw_state()
info["pre"] = {"ledger_head": max(versions(pre["ledger"])), "sessions": len(pre["metadata"]), "table": pre["table"]}
checks.check("pre-upgrade: ledger head 13 written by 0.1.11, no session_metadata_strings, metadata = seed",
             max(versions(pre["ledger"])) == 13 and not pre["table"] and L.metadata_digest(pre["metadata"]) == SEED["metadata_sha256"],
             observed=info["pre"])

if CASE == "normal":
    storage = open_012()
    try:
        applied = tuple(storage.migration_result.applied_versions)
        checks.check("normal: the first 0.1.12 open applies exactly migrations 14 and 15", applied == (14, 15), observed=applied)
        check_upgraded("normal", storage, pre)
        check_filters("normal", storage)
        led = L.ledger(storage)
    finally:
        storage.close()
    reopen_and_write("normal", led)

elif CASE == "via14":
    from openminion.modules.storage.runtime.migrations import DEFAULT_MIGRATIONS, migrate_database, migrate_record_store

    before15 = tuple(m for m in DEFAULT_MIGRATIONS if m.version < 15)
    if BACKEND == "sqlite":
        res = migrate_database(DB, migrations=before15)
    else:
        from openminion.modules.storage.backends.postgres import RecordStorePostgres

        store = RecordStorePostgres(URL)
        try:
            res = migrate_record_store(store, backend_type="postgres", migrations=before15)
        finally:
            store.close()
    mid = raw_state()
    checks.check("via14: the package's migrations up to 14 apply only 14", tuple(res.applied_versions) == (14,) and res.current_version == 14,
                 observed=(res.applied_versions, res.current_version))
    checks.check("via14: at migration 14 there is no session_metadata_strings and metadata is unchanged",
                 versions(mid["ledger"])[-1] == 14 and not mid["table"] and mid["metadata"] == pre["metadata"])
    storage = open_012()
    try:
        applied = tuple(storage.migration_result.applied_versions)
        checks.check("via14: the next 0.1.12 open applies migration 15 alone", applied == (15,), observed=applied)
        check_upgraded("via14", storage, pre)
        check_filters("via14", storage)
        led = L.ledger(storage)
    finally:
        storage.close()
    reopen_and_write("via14", led)

elif CASE in ("malformed", "extraction"):
    from openminion.modules.storage.runtime.session_store import metadata_strings

    calls = {"n": 0}
    original = metadata_strings.metadata_string_rows

    def counting(session_id: str, stored: str):
        calls["n"] += 1
        if CASE == "extraction" and session_id == "zz-last":
            raise RuntimeError(f"extraction failed for {session_id}")
        return original(session_id, stored)

    error = None
    with patch.object(metadata_strings, "metadata_string_rows", counting):
        try:
            storage = open_012()
            storage.close()
        except Exception as exc:  # noqa: BLE001
            error = exc
    msg = "" if error is None else f"{type(error).__name__}: {error}"
    info["error"] = msg
    info["extraction_calls_before_failure"] = calls["n"]
    print("open error:", msg[:400])
    victim = "zz-bad" if CASE == "malformed" else "zz-last"
    if CASE == "malformed":
        named = error is not None and "Migration v15 (add_session_metadata_strings) failed" in msg and repr(victim) in msg
    else:
        named = error is not None and f"extraction failed for {victim}" in msg
    checks.check(f"{CASE}: the 0.1.12 open raises while applying migration 15, naming {victim!r}", named, observed=msg[:300])
    checks.check(f"{CASE}: the backfill had read past the first 1,000-session batch before failing (rows of batch 1 were already inserted)",
                 calls["n"] > 1000, observed=calls["n"])
    post = raw_state()
    checks.check(f"{CASE}: ledger has no entry 15 (14 committed in its own transaction before 15)",
                 15 not in versions(post["ledger"]) and versions(post["ledger"])[-1] == 14, observed=versions(post["ledger"])[-3:])
    checks.check(f"{CASE}: migration 15 left nothing: no session_metadata_strings table, no rows",
                 not post["table"] and not post["rows"], observed=post["table"])
    checks.check(f"{CASE}: stored metadata unchanged by the failed migration", post["metadata"] == pre["metadata"])
    if CASE == "malformed":
        raw_exec("UPDATE sessions SET metadata_json = ? WHERE id = ?", ('{"surface": "acp", "repaired": "yes"}', "zz-bad"))
        print("repaired zz-bad with an UPDATE outside openminion")
    storage = open_012()
    try:
        applied = tuple(storage.migration_result.applied_versions)
        checks.check(f"{CASE}: after {'repair' if CASE == 'malformed' else 'removing the injected failure'} the rerun applies 15 once",
                     applied == (15,), observed=applied)
        led = L.ledger(storage)
        checks.check(f"{CASE}: ledger 1..15, each once", versions(led) == list(range(1, 16)), observed=versions(led)[-3:])
        rows = L.stored_rows(storage)
        meta = L.stored_metadata(storage)
        checks.check(f"{CASE}: rows = json.loads-derived rows after the rerun", rows == L.expected_rows(meta), observed=len(rows))
        if CASE == "malformed":
            got = L.outcome_ids(storage.sessions, {"repaired": "yes"}, "legacy-last")
            checks.check("malformed: the repaired session is filterable", got == ["zz-bad"], observed=got)
    finally:
        storage.close()
else:
    raise SystemExit(f"unknown case {CASE}")

summary = checks.summary()
write_result(OUT, {**info, "case": CASE, "summary": summary, "checks": checks.items})
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if checks.all_pass else 1)
