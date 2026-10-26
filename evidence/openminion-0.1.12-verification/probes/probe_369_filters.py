"""#369 on the installed package: fresh database, filters, writes, consumers.

    python -I probe_369_filters.py <sqlite|postgres> <work> <out.json> [--baseline <0.1.11 out.json>]

PostgreSQL needs OPENMINION_TEST_POSTGRES_URL (each section uses its own new
schema, dropped at the end). Runs on 0.1.12 and, as the negative control, on
0.1.11; checks that need 0.1.12 internals are listed as skipped on 0.1.11.

Sections:
  fresh        a new runtime database: migrations 1..15 once, table/indexes/FK, reopen applies nothing
  matrix       filter contract table (both backends), unfiltered listing
  differences  documented SQLite/PostgreSQL reading differences
  odd          NaN/Infinity/NUL/surrogates/long/duplicate-NaN-key metadata: PostgreSQL literal contract, stored rows
  bounded      limit and order among unparseable documents (limits 1..1000, both directions)
  collision    forced entry_digest collision: full name/value comparison
  writes       insert, replacement, keyless resolve, explicit, room, ignored conflict, update, delete, FK cascade
  rollback     writes that fail after their rows leave session and rows unchanged; fence failure
  falsey       None/0/False/""/[]/{}/() filter values sent as ""; non-string values
  concurrent   (PostgreSQL) concurrent metadata writers, racing creations, update racing delete
  acp          SQLite: the installed `openminion acp` stdio server; PostgreSQL: the installed OpenMinionACPAgent
               in-process over a PostgreSQL runtime SessionStore (APIRuntime cannot start on PostgreSQL storage,
               recorded as a note): initialize, session/new, session/list (cwd, all, limit 100)
  focus        Focus directory sessions (SQLite: real APIRuntime; PostgreSQL: runtime object over the PostgreSQL
               SessionStore): working_dir + agent + target filters, order, limit
"""

from __future__ import annotations

import gc
import json
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, write_result  # noqa: E402
import probe_369_lib as L  # noqa: E402

BACKEND = sys.argv[1]
WORK = Path(sys.argv[2]).resolve()
OUT = Path(sys.argv[3])
BASELINE = None
if "--baseline" in sys.argv:
    BASELINE = json.loads(Path(sys.argv[sys.argv.index("--baseline") + 1]).read_text(encoding="utf-8"))
PGURL = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
TAG = uuid.uuid4().hex[:8]

quiet()
clear_override_env()
WORK.mkdir(parents=True, exist_ok=True)
checks = Checks()
observations: dict[str, Any] = {}
skipped: list[str] = []
sections: dict[str, str] = {}
info = banner("probe_369_filters", backend=BACKEND)
if BACKEND == "postgres":
    if not PGURL:
        print("FAIL: OPENMINION_TEST_POSTGRES_URL not set; PostgreSQL not run")
        sys.exit(2)
    info["postgres_server_version"] = L.pg_server_version(PGURL)
    print("postgres:", info["postgres_server_version"])


def observe(section: str, flt: dict[Any, Any], value: Any) -> None:
    observations[f"{section} {L.show_filter(flt)}"] = value


def new_storage(name: str):
    """A new runtime database for one section: its own SQLite file or PostgreSQL schema."""
    if BACKEND == "sqlite":
        return L.open_storage("sqlite", db_path=WORK / f"{name}-{TAG}.db"), None
    schema = f"om369_{name}_{TAG}"
    L.create_schema(PGURL, schema)
    return L.open_storage("postgres", url=L.schema_url(PGURL, schema), work=WORK), schema


def done(storage, schema) -> None:
    try:
        storage.close()
    finally:
        if schema:
            L.drop_schema(PGURL, schema)


def section(name: str):
    def wrap(fn):
        print(f"=== {name}", flush=True)
        try:
            fn()
            sections[name] = "ran"
        except Exception as exc:  # noqa: BLE001
            sections[name] = f"raised {type(exc).__name__}"
            checks.check(f"{name}: section completed without an unexpected exception", False,
                         observed=f"{type(exc).__name__}: {str(exc)[:500]}")
            traceback.print_exc()
        return fn
    return wrap


def need_0112(what: str) -> bool:
    if L.IS_0112:
        return True
    skipped.append(f"{what}: needs 0.1.12 (no session_metadata_strings on {L.VERSION})")
    print("SKIP", skipped[-1])
    return False


