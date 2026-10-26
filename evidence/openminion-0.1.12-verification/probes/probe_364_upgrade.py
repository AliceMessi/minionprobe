"""#364 upgrade on the installed packages: runtime migration 14 on a populated
migration-13 database, following the shipped coordinated-upgrade procedure.

    python -I probe_364_upgrade.py <work_dir> <out.json> --py0111 <python of a
        venv with openminion==0.1.11> [--backend sqlite|postgres] [--rooms N]

The driver runs under openminion 0.1.12. Each phase runs in its own process
(`--step`), with the package that phase needs, one after the other, so no
writer is running while the next phase starts:

 1. seed (0.1.11): a new runtime database at migration 13, populated through
    the 0.1.11 session store: rooms whose agents join on one tick with row ids
    in reverse join order, earlier/later joins, humans, participants that left,
    rejoins, explicit active agents, messages, and legacy sessions with their
    owner record. It records the 0.1.11 listings, active agents, an
    independent (joined_at, id) numbering of every row and a digest of the rows.
 2. backup (writers stopped): SQLite with the 0.1.11 CLI
    `openminion storage backup --sqlite <db> --output <file>`; PostgreSQL with
    pg_dump -Fc of the schema (the guidance `storage backup --backend postgres`
    prints).
 3. two injected failures inside migration 14 (0.1.12): a trigger that fails
    the participants UPDATE (the backfill statement), then one that fails the
    ledger insert of version 14 (after every migration-14 statement ran). Each
    upgrade must raise and leave the ledger, schema and rows as they were.
    The trigger is then dropped (the "fix the cause" step).
 4. upgrade (0.1.12): migrations 14 and 15 apply once; ledger, column, unique
    index; every row (including left participants) numbered in the
    pre-upgrade (joined_at, id) order; listings and active agents unchanged;
    rows unchanged; then new tied joins, a rejoin and a fallback.
 5. rerun (0.1.12, new process): nothing applies again; ledger and join_seq
    unchanged; direct migrate calls apply nothing.
 6. rollback: restore the pre-upgrade backup (file copy for SQLite, see the
    note below; pg_restore for PostgreSQL) and open it with 0.1.11: migration
    13, no join_seq, the seeded listings and rows.

Before every 0.1.11 step the driver reads the ledger and checks the database is
not past migration 13, so no 0.1.11 writer ever opens an upgraded database.
Note: the SQLite restore copies the snapshot file back; `openminion storage
restore` is not used because of openminion#412.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
HERE = Path(__file__).resolve()
from probe_364_lib import (  # noqa: E402
    COUNTED_JOIN_SEQ_SQL,
    EARLIER_AT,
    LATER_AT,
    PARTICIPANT_CLOCK,
    PARTICIPANT_UUID,
    TIED_AT,
    Raw,
    id_feed,
    join_agents,
    open_storage,
    participant_ids,
    remove_agent,
    rid,
    rows_digest,
)

INDEX = "idx_room_participants_session_join_seq"


# =================================================================== steps
def emit(payload: dict) -> None:
    print("RESULT " + json.dumps(payload, default=str), flush=True)


def snapshot(sessions, raw: Raw, session_ids: list[str]) -> dict:
    listing = {sid: participant_ids(sessions, sid) for sid in session_ids}
    active = {sid: sessions.get_active_agent(sid) for sid in session_ids}
    counted = {str(r["id"]): int(r["join_seq"]) for r in raw.query(COUNTED_JOIN_SEQ_SQL)}
    rows = raw.participant_rows()
    return {"listing": listing, "active": active, "counted": counted, "digest": rows_digest(rows), "rows": len(rows)}


def step_seed(db: Path, url: str, state: Path, rooms: int) -> None:
    import openminion

    rng = random.Random(364)
    storage = open_storage(db, url)
    s = storage.sessions
    raw = Raw(db, url)
    session_ids: list[str] = []
    insertion: dict[str, list[str]] = {}
    left: list[str] = []
    try:
        # room-0: four agents on one tick, ids f, e, d, c (reverse join order),
        # one that left on the same tick, the first agent active.
        r0 = s.create_room(channel="cli", target="room-0", session_id="room-0", metadata={"name": "room 0"})
        join_agents(s, r0.id, ("a1", "a2", "a3", "a4", "a-left"), ids="fedc8")
        remove_agent(s, r0.id, "a-left", at=LATER_AT)
        insertion[r0.id] = ["a1", "a2", "a3", "a4"]
        left.append(f"{r0.id}:a-left")
        session_ids.append(r0.id)
        for i in range(1, rooms):
            sid = f"room-{i}"
            s.create_room(channel="cli", target=sid, session_id=sid, metadata={"name": f"room {i}", "room_routing_mode": rng.choice(["broadcast", "addressed", "sequential"])})
            session_ids.append(sid)
            if rng.random() < 0.5:
                with patch(PARTICIPANT_CLOCK, return_value=TIED_AT), patch(PARTICIPANT_UUID, side_effect=id_feed([rid(sid, "9")])):
                    s.add_participant(session_id=sid, participant_type="human", participant_id=f"human-{i}")
            agents = [f"r{i}-agent-{j}" for j in range(rng.randint(3, 6))]
            for j, agent in enumerate(agents):
                at = rng.choice([TIED_AT, TIED_AT, TIED_AT, EARLIER_AT, LATER_AT])
                join_agents(s, sid, (agent,), ids=f"{15 - j:x}", at=at)
            insertion[sid] = agents
            if rng.random() < 0.4:
                gone = rng.choice(agents)
                remove_agent(s, sid, gone, at=LATER_AT)
                left.append(f"{sid}:{gone}")
                if rng.random() < 0.4:
                    join_agents(s, sid, (gone,), ids="7", at=TIED_AT)
                    left.remove(f"{sid}:{gone}")
            if rng.random() < 0.3:
                active_agents = [p for p in participant_ids(s, sid) if p in agents]
                s.set_active_agent(session_id=sid, agent_id=rng.choice(active_agents))
            if rng.random() < 0.3:
                s.append_message(session_id=sid, role="user", body=f"hello {sid}")
                s.append_message(session_id=sid, role="assistant", body=f"hi from {sid}")
        for i in range(max(2, rooms // 4)):
            legacy = s.resolve_session(agent_id=f"legacy-{i}", channel="console", target=f"legacy-{i}")
            # owner record and the first invited human on one tick; ids in a
            # random order, so either may list first before the upgrade
            order = "f0" if rng.random() < 0.5 else "0f"
            with patch(PARTICIPANT_CLOCK, return_value=TIED_AT), patch(PARTICIPANT_UUID, side_effect=id_feed([rid(legacy.id, c) for c in order])):
                s.add_participant(session_id=legacy.id, participant_type="human", participant_id=f"guest-{i}")
            session_ids.append(legacy.id)
        snap = snapshot(s, raw, session_ids)
        tie_groups = raw.query(
            "SELECT COUNT(*) AS n FROM (SELECT session_id, joined_at FROM room_participants "
            "GROUP BY session_id, joined_at HAVING COUNT(*) > 1) AS tied"
        )[0]["n"]
        left_rows = raw.query("SELECT COUNT(*) AS n FROM room_participants WHERE left_at IS NOT NULL")[0]["n"]
        messages = raw.query("SELECT COUNT(*) AS n FROM messages")[0]["n"]
        oracle = {
            "version": openminion.__version__,
            "migration": {"applied": list(storage.migration_result.applied_versions), "current": storage.migration_result.current_version},
            "session_ids": session_ids,
            "insertion": insertion,
            "left": left,
            "counts": {"sessions": len(session_ids), "participants": snap["rows"], "left_participants": int(left_rows), "tied_groups": int(tie_groups), "messages": int(messages)},
            **snap,
        }
    finally:
        storage.close()
    state.write_text(json.dumps(oracle, indent=1), encoding="utf-8")
    emit({k: oracle[k] for k in ("version", "migration", "counts", "digest")})


def step_expect_fail(db: Path, url: str) -> None:
    import openminion
    from openminion.modules.storage.runtime.migrations import MigrationError

    try:
        storage = open_storage(db, url)
    except MigrationError as exc:
        emit({"version": openminion.__version__, "raised": "MigrationError", "message": str(exc)[:400]})
        return
    except Exception as exc:  # noqa: BLE001
        emit({"version": openminion.__version__, "raised": type(exc).__name__, "message": str(exc)[:400]})
        return
    current = storage.migration_result.current_version
    storage.close()
    emit({"version": openminion.__version__, "raised": None, "current": current})


def step_upgrade(db: Path, url: str, state: Path) -> None:
    import openminion

    oracle = json.loads(state.read_text(encoding="utf-8"))
    storage = open_storage(db, url)
    s = storage.sessions
    raw = Raw(db, url)
    out: dict = {"version": openminion.__version__, "migration": {"applied": list(storage.migration_result.applied_versions), "current": storage.migration_result.current_version}}
    try:
        sids = oracle["session_ids"]
        out["listing"] = {sid: participant_ids(s, sid) for sid in sids}
        out["active"] = {sid: s.get_active_agent(sid) for sid in sids}
        rows = raw.query("SELECT id, session_id, join_seq FROM room_participants")
        out["join_seq"] = {str(r["id"]): int(r["join_seq"]) for r in rows}
        per_session: dict[str, list[int]] = {}
        for r in rows:
            per_session.setdefault(str(r["session_id"]), []).append(int(r["join_seq"]))
        out["per_session_dense"] = all(sorted(v) == list(range(1, len(v) + 1)) for v in per_session.values())
        out["digest"] = rows_digest(raw.participant_rows())
        # Writes after the upgrade, on room-0's tick (TIED_AT): a new agent
        # whose row id sorts before every existing one, then the participant
        # that had left, then the active agent leaves.
        before = participant_ids(s, "room-0")
        join_agents(s, "room-0", ("a-new",), ids="0")
        after_new = participant_ids(s, "room-0")
        join_agents(s, "room-0", ("a-left",), ids="1")
        after_rejoin = participant_ids(s, "room-0")
        seq0 = {str(r["participant_id"]): int(r["join_seq"]) for r in raw.query("SELECT participant_id, join_seq FROM room_participants WHERE session_id = ?", ("room-0",))}
        active0 = s.get_active_agent("room-0")
        remove_agent(s, "room-0", active0)
        out["post_writes"] = {
            "before": before,
            "after_new": after_new,
            "after_rejoin": after_rejoin,
            "join_seq": seq0,
            "active_before_leave": active0,
            "listing_after_leave": participant_ids(s, "room-0"),
            "active_after_leave": s.get_active_agent("room-0"),
        }
        out["join_seq_after_writes"] = {str(r["id"]): int(r["join_seq"]) for r in raw.query("SELECT id, join_seq FROM room_participants")}
        out["listing_after_writes"] = {sid: participant_ids(s, sid) for sid in sids}
    finally:
        storage.close()
    emit(out)


def step_rerun(db: Path, url: str, state: Path) -> None:
    import openminion
    from openminion.modules.storage.runtime.migrations import migrate_database, migrate_record_store

    oracle = json.loads(state.read_text(encoding="utf-8"))
    storage = open_storage(db, url)
    s = storage.sessions
    raw = Raw(db, url)
    out = {"version": openminion.__version__, "migration": {"applied": list(storage.migration_result.applied_versions), "current": storage.migration_result.current_version}}
    try:
        if url:
            direct = migrate_record_store(storage.record_store, backend_type="postgres")
        else:
            direct = migrate_database(storage.sqlite_path)
        out["direct_migrate"] = {"applied": list(direct.applied_versions), "current": direct.current_version}
        out["join_seq"] = {str(r["id"]): int(r["join_seq"]) for r in raw.query("SELECT id, join_seq FROM room_participants")}
        out["listing"] = {sid: participant_ids(s, sid) for sid in oracle["session_ids"]}
    finally:
        storage.close()
    emit(out)


def step_verify_restored(db: Path, url: str, state: Path) -> None:
    import openminion

    oracle = json.loads(state.read_text(encoding="utf-8"))
    raw = Raw(db, url)
    storage = open_storage(db, url)
    s = storage.sessions
    try:
        out = {
            "version": openminion.__version__,
            "migration": {"applied": list(storage.migration_result.applied_versions), "current": storage.migration_result.current_version},
            "listing": {sid: participant_ids(s, sid) for sid in oracle["session_ids"]},
            "active": {sid: s.get_active_agent(sid) for sid in oracle["session_ids"]},
            "digest": rows_digest(raw.participant_rows()),
            "has_join_seq": "join_seq" in raw.columns("room_participants"),
        }
    finally:
        storage.close()
    emit(out)


# =================================================================== driver
def run_step(py: str, step: str, db: Path, url: str, state: Path, log: list, extra: list[str] | None = None) -> dict:
    cmd = [py, "-I", "-X", "utf8", str(HERE), "--step", step, "--db", str(db), "--url", url, "--state", str(state)] + (extra or [])
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=os.environ.copy())
    elapsed = round(time.perf_counter() - t0, 2)
    result = {}
    for line in proc.stdout.splitlines():
        if line.startswith("RESULT "):
            result = json.loads(line[len("RESULT "):])
    log.append({"step": step, "python": "0.1.11" if py != sys.executable else "0.1.12", "rc": proc.returncode, "seconds": elapsed,
                "stderr_tail": proc.stderr.strip().splitlines()[-6:]})
    print(f"--- step {step} rc={proc.returncode} {elapsed}s", flush=True)
    if proc.returncode != 0:
        print(proc.stderr[-3000:], flush=True)
    return result


def pg_parts(url: str) -> tuple[str, str]:
    from sqlalchemy.engine import make_url

    u = make_url(url)
    return u.username or "postgres", u.database or "postgres"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work", nargs="?")
    ap.add_argument("out", nargs="?")
    ap.add_argument("--py0111", default="")
    ap.add_argument("--backend", default="sqlite", choices=("sqlite", "postgres"))
    ap.add_argument("--rooms", type=int, default=40)
    ap.add_argument("--step", default="")
    ap.add_argument("--db", default="")
    ap.add_argument("--url", default="")
    ap.add_argument("--state", default="")
    args = ap.parse_args()

    from _common import clear_override_env, quiet

    quiet()
    clear_override_env()
    if args.step:
        db, url, state = Path(args.db), args.url, Path(args.state)
        {"seed": lambda: step_seed(db, url, state, args.rooms),
         "expect-fail": lambda: step_expect_fail(db, url),
         "upgrade": lambda: step_upgrade(db, url, state),
         "rerun": lambda: step_rerun(db, url, state),
         "verify-restored": lambda: step_verify_restored(db, url, state)}[args.step]()
        return 0
    return drive(args)


def drive(args) -> int:
    from _common import Checks, banner, write_result
    from probe_364_lib import pg_base_url, pg_create_schema, pg_drop_schema, pg_server_version, schema_url

    checks = Checks()
    label = args.backend
    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    db = work / "upgrade" / "state" / "openminion.db"
    db.parent.mkdir(parents=True)
    state = work / "oracle.json"
    log: list = []
    py11 = args.py0111
    url = schema = ""
    extra = {"backend": args.backend, "rooms": args.rooms, "py0111": "<VENV0111>"}
    if args.backend == "postgres":
        base = pg_base_url()
        if not base:
            print("OPENMINION_TEST_POSTGRES_URL is not set: PostgreSQL not run")
            return 2
        schema = f"up364_{os.getpid()}"
        pg_create_schema(base, schema)
        url = schema_url(base, schema)
        extra["postgres_server"] = pg_server_version(url)
    info = banner("probe_364_upgrade", **extra)
    raw = Raw(db, url)
    guide = {}
    try:
        from openminion.modules.storage.runtime.migrations import DEFAULT_MIGRATIONS

        names = {m.version: m.name for m in DEFAULT_MIGRATIONS}

        def guard_old(when: str) -> None:
            try:
                versions = [r["version"] for r in raw.ledger()]
            except Exception:  # noqa: BLE001 - no ledger yet (new database)
                versions = []
            checks.check(f"{label} coordinated upgrade: before the 0.1.11 step '{when}' the database is not past migration 13 (no older writer on an upgraded database)", not versions or max(versions) <= 13, observed={"ledger_max": max(versions) if versions else None})

        # 1. seed with 0.1.11
        guard_old("seed")
        seed = run_step(py11, "seed", db, url, state, log, ["--rooms", str(args.rooms)])
        oracle = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        checks.check(
            f"{label} seed: openminion 0.1.11 created and populated a migration-13 database",
            seed.get("version") == "0.1.11" and seed.get("migration", {}).get("current") == 13,
            observed={k: seed.get(k) for k in ("version", "migration", "counts")},
        )
        ledger_seed = raw.ledger()
        checks.check(f"{label} seed: ledger ends at 13, no join_seq column", max(r["version"] for r in ledger_seed) == 13 and "join_seq" not in raw.columns("room_participants"), observed=[r["version"] for r in ledger_seed][-3:])
        r0 = oracle.get("listing", {}).get("room-0")
        checks.check(
            f"{label} seed: 0.1.11 lists the tied joins of room-0 in reverse join order (the bug, before the upgrade)",
            r0 == ["a4", "a3", "a2", "a1"] and oracle.get("insertion", {}).get("room-0") == ["a1", "a2", "a3", "a4"],
            observed={"listed": r0, "joined_in_order": oracle.get("insertion", {}).get("room-0")},
        )

        # 2. backup with writers stopped
        backup = work / "pre-upgrade-backup.db"
        dump = work / "pre-upgrade-backup.dump"
        if not url:
            guard_old("storage backup")
            proc = subprocess.run([py11, "-I", "-m", "openminion.cli.main", "storage", "backup", "--sqlite", str(db), "--output", str(backup), "--json"], capture_output=True, text=True, encoding="utf-8", errors="replace")
            ok_json = {}
            try:
                ok_json = json.loads(proc.stdout.strip().splitlines()[-1])
            except Exception:  # noqa: BLE001
                pass
            braw = Raw(backup)
            bdigest = rows_digest(braw.participant_rows()) if backup.exists() else None
            bmax = max(r["version"] for r in braw.ledger()) if backup.exists() else None
            checks.check(
                f"{label} backup: `openminion storage backup --sqlite <db> --output <file>` (0.1.11 CLI) succeeds; the snapshot is at migration 13 with the seeded rows",
                proc.returncode == 0 and ok_json.get("ok") is True and bmax == 13 and bdigest == oracle.get("digest"),
                observed={"rc": proc.returncode, "ok": ok_json.get("ok"), "backup_ledger_max": bmax, "digest_matches_seed": bdigest == oracle.get("digest"), "stderr_tail": proc.stderr.strip().splitlines()[-2:]},
            )
            guide["backup"] = "openminion storage backup --sqlite <db> --output <file> (0.1.11)"
        else:
            user, dbname = pg_parts(url)
            proc = subprocess.run(["docker", "exec", "pg", "pg_dump", "-U", user, "-d", dbname, "-n", schema, "-Fc"], capture_output=True)
            dump.write_bytes(proc.stdout)
            checks.check(f"{label} backup: pg_dump -Fc of the runtime schema (the guidance `storage backup --backend postgres` prints)", proc.returncode == 0 and dump.stat().st_size > 0, observed={"rc": proc.returncode, "bytes": dump.stat().st_size, "stderr": proc.stderr.decode(errors="replace")[-300:]})
            guide["backup"] = "pg_dump -Fc -n <schema> (docker exec pg)"

        # 3. injected failures inside migration 14
        if url:
            fail_update = [
                "CREATE FUNCTION probe364_fail_update() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected failure: participant update'; END $$",
                "CREATE TRIGGER probe364_fail_update BEFORE UPDATE ON room_participants FOR EACH ROW EXECUTE FUNCTION probe364_fail_update()",
            ]
            drop_update = ["DROP TRIGGER probe364_fail_update ON room_participants", "DROP FUNCTION probe364_fail_update()"]
            fail_ledger = [
                "CREATE FUNCTION probe364_fail_v14() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.version = 14 THEN RAISE EXCEPTION 'injected failure: ledger v14'; END IF; RETURN NEW; END $$",
                "CREATE TRIGGER probe364_fail_v14 BEFORE INSERT ON migrations FOR EACH ROW EXECUTE FUNCTION probe364_fail_v14()",
            ]
            drop_ledger = ["DROP TRIGGER probe364_fail_v14 ON migrations", "DROP FUNCTION probe364_fail_v14()"]
        else:
            fail_update = ["CREATE TRIGGER probe364_fail_update BEFORE UPDATE ON room_participants BEGIN SELECT RAISE(ABORT, 'injected failure: participant update'); END"]
            drop_update = ["DROP TRIGGER probe364_fail_update"]
            fail_ledger = ["CREATE TRIGGER probe364_fail_v14 BEFORE INSERT ON migrations WHEN NEW.version = 14 BEGIN SELECT RAISE(ABORT, 'injected failure: ledger v14'); END"]
            drop_ledger = ["DROP TRIGGER probe364_fail_v14"]
        for name, install, remove, where in (
            ("participant UPDATE (migration 14's numbering statement)", fail_update, drop_update, "update"),
            ("ledger insert of version 14 (after all migration-14 statements ran)", fail_ledger, drop_ledger, "ledger"),
        ):
            raw.execute_script(install)
            res = run_step(sys.executable, "expect-fail", db, url, state, log)
            ledger_now = raw.ledger()
            cols = raw.columns("room_participants")
            idx = raw.index(INDEX)
            tables = raw.tables()
            digest_now = rows_digest(raw.participant_rows())
            checks.check(
                f"{label} failure rollback ({where}): with a failure injected in the {name}, the 0.1.12 upgrade raises MigrationError for v14",
                res.get("raised") == "MigrationError" and "v14" in str(res.get("message")) and "injected failure" in str(res.get("message")),
                observed=res,
            )
            checks.check(
                f"{label} failure rollback ({where}): ledger unchanged at 13 (no 14, no 15), no join_seq column, no index, no session_metadata_strings, participant rows unchanged",
                ledger_now == ledger_seed and "join_seq" not in cols and not idx and "session_metadata_strings" not in tables and digest_now == oracle.get("digest"),
                observed={"ledger": [r["version"] for r in ledger_now][-2:], "join_seq_column": "join_seq" in cols, "index": bool(idx), "metadata_strings_table": "session_metadata_strings" in tables, "rows_unchanged": digest_now == oracle.get("digest")},
            )
            raw.execute_script(remove)

        # 4. upgrade with 0.1.12
        up = run_step(sys.executable, "upgrade", db, url, state, log)
        checks.check(f"{label} upgrade: 0.1.12 applies migrations 14 and 15 once, head 15", up.get("migration") == {"applied": [14, 15], "current": 15}, observed=up.get("migration"))
        ledger_up = raw.ledger()
        versions = [r["version"] for r in ledger_up]
        checks.check(
            f"{label} upgrade: ledger has 14 'add_room_participant_join_seq' and 15 once each, earlier entries unchanged",
            versions.count(14) == 1 and versions.count(15) == 1 and [r for r in ledger_up if r["version"] <= 13] == ledger_seed
            and {r["version"]: r["name"] for r in ledger_up}.get(14) == names.get(14) == "add_room_participant_join_seq",
            observed=[(r["version"], r["name"]) for r in ledger_up if r["version"] >= 13],
        )
        idx = raw.index(INDEX)
        col = raw.column_info("room_participants", "join_seq")
        if url:
            idx_ok = "CREATE UNIQUE INDEX" in str(idx.get("indexdef")) and "(session_id, join_seq)" in str(idx.get("indexdef"))
            col_ok = col.get("is_nullable") == "NO" and str(col.get("column_default")) == "0"
        else:
            idx_ok = idx.get("unique") is True and idx.get("columns") == ["session_id", "join_seq"]
            col_ok = col.get("notnull") == 1 and str(col.get("dflt_value")) == "0"
        checks.check(f"{label} upgrade: unique index {INDEX} on (session_id, join_seq); join_seq INTEGER NOT NULL DEFAULT 0", idx_ok and col_ok, observed={"index": idx, "column": col})
        seq = up.get("join_seq", {})
        counted = oracle.get("counted", {})
        mism = [k for k in counted if seq.get(k) != counted[k]]
        checks.check(
            f"{label} upgrade: every row, including {oracle.get('counts', {}).get('left_participants')} left participants, gets join_seq = its place in the pre-upgrade (joined_at, id) order; dense 1..n per session",
            bool(counted) and not mism and len(seq) == len(counted) and up.get("per_session_dense") is True,
            observed={"rows": len(seq), "mismatches": len(mism), "dense": up.get("per_session_dense")},
        )
        lst_diff = [sid for sid in oracle.get("session_ids", []) if up.get("listing", {}).get(sid) != oracle["listing"].get(sid)]
        act_diff = [sid for sid in oracle.get("session_ids", []) if up.get("active", {}).get(sid) != oracle["active"].get(sid)]
        checks.check(
            f"{label} upgrade: all {len(oracle.get('session_ids', []))} sessions list their participants exactly as 0.1.11 did, with the same active agents",
            not lst_diff and not act_diff,
            observed={"listing_differs": lst_diff[:5], "active_differs": act_diff[:5]},
        )
        checks.check(f"{label} upgrade: participant rows (ids, joined_at, left_at, roles, names) unchanged", up.get("digest") == oracle.get("digest"), observed={"digest_matches_seed": up.get("digest") == oracle.get("digest")})
        checks.check(
            f"{label} upgrade: old tied rows keep the pre-upgrade order, not their true join order (which was not stored)",
            up.get("listing", {}).get("room-0") == ["a4", "a3", "a2", "a1"] != oracle.get("insertion", {}).get("room-0"),
            observed={"after_upgrade": up.get("listing", {}).get("room-0"), "true_join_order": oracle.get("insertion", {}).get("room-0")},
        )
        pw = up.get("post_writes", {})
        checks.check(
            f"{label} after upgrade: a new join on the old tied timestamp, with a row id that sorts before all, lists after every existing participant",
            pw.get("after_new") == pw.get("before", []) + ["a-new"],
            observed={"before": pw.get("before"), "after_new": pw.get("after_new")},
        )
        rs = pw.get("join_seq", {})
        checks.check(
            f"{label} after upgrade: the participant that had left rejoins on the same tick and takes the next place; join_seq a-new=6, a-left=7 after a1..a4 and the old a-left row (1..5)",
            pw.get("after_rejoin") == pw.get("before", []) + ["a-new", "a-left"] and rs.get("a-new") == 6 and rs.get("a-left") == 7,
            observed={"after_rejoin": pw.get("after_rejoin"), "join_seq": rs},
        )
        checks.check(
            f"{label} after upgrade: the active agent leaving selects the first remaining participant in the listing",
            pw.get("active_after_leave") == next((p for p in pw.get("listing_after_leave", []) if p.startswith("a")), None) and pw.get("active_before_leave") not in pw.get("listing_after_leave", []),
            observed={"left": pw.get("active_before_leave"), "listing": pw.get("listing_after_leave"), "selected": pw.get("active_after_leave")},
        )

        # 5. rerun (idempotence), new process
        rr = run_step(sys.executable, "rerun", db, url, state, log)
        checks.check(
            f"{label} rerun: opening the upgraded database again applies nothing (head 15), nor does a direct migrate call",
            rr.get("migration") == {"applied": [], "current": 15} and rr.get("direct_migrate") == {"applied": [], "current": 15},
            observed={"open": rr.get("migration"), "direct": rr.get("direct_migrate")},
        )
        checks.check(
            f"{label} rerun: ledger identical (versions, names, applied_at); join_seq and listings unchanged",
            raw.ledger() == ledger_up and rr.get("join_seq") == up.get("join_seq_after_writes") and rr.get("listing") == up.get("listing_after_writes"),
            observed={"ledger_same": raw.ledger() == ledger_up, "join_seq_same": rr.get("join_seq") == up.get("join_seq_after_writes"), "listing_same": rr.get("listing") == up.get("listing_after_writes")},
        )

        # 6. rollback = restore the matching pre-upgrade backup, then 0.1.11
        if not url:
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(db) + suffix)
                if p.exists():
                    p.unlink()
            shutil.copy2(backup, db)
            restored_how = "copied the pre-upgrade snapshot file over the database"
            restore_ok = True
        else:
            pg_drop_schema(pg_base_url(), schema)
            user, dbname = pg_parts(url)
            proc = subprocess.run(["docker", "exec", "-i", "pg", "pg_restore", "-U", user, "-d", dbname], input=dump.read_bytes(), capture_output=True)
            restored_how = "dropped the schema and ran pg_restore of the pre-upgrade dump"
            restore_ok = proc.returncode == 0
            if not restore_ok:
                print(proc.stderr.decode(errors="replace")[-2000:])
        guard_old("verify restored backup")
        vr = run_step(py11, "verify-restored", db, url, state, log)
        checks.check(
            f"{label} rollback: {restored_how}; 0.1.11 opens it at migration 13 (nothing applied), without join_seq",
            restore_ok and vr.get("version") == "0.1.11" and vr.get("migration") == {"applied": [], "current": 13} and vr.get("has_join_seq") is False,
            observed={k: vr.get(k) for k in ("version", "migration", "has_join_seq")},
        )
        checks.check(
            f"{label} rollback: the restored database has the seeded rows, listings and active agents (post-upgrade writes are gone with the backup)",
            vr.get("digest") == oracle.get("digest") and vr.get("listing") == oracle.get("listing") and vr.get("active") == oracle.get("active"),
            observed={"digest": vr.get("digest") == oracle.get("digest"), "listing": vr.get("listing") == oracle.get("listing"), "active": vr.get("active") == oracle.get("active")},
        )
    finally:
        if url:
            pg_drop_schema(pg_base_url(), schema)
    summary = checks.summary()
    write_result(Path(args.out), {"banner": info, "procedure": guide, "steps": log, "seed_counts": json.loads(state.read_text(encoding="utf-8")).get("counts") if state.exists() else None, "checks": checks.items, "summary": summary})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
