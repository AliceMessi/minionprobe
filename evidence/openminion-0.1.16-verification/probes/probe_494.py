"""#494 (PR #521) on the installed package: the `controlplane` and `telemetry`
Alembic baselines on PostgreSQL, and SQLite unchanged.

    python -I -X utf8 probe_494.py <work_dir> <out.json> --backend postgres
    python -I -X utf8 probe_494.py <work_dir> <out.json> --backend sqlite [--previous-python <0.1.15 python>]

Every migration is a subprocess of the installed `openminion` console script
(`openminion storage migrate ... --module <id>`), from a new empty working
directory, with OPENMINION_HOME / OPENMINION_DATA_ROOT inside the case folder.
The stores are the installed ones, in process: `PostgresControlPlaneStore` /
`PostgresTelemetryStore` over `RecordStorePostgres`, and `SQLiteControlPlaneStore`
/ `SQLiteTelemetryStore`. Downgrades use Alembic's `command.downgrade` with the
installed module's own `alembic.ini` and script location (the package has no
downgrade CLI). The databases are read back independently of openminion
(`information_schema`, `pg_indexes`, `pg_constraint`, or `sqlite3`).

PostgreSQL (items 1-3): one new database per run on the server in
OPENMINION_TEST_POSTGRES_URL, with a new schema per case, each owned by one
module (`search_path` set in the URL), so no two modules share an
`alembic_version` (#446 is not exercised). A second new database holds the
injected-failure case, whose event trigger is database-wide. Both databases are
dropped at the end; passwords are masked in every output.

SQLite (item 4): new files under the work dir. With --previous-python (the
interpreter of the published 0.1.15), the same steps also run through that
release's console script, and its revision history and store-created files are
read through this file's helper modes, run with that interpreter:

    probe_494.py - - --emit-history
    probe_494.py <db file> - --emit-store-file <module>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, quiet, sanitize, sanitize_text, write_result  # noqa: E402

MODULES = ("controlplane", "telemetry")
HEADS = {"controlplane": "0002_index_audit", "telemetry": "0003_debug_query_indexes"}
PREVIOUS = {"controlplane": "0001_baseline", "telemetry": "0002_event_v2"}
SERIAL_TABLES = {
    "controlplane": ("cp_inbound_messages", "cp_outbound_messages", "cp_pair_attempts"),
    "telemetry": ("events",),
}
DATA_TABLES = {
    "controlplane": ("cp_inbound_messages", "cp_outbound_messages", "cp_pair_attempts"),
    "telemetry": ("events",),
}
TIMESTAMPS = (1761650000.123456, 1761650001.654321, 1761650002.000001)
FIXED_TS = "2026-10-30T00:00:00+00:00"
STRIP_ENV = (
    "OPENMINION_HOME",
    "OPENMINION_DATA_ROOT",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
    "OPENMINION_STORAGE_BACKEND",
    "OPENMINION_STORAGE_POSTGRES_URL",
    "OPENMINION_TEST_POSTGRES_URL",
    "PGURL",
)
CASES: dict[str, Any] = {}
REFUSAL = "Existing tables differ from this revision and are not adopted: "


def mask(text: Any) -> str:
    return sanitize_text(re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1***@", str(text)))


def sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def outcome(fn) -> tuple[Any, str | None]:
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001 - the probe reports it
        return None, mask(f"{type(exc).__name__}: {exc}")[:700]


# ----------------------------------------------------------------- CLI
def console_script(python: str | None = None) -> Path:
    bindir = Path(python).parent if python else Path(sys.executable).parent
    return bindir / ("openminion.exe" if os.name == "nt" else "openminion")


class Cli:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.n = 0
        self.runs: list[dict[str, Any]] = []

    def __call__(self, label: str, args: list[str], *, python: str | None = None) -> dict[str, Any]:
        self.n += 1
        case = self.work / "cli" / f"{self.n:03d}"
        cwd = case / "cwd"
        cwd.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env["OPENMINION_HOME"] = str(case / "home")
        env["OPENMINION_DATA_ROOT"] = str(case / "data")
        env["PYTHONIOENCODING"] = "utf-8"
        exe = console_script(python)
        argv = [str(exe), *args]
        started = time.monotonic()
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=900)
        out = proc.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
        err = proc.stderr.decode("utf-8", errors="replace").replace("\r\n", "\n")
        run: dict[str, Any] = {
            "n": self.n,
            "label": label,
            "release": "previous" if python else "installed",
            "argv": [mask(a) for a in ["openminion", *args]],
            "exit_code": proc.returncode,
            "stdout": mask(out)[-6000:],
            "stderr_tail": mask(err[-1500:]),
            "stderr_has_traceback": "Traceback (most recent call last)" in err,
            "seconds": round(time.monotonic() - started, 2),
        }
        try:
            payload = json.loads(out)
            run["entries"] = payload.get("modules") if isinstance(payload, dict) else None
        except ValueError:
            run["entries"] = None
        self.runs.append(run)
        shown = json.dumps(run["entries"])[:600] if run["entries"] is not None else run["stdout"][:600]
        print(f"--- CLI {self.n} [{run['release']}] {label}: exit={proc.returncode} {shown}", flush=True)
        return run


def entry(run: dict[str, Any]) -> dict[str, Any]:
    entries = run.get("entries")
    if isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict):
        return entries[0]
    return {}


def ok_run(run: dict[str, Any], module: str) -> bool:
    return (
        run["exit_code"] == 0
        and entry(run) == {"module_id": module, "status": "ok", "success": True}
        and not run["stderr_has_traceback"]
    )


# ----------------------------------------------------------------- PostgreSQL
class Pg:
    def __init__(self, base: str, tag: str) -> None:
        import sqlalchemy as sa

        self.sa = sa
        self.base = base
        self.db = f"p494_{tag}_{uuid.uuid4().hex[:8]}"
        admin = sa.create_engine(base, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{self.db}"'))
        admin.dispose()
        self.db_url = sa.engine.make_url(base).set(database=self.db).render_as_string(hide_password=False)

    def schema(self, name: str) -> str:
        self.run(self.db_url, f'CREATE SCHEMA "{name}"')
        return self.schema_url(name)

    def schema_url(self, name: str) -> str:
        sep = "&" if "?" in self.db_url else "?"
        return f"{self.db_url}{sep}options={quote_plus(f'-csearch_path={name}')}"

    def run(self, url: str, *statements: str) -> None:
        engine = self.sa.create_engine(url)
        try:
            with engine.begin() as conn:
                for statement in statements:
                    # exec_driver_sql hands the text to psycopg with a parameter
                    # tuple, so a literal percent sign is written as %%.
                    conn.exec_driver_sql(statement.replace("%", "%%"))
        finally:
            engine.dispose()

    def q(self, url: str, sql: str, **params: Any) -> list[list[Any]]:
        engine = self.sa.create_engine(url)
        try:
            with engine.connect() as conn:
                return [list(r) for r in conn.execute(self.sa.text(sql), params).all()]
        finally:
            engine.dispose()

    def catalog(self, url: str) -> dict[str, Any]:
        sa = self.sa
        engine = sa.create_engine(url)
        try:
            with engine.connect() as conn:
                def q(sql: str) -> list[list[Any]]:
                    return [list(r) for r in conn.execute(sa.text(sql)).all()]

                tables = [r[0] for r in q(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() ORDER BY 1")]
                columns: dict[str, list[list[Any]]] = {}
                for t, name, dtype, default, nullable in q(
                        "SELECT table_name, column_name, data_type, column_default, is_nullable "
                        "FROM information_schema.columns WHERE table_schema = current_schema() "
                        "ORDER BY table_name, ordinal_position"):
                    columns.setdefault(t, []).append([name, dtype, default, nullable])
                indexes = {r[0]: r[1] for r in q(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() ORDER BY 1")}
                sequences = [r[0] for r in q(
                    "SELECT sequence_name FROM information_schema.sequences "
                    "WHERE sequence_schema = current_schema() ORDER BY 1")]
                constraints = q(
                    "SELECT conrelid::regclass::text, conname, contype::text, pg_get_constraintdef(c.oid) "
                    "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
                    "WHERE n.nspname = current_schema() ORDER BY 1, 2")
                alembic = [r[0] for r in q("SELECT version_num FROM alembic_version")] if "alembic_version" in tables else None
                om_meta = {r[0]: r[1] for r in q("SELECT key, value FROM om_meta")} if "om_meta" in tables else None
                counts = {t: q(f'SELECT COUNT(*) FROM "{t}"')[0][0] for t in tables}
                schema = q("SELECT current_schema()")[0][0]
        finally:
            engine.dispose()
        return {
            "schema": schema,
            "tables": tables,
            "columns": columns,
            "indexes": indexes,
            "sequences": sequences,
            "constraints": constraints,
            "alembic_version": alembic,
            "om_meta": om_meta,
            "row_counts": counts,
        }

    def data(self, url: str, tables: tuple[str, ...] | list[str]) -> dict[str, list[list[Any]]]:
        out = {}
        present = {r[0] for r in self.q(url, "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
        for t in tables:
            if t in present:
                out[t] = [[repr(v) if isinstance(v, float) else v for v in row]
                          for row in self.q(url, f'SELECT * FROM "{t}" ORDER BY 1')]
        return out

    def server(self) -> dict[str, Any]:
        r = self.q(self.db_url, "SELECT version(), current_database(), current_setting('server_version')")[0]
        return {"version": r[0], "database": r[1], "server_version": r[2]}

    def other_connections(self) -> int:
        return int(self.q(self.base, "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid()", d=self.db)[0][0])

    def drop(self) -> None:
        admin = self.sa.create_engine(self.base, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(self.sa.text(f'DROP DATABASE IF EXISTS "{self.db}" WITH (FORCE)'))
        admin.dispose()


def schema_only(cat: dict[str, Any]) -> dict[str, Any]:
    """The catalog without the module ledger rows and the row counts."""
    return {k: cat[k] for k in ("tables", "columns", "indexes", "sequences", "constraints")}


def serial_ids(cat: dict[str, Any], module: str) -> dict[str, Any]:
    out = {}
    for t in SERIAL_TABLES[module]:
        cols = {c[0]: c for c in cat["columns"].get(t, [])}
        out[t] = cols.get("id", ["id", "missing", None, None])[1:3]
    return out


def serial_ok(cat: dict[str, Any], module: str) -> bool:
    return all(v == ["integer", f"nextval('{t}_id_seq'::regclass)"] for t, v in serial_ids(cat, module).items())


def ts_type(cat: dict[str, Any]) -> Any:
    cols = {c[0]: c for c in cat["columns"].get("events", [])}
    return cols.get("timestamp", [None, "missing"])[1]


@contextmanager
def pg_store(url: str, module: str) -> Iterator[Any]:
    import sqlalchemy as sa

    from openminion.modules.storage.backends.postgres import RecordStorePostgres

    engine = sa.create_engine(url, future=True)
    try:
        record_store = RecordStorePostgres(engine)
        if module == "controlplane":
            from openminion.modules.controlplane.storage.store import PostgresControlPlaneStore

            store = PostgresControlPlaneStore(record_store=record_store)
        else:
            from openminion.modules.telemetry.storage.store import PostgresTelemetryStore

            store = PostgresTelemetryStore(record_store=record_store)
        try:
            yield store
        finally:
            store.close()
    finally:
        engine.dispose()


def telemetry_event(index: int):
    from openminion.modules.telemetry.schemas import TelemetryEvent, normalize_telemetry_event

    return normalize_telemetry_event(TelemetryEvent(
        session_id="session-1", turn_id=f"turn-{index}", event_type="tool.completed",
        timestamp=TIMESTAMPS[index], data={"index": index}, event_id=f"probe-494-event-{index}"))


def write_rows(store: Any, module: str, indexes: list[int]) -> dict[str, Any]:
    if module == "controlplane":
        inbound = [store.put_inbound(chat_key="chat:1", user_key="user:1", text=f"message {i}",
                                     payload={"message_id": f"message-{i}"}, session_id="session-1",
                                     timestamp=FIXED_TS) for i in indexes]
        outbound = store.put_outbound(chat_key="chat:1", text="reply", payload={}, session_id="session-1",
                                      timestamp=FIXED_TS)
        store.record_pair_attempt(channel="telegram", account_id="account-1", chat_key="chat:1",
                                  token="not-a-token", outcome="invalid_token")
        return {"inbound_ids": inbound, "outbound_id": outbound,
                "pair_attempts": store.count_recent_pair_attempts(channel="telegram", account_id="account-1", since_ts=0)}
    for i in indexes:
        store.insert_event(telemetry_event(i))
    return {"inserted": len(indexes)}


def read_rows(store: Any, module: str) -> list[list[Any]]:
    if module == "controlplane":
        return [[int(t["id"]), t["text"]] for t in store.list_turns("session-1")]
    page = store.fetch_event_page(high_water=store.event_high_water(), limit=20)
    return sorted([[row.row_id, row.event.timestamp, bool(row.timestamp_valid)] for row in page])


def expected_rows(module: str, ids: list[int], indexes: list[int]) -> list[list[Any]]:
    if module == "controlplane":
        return [[i, f"message {k}"] for i, k in zip(ids, indexes)]
    return [[i, TIMESTAMPS[k], True] for i, k in zip(ids, indexes)]


def alembic_command(url: str, module: str, action: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

    ini_path, script_location = discover_module_alembic_paths(module)
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(script_location))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    getattr(command, action)(config, revision)


def migrate_args(url: str, module: str, *, as_json: bool = True) -> list[str]:
    return ["storage", "migrate", "--backend", "postgres", "--postgres-url", url, "--module", module] + (
        ["--json"] if as_json else [])


INCOMPATIBLE = [
    ("telemetry-real-timestamp", "telemetry", "events",
     ["CREATE TABLE events (id SERIAL PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
      "event_type TEXT NOT NULL, timestamp REAL NOT NULL, data TEXT NOT NULL)",
      "INSERT INTO events (session_id, turn_id, event_type, timestamp, data) "
      "VALUES ('session-1', 'turn-1', 'tool.completed', 1761650000.123456, '{}')"],
     "events.timestamp is REAL, expected DOUBLE PRECISION"),
    ("telemetry-bigint-identity-id", "telemetry", "events",
     ["CREATE TABLE events (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, session_id TEXT NOT NULL, "
      "turn_id TEXT NOT NULL, event_type TEXT NOT NULL, timestamp DOUBLE PRECISION NOT NULL, data TEXT NOT NULL)",
      "INSERT INTO events (session_id, turn_id, event_type, timestamp, data) "
      "VALUES ('session-1', 'turn-1', 'tool.completed', 1761650000.123456, '{}')"],
     "events.id is BIGINT, expected SERIAL"),
    ("telemetry-other-columns", "telemetry", "events",
     ["CREATE TABLE events (event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload_json TEXT NOT NULL)",
      "INSERT INTO events VALUES ('event-1', 'session-1', '{}')"],
     "events.id is missing, expected SERIAL; events.timestamp is missing, expected DOUBLE PRECISION"),
    ("controlplane-integer-id", "controlplane", "cp_inbound_messages",
     ["CREATE TABLE cp_inbound_messages (id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, chat_key TEXT NOT NULL, "
      "user_key TEXT NOT NULL, session_id TEXT, agent_id TEXT, timestamp TEXT NOT NULL, text TEXT, "
      "payload_json TEXT NOT NULL)",
      "INSERT INTO cp_inbound_messages VALUES (7, 'message-7', 'chat:1', 'user:1', NULL, NULL, 'ts', 'hello', '{}')"],
     "cp_inbound_messages.id is INTEGER, expected SERIAL"),
    ("controlplane-text-id", "controlplane", "cp_pair_attempts",
     ["CREATE TABLE cp_pair_attempts (id TEXT PRIMARY KEY, channel TEXT NOT NULL)",
      "INSERT INTO cp_pair_attempts VALUES ('attempt-1', 'telegram')"],
     "cp_pair_attempts.id is TEXT, expected SERIAL"),
]

# A real failure part-way through the baseline: the column check passes, then a
# later statement of the same revision fails, so earlier DDL must roll back.
MID_BASELINE = [
    ("telemetry-events-without-turn_id", "telemetry", "events",
     ["CREATE TABLE events (id SERIAL PRIMARY KEY, session_id TEXT NOT NULL, event_type TEXT NOT NULL, "
      "timestamp DOUBLE PRECISION NOT NULL, data TEXT NOT NULL)",
      "INSERT INTO events (session_id, event_type, timestamp, data) "
      "VALUES ('session-1', 'tool.completed', 1761650000.123456, '{}')"],
     "turn_id"),
    ("controlplane-cp_users-without-role", "controlplane", "cp_users",
     ["CREATE TABLE cp_users (user_id TEXT PRIMARY KEY, display_name TEXT, updated_at TEXT)",
      "INSERT INTO cp_users VALUES ('user-1', 'Probe', 'ts')"],
     "role"),
]

INJECT_AT = {"controlplane": "idx_cp_pairings_status_channel", "telemetry": "idx_events_session_turn_invocation_id"}
TRIGGER_SQL = [
    """
    CREATE OR REPLACE FUNCTION public.probe_494_fail() RETURNS event_trigger LANGUAGE plpgsql AS $fn$
    DECLARE
      obj record;
      n integer;
    BEGIN
      FOR obj IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
        n := coalesce(nullif(current_setting('probe494.ddl_count', true), ''), '0')::integer + 1;
        PERFORM set_config('probe494.ddl_count', n::text, true);
        IF obj.command_tag = 'CREATE INDEX' AND (
             obj.object_identity LIKE '%.idx_cp_pairings_status_channel'
             OR obj.object_identity LIKE '%.idx_events_session_turn_invocation_id') THEN
          RAISE EXCEPTION 'probe 494: injected failure at % after % DDL objects in this transaction',
            obj.object_identity, n;
        END IF;
      END LOOP;
    END
    $fn$
    """,
    "CREATE EVENT TRIGGER probe_494_fail ON ddl_command_end EXECUTE FUNCTION public.probe_494_fail()",
]


def run_postgres(checks: Checks, cli: Cli, base: str) -> None:
    import sqlalchemy as sa

    pg = Pg(base, "main")
    inj = Pg(base, "inject")
    try:
        server = pg.server()
        CASES["postgres_server"] = server
        CASES["databases"] = [pg.db, inj.db]
        checks.check("setup: PostgreSQL server reached; new databases created for this run",
                     server["server_version"].split(".")[0].isdigit(), observed=server)
        CASES["sqlalchemy"] = sa.__version__
        try:
            import psycopg

            CASES["psycopg"] = psycopg.__version__
        except Exception:  # noqa: BLE001
            CASES["psycopg"] = None
        real = pg.q(pg.db_url, "SELECT CAST(1761650000.123456 AS REAL)::double precision, "
                               "CAST(1761650000.123456 AS DOUBLE PRECISION)")[0]
        CASES["reference_real_vs_double"] = {"real": real[0], "double precision": real[1]}

        # ------------------------------------------------------------ item 1
        for m in MODULES:
            url = pg.schema(f"i1_{m}")
            before = pg.catalog(url)
            r1 = cli(f"item 1 {m}: migrate a new module-owned schema", migrate_args(url, m))
            cat1 = pg.catalog(url)
            CASES[f"item1_{m}_after_migrate"] = cat1
            # The store and verify checks below are about the schema the real
            # migration created, so each one requires that migration to have succeeded.
            migrated = ok_run(r1, m) and cat1["alembic_version"] == [HEADS[m]]
            checks.check(f"item 1 ({m}): `openminion storage migrate --backend postgres --module {m} --json` "
                         "on a new, empty schema exits 0 with status ok",
                         before["tables"] == [] and ok_run(r1, m),
                         observed={"tables_before": before["tables"], "exit": r1["exit_code"], "entry": entry(r1)})
            checks.check(f"item 1 ({m}): the schema is at the expected head {HEADS[m]}, with the module's om_meta",
                         cat1["alembic_version"] == [HEADS[m]]
                         and (cat1["om_meta"] or {}).get("module_id") == m
                         and (cat1["om_meta"] or {}).get("schema_head") == HEADS[m],
                         observed={"alembic_version": cat1["alembic_version"], "om_meta": cat1["om_meta"]})
            checks.check(f"item 1 ({m}): generated ids: every AUTOINCREMENT id of the baseline is an integer "
                         "with a sequence default on PostgreSQL (SERIAL)",
                         serial_ok(cat1, m), observed={"id columns": serial_ids(cat1, m), "sequences": cat1["sequences"]})
            if m == "telemetry":
                checks.check("item 1 (telemetry): events.timestamp is DOUBLE PRECISION on the server",
                             ts_type(cat1) == "double precision", observed=ts_type(cat1))
            r2 = cli(f"item 1 {m}: rerun at head", migrate_args(url, m))
            cat2 = pg.catalog(url)
            checks.check(f"item 1 ({m}): a second migrate exits 0 with status ok and changes nothing "
                         "(tables, columns, indexes, sequences, constraints, alembic_version, om_meta module_id/schema_head)",
                         ok_run(r2, m) and schema_only(cat2) == schema_only(cat1)
                         and cat2["alembic_version"] == cat1["alembic_version"]
                         and {k: (cat2["om_meta"] or {}).get(k) for k in ("module_id", "schema_head")}
                         == {k: (cat1["om_meta"] or {}).get(k) for k in ("module_id", "schema_head")},
                         observed={"exit": r2["exit_code"], "entry": entry(r2),
                                   "catalog_sha256": [sha(schema_only(cat1)), sha(schema_only(cat2))],
                                   "om_meta_last_migrated_at": [(cat1["om_meta"] or {}).get("last_migrated_at"),
                                                                (cat2["om_meta"] or {}).get("last_migrated_at")]})
            rv = cli(f"item 1 {m}: storage verify", ["storage", "verify", "--backend", "postgres",
                                                       "--postgres-url", url, "--module", m, "--json"])
            checks.check(f"item 1 ({m}): `openminion storage verify --backend postgres --module {m}` passes on the migrated schema",
                         migrated and rv["exit_code"] == 0 and entry(rv).get("status") == "passed",
                         observed={"migrated": migrated, "exit": rv["exit_code"], "status": entry(rv).get("status")})

            # round trip through the actual store
            def first_open() -> dict[str, Any]:
                with pg_store(url, m) as store:
                    written = write_rows(store, m, [0, 1])
                    return {"written": written, "read": read_rows(store, m)}

            got, err = outcome(first_open)
            cat3 = pg.catalog(url)
            CASES[f"item1_{m}_round_trip"] = {"result": got, "error": err}
            exp_ids = [1, 2]
            if m == "controlplane":
                checks.check("item 1 (controlplane): PostgresControlPlaneStore opens the schema the baseline created "
                             "(no DuplicateColumn on cp_inbox.next_attempt_at) and its generated ids are 1, 2; "
                             "outbound id 1; one pair attempt",
                             migrated and err is None and got["written"]["inbound_ids"] == exp_ids and got["written"]["outbound_id"] == 1
                             and got["written"]["pair_attempts"] == 1 and got["read"] == expected_rows(m, exp_ids, [0, 1]),
                             observed={"migrated": migrated, "error": err, "result": got})
                cp_versions = pg.q(url, "SELECT version FROM cp_migrations ORDER BY version") if "cp_migrations" in cat3["tables"] else None
                from openminion.modules.controlplane.storage.schema import MIGRATIONS as CP_MIGRATIONS

                inbox_cols = [c[0] for c in cat3["columns"].get("cp_inbox", [])]
                checks.check("item 1 (controlplane): opening the store records every store migration in cp_migrations "
                             "and leaves the baseline's tables and columns unchanged (cp_inbox.next_attempt_at once)",
                             migrated and err is None and cp_versions == [[v] for v, _n, _s in CP_MIGRATIONS]
                             and schema_only(cat3) == schema_only(cat1) and inbox_cols.count("next_attempt_at") == 1,
                             observed={"cp_migrations": cp_versions, "catalog_unchanged": schema_only(cat3) == schema_only(cat1),
                                       "next_attempt_at_columns": inbox_cols.count("next_attempt_at")})
                server_ids = {t: pg.q(url, f'SELECT id FROM "{t}" ORDER BY id') if t in cat3["tables"] else None
                              for t in SERIAL_TABLES[m]}
                checks.check("item 1 (controlplane): read on the server, the rows carry the generated ids "
                             "(cp_inbound_messages 1, 2; cp_outbound_messages 1; cp_pair_attempts 1)",
                             migrated and server_ids == {"cp_inbound_messages": [[1], [2]], "cp_outbound_messages": [[1]],
                                            "cp_pair_attempts": [[1]]}, observed=server_ids)
            else:
                checks.check("item 1 (telemetry): PostgresTelemetryStore writes two events on the baseline schema and "
                             "reads them back with ids 1, 2 and the exact timestamps 1761650000.123456, 1761650001.654321",
                             migrated and err is None and got["read"] == expected_rows(m, exp_ids, [0, 1]),
                             observed={"migrated": migrated, "error": err, "result": got})
                server = pg.q(url, "SELECT id, timestamp, pg_typeof(timestamp)::text FROM events ORDER BY id") if "events" in cat3["tables"] else None
                checks.check("item 1 (telemetry): read on the server, events (id, timestamp) are (1, 1761650000.123456) and "
                             "(2, 1761650001.654321), typed double precision; REAL would have stored 1761650048.0",
                             migrated and server == [[1, TIMESTAMPS[0], "double precision"], [2, TIMESTAMPS[1], "double precision"]],
                             observed={"server_rows": server, "reference_real_vs_double": CASES["reference_real_vs_double"]})
                checks.check("item 1 (telemetry): opening the store leaves the baseline's tables and columns unchanged",
                             migrated and err is None and schema_only(cat3) == schema_only(cat1),
                             observed={"catalog_unchanged": schema_only(cat3) == schema_only(cat1)})

            def reopen() -> dict[str, Any]:
                with pg_store(url, m) as store:
                    written = write_rows(store, m, [2])
                    return {"written": written, "read": read_rows(store, m)}

            got2, err2 = outcome(reopen)
            if m == "controlplane":
                ok2 = err2 is None and got2["written"]["inbound_ids"] == [3] and got2["read"] == expected_rows(m, [1, 2, 3], [0, 1, 2])
            else:
                ok2 = err2 is None and got2["read"] == expected_rows(m, [1, 2, 3], [0, 1, 2])
            checks.check(f"item 1 ({m}): a second store instance reads the rows and its next generated id is 3",
                         migrated and ok2, observed={"migrated": migrated, "error": err2, "result": got2})
            cat_r = pg.catalog(url)
            rows_r = pg.data(url, DATA_TABLES[m])
            r3 = cli(f"item 1 {m}: migrate after the store wrote", migrate_args(url, m))
            cat4 = pg.catalog(url)
            checks.check(f"item 1 ({m}): migrate after the store's writes exits 0 and changes nothing; every row is unchanged",
                         ok_run(r3, m) and schema_only(cat4) == schema_only(cat_r)
                         and cat4["alembic_version"] == [HEADS[m]] and pg.data(url, DATA_TABLES[m]) == rows_r,
                         observed={"exit": r3["exit_code"], "entry": entry(r3), "catalog_unchanged": schema_only(cat4) == schema_only(cat_r),
                                   "row_counts": {t: cat4["row_counts"].get(t) for t in DATA_TABLES[m]}})

        # ------------------------------------------------------------ item 2
        for m in MODULES:
            url = pg.schema(f"i2_store_{m}")

            def store_first() -> dict[str, Any]:
                with pg_store(url, m) as store:
                    return {"written": write_rows(store, m, [0]), "read": read_rows(store, m)}

            got, err = outcome(store_first)
            cat0 = pg.catalog(url)
            # om_meta is the module's metadata, which the migration updates
            # (schema_head, last_migrated_at); it is reported separately.
            store_tables = [t for t in cat0["tables"] if t != "om_meta"]
            data0 = pg.data(url, store_tables)
            CASES[f"item2_{m}_store_created"] = {"store": got, "error": err, "catalog": cat0}
            checks.check(f"item 2 ({m}): the store created its own tables on a new schema and wrote rows; no alembic_version",
                         err is None and cat0["alembic_version"] is None and all(t in cat0["tables"] for t in SERIAL_TABLES[m]),
                         observed={"error": err, "tables": cat0["tables"], "row_counts": cat0["row_counts"]})
            r = cli(f"item 2 {m}: migrate store-created tables", migrate_args(url, m))
            cat1 = pg.catalog(url)
            data1 = pg.data(url, store_tables)
            CASES[f"item2_{m}_after_migrate"] = cat1
            CASES[f"item2_{m}_om_meta"] = {"before": cat0["om_meta"], "after": cat1["om_meta"]}
            # "after migration" below requires the migration to have succeeded.
            migrated = ok_run(r, m) and cat1["alembic_version"] == [HEADS[m]]
            checks.check(f"item 2 ({m}): migrate over the store-created tables exits 0 with status ok and reaches {HEADS[m]}",
                         ok_run(r, m) and cat1["alembic_version"] == [HEADS[m]],
                         observed={"exit": r["exit_code"], "entry": entry(r), "alembic_version": cat1["alembic_version"],
                                   "error": entry(r).get("error")})
            same_cols = {t: cat1["columns"].get(t) == cat0["columns"].get(t) for t in cat0["tables"]}
            checks.check(f"item 2 ({m}): every store-created table keeps its columns and types "
                         "(SERIAL ids" + (", DOUBLE PRECISION timestamp" if m == "telemetry" else "") + ")",
                         migrated and all(same_cols.values()) and serial_ok(cat1, m) and (m != "telemetry" or ts_type(cat1) == "double precision"),
                         observed={"migrated": migrated, "columns_unchanged": same_cols, "id columns": serial_ids(cat1, m),
                                   "timestamp": ts_type(cat1) if m == "telemetry" else None})
            checks.check(f"item 2 ({m}): every row of every store-created table is unchanged after the migration "
                         "(om_meta aside, which the migration updates)",
                         migrated and data1 == data0 and bool(data0), observed={"migrated": migrated, "rows_before_sha256": sha(data0), "rows_after_sha256": sha(data1),
                                                                   "row_counts": {t: len(v) for t, v in data1.items()},
                                                                   "om_meta": {"before": cat0["om_meta"], "after": cat1["om_meta"]}})
            added = sorted(set(cat1["indexes"]) - set(cat0["indexes"]))
            CASES[f"item2_{m}_indexes_added"] = added

            def after() -> dict[str, Any]:
                with pg_store(url, m) as store:
                    written = write_rows(store, m, [1])
                    return {"written": written, "read": read_rows(store, m)}

            got2, err2 = outcome(after)
            if m == "controlplane":
                ok2 = err2 is None and got2["written"]["inbound_ids"] == [2] and got2["read"] == expected_rows(m, [1, 2], [0, 1])
            else:
                ok2 = err2 is None and got2["read"] == expected_rows(m, [1, 2], [0, 1])
            checks.check(f"item 2 ({m}): the store reopened after the migration reads the old rows and writes new ones "
                         "(next generated id 2" + (", exact timestamps" if m == "telemetry" else "") + ")",
                         migrated and ok2, observed={"migrated": migrated, "error": err2, "result": got2})
            cat_before_rerun = pg.catalog(url)
            data_before_rerun = pg.data(url, store_tables)
            r2 = cli(f"item 2 {m}: rerun", migrate_args(url, m))
            cat2 = pg.catalog(url)
            checks.check(f"item 2 ({m}): a second migrate exits 0 and changes nothing (catalog and rows)",
                         ok_run(r2, m) and schema_only(cat2) == schema_only(cat_before_rerun)
                         and pg.data(url, store_tables) == data_before_rerun and cat2["alembic_version"] == [HEADS[m]],
                         observed={"exit": r2["exit_code"], "entry": entry(r2), "alembic_version": cat2["alembic_version"]})

        for name, m, table, statements, message in INCOMPATIBLE:
            url = pg.schema(f"i2_{name.replace('-', '_')}")
            pg.run(url, *statements)
            cat0 = pg.catalog(url)
            data0 = pg.data(url, [table])
            r = cli(f"item 2 {name}: migrate over an incompatible table", migrate_args(url, m))
            cat1 = pg.catalog(url)
            data1 = pg.data(url, [table])
            err = str(entry(r).get("error") or "")
            CASES[f"item2_refused_{name}"] = {"before": cat0, "after": cat1, "rows": data1}
            checks.check(f"item 2 ({name}): migrate is refused: exit 1, status error, and the error says "
                         f"`{REFUSAL}{message}`",
                         r["exit_code"] == 1 and entry(r).get("status") == "error" and entry(r).get("success") is False
                         and (REFUSAL + message) in err,
                         observed={"exit": r["exit_code"], "status": entry(r).get("status"), "error": err[:500]})
            checks.check(f"item 2 ({name}): nothing changed: no partial objects (tables, columns, indexes, sequences, "
                         "constraints identical), no ledger stamp (no alembic_version, no om_meta), no data loss",
                         cat1 == cat0 and data1 == data0 and cat1["alembic_version"] is None and cat1["om_meta"] is None
                         and cat1["tables"] == [table],
                         observed={"tables": cat1["tables"], "alembic_version": cat1["alembic_version"], "om_meta": cat1["om_meta"],
                                   "catalog_identical": cat1 == cat0, "rows": data1})
            if name == "telemetry-real-timestamp":
                rt = cli(f"item 2 {name}: text output", migrate_args(url, m, as_json=False))
                checks.check(f"item 2 ({name}): the text output also refuses: exit 1, `FAIL  telemetry: ...{message}`; still nothing changed",
                             rt["exit_code"] == 1 and f"FAIL  {m}: " in rt["stdout"] and message in rt["stdout"]
                             and pg.catalog(url) == cat0 and pg.data(url, [table]) == data0,
                             observed={"exit": rt["exit_code"], "stdout": rt["stdout"][:500]})

        # ------------------------------------------------------------ item 3
        for m in MODULES:
            url = pg.schema(f"i3_{m}")
            r = cli(f"item 3 {m}: migrate", migrate_args(url, m))
            head_cat = pg.catalog(url)
            _w, werr = outcome(lambda: _write_once(url, m, [0]))
            rows0 = pg.data(url, DATA_TABLES[m])
            checks.check(f"item 3 ({m}): disposable schema migrated to {HEADS[m]} and one store write",
                         ok_run(r, m) and head_cat["alembic_version"] == [HEADS[m]] and werr is None,
                         observed={"exit": r["exit_code"], "alembic_version": head_cat["alembic_version"], "write_error": werr})
            _, derr = outcome(lambda: alembic_command(url, m, "downgrade", PREVIOUS[m]))
            step_cat = pg.catalog(url)
            dropped = sorted(set(head_cat["indexes"]) - set(step_cat["indexes"]))
            checks.check(f"item 3 ({m}): Alembic downgrade {HEADS[m]} -> {PREVIOUS[m]} removes that revision's indexes "
                         "only; tables, ids and rows stay",
                         derr is None and step_cat["alembic_version"] == [PREVIOUS[m]] and bool(dropped)
                         and step_cat["tables"] == head_cat["tables"] and step_cat["sequences"] == head_cat["sequences"]
                         and pg.data(url, DATA_TABLES[m]) == rows0,
                         observed={"error": derr, "alembic_version": step_cat["alembic_version"], "indexes_dropped": dropped})
            r = cli(f"item 3 {m}: re-upgrade from {PREVIOUS[m]}", migrate_args(url, m))
            up_cat = pg.catalog(url)
            checks.check(f"item 3 ({m}): migrate from {PREVIOUS[m]} exits 0 and restores exactly the head catalog; rows kept",
                         ok_run(r, m) and schema_only(up_cat) == schema_only(head_cat) and up_cat["alembic_version"] == [HEADS[m]]
                         and pg.data(url, DATA_TABLES[m]) == rows0,
                         observed={"exit": r["exit_code"], "alembic_version": up_cat["alembic_version"],
                                   "catalog_identical": schema_only(up_cat) == schema_only(head_cat)})
            _, derr = outcome(lambda: alembic_command(url, m, "downgrade", "base"))
            base_cat = pg.catalog(url)
            checks.check(f"item 3 ({m}): Alembic downgrade to base leaves only alembic_version (empty) and om_meta, "
                         "no sequence and no index other than their keys",
                         derr is None and base_cat["alembic_version"] == [] and base_cat["tables"] == ["alembic_version", "om_meta"]
                         and base_cat["sequences"] == [],
                         observed={"error": derr, "tables": base_cat["tables"], "alembic_version": base_cat["alembic_version"],
                                   "sequences": base_cat["sequences"], "indexes": sorted(base_cat["indexes"])})
            r = cli(f"item 3 {m}: re-upgrade from base", migrate_args(url, m))
            re_cat = pg.catalog(url)
            checks.check(f"item 3 ({m}): migrate from base exits 0 and recreates exactly the head catalog "
                         "(tables, column types and defaults, indexes, sequences, constraints)",
                         ok_run(r, m) and schema_only(re_cat) == schema_only(head_cat) and re_cat["alembic_version"] == [HEADS[m]],
                         observed={"exit": r["exit_code"], "alembic_version": re_cat["alembic_version"],
                                   "catalog_identical": schema_only(re_cat) == schema_only(head_cat)})
            got, err = outcome(lambda: _write_once(url, m, [0, 1]))
            if m == "controlplane":
                okr = err is None and got["written"]["inbound_ids"] == [1, 2]
            else:
                okr = err is None and got["read"] == expected_rows(m, [1, 2], [0, 1])
            checks.check(f"item 3 ({m}): after the re-upgrade the store round-trips again (ids 1, 2 from the new sequence"
                         + (", exact timestamps" if m == "telemetry" else "") + ")",
                         okr, observed={"error": err, "result": got})

        for name, m, table, statements, column in MID_BASELINE:
            url = pg.schema(f"i3_{name.replace('-', '_')}")
            pg.run(url, *statements)
            cat0 = pg.catalog(url)
            data0 = pg.data(url, [table])
            r = cli(f"item 3 {name}: a statement of the baseline fails part-way", migrate_args(url, m))
            cat1 = pg.catalog(url)
            err = str(entry(r).get("error") or "")
            CASES[f"item3_rollback_{name}"] = {"before": cat0, "after": cat1}
            checks.check(f"item 3 ({name}): a later statement of the baseline fails on the existing table "
                         f"(UndefinedColumn `{column}`): exit 1, status error",
                         r["exit_code"] == 1 and entry(r).get("status") == "error" and "UndefinedColumn" in err and column in err,
                         observed={"exit": r["exit_code"], "error": err[:500]})
            checks.check(f"item 3 ({name}): the transaction rolled back: the baseline's earlier tables/indexes/sequences, "
                         "alembic_version and om_meta are gone; the existing table and its row are unchanged",
                         cat1 == cat0 and pg.data(url, [table]) == data0 and cat1["alembic_version"] is None and cat1["om_meta"] is None,
                         observed={"tables": cat1["tables"], "indexes": sorted(cat1["indexes"]), "sequences": cat1["sequences"],
                                   "catalog_identical": cat1 == cat0})

        # injected failure in the last revision, after the baseline ran in the same transaction
        inj.run(inj.db_url, *TRIGGER_SQL)
        for m in MODULES:
            url = inj.schema(f"inj_{m}")
            cat0 = inj.catalog(url)
            r = cli(f"item 3 {m}: injected failure at {INJECT_AT[m]}", migrate_args(url, m))
            cat1 = inj.catalog(url)
            err = str(entry(r).get("error") or "")
            n = re.search(r"after (\d+) DDL objects", err)
            CASES[f"item3_injected_{m}"] = {"error": err[:800], "after": cat1}
            checks.check(f"item 3 ({m}): an event trigger makes the last revision's index {INJECT_AT[m]} fail after the "
                         "earlier revisions' DDL ran in the same transaction: exit 1 with the injected error",
                         r["exit_code"] == 1 and entry(r).get("status") == "error"
                         and f"probe 494: injected failure at inj_{m}.{INJECT_AT[m]}" in err and n is not None and int(n.group(1)) > 3,
                         observed={"exit": r["exit_code"], "error": err[:500], "ddl_objects_before_failure": int(n.group(1)) if n else None})
            checks.check(f"item 3 ({m}): after the injected failure nothing is left: no table, sequence, index, "
                         "alembic_version or om_meta in the schema",
                         cat1 == cat0 and cat1["tables"] == [] and cat1["sequences"] == [] and cat1["indexes"] == {},
                         observed={"tables": cat1["tables"], "sequences": cat1["sequences"], "indexes": cat1["indexes"]})
        inj.run(inj.db_url, "DROP EVENT TRIGGER probe_494_fail")
        for m in MODULES:
            url = inj.schema_url(f"inj_{m}")
            r = cli(f"item 3 {m}: the same schema once the trigger is dropped", migrate_args(url, m))
            cat2 = inj.catalog(url)
            checks.check(f"item 3 ({m}): with the trigger dropped, the same schema migrates to {HEADS[m]} (exit 0)",
                         ok_run(r, m) and cat2["alembic_version"] == [HEADS[m]],
                         observed={"exit": r["exit_code"], "entry": entry(r), "alembic_version": cat2["alembic_version"]})

        left = {pg.db: pg.other_connections(), inj.db: inj.other_connections()}
        CASES["server_connections_left"] = left
        checks.check("setup: no connection to the probe databases is left open (pg_stat_activity)",
                     all(v == 0 for v in left.values()), observed=left)
    finally:
        for db in (pg, inj):
            try:
                db.drop()
            except Exception as exc:  # noqa: BLE001
                print("drop failed:", mask(exc))


def _write_once(url: str, m: str, indexes: list[int]) -> dict[str, Any]:
    with pg_store(url, m) as store:
        written = write_rows(store, m, indexes)
        return {"written": written, "read": read_rows(store, m)}


# ----------------------------------------------------------------- SQLite
def sqlite_facts(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path)) as conn:
        master = [list(r) for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]
        names = {r[1] for r in master if r[0] == "table"}
        alembic = [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")] if "alembic_version" in names else None
        om_meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM om_meta")} if "om_meta" in names else None
        cp_versions = [r[0] for r in conn.execute("SELECT version, name FROM cp_migrations ORDER BY version")] if "cp_migrations" in names else None
        inbox_cols = [r[1] for r in conn.execute("PRAGMA table_info(cp_inbox)")] if "cp_inbox" in names else None
        rows = {}
        for t in ("cp_inbound_messages", "events"):
            if t in names:
                rows[t] = [[repr(v) if isinstance(v, float) else v for v in r] for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY 1')]
        return {
            "sqlite_master": master,
            "sqlite_master_sha256": sha(master),
            "alembic_version": alembic,
            "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "om_meta": om_meta,
            "cp_migrations": cp_versions,
            "cp_inbox_next_attempt_at_columns": None if inbox_cols is None else inbox_cols.count("next_attempt_at"),
            "rows": rows,
        }


def comparable(f: dict[str, Any]) -> dict[str, Any]:
    out = {k: f[k] for k in ("sqlite_master", "alembic_version", "application_id", "user_version",
                            "cp_migrations", "cp_inbox_next_attempt_at_columns", "rows")}
    out["om_meta"] = None if f["om_meta"] is None else {k: v for k, v in f["om_meta"].items() if not k.endswith("_at")}
    return out


def history() -> dict[str, Any]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from openminion.modules.controlplane.storage.schema import MIGRATIONS as CP_MIGRATIONS
    from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

    out: dict[str, Any] = {}
    for m in MODULES:
        _ini, location = discover_module_alembic_paths(m)
        cfg = Config()
        cfg.set_main_option("script_location", str(location))
        script = ScriptDirectory.from_config(cfg)
        baseline = script.get_revision("0001_baseline").module
        ddl = [str(s).strip() for s in baseline.DDL]
        out[m] = {
            "revisions": [[r.revision, r.down_revision] for r in script.walk_revisions()],
            "head": script.get_current_head(),
            "version_files": sorted(p.name for p in (Path(location) / "versions").glob("*.py")),
            "sqlite_ddl_statements": len(ddl),
            "sqlite_ddl_sha256": sha(ddl),
            "sqlite_ddl_autoincrement_ids": sum("id INTEGER PRIMARY KEY AUTOINCREMENT" in s for s in ddl),
            "sqlite_ddl_real_timestamp": sum("timestamp REAL NOT NULL" in s for s in ddl),
            "has_POSTGRES_DDL": hasattr(baseline, "POSTGRES_DDL"),
        }
    out["controlplane"]["store_migrations_sha256"] = sha([list(x) for x in CP_MIGRATIONS])
    out["controlplane"]["store_migrations"] = [v for v, _n, _s in CP_MIGRATIONS]
    return out


def make_store_file(path: Path, module: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if module == "controlplane":
        from openminion.modules.controlplane.storage.store import SQLiteControlPlaneStore

        store = SQLiteControlPlaneStore(path)
        try:
            store.put_inbound(chat_key="chat:1", user_key="user:1", text="hello", payload={"message_id": "message-1"},
                              session_id="session-1", timestamp=FIXED_TS)
        finally:
            store.close()
    else:
        from openminion.modules.telemetry.storage.store import SQLiteTelemetryStore

        store = SQLiteTelemetryStore(path)
        try:
            store.insert_event(telemetry_event(0))
        finally:
            store.close()


def other_python(python: str, args: list[str]) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    proc = subprocess.run([python, "-I", "-X", "utf8", str(Path(__file__).resolve()), *args],
                          capture_output=True, env=env, timeout=600)
    out = proc.stdout.decode("utf-8", errors="replace")
    lines = [line for line in out.splitlines() if line.startswith("JSON ")]
    try:
        payload = json.loads(lines[-1][5:]) if lines else None
    except ValueError:
        payload = None
    return {"exit": proc.returncode, "payload": payload, "stderr_tail": mask(proc.stderr.decode("utf-8", errors="replace")[-800:])}


def run_sqlite(checks: Checks, cli: Cli, work: Path, previous: str | None) -> None:
    from openminion.modules.controlplane.storage.migrations import run_migrations as cp_run_migrations
    from openminion.modules.controlplane.storage.schema import MIGRATIONS as CP_MIGRATIONS
    from openminion.modules.controlplane.storage.store import SQLiteControlPlaneStore
    from openminion.modules.telemetry.storage.store import SQLiteTelemetryStore

    db = work / "sqlite"
    db.mkdir(parents=True, exist_ok=True)

    def new_file(name: str) -> Path:
        p = db / f"{name}.db"
        sqlite3.connect(p).close()
        return p

    def migrate(path: Path, m: str, *, python: str | None = None, label: str = "") -> dict[str, Any]:
        return cli(label or f"sqlite {m} {path.name}", ["storage", "migrate", "--sqlite", str(path), "--module", m, "--json"],
                   python=python)

    current_history = history()
    CASES["history_installed"] = current_history
    if previous:
        prev = other_python(previous, ["-", "-", "--emit-history"])
        CASES["history_previous"] = prev
        ph = prev["payload"] or {}
        for m in MODULES:
            cur = {k: v for k, v in current_history[m].items() if k != "has_POSTGRES_DDL"}
            old = {k: v for k, v in (ph.get(m) or {}).items() if k != "has_POSTGRES_DDL"}
            checks.check(f"item 4 ({m}): the Alembic history is the shipped one: same revisions, head, version files and "
                         "SQLite baseline DDL (same sha256) as the published 0.1.15"
                         + ("; the store's own migrations are unchanged too" if m == "controlplane" else ""),
                         prev["exit"] == 0 and cur == old,
                         observed={"installed": cur, "0.1.15": old,
                                   "has_POSTGRES_DDL": [current_history[m]["has_POSTGRES_DDL"], (ph.get(m) or {}).get("has_POSTGRES_DDL")]})

    for m in MODULES:
        f_new = new_file(f"{m}-new")
        r = migrate(f_new, m, label=f"item 4 {m}: new file")
        facts1 = sqlite_facts(f_new)
        CASES[f"sqlite_{m}_new"] = facts1
        checks.check(f"item 4 ({m}): `openminion storage migrate --sqlite <new file> --module {m}` exits 0 and reaches {HEADS[m]}",
                     ok_run(r, m) and facts1["alembic_version"] == [HEADS[m]],
                     observed={"exit": r["exit_code"], "entry": entry(r), "alembic_version": facts1["alembic_version"],
                               "application_id": facts1["application_id"], "user_version": facts1["user_version"]})
        tables_sql = {row[1]: str(row[3]) for row in facts1["sqlite_master"] if row[0] == "table"}
        ai = {t: "id INTEGER PRIMARY KEY AUTOINCREMENT" in tables_sql.get(t, "") for t in SERIAL_TABLES[m]}
        checks.check(f"item 4 ({m}): SQLite keeps its own DDL: AUTOINCREMENT ids"
                     + (" and `timestamp REAL NOT NULL`" if m == "telemetry" else ""),
                     all(ai.values()) and (m != "telemetry" or "timestamp REAL NOT NULL" in tables_sql.get("events", "")),
                     observed={"autoincrement": ai, "events_sql": tables_sql.get("events") if m == "telemetry" else None})
        r2 = migrate(f_new, m, label=f"item 4 {m}: rerun")
        facts2 = sqlite_facts(f_new)
        checks.check(f"item 4 ({m}): a second migrate exits 0 and changes nothing (sqlite_master, alembic_version, "
                     "application_id, user_version)",
                     ok_run(r2, m) and comparable(facts2) == comparable(facts1),
                     observed={"exit": r2["exit_code"], "sqlite_master_sha256": [facts1["sqlite_master_sha256"], facts2["sqlite_master_sha256"]]})
        if previous:
            f_prev = new_file(f"{m}-by-0.1.15")
            rp = migrate(f_prev, m, python=previous, label=f"item 4 {m}: new file by 0.1.15")
            fp = sqlite_facts(f_prev)
            CASES[f"sqlite_{m}_by_0115"] = fp
            checks.check(f"item 4 ({m}): a new file migrated by the 0.1.15 console script is identical to one migrated by "
                         "this release (sqlite_master, alembic_version, application_id, user_version, om_meta module_id/schema_head)",
                         ok_run(rp, m) and comparable(fp) == comparable(facts1),
                         observed={"0.1.15_exit": rp["exit_code"], "sqlite_master_sha256": {"installed": facts1["sqlite_master_sha256"],
                                                                                            "0.1.15": fp["sqlite_master_sha256"]},
                                   "alembic_version": [facts1["alembic_version"], fp["alembic_version"]]})
            ru = migrate(f_prev, m, label=f"item 4 {m}: 0.1.15 file upgraded by this release")
            fu = sqlite_facts(f_prev)
            checks.check(f"item 4 ({m}): the file migrated by 0.1.15, migrated again by this release: exit 0, nothing changed",
                         ok_run(ru, m) and comparable(fu) == comparable(fp),
                         observed={"exit": ru["exit_code"], "sqlite_master_sha256": [fp["sqlite_master_sha256"], fu["sqlite_master_sha256"]],
                                   "alembic_version": fu["alembic_version"]})
            # store-created files: one by each release's store, each migrated by its own release
            f_sa = db / f"{m}-store-installed.db"
            f_sb = db / f"{m}-store-0.1.15.db"
            _, serr = outcome(lambda: make_store_file(f_sa, m))
            pb = other_python(previous, [str(f_sb), "-", "--emit-store-file", m])
            sa_facts, sb_facts = sqlite_facts(f_sa), sqlite_facts(f_sb)
            checks.check(f"item 4 ({m}): the {m} store creates the same SQLite file as the 0.1.15 store "
                         "(sqlite_master, cp_migrations, rows)",
                         serr is None and pb["exit"] == 0 and comparable(sa_facts) == comparable(sb_facts),
                         observed={"error": serr, "0.1.15_exit": pb["exit"], "0.1.15_stderr": pb["stderr_tail"][-300:],
                                   "sqlite_master_sha256": [sa_facts["sqlite_master_sha256"], sb_facts["sqlite_master_sha256"]]})
            ra = migrate(f_sa, m, label=f"item 4 {m}: store-created file")
            rb = migrate(f_sb, m, python=previous, label=f"item 4 {m}: store-created file by 0.1.15")
            fa, fb = sqlite_facts(f_sa), sqlite_facts(f_sb)
            checks.check(f"item 4 ({m}): migrating a store-created file gives the same result as 0.1.15 "
                         "(exit, entry, sqlite_master, alembic_version, rows)",
                         ra["exit_code"] == rb["exit_code"] and entry(ra) == entry(rb) and comparable(fa) == comparable(fb)
                         and fa["rows"] == sa_facts["rows"],
                         observed={"exit": [ra["exit_code"], rb["exit_code"]], "entry": [entry(ra), entry(rb)],
                                   "alembic_version": [fa["alembic_version"], fb["alembic_version"]],
                                   "sqlite_master_sha256": [fa["sqlite_master_sha256"], fb["sqlite_master_sha256"]]})

    # controlplane opens a file its baseline created
    for how in ("cli", "run_migrations"):
        f = new_file(f"controlplane-baseline-{how}")
        if how == "cli":
            r = migrate(f, "controlplane", label="item 4 controlplane: baseline file for the store")
            made = ok_run(r, "controlplane")
        else:
            _, merr = outcome(lambda: cp_run_migrations(f))
            made = merr is None
        before = sqlite_facts(f)

        def open_store() -> dict[str, Any]:
            store = SQLiteControlPlaneStore(f)
            try:
                ids = [store.put_inbound(chat_key="chat:1", user_key="user:1", text=t, payload={"message_id": t},
                                         session_id="session-1", timestamp=FIXED_TS) for t in ("one", "two")]
                return {"inbound_ids": ids, "turns": [[int(x["id"]), x["text"]] for x in store.list_turns("session-1")]}
            finally:
                store.close()

        got, err = outcome(open_store)
        after = sqlite_facts(f)
        CASES[f"sqlite_controlplane_store_on_baseline_{how}"] = {"result": got, "error": err, "after": after}
        entry_point = "`openminion storage migrate`" if how == "cli" else "the module's own `run_migrations`"
        checks.check(f"item 4 (controlplane, file made by {entry_point}): SQLiteControlPlaneStore opens it without "
                     "`duplicate column name: next_attempt_at` and round-trips (ids 1, 2)",
                     made and err is None and got["inbound_ids"] == [1, 2] and got["turns"] == [[1, "one"], [2, "two"]],
                     observed={"baseline_made": made, "error": err, "result": got})
        checks.check(f"item 4 (controlplane, file made by {entry_point}): the store records every store migration, "
                     "cp_inbox.next_attempt_at exists once, sqlite_master and alembic_version unchanged",
                     err is None and after["cp_migrations"] == [v for v, _n, _s in CP_MIGRATIONS]
                     and after["cp_inbox_next_attempt_at_columns"] == 1 and after["sqlite_master"] == before["sqlite_master"]
                     and after["alembic_version"] == [HEADS["controlplane"]],
                     observed={"cp_migrations": after["cp_migrations"], "next_attempt_at_columns": after["cp_inbox_next_attempt_at_columns"],
                               "sqlite_master_unchanged": after["sqlite_master"] == before["sqlite_master"],
                               "alembic_version": after["alembic_version"]})

    # unchanged: an older store file (first five store migrations) still gets the retry column
    f_old = db / "controlplane-older.db"
    with closing(sqlite3.connect(f_old)) as conn:
        for version, name, ddl in CP_MIGRATIONS[:5]:
            conn.executescript(ddl)
            conn.execute("INSERT INTO cp_migrations(version, name, applied_at) VALUES (?,?,?)",
                         (version, name, "2026-01-01T00:00:00+00:00"))
        conn.commit()
    before_cols = sqlite_facts(f_old)["cp_inbox_next_attempt_at_columns"]
    _, oerr = outcome(lambda: SQLiteControlPlaneStore(f_old).close())
    after_old = sqlite_facts(f_old)
    checks.check("item 4 (controlplane, unchanged behavior): an older store file without cp_inbox.next_attempt_at "
                 "still gets the column when the store opens it",
                 before_cols == 0 and oerr is None and after_old["cp_inbox_next_attempt_at_columns"] == 1
                 and after_old["cp_migrations"] == [v for v, _n, _s in CP_MIGRATIONS],
                 observed={"before": before_cols, "after": after_old["cp_inbox_next_attempt_at_columns"], "error": oerr,
                           "cp_migrations": after_old["cp_migrations"]})

    # telemetry store on a baseline-created SQLite file
    f_t = new_file("telemetry-baseline-store")
    rt = migrate(f_t, "telemetry", label="item 4 telemetry: baseline file for the store")

    def tel() -> list[list[Any]]:
        store = SQLiteTelemetryStore(f_t)
        try:
            for i in (0, 1):
                store.insert_event(telemetry_event(i))
            return read_rows(store, "telemetry")
        finally:
            store.close()

    got, err = outcome(tel)
    checks.check("item 4 (telemetry): SQLiteTelemetryStore on the file the baseline created writes and reads ids 1, 2 "
                 "with the exact timestamps",
                 ok_run(rt, "telemetry") and err is None and got == expected_rows("telemetry", [1, 2], [0, 1]),
                 observed={"exit": rt["exit_code"], "error": err, "rows": got})


# ----------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--backend", choices=("sqlite", "postgres"))
    ap.add_argument("--previous-python", default="")
    ap.add_argument("--emit-history", action="store_true")
    ap.add_argument("--emit-store-file", default="")
    a = ap.parse_args()
    quiet()
    if a.emit_history:
        print("JSON " + json.dumps(history()))
        return 0
    if a.emit_store_file:
        make_store_file(Path(a.work), a.emit_store_file)
        print("JSON " + json.dumps({"file": str(Path(a.work).name)}))
        return 0

    base = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
    if a.backend == "postgres" and not base:
        print("NOT RUN: OPENMINION_TEST_POSTGRES_URL is not set")
        return 2
    work = Path(a.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    exe = console_script()
    prev_exe = console_script(a.previous_python) if a.previous_python else None
    info = banner("probe_494", backend=a.backend, console_script=str(exe), console_script_exists=exe.exists(),
                  previous_console_script=str(prev_exe) if prev_exe else None,
                  previous_console_script_exists=prev_exe.exists() if prev_exe else None)
    checks = Checks()
    cli = Cli(work)
    checks.check("setup: the installed `openminion` console script exists next to this interpreter", exe.exists(),
                 observed=str(exe))
    if prev_exe is not None:
        checks.check("setup: the 0.1.15 console script exists next to --previous-python", prev_exe.exists(),
                     observed=str(prev_exe))
    try:
        if a.backend == "postgres":
            run_postgres(checks, cli, base)
        else:
            run_sqlite(checks, cli, work, a.previous_python or None)
    except Exception as exc:  # noqa: BLE001 - recorded as a failing check
        import traceback

        traceback.print_exc()
        checks.check("probe completed without an unexpected exception", False, observed=mask(f"{type(exc).__name__}: {exc}"))
    summary = checks.summary()
    write_result(Path(a.out), {"banner": info, "summary": summary, "checks": checks.items,
                               "cli_runs": cli.runs, "cases": sanitize(CASES)})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}", flush=True)
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