# ---------------------------------------------------------------- fresh
@section("fresh")
def _fresh() -> None:
    if not need_0112("fresh"):
        return
    storage, schema = new_storage("fresh")
    try:
        first = tuple(storage.migration_result.applied_versions)
        checks.check("fresh: first open applies migrations 1..15 in order", first == tuple(range(1, 16)), observed=first)
        led = L.ledger(storage)
        versions = [v for v, _ in led]
        checks.check("fresh: ledger has each version 1..15 exactly once", versions == list(range(1, 16)), observed=versions)
        checks.check("fresh: ledger entry 15 is add_session_metadata_strings",
                     (15, "add_session_metadata_strings") in led, observed=[x for x in led if x[0] >= 14])
        checks.check("fresh: session_metadata_strings exists", L.table_exists(storage, BACKEND, "session_metadata_strings"))
        idx = L.index_names(storage, BACKEND, "session_metadata_strings")
        checks.check("fresh: indexes idx_session_metadata_strings_entry and _session exist",
                     {"idx_session_metadata_strings_entry", "idx_session_metadata_strings_session"} <= set(idx), observed=idx)
        if BACKEND == "postgres":
            fk = L.q(storage, """
                SELECT rc.delete_rule FROM information_schema.referential_constraints rc
                JOIN information_schema.table_constraints tc ON tc.constraint_name = rc.constraint_name
                 AND tc.constraint_schema = rc.constraint_schema
                WHERE tc.table_schema = current_schema() AND tc.table_name = 'session_metadata_strings'""")
        else:
            fk = L.q(storage, "SELECT \"on_delete\" AS delete_rule, \"table\" AS ref FROM pragma_foreign_key_list('session_metadata_strings')")
        checks.check("fresh: session_id references sessions ON DELETE CASCADE",
                     [str(r["delete_rule"]).upper() for r in fk] == ["CASCADE"], observed=fk)
        L.store_sessions(storage.sessions, {"fresh-a": {"surface": "acp", "n": 1}}, "m-fresh")
        rows = L.stored_rows(storage)
        checks.check("fresh: a new session's string members are written with it",
                     rows == L.expected_rows(L.stored_metadata(storage)) and len(rows) == 1, observed=sorted(rows))
    finally:
        storage.close()
    # Reopen: nothing is applied again.
    if BACKEND == "sqlite":
        again = L.open_storage("sqlite", db_path=WORK / f"fresh-{TAG}.db")
    else:
        again = L.open_storage("postgres", url=L.schema_url(PGURL, schema), work=WORK)
    try:
        checks.check("fresh: reopening applies no migration", tuple(again.migration_result.applied_versions) == (),
                     observed=again.migration_result.applied_versions)
        checks.check("fresh: ledger unchanged after reopen", [v for v, _ in L.ledger(again)] == list(range(1, 16)))
    finally:
        done(again, schema)


# ---------------------------------------------------------------- matrix
@section("matrix")
def _matrix() -> None:
    storage, schema = new_storage("matrix")
    try:
        L.store_sessions(storage.sessions, L.FILTER_SESSIONS, "m-matrix")
        for flt, expected in L.FILTER_EXPECTED:
            got = L.outcome_ids(storage.sessions, flt, "m-matrix")
            observe("matrix", flt, got)
            checks.check(f"matrix: {L.show_filter(flt)} selects {sorted(expected)}", got == sorted(expected), observed=got)
        every = L.outcome_ids(storage.sessions, {}, "m-matrix")
        checks.check("matrix: no filter lists every session", every == sorted(L.FILTER_SESSIONS), observed=len(every) if isinstance(every, list) else every)
        if L.IS_0112:
            checks.check("matrix: stored rows equal the rows derived from stored metadata",
                         L.stored_rows(storage) == L.expected_rows(L.stored_metadata(storage)))
    finally:
        done(storage, schema)


# ---------------------------------------------------------------- differences
@section("differences")
def _differences() -> None:
    storage, schema = new_storage("diff")
    try:
        L.store_sessions(storage.sessions, L.DIFF_SESSIONS, "m-diff")
        for flt, sqlite_ids, pg_ids in L.DIFF_FILTERS:
            expected = sorted(pg_ids if BACKEND == "postgres" else sqlite_ids)
            got = L.outcome_ids(storage.sessions, flt, "m-diff")
            observe("differences", flt, got)
            checks.check(f"differences ({BACKEND}): {L.show_filter(flt)} selects {expected}", got == expected, observed=got)
    finally:
        done(storage, schema)


# ---------------------------------------------------------------- odd
@section("odd")
def _odd() -> None:
    storage, schema = new_storage("odd")
    try:
        L.store_sessions(storage.sessions, L.ODD_METADATA, "m-odd")
        meta = L.stored_metadata(storage)
        checks.check("odd: NaN, Infinity and the NUL/surrogate escapes are stored as written (json.dumps text)",
                     "NaN" in meta["odd-nan-other"] and "Infinity" in meta["odd-inf-other"]
                     and "\\u0000" in meta["odd-nul-other"] and "\\ud800" in meta["odd-surrogate-other"],
                     observed={k: meta[k] for k in ("odd-nan-other", "odd-inf-other", "odd-nul-other", "odd-surrogate-other")})
        for flt, expected in L.POSTGRES_ODD_EXPECTED:
            got = L.outcome_ids(storage.sessions, flt, "m-odd")
            observe("odd", flt, got)
            if BACKEND == "postgres":
                ref = sorted(L.reference_ids(storage, flt, "m-odd", limit=1000, newest_first=True))
                checks.check(f"odd (postgres): {L.show_filter(flt)} selects {sorted(expected)}",
                             got == sorted(expected) and got == ref, observed=got)
        if BACKEND == "sqlite":
            # SQLite keeps json_extract; its results are compared with 0.1.11 below.
            print("odd (sqlite): results recorded for the 0.1.11 comparison")
        if L.IS_0112:
            rows = L.stored_rows(storage)
            checks.check("odd: stored rows equal the rows derived with json.loads from stored metadata",
                         rows == L.expected_rows(meta), observed=len(rows))
            checks.check("odd: every stored name/value is ASCII JSON text without a NUL byte",
                         all(t.isascii() and L.NUL not in t for r in rows for t in r))
            long_rows = [r for r in rows if r[0] == "odd-long" and len(r[1]) > 4000]
            checks.check("odd: the 5,000-char name and 100,000-char value are stored in full",
                         len(long_rows) == 1 and len(long_rows[0][1]) == 5002 and len(long_rows[0][2]) == 100002,
                         observed=[(len(r[1]), len(r[2])) for r in long_rows])
    finally:
        done(storage, schema)


# ---------------------------------------------------------------- bounded
@section("bounded")
def _bounded() -> None:
    storage, schema = new_storage("bounded")
    try:
        for index in range(24):
            metadata: dict[str, Any] = {"surface": "acp", "index": index}
            if index % 3 == 0:
                metadata["score"] = L.NAN
            if index % 4 == 0:
                metadata["note"] = "a" + L.NUL + "b"
            L.store_sessions(storage.sessions, {f"bounded-{index:02d}": metadata}, "m-bounded")
        for index in range(30):
            L.store_sessions(storage.sessions, {f"other-{index:02d}": {"surface": "cli", "score": L.INF}}, "m-bounded")
        bad = []
        for limit in (1, 2, 5, 23, 24, 25, 1000):
            for newest_first in (True, False):
                try:
                    got = L.selected(storage.sessions, {"surface": "acp"}, "m-bounded", limit=limit, newest_first=newest_first)
                except Exception as exc:  # noqa: BLE001
                    got = f"{type(exc).__name__}: {exc}"
                ref = L.reference_ids(storage, {"surface": "acp"}, "m-bounded", limit=limit, newest_first=newest_first)
                observations[f"bounded limit={limit} newest_first={newest_first}"] = got
                if got != ref or len(got) != min(limit, 24):
                    bad.append((limit, newest_first, got if isinstance(got, str) else len(got)))
        checks.check("bounded: limits 1,2,5,23,24,25,1000 x both orders return min(limit,24) sessions in (updated_at,id) order",
                     not bad, observed=bad or "all 14 match")
    finally:
        done(storage, schema)


# ---------------------------------------------------------------- collision
@section("collision")
def _collision() -> None:
    if not need_0112("collision"):
        return
    from openminion.modules.storage.runtime.session_store import metadata_strings

    storage, schema = new_storage("collision")
    try:
        with patch.object(metadata_strings, "entry_digest", lambda key, value: "0" * 16):
            L.store_sessions(storage.sessions, L.ODD_METADATA, "m-collision")
            L.store_sessions(storage.sessions, {"near": {"surface": "acq", "a.b": "literals"}}, "m-collision")
            digests = {r[3] for r in L.stored_rows(storage)}
            checks.check("collision: every row shares one forced digest", digests == {"0" * 16}, observed=sorted(digests))
            if BACKEND == "postgres":
                wrong = []
                for flt, expected in L.POSTGRES_ODD_EXPECTED:
                    got = L.outcome_ids(storage.sessions, flt, "m-collision")
                    if got != sorted(expected):
                        wrong.append((L.show_filter(flt), got))
                checks.check("collision (postgres): all 23 odd filters still select exactly their sessions", not wrong, observed=wrong or "23/23")
                near = L.outcome_ids(storage.sessions, {"surface": "acq"}, "m-collision")
                checks.check("collision (postgres): near-miss value 'acq' selects only 'near'", near == ["near"], observed=near)
                miss = L.outcome_ids(storage.sessions, {"a.b": "literal"}, "m-collision")
                checks.check("collision (postgres): 'a.b'='literal' does not match 'literals'", miss == ["odd-path-keys"], observed=miss)
            else:
                print("collision (sqlite): the SQLite reader does not use the digest; rows only")
    finally:
        done(storage, schema)


# ---------------------------------------------------------------- writes
@section("writes")
def _writes() -> None:
    if not need_0112("writes"):
        return
    storage, schema = new_storage("writes")
    s = storage.sessions
    T = "m-writes"

    def consistent(label: str) -> None:
        checks.check(f"writes: rows match stored metadata after {label}",
                     L.stored_rows(storage) == L.expected_rows(L.stored_metadata(storage)))

    try:
        created = s.resolve_session(agent_id="main", channel="console", target=T, metadata={"surface": "acp", "count": 1})
        consistent("keyed insert")
        s.resolve_session(agent_id="main", channel="console", target=T, metadata={"surface": "cli", "working_dir": "/work/a"})
        consistent("keyed metadata replacement")
        checks.check("writes: after replacement the old value no longer matches and the new one does",
                     L.outcome_ids(s, {"surface": "acp"}, T) == [] and L.outcome_ids(s, {"surface": "cli"}, T) == [created.id])
        before = L.stored_rows(storage)
        s.resolve_session(agent_id="main", channel="console", target=T)
        checks.check("writes: resolve without metadata keeps rows", L.stored_rows(storage) == before)
        explicit = s.resolve_session(agent_id="main", channel="console", target=T, session_id="explicit-a", metadata={"surface": "acp"})
        room = s.create_room(channel="cli", target="room", session_id="room-a", metadata={"topic": L.CAFE})
        consistent("explicit session and room creation")
        before = L.stored_rows(storage)
        from openminion.modules.storage.runtime.session_store.rows import metadata_json

        s._sessions.insert_session(
            session_id=explicit.id, session_key="other-key", channel="console", target=T,
            session_metadata_json=metadata_json({"surface": "replaced"}),
            created_at="2026-10-26T00:00:00+00:00", updated_at="2026-10-26T00:00:00+00:00", ignore_conflict=True,
        )
        kept = s.get_session(explicit.id)
        checks.check("writes: an ignored conflicting insert writes no rows and keeps the session",
                     L.stored_rows(storage) == before and kept is not None and kept.metadata == {"surface": "acp"})
        checks.check("writes: 'replaced' matches nothing after the ignored insert", L.outcome_ids(s, {"surface": "replaced"}, T) == [])
        s.update_session_metadata(session_id=created.id, patch={"surface": "focus", "working_dir": 7, "title": "t"})
        consistent("update_session_metadata (changed value, string->number, new member)")
        checks.check("writes: updated filters follow the update",
                     L.outcome_ids(s, {"surface": "focus"}, T) == [created.id] and L.outcome_ids(s, {"working_dir": "/work/a"}, T) == []
                     and L.outcome_ids(s, {"title": "t"}, T) == [created.id])
        checks.check("writes: delete_session returns True", s.delete_session(room.id))
        remaining = {r[0] for r in L.stored_rows(storage)}
        checks.check("writes: delete_session removes the session's rows", room.id not in remaining and {created.id, explicit.id} <= remaining,
                     observed=sorted(remaining))
        L.store_sessions(s, {"del-direct": {"surface": "acp"}}, T)
        s._backend.execute_count("DELETE FROM sessions WHERE id = ?", ("del-direct",))
        checks.check("writes: a direct DELETE FROM sessions cascades to the rows", "del-direct" not in {r[0] for r in L.stored_rows(storage)})
        consistent("all writes")
    finally:
        done(storage, schema)


# ---------------------------------------------------------------- rollback
@section("rollback")
def _rollback() -> None:
    if not need_0112("rollback"):
        return
    from openminion.modules.storage.runtime.session_store import metadata_strings
    from openminion.modules.storage.runtime.session_store.turn_leases import RuntimeSessionTurnFenceError

    mod = "openminion.modules.storage.runtime.session_store.sessions"
    storage, schema = new_storage("rollback")
    s = storage.sessions
    T = "m-rollback"

    def failing_after(name: str):
        original = getattr(metadata_strings, name)

        def write_then_fail(*a: Any, **k: Any) -> None:
            original(*a, **k)
            raise RuntimeError("injected failure after the metadata strings")
        return write_then_fail

    def raises(action) -> str:
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
        return "no error"

    try:
        session = s.resolve_session(agent_id="main", channel="console", target=T, session_id="rollback-a", metadata={"surface": "acp"})
        meta0, rows0 = L.stored_metadata(storage), L.stored_rows(storage)
        with patch(f"{mod}.replace_metadata_strings", failing_after("replace_metadata_strings")):
            err = raises(lambda: s.update_session_metadata(session_id=session.id, patch={"surface": "cli"}))
        checks.check("rollback: update_session_metadata failing after its rows raises and changes nothing",
                     "injected failure" in err and L.stored_metadata(storage) == meta0 and L.stored_rows(storage) == rows0, observed=err)
        keyed = s.resolve_session(agent_id="main", channel="console", target=T, metadata={"surface": "a"})
        meta0, rows0 = L.stored_metadata(storage), L.stored_rows(storage)
        with patch(f"{mod}.replace_metadata_strings", failing_after("replace_metadata_strings")):
            err = raises(lambda: s.resolve_session(agent_id="main", channel="console", target=T, metadata={"surface": "b"}))
        kept = s.get_session(keyed.id)
        checks.check("rollback: keyed resolve_session metadata update failing after its rows changes nothing",
                     "injected failure" in err and L.stored_metadata(storage) == meta0 and L.stored_rows(storage) == rows0
                     and kept is not None and kept.metadata == {"surface": "a"}, observed=err)
        with patch(f"{mod}.insert_metadata_strings", failing_after("insert_metadata_strings")):
            err = raises(lambda: s.create_room(channel="cli", target="room", session_id="room-b", metadata={"x": "y"}))
        checks.check("rollback: a new session whose rows fail is not stored",
                     "injected failure" in err and s.get_session("room-b") is None
                     and L.stored_metadata(storage) == meta0 and L.stored_rows(storage) == rows0, observed=err)
        try:
            s.update_session_metadata(session_id=session.id, patch={"surface": "x"}, session_turn_fence_token=1)
            fence = "no error"
        except RuntimeSessionTurnFenceError as exc:
            fence = f"RuntimeSessionTurnFenceError: {exc}"
        checks.check("rollback: a failing fence check comes before any write",
                     fence.startswith("RuntimeSessionTurnFenceError") and L.stored_metadata(storage) == meta0
                     and L.stored_rows(storage) == rows0, observed=fence)
        checks.check("rollback: filters still see the committed values only",
                     L.outcome_ids(s, {"surface": "acp"}, T) == ["rollback-a"] and L.outcome_ids(s, {"surface": "cli"}, T) == []
                     and L.outcome_ids(s, {"surface": "b"}, T) == [])
    finally:
        done(storage, schema)


# ---------------------------------------------------------------- falsey
@section("falsey")
def _falsey() -> None:
    storage, schema = new_storage("falsey")
    s = storage.sessions
    T = "m-falsey"
    try:
        L.store_sessions(s, {
            "f-empty": {"surface": ""}, "f-zero-text": {"surface": "0"}, "f-false-text": {"surface": "False"},
            "f-none-text": {"surface": "None"}, "f-int": {"surface": 1}, "f-true": {"surface": True},
            "f-null": {"surface": None}, "f-missing": {},
        }, T)
        for value in (None, 0, False, "", [], {}, ()):
            flt = {"surface": value}
            got = L.outcome_ids(s, flt, T)
            observe("falsey", flt, got)
            checks.check(f"falsey: {{'surface': {value!r}}} is sent as '' and selects ['f-empty']", got == ["f-empty"], observed=got)
        for value in (1, True, 1.5):
            flt = {"surface": value}
            got = L.outcome_ids(s, flt, T)
            observe("falsey", flt, got)
            if BACKEND == "postgres":
                checks.check(f"non-string (postgres): {{'surface': {value!r}}} raises TypeError",
                             isinstance(got, str) and got.startswith("TypeError"), observed=got)
            else:
                print(f"non-string (sqlite): {{'surface': {value!r}}} -> {got} (compared with 0.1.11 below)")
    finally:
        done(storage, schema)


# ---------------------------------------------------------------- concurrent (PostgreSQL)
@section("concurrent")
def _concurrent() -> None:
    if BACKEND != "postgres":
        skipped.append("concurrent: PostgreSQL only (checklist: concurrent PostgreSQL writers)")
        print("SKIP", skipped[-1])
        return
    if not need_0112("concurrent"):
        return
    schema = f"om369_conc_{TAG}"
    L.create_schema(PGURL, schema)
    url = L.schema_url(PGURL, schema)
    main = L.open_storage("postgres", url=url, work=WORK)
    writers = [L.open_storage("postgres", url=url, work=WORK) for _ in range(4)]
    T = "m-concurrent"
    try:
        s = main.sessions
        session = s.resolve_session(agent_id="main", channel="console", target=T, session_id="concurrent-a", metadata={"surface": "start"})
        barrier = threading.Barrier(len(writers))
        errors: list[str] = []

        def write(index: int, store) -> None:
            try:
                barrier.wait()
                for step in range(25):
                    store.update_session_metadata(session_id=session.id, patch={
                        "surface": f"writer-{index}-{step}", f"writer-{index}": "x" if step % 2 else 1})
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=write, args=(i, w.sessions)) for i, w in enumerate(writers)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        final = s.get_session(session.id)
        checks.check("concurrent: 4 writers x 25 metadata updates on one session complete without errors", not errors, observed=errors[:3])
        checks.check("concurrent: rows match the final stored metadata",
                     L.stored_rows(main) == L.expected_rows(L.stored_metadata(main)))
        listed = L.outcome_ids(s, {"surface": final.metadata["surface"]}, T)
        checks.check("concurrent: the final surface value selects the session; no stale value does",
                     listed == ["concurrent-a"] and L.outcome_ids(s, {"surface": "start"}, T) == [], observed=(final.metadata["surface"], listed))

        creators = [L.open_storage("postgres", url=url, work=WORK) for _ in range(8)]
        try:
            barrier2 = threading.Barrier(len(creators))
            errors2: list[str] = []

            def create(index: int, store) -> None:
                try:
                    barrier2.wait()
                    store.resolve_session(agent_id="main", channel="console", target="raced-target", metadata={"surface": f"creator-{index}"})
                except BaseException as exc:  # noqa: BLE001
                    errors2.append(f"{type(exc).__name__}: {exc}")

            threads = [threading.Thread(target=create, args=(i, c.sessions)) for i, c in enumerate(creators)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            raced = s.list_sessions(limit=100, target="raced-target")
            checks.check("concurrent: 8 racing creations of one keyed session finish without errors, one session",
                         not errors2 and len(raced) == 1, observed=(errors2[:3], len(raced)))
            checks.check("concurrent: the raced session's rows match its stored metadata",
                         L.stored_rows(main) == L.expected_rows(L.stored_metadata(main)))
        finally:
            for c in creators:
                c.close()

        updater, deleter = writers[0].sessions, writers[1].sessions
        s.resolve_session(agent_id="main", channel="console", target=T, session_id="race-delete", metadata={"surface": "start"})
        barrier3 = threading.Barrier(2)
        errors3: list[str] = []
        outcome: dict[str, Any] = {}

        def update() -> None:
            try:
                barrier3.wait()
                for step in range(40):
                    try:
                        updater.update_session_metadata(session_id="race-delete", patch={"surface": f"s{step}"})
                    except (ValueError, RuntimeError) as exc:
                        if "Session not found" not in str(exc) and "Failed to update metadata" not in str(exc):
                            raise
                        outcome["update_stopped"] = f"{type(exc).__name__}: {exc}"
                        return
            except BaseException as exc:  # noqa: BLE001
                errors3.append(f"{type(exc).__name__}: {exc}")

        def delete() -> None:
            try:
                barrier3.wait()
                outcome["deleted"] = deleter.delete_session("race-delete")
            except BaseException as exc:  # noqa: BLE001
                errors3.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=update), threading.Thread(target=delete)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        checks.check("concurrent: a metadata update racing a delete: no deadlock/integrity error, session and rows gone",
                     not errors3 and s.get_session("race-delete") is None and "race-delete" not in {r[0] for r in L.stored_rows(main)},
                     observed={"errors": errors3[:3], **outcome})
        checks.check("concurrent: rows match stored metadata at the end", L.stored_rows(main) == L.expected_rows(L.stored_metadata(main)))
        # Rollback with a concurrent reader: a failing update leaves other connections seeing the old rows.
        from openminion.modules.storage.runtime.session_store import metadata_strings
        original = metadata_strings.replace_metadata_strings

        def fail_after(*a: Any, **k: Any) -> None:
            original(*a, **k)
            raise RuntimeError("injected failure after the metadata strings")

        before = L.stored_rows(writers[2])
        with patch("openminion.modules.storage.runtime.session_store.sessions.replace_metadata_strings", fail_after):
            try:
                s.update_session_metadata(session_id="concurrent-a", patch={"surface": "rolled-back"})
                err = "no error"
            except RuntimeError as exc:
                err = str(exc)
        checks.check("concurrent: a failed PostgreSQL update rolls back; another connection sees the old rows and no 'rolled-back' match",
                     "injected failure" in err and L.stored_rows(writers[2]) == before
                     and L.outcome_ids(writers[2].sessions, {"surface": "rolled-back"}, T) == [], observed=err)
    finally:
        for w in writers:
            w.close()
        main.close()
        L.drop_schema(PGURL, schema)


# ---------------------------------------------------------------- PostgreSQL runtime bootstrap (note)
# On PostgreSQL runtime storage, APIRuntime (and so `openminion acp` and a real
# Focus runtime) does not start: api/core/bootstrap.py passes
# `runtime_storage.connection`, which raises for a non-SQLite record store
# (also on 0.1.11). The ACP and Focus checks below then drive the installed
# ACP handler and Focus runtime in-process over a real PostgreSQL runtime
# SessionStore, as upstream's PostgreSQL ACP test does. This note is recorded,
# not counted as a check.
notes: dict[str, Any] = {}
if BACKEND == "postgres":
    print("=== pg-runtime-bootstrap (note)", flush=True)
    schema_n = f"om369_boot_{TAG}"
    L.create_schema(PGURL, schema_n)
    root_n = WORK / f"boot-{TAG}"
    try:
        try:
            rt_n = L.api_runtime(root_n, pg_url=L.schema_url(PGURL, schema_n))
            rt_n.close()
            notes["APIRuntime with storage.backend=postgres"] = "started"
        except Exception as exc:  # noqa: BLE001
            notes["APIRuntime with storage.backend=postgres"] = f"{type(exc).__name__}: {exc}"
        cfg_n = L.write_config(root_n, pg_url=L.schema_url(PGURL, schema_n))
        proc_n = L.AcpProcess(root_n, cfg_n, root_n / "acp-stderr.log")
        try:
            proc_n.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=240)
            notes["openminion acp with storage.backend=postgres"] = "initialized"
        except Exception as exc:  # noqa: BLE001
            notes["openminion acp with storage.backend=postgres"] = f"no reply ({type(exc).__name__})"
        rc_n = proc_n.close()
        tail = [ln for ln in (root_n / "acp-stderr.log").read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()][-1:]
        notes["openminion acp exit code and last stderr line"] = [rc_n, tail]
    finally:
        L.drop_schema(PGURL, schema_n)
    for k, v in notes.items():
        print(f"NOTE {k}: {v}")


class CliAcp:
    """The installed `openminion acp` stdio server (SQLite runtime storage)."""

    kind = "openminion acp (stdio JSON-RPC)"

    def __init__(self, root: Path, url: str | None) -> None:
        self.proc = L.AcpProcess(root, L.write_config(root, pg_url=url), root / "acp-stderr.log")

    def initialize(self) -> tuple[Any, Any]:
        r = self.proc.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})["result"]
        return r["agentInfo"]["version"], r["agentCapabilities"]["sessionCapabilities"].get("list")

    def new(self, cwd: Path) -> str:
        return self.proc.call("session/new", {"cwd": str(cwd), "mcpServers": []})["result"]["sessionId"]

    def list(self, cwd: Path | None) -> Any:
        msg = self.proc.call("session/list", {"cwd": str(cwd)} if cwd else {})
        if "error" in msg:
            return f"error {msg['error'].get('code')}: {str(msg['error'].get('message'))[:200]} {str(msg['error'].get('data'))[:300]}"
        return [item["sessionId"] for item in msg["result"]["sessions"]]

    def close(self) -> Any:
        return self.proc.close()


class InProcessAcp:
    """The installed OpenMinionACPAgent over a real runtime SessionStore (PostgreSQL)."""

    kind = "installed OpenMinionACPAgent in-process (runtime object: sessions + config)"

    def __init__(self, sessions) -> None:
        import asyncio
        from types import SimpleNamespace

        from openminion.api.operations.acp import OpenMinionACPAgent
        from openminion.base.config import OpenMinionConfig

        config = OpenMinionConfig.from_dict({"agents": {"openminion": {"name": "openminion", "provider": "echo"}},
                                             "default_agent": "openminion"})
        self.agent = OpenMinionACPAgent(SimpleNamespace(sessions=sessions, config=config))
        self.loop = asyncio.new_event_loop()

    def initialize(self) -> tuple[Any, Any]:
        r = self.loop.run_until_complete(self.agent.initialize(protocol_version=1))
        caps = r.agent_capabilities.session_capabilities
        return r.agent_info.version, ({} if caps is not None and caps.list is not None else None)

    def new(self, cwd: Path) -> str:
        return self.loop.run_until_complete(self.agent.new_session(cwd=str(cwd))).session_id

    def list(self, cwd: Path | None) -> Any:
        try:
            r = self.loop.run_until_complete(self.agent.list_sessions(cwd=str(cwd) if cwd else None))
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {str(exc)[:300]}"
        return [item.session_id for item in r.sessions]

    def close(self) -> Any:
        self.loop.close()
        return 0


# ---------------------------------------------------------------- acp
@section("acp")
def _acp() -> None:
    root = WORK / f"acp-{TAG}"
    schema = None
    url = None
    side = None
    if BACKEND == "postgres":
        schema = f"om369_acp_{TAG}"
        L.create_schema(PGURL, schema)
        url = L.schema_url(PGURL, schema)
    ws = {name: (root / f"ws-{name}") for name in ("a", "b", "c")}
    for p in ws.values():
        p.mkdir(parents=True, exist_ok=True)
    if BACKEND == "sqlite":
        client: Any = CliAcp(root, url)
    else:
        side = L.open_storage("postgres", url=url, work=WORK)
        client = InProcessAcp(side.sessions)
    observations["acp path"] = client.kind
    print("acp path:", client.kind)
    try:
        version, list_cap = client.initialize()
        checks.check(f"acp ({client.kind}): initialize reports the installed version and session/list capability",
                     version == L.VERSION and list_cap == {}, observed=version)
        s1 = client.new(ws["a"])
        s2 = client.new(ws["b"])
        if side is None:  # other sessions in the same database, written by a second store
            side = L.open_storage("sqlite", db_path=root / ".openminion" / "state.db")
        wa = str(ws["a"].resolve())
        extra = {
            "cli-in-a": {"surface": "cli", "workspace_root": wa},
            "list-in-a": {"surface": ["acp"], "workspace_root": wa},
            "true-in-a": {"surface": True, "workspace_root": wa},
            "upper-in-a": {"surface": "ACP", "workspace_root": wa},
            "none-in-a": {"workspace_root": wa},
            "odd-acp-in-a": {"surface": "acp", "workspace_root": wa, "score": L.NAN, "note": "a" + L.NUL + "b" + L.LONE_HIGH},
        }
        for sid, meta in extra.items():
            side.sessions.resolve_session(agent_id="openminion", channel="console", target="local-client", session_id=sid, metadata=meta)

        def ref(workspace: str | None, limit: int = 100) -> list[str]:
            rows = L.q(side, "SELECT id, metadata_json, updated_at FROM sessions")
            hits = []
            for r in rows:
                m = json.loads(str(r["metadata_json"]))
                if isinstance(m.get("surface"), str) and m["surface"] == "acp":
                    hits.append((str(r["updated_at"]), str(r["id"]), str(m.get("workspace_root", ""))))
            hits.sort(reverse=True)
            hits = hits[:limit]
            return [sid for _, sid, w in hits if workspace is None or w == workspace]

        in_a = client.list(ws["a"])
        observations["acp session/list cwd=a"] = in_a
        checks.check("acp: session/list cwd=a lists only ACP sessions of workspace a, newest first (incl. NaN/NUL/surrogate in other fields)",
                     in_a == ref(wa) and set(in_a) == {s1, "odd-acp-in-a"}, observed=in_a)
        every = client.list(None)
        observations["acp session/list all"] = every
        checks.check("acp: session/list without cwd lists every ACP session newest first; cli/list/True/ACP/missing surfaces excluded",
                     every == ref(None) and set(every) == {s1, s2, "odd-acp-in-a"}, observed=every)
        in_b = client.list(ws["b"])
        checks.check("acp: session/list cwd=b lists only the workspace-b session", in_b == [s2], observed=in_b)
        for _ in range(105):
            client.new(ws["c"])
        capped = client.list(None)
        checks.check("acp: with 108 ACP sessions session/list returns the newest 100 (store limit), in order",
                     isinstance(capped, list) and len(capped) == 100 and capped == ref(None, 100),
                     observed=capped if isinstance(capped, str) else len(capped))
    finally:
        rc = client.close()
        if BACKEND == "sqlite":
            checks.check("acp: the acp server exits cleanly on stdin EOF", rc == 0, observed=rc)
        if side is not None:
            side.close()
        if schema:
            L.drop_schema(PGURL, schema)


# ---------------------------------------------------------------- focus
class _FocusGateway:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


class FocusRuntimeShim:
    """What Focus session creation/listing reads from APIRuntime, over a real runtime SessionStore."""

    def __init__(self, sessions) -> None:
        from types import SimpleNamespace

        self.sessions = sessions
        self.config = SimpleNamespace(
            default_agent="alpha",
            runtime=SimpleNamespace(session_context_token_budget=200000),
            agents={n: SimpleNamespace(name=n, provider="echo", default_channel="cli") for n in ("alpha", "beta")},
        )

    def resolve_agent_profile(self, agent_id: str | None = None):
        from types import SimpleNamespace

        return SimpleNamespace(name=str(agent_id or "").strip() or "alpha")

    def resolve_gateway(self, agent_id: str | None = None, *, overrides: object | None = None):
        return _FocusGateway(str(agent_id or "").strip() or "alpha")

    def close(self) -> None:
        pass


@section("focus")
def _focus() -> None:
    from openminion.cli.interactive.runtime import OpenMinionRuntime

    root = WORK / f"focus-{TAG}"
    schema = None
    storage = None
    if BACKEND == "postgres":
        schema = f"om369_focus_{TAG}"
        L.create_schema(PGURL, schema)
        storage = L.open_storage("postgres", url=L.schema_url(PGURL, schema), work=WORK)
        rt: Any = FocusRuntimeShim(storage.sessions)
        kind = "installed Focus OpenMinionRuntime over a PostgreSQL runtime SessionStore (runtime object shim)"
    else:
        rt = L.api_runtime(root, agents=("alpha", "beta"))
        kind = "installed Focus OpenMinionRuntime over a real APIRuntime"
    observations["focus path"] = kind
    print("focus path:", kind)
    try:
        project_a, project_b = root / "project-a", root / "project-b"
        project_a.mkdir(parents=True, exist_ok=True)
        project_b.mkdir(parents=True, exist_ok=True)

        def focus(working_dir: Path, agent_id: str | None = None):
            return OpenMinionRuntime(rt, channel="cli", target="focus", agent_id=agent_id, working_dir=str(working_dir), bind_immediately=False)

        fa = focus(project_a)
        created = [fa.create_new_session() for _ in range(3)]
        focus(project_b).create_new_session()
        beta_id = focus(project_a, agent_id="beta").create_new_session()
        # A session of this directory whose other metadata PostgreSQL cannot parse as JSON.
        rt.sessions.resolve_session(agent_id="alpha", channel="cli", target="focus", session_id="focus-odd",
                                    metadata={"working_dir": str(project_a.resolve()), "focus_mode": True, "score": L.NAN, "note": L.NUL})
        stored = rt.sessions.get_session(created[0])
        checks.check("focus: a new Focus session stores working_dir (resolved) and focus_mode",
                     stored is not None and stored.metadata.get("working_dir") == str(project_a.resolve()) and stored.metadata.get("focus_mode") is True,
                     observed=None if stored is None else {k: stored.metadata.get(k) for k in ("working_dir", "focus_mode")})
        # Expected order computed here from the stored rows: (updated_at, id) descending.
        rows = sorted(rt.sessions.list_sessions(limit=1000, target="focus"), key=lambda r: (str(r.updated_at), str(r.id)), reverse=True)
        expected_all = [r.id for r in rows if r.metadata.get("working_dir") == str(project_a.resolve())
                        and r.id != beta_id]
        listed = [item.id for item in fa.list_directory_sessions()]
        observations["focus list_directory_sessions"] = listed
        checks.check("focus: list_directory_sessions = alpha's sessions of project-a, newest first (beta's and project-b's excluded)",
                     listed == expected_all and set(listed) == set(created) | {"focus-odd"}, observed=listed)
        two = [item.id for item in fa.list_directory_sessions(limit=2)]
        checks.check("focus: limit=2 returns the newest two", two == expected_all[:2], observed=two)
        cand = fa.find_candidate_session()
        checks.check("focus: find_candidate_session is the newest", cand is not None and cand.id == expected_all[0], observed=None if cand is None else cand.id)
        beta_list = [item.id for item in focus(project_a, agent_id="beta").list_directory_sessions()]
        checks.check("focus: beta lists only its own project-a session", beta_list == [beta_id], observed=beta_list)
        b_list = [item.id for item in focus(project_b).list_directory_sessions()]
        checks.check("focus: project-b lists its one session", len(b_list) == 1 and b_list[0] not in created, observed=len(b_list))
    finally:
        rt.close()
        if storage is not None:
            storage.close()
        if schema:
            L.drop_schema(PGURL, schema)


# ---------------------------------------------------------------- 0.1.11 comparison (SQLite legacy behavior)
if BASELINE is not None:
    base_obs = BASELINE.get("observations", {})
    common = sorted(set(base_obs) & set(observations))
    diffs = [(k, base_obs[k], observations[k]) for k in common if base_obs[k] != observations[k]
             and not k.startswith("acp ") and not k.startswith("focus ")]
    checks.check(f"legacy ({BACKEND}): {len(common)} filter results identical to the published 0.1.11 on this machine",
                 not diffs and len(common) > 0, observed=diffs[:5] or f"{len(common)} identical")

summary = checks.summary()
result = {**info, "sections": sections, "skipped": skipped, "notes": notes, "summary": summary, "checks": checks.items, "observations": observations}
write_result(OUT, result)
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']} skipped={len(skipped)}")
sys.exit(0 if checks.all_pass else 1)
