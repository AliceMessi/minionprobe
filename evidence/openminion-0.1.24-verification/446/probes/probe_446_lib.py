"""#446 helpers for the installed-package probes on PostgreSQL (no repo test fixtures).

Catalog reads are the probe's own SQL on the server (pg_catalog), independent of openminion.
The only openminion code used here is the installed package's public entry points: the
`openminion` console script, build_runtime_storage, the module store factories, the module
Alembic environments, and (for the layouts earlier code wrote) runtime storage's own
migrate_record_store and the module Alembic chains run on a connection to the base schema,
as the PR's own tests build them. Nothing here imports postgres_schema, so the same helpers
run on the published 0.1.16 (the control).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import text

BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "")
os.environ.pop("PGOPTIONS", None)
os.environ.setdefault("OPENMINION_SECRET_KEY", "ZmFrZS1rZXktZm9yLXRoZS00NDYtcHJvYmUtb25seS0=")
SYSTEM = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
LEDGER_TABLES = {"alembic_version", "om_meta"}
ENGINE_TABLES = {"core_events", "core_sidecar_rows", "sidecar_ingest_log"}
NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
NOTHING = "Nothing was written"
GUIDANCE = 'follow "PostgreSQL schemas per owner" in docs/runtime-database-upgrades.md'

WORKING_MODULES = ("secret", "session", "identity", "registry", "task", "skill", "policy", "compress",
                   "retrieve", "artifact", "a2a", "memory", "controlplane", "telemetry")
# Modules whose Alembic base layout is listed (memory, session and skill downgrades fail).
BASE_LISTED = tuple(m for m in WORKING_MODULES if m not in ("memory", "session", "skill"))
COLLIDING = ("sessions", "events", "idempotency_keys", "memory_records", "migrations", "agents",
             "session_turn_leases", "session_retention_holds")

FACTORIES = {
    # store: (module id, import path, factory, database file)
    "a2a_state_store": ("a2a", "openminion.modules.a2a.storage.factory", "build_a2a_state_store", "a2a.db"),
    "session_store": ("session", "openminion.modules.session.runtime.factory", "build_module_session_store",
                      "sessions.db"),
    "controlplane_store": ("controlplane", "openminion.modules.controlplane.storage", "build_controlplane_store",
                           "cp.db"),
    "telemetry_store": ("telemetry", "openminion.modules.telemetry.storage", "build_telemetry_store", "t.db"),
    "registry_store": ("registry", "openminion.modules.registry.storage", "build_registry_store", "registry.db"),
    "artifact_store": ("artifact", "openminion.modules.artifact.storage", "build_artifact_index", "artifact.db"),
    "identity_store": ("identity", "openminion.modules.identity.storage", "build_identity_store", "identity.db"),
    "policy_store": ("policy", "openminion.modules.policy.storage", "build_policy_store", "policy.db"),
    "retrieve_store": ("retrieve", "openminion.modules.retrieve.storage", "build_retrieve_store", "retrieve.db"),
    "skill_store": ("skill", "openminion.modules.skill.storage", "build_skill_store", "skill.db"),
    "task_store": ("task", "openminion.modules.task.storage", "build_task_store", "task.db"),
    "compress_store": ("compress", "openminion.modules.context.compress.storage", "build_compress_telemetry_store",
                       "compress.db"),
}
STORES = ("memory_store", "a2a_audit_store", *FACTORIES)


def store_module(store: str) -> str:
    return {"memory_store": "memory", "a2a_audit_store": "a2a"}.get(store) or FACTORIES[store][0]


# ---------------------------------------------------------------------------- server

def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def admin():
    return sa.create_engine(BASE, isolation_level="AUTOCOMMIT")


def db_url(name: str, **query: str) -> str:
    url = sa.engine.make_url(BASE).set(database=name)
    if query:
        url = url.update_query_dict(query)
    return url.render_as_string(hide_password=False)


def with_options(url: str, options: str, **query: str) -> str:
    return sa.engine.make_url(url).update_query_dict({"options": options, **query}).render_as_string(
        hide_password=False)


def schema_url(url: str, schema: str) -> str:
    return with_options(url, f'-c search_path="{schema}"')


def dbname(url: str) -> str:
    return str(sa.engine.make_url(url).database)


CREATED: list[str] = []


def new_db(tag: str) -> str:
    name = f"om446_{tag[:30]}_{uuid.uuid4().hex[:6]}".lower().replace("-", "_")
    eng = admin()
    with eng.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {qi(name)}"))
    eng.dispose()
    CREATED.append(name)
    return db_url(name)


def drop_db(name: str) -> None:
    eng = admin()
    try:
        with eng.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {qi(name)} WITH (FORCE)"))
    finally:
        eng.dispose()


def drop_all() -> int:
    n = 0
    for name in list(CREATED):
        try:
            drop_db(name)
            n += 1
        except Exception:  # noqa: BLE001
            pass
    CREATED.clear()
    return n


def execute(url: str, *statements: str) -> None:
    eng = sa.create_engine(url)
    try:
        with eng.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        eng.dispose()


def scalars(url: str, sql: str, **params: Any) -> list:
    eng = sa.create_engine(url)
    try:
        with eng.connect() as conn:
            return list(conn.execute(text(sql), params).scalars())
    finally:
        eng.dispose()


def schemas(url: str) -> list[str]:
    return scalars(url, f"SELECT n.nspname FROM pg_namespace n WHERE {SYSTEM} ORDER BY 1")


def tables(url: str, schema: str) -> list[str]:
    return scalars(url, "SELECT tablename FROM pg_tables WHERE schemaname = :s ORDER BY 1", s=schema)


def connections(url: str) -> int:
    """Client connections to the URL's database, read from pg_stat_activity on `postgres`."""
    eng = admin()
    try:
        with eng.connect() as conn:
            return int(conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = :d AND pid <> pg_backend_pid() "
                "AND backend_type = 'client backend'"), {"d": dbname(url)}).scalar())
    finally:
        eng.dispose()


def wait_connections_zero(url: str, seconds: float = 5.0) -> int:
    end = time.monotonic() + seconds
    count = connections(url)
    while count and time.monotonic() < end:
        time.sleep(0.2)
        count = connections(url)
    return count


def server_version() -> str:
    eng = admin()
    try:
        with eng.connect() as conn:
            return str(conn.execute(text("SELECT version()")).scalar())
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------- catalog reads

def snapshot(url: str, *, ignore_migrated_at: bool = False) -> dict:
    """Every non-system schema: owner; tables (columns with type, NOT NULL and default,
    primary key, constraints, row count and md5 of all rows); indexes; functions; triggers;
    sequences (last value); views; ledgers (alembic_version, om_meta, migrations)."""
    eng = sa.create_engine(url)
    out: dict = {}
    try:
        with eng.connect() as conn:
            for schema, owner, oid in conn.execute(text(
                    f"SELECT n.nspname, pg_get_userbyid(n.nspowner), n.oid FROM pg_namespace n WHERE {SYSTEM} "
                    "ORDER BY 1")).all():
                block: dict = {"owner": owner, "tables": {}}
                rels = conn.execute(text(
                    "SELECT c.oid, c.relname FROM pg_class c WHERE c.relnamespace = :o AND c.relkind IN ('r','p') "
                    "ORDER BY 2"), {"o": oid}).all()
                for roid, rel in rels:
                    cols = [list(r) for r in conn.execute(text(
                        "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                        "coalesce(pg_get_expr(d.adbin, d.adrelid), '') FROM pg_attribute a "
                        "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                        "WHERE a.attrelid = :r AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"),
                        {"r": roid})]
                    cons = sorted([list(r) for r in conn.execute(text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = :r"),
                        {"r": roid})])
                    where = ""
                    if rel == "om_meta" and ignore_migrated_at and any(c[0] == "key" for c in cols):
                        where = " WHERE x.key <> 'last_migrated_at'"
                    count, md5 = conn.execute(text(
                        "SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                        f"FROM {qi(schema)}.{qi(rel)} x{where}")).one()
                    block["tables"][rel] = {"columns": cols, "constraints": cons, "rows": int(count), "md5": md5}
                block["indexes"] = [list(r) for r in conn.execute(text(
                    "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s ORDER BY 1"), {"s": schema})]
                block["functions"] = [list(r) for r in conn.execute(text(
                    "SELECT p.proname, md5(coalesce(p.prosrc, '')) FROM pg_proc p WHERE p.pronamespace = :o "
                    "ORDER BY 1"), {"o": oid})]
                block["triggers"] = [list(r) for r in conn.execute(text(
                    "SELECT t.tgname, c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relnamespace = :o AND NOT t.tgisinternal ORDER BY 1"), {"o": oid})]
                block["sequences"] = [list(r) for r in conn.execute(text(
                    "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = :s ORDER BY 1"),
                    {"s": schema})]
                block["views"] = sorted(conn.execute(text(
                    "SELECT viewname FROM pg_views WHERE schemaname = :s"), {"s": schema}).scalars())
                block["ledgers"] = ledgers_on(conn, schema, block["tables"], ignore_migrated_at)
                out[schema] = block
    finally:
        eng.dispose()
    return out


def ledgers_on(conn, schema: str, tabs: dict, ignore_migrated_at: bool = False) -> dict:
    led: dict = {}
    q = qi(schema)
    if "alembic_version" in tabs and any(c[0] == "version_num" for c in tabs["alembic_version"]["columns"]):
        led["alembic_version"] = sorted(str(r) for r in conn.execute(text(f"SELECT version_num FROM {q}.alembic_version")).scalars())
    if "om_meta" in tabs and {"key", "value"} <= {c[0] for c in tabs["om_meta"]["columns"]}:
        led["om_meta"] = {str(k): str(v) for k, v in conn.execute(text(f"SELECT key, value FROM {q}.om_meta"))
                          if not (ignore_migrated_at and k == "last_migrated_at")}
    if "migrations" in tabs and {"version", "name"} <= {c[0] for c in tabs["migrations"]["columns"]}:
        try:
            led["migrations"] = [[str(v), str(n)] for v, n in conn.execute(text(
                f"SELECT version, name FROM {q}.migrations ORDER BY version"))]
        except Exception:  # noqa: BLE001
            conn.rollback()
    return led


def diff_snap(a: dict, b: dict) -> list[str]:
    """Readable differences between two snapshots (empty when equal)."""
    out = []
    for schema in sorted(set(a) | set(b)):
        if schema not in a:
            out.append(f"+schema {schema}")
            continue
        if schema not in b:
            out.append(f"-schema {schema}")
            continue
        x, y = a[schema], b[schema]
        for t in sorted(set(x["tables"]) | set(y["tables"])):
            if x["tables"].get(t) != y["tables"].get(t):
                out.append(f"~{schema}.{t}" if t in x["tables"] and t in y["tables"]
                           else (f"+{schema}.{t}" if t in y["tables"] else f"-{schema}.{t}"))
        for key in ("owner", "indexes", "functions", "triggers", "sequences", "views", "ledgers"):
            if x.get(key) != y.get(key):
                out.append(f"~{schema} {key}")
        mx, my = x.get("ledgers", {}).get("om_meta") or {}, y.get("ledgers", {}).get("om_meta") or {}
        for k in sorted(set(mx) | set(my)):
            if mx.get(k) != my.get(k) and k != "last_migrated_at":
                out.append(f"~{schema}.om_meta[{k}]: {mx.get(k)!r} -> {my.get(k)!r}")
    return out


def signatures(url: str, schema: str) -> dict[str, str]:
    """`name type [NOT NULL], ...; pk(a, b)`, the format of postgres_histories.json."""
    eng = sa.create_engine(url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(
                "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "coalesce(a.attnum = ANY(i.indkey), false) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_attribute a ON a.attrelid = c.oid "
                "LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary "
                "WHERE n.nspname = :s AND c.relkind IN ('r','p') AND a.attnum > 0 AND NOT a.attisdropped"),
                {"s": schema}).all()
    finally:
        eng.dispose()
    cols: dict[str, list] = {}
    keys: dict[str, list[str]] = {}
    for table, name, typ, notnull, key in rows:
        cols.setdefault(table, []).append((name, typ, bool(notnull)))
        if key:
            keys.setdefault(table, []).append(name)
    out = {}
    for table, c in cols.items():
        body = ", ".join(f"{n} {t}{' NOT NULL' if nn else ''}" for n, t, nn in sorted(c))
        out[table] = f"{body}; pk({', '.join(sorted(keys.get(table, [])))})"
    return out


class Histories:
    """postgres_histories.json read as data (the installed 0.1.17 file is the reference)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw.decode("utf-8"))
        sigs = data["signatures"]
        self.items = [{"owner": h["owner"], "ledger": h["ledger"], "measured": list(h["measured"]),
                       "tables": {r.split("/")[0]: sigs[r] for r in h["tables"]}} for h in data["histories"]]
        self.engine_tables = {t: set(v) for t, v in data["engine_tables"].items()}
        self.runtime_versions = {int(k): v for k, v in data["runtime_versions"].items()}
        self.names: dict[str, set[str]] = {}
        for h in self.items:
            self.names.setdefault(h["owner"], set()).update(h["tables"])

    def ledger_of(self, owner: str, led: dict) -> str | None:
        if owner == "runtime":
            rows = led.get("migrations") or []
            ok = rows and [[str(v), n] for v, n in rows] == [[str(v), self.runtime_versions.get(v)]
                                                             for v in range(1, len(rows) + 1)]
            return f"v{len(rows)}" if ok else None
        meta = led.get("om_meta") or {}
        if meta.get("module_id") != owner:
            return None
        alembic = led.get("alembic_version")
        if alembic is None:
            return f"store {meta.get('schema_head', '')}"
        if not alembic:
            return "alembic base"
        return f"alembic {alembic[0]}" if len(alembic) == 1 else None

    def match(self, owner: str, url: str, schema: str, *, current_only: bool = True) -> str | None:
        """The listed history the schema holds exactly for `owner` (its owned tables are exactly
        one listed history's, its ledger is that history's), else None. Next to a module's
        tables, the storage engine's tables must have one of the listed engine signatures."""
        snap = snapshot(url)
        if schema not in snap:
            return None
        led = snap[schema]["ledgers"]
        ledger = self.ledger_of(owner, led)
        sigs = signatures(url, schema)
        owned = {t: s for t, s in sigs.items() if t in self.names.get(owner, set())}
        if owner != "runtime":
            for t in ENGINE_TABLES & set(sigs):
                if sigs[t] not in self.engine_tables.get(t, set()):
                    return None
        for h in self.items:
            if h["owner"] == owner and h["ledger"] == ledger and h["tables"] == owned:
                if current_only and "current" not in h["measured"]:
                    continue
                return f"{owner} {ledger}" + ("" if "current" in h["measured"] else " (historical)")
        return None

    def runtime_signature(self, table: str) -> set[str]:
        return {h["tables"][table] for h in self.items if h["owner"] == "runtime" and table in h["tables"]}


def module_head(module_id: str) -> str | None:
    from alembic.script import ScriptDirectory

    from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

    _, location = discover_module_alembic_paths(module_id)
    return None if location is None else ScriptDirectory(str(location)).get_current_head()


def module_chain(module_id: str) -> list[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

    _, location = discover_module_alembic_paths(module_id)
    cfg = Config()
    cfg.set_main_option("script_location", str(location))
    return [s.revision for s in reversed(list(ScriptDirectory.from_config(cfg).walk_revisions()))]


# ---------------------------------------------------------------------------- generic rows

VALUES = {"text": "'x'", "character varying": "'x'", "integer": "1", "bigint": "1", "smallint": "1",
          "double precision": "1.0", "real": "1.0", "numeric": "1", "boolean": "false", "jsonb": "'{}'::jsonb",
          "json": "'{}'::json", "bytea": "'\\x00'::bytea", "timestamp with time zone": "now()",
          "timestamp without time zone": "now()", "date": "current_date"}


def add_rows(url: str, schema: str, skip: set[str] | None = None) -> list[str]:
    """One row in every non-ledger table of `schema`, generic values for NOT NULL columns
    without a default (tables a generic row cannot satisfy are skipped)."""
    skip = set(skip or ()) | LEDGER_TABLES | {"migrations"}
    eng = sa.create_engine(url)
    added = []
    try:
        with eng.connect() as conn:
            names = list(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = :s ORDER BY 1"),
                                      {"s": schema}).scalars())
            conn.commit()
            for table in names:
                if table in skip:
                    continue
                cols = conn.execute(text(
                    "SELECT a.attname, format_type(a.atttypid, NULL) FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :s AND c.relname = :t AND a.attnum > 0 AND NOT a.attisdropped "
                    "AND a.attnotnull AND a.atthasdef = false AND a.attidentity = '' AND a.attgenerated = ''"),
                    {"s": schema, "t": table}).all()
                conn.commit()
                if any(t not in VALUES for _, t in cols):
                    continue
                names_sql = ", ".join(qi(c) for c, _ in cols)
                values = ", ".join(VALUES[t] for _, t in cols)
                sql = (f"INSERT INTO {qi(schema)}.{qi(table)} ({names_sql}) VALUES ({values})" if cols
                       else f"INSERT INTO {qi(schema)}.{qi(table)} DEFAULT VALUES")
                try:
                    with conn.begin():
                        conn.execute(text(sql))
                    added.append(table)
                except Exception:  # noqa: BLE001
                    if conn.in_transaction():
                        conn.rollback()
    finally:
        eng.dispose()
    return added


def row_texts(url: str, schema: str, table: str, columns: list[str]) -> list[str]:
    eng = sa.create_engine(url)
    try:
        with eng.connect() as conn:
            names = ", ".join(qi(c) for c in columns)
            return sorted(str(r) for r in conn.execute(text(
                f"SELECT ROW({names})::text FROM {qi(schema)}.{qi(table)}")).scalars())
    finally:
        eng.dispose()


def column_names(url: str, schema: str, table: str) -> list[str]:
    return scalars(url, "SELECT column_name FROM information_schema.columns WHERE table_schema = :s "
                        "AND table_name = :t ORDER BY ordinal_position", s=schema, t=table)


# ---------------------------------------------------------------------------- CLI

def console(python: str | None = None) -> str:
    exe = Path(python or sys.executable)
    name = "openminion.exe" if os.name == "nt" else "openminion"
    return str(exe.with_name(name))


RUNS: list[dict] = []


def cli(argv: list[str], root: Path, *, python: str | None = None, env_extra: dict | None = None,
        timeout: int = 900) -> dict:
    """The installed `openminion` console script, from a new empty working directory with
    OPENMINION_HOME and OPENMINION_DATA_ROOT inside the case folder."""
    cwd = root / f"cwd-{uuid.uuid4().hex[:6]}"
    cwd.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_STORAGE")}
    env.update({"OPENMINION_HOME": str(root / "home"), "OPENMINION_DATA_ROOT": str(root / "data")})
    env.update(env_extra or {})
    env.pop("PGOPTIONS", None)
    if env_extra and "PGOPTIONS" in env_extra:
        env["PGOPTIONS"] = env_extra["PGOPTIONS"]
    started = time.monotonic()
    proc = subprocess.run([console(python), *argv], cwd=cwd, capture_output=True, text=True, env=env,
                          timeout=timeout)
    left = sorted(p.name for p in cwd.iterdir())
    record = {"argv": ["openminion", *[redact(a) for a in argv]], "exit": proc.returncode,
              "stdout": proc.stdout, "stderr": proc.stderr[-3000:], "cwd_left": left,
              "seconds": round(time.monotonic() - started, 2)}
    RUNS.append({k: v for k, v in record.items() if k != "stdout"} | {"stdout": proc.stdout[-4000:]})
    return record


def redact(arg: str) -> str:
    if arg.startswith("postgresql"):
        try:
            url = sa.engine.make_url(arg)
            return url.set(password="***").render_as_string(hide_password=False).replace(
                url.host or "", "<host>").replace(f"/{url.database}", "/<db>")
        except Exception:  # noqa: BLE001
            return "<url>"
    return arg


def migrate(url: str, root: Path, module: str | None = None, *, plan: bool = False, as_json: bool = True,
            verify: bool = False, python: str | None = None, env_extra: dict | None = None) -> dict:
    """`openminion storage migrate --backend postgres --postgres-url URL [--module M] [--plan] --json`."""
    argv = ["storage", "migrate", "--backend", "postgres", "--postgres-url", url]
    if module:
        argv += ["--module", module]
    if plan:
        argv.append("--plan")
    if verify:
        argv.append("--verify")
    if as_json:
        argv.append("--json")
    run = cli(argv, root, python=python, env_extra=env_extra)
    run["entries"] = {}
    if as_json:
        try:
            run["entries"] = {e["module_id"]: e for e in json.loads(run["stdout"])["modules"]}
        except Exception:  # noqa: BLE001
            run["entries"] = {}
    return run


def entry_error(run: dict, module: str) -> str:
    e = run.get("entries", {}).get(module) or {}
    return " ".join(str(e.get("error", "")).split())


# ---------------------------------------------------------------------------- owners

def storage_config(url: str, root: Path):
    from openminion.modules.storage.engine import StorageEngineConfig

    root.mkdir(parents=True, exist_ok=True)
    return StorageEngineConfig(root_dir=root, sqlite_path=root / "state.db", fallback_root=root,
                               record_backend="record.postgres", record_backend_options={"url": url})


def start_runtime(url: str, root: Path) -> int:
    from openminion.modules.storage.runtime.context import build_runtime_storage

    ctx = build_runtime_storage(root / "state.db", env={}, record_backend="record.postgres",
                                record_backend_options={"url": url})
    try:
        return int(ctx.migration_result.current_version)
    finally:
        ctx.close()


def runtime_rows(url: str) -> None:
    execute(url,
            "INSERT INTO public.sessions(id, session_key, channel, target) VALUES ('s-1', 'k-1', 'cli', 'probe')",
            "INSERT INTO public.idempotency_keys(method, idempotency_key, request_hash, response_json, status) "
            "VALUES ('m', 'k', 'h', '{}', 'done')")


def legacy_runtime(url: str) -> str:
    """Runtime storage's migrations as earlier code ran them: no schema check."""
    from openminion.modules.storage.backends.postgres import RecordStorePostgres
    from openminion.modules.storage.runtime.migrations import migrate_record_store

    store = RecordStorePostgres(url)
    try:
        migrate_record_store(store, backend_type="postgres")
    except Exception as exc:  # noqa: BLE001
        return str(exc).splitlines()[0][:500]
    finally:
        store.close()
    return "ok"


def legacy_module(url: str, module_id: str, target: str = "head", *, downgrade: bool = False) -> str:
    """A module's Alembic chain on a connection to the URL's schema, as earlier code ran every
    module (and as the PR's tests build legacy layouts)."""
    from alembic import command
    from alembic.config import Config

    from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

    ini, location = discover_module_alembic_paths(module_id)
    cfg = Config(str(ini)) if ini else Config()
    cfg.set_main_option("script_location", str(location))
    eng = sa.create_engine(url)
    try:
        with eng.begin() as conn:
            cfg.attributes["connection"] = conn
            (command.downgrade if downgrade else command.upgrade)(cfg, target)
    finally:
        eng.dispose()
    return "ok"


def memory_record(record_id: str):
    from openminion.modules.memory.models import MemoryRecord

    return MemoryRecord(id=record_id, scope="agent:a", type="fact", title=f"title {record_id}",
                        content=f"content {record_id}", created_at=NOW, updated_at=NOW)


def legacy_memory(url: str, root: Path, record_id: str = "rec-1") -> str:
    """A dedicated memory database as earlier releases built it: the memory store on an engine
    for the URL's schema."""
    from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

    store = PostgresMemoryStore(sa.create_engine(url, future=True), database_path=root / "legacy-memory.db",
                                owns_engine=True)
    try:
        store.put(memory_record(record_id))
    finally:
        store.close()
    return "ok"


def memory_store(url: str, root: Path):
    from openminion.modules.memory.storage.factory import resolve_memory_backend

    root.mkdir(parents=True, exist_ok=True)
    return resolve_memory_backend(config={"store": {"backend": "postgres", "postgres_url": url}},
                                  db_path=root / "memory.db").store


def a2a_audit_store(url: str, root: Path):
    from openminion.modules.a2a.storage.factory import build_a2a_audit_store

    return build_a2a_audit_store(config=storage_config(url, root), audit_root=root / "a2a-audit")


def audit_record(msg_id: str):
    from openminion.modules.a2a.models import AuditRecord

    return AuditRecord(ts=NOW, msg_id=msg_id, trace_id="t-1", from_agent="a", to_agent="b", to_capability=None,
                       type="request", method="probe.method", status="ok")


def build_store(store: str, url: str, root: Path):
    if store == "memory_store":
        return memory_store(url, root)
    if store == "a2a_audit_store":
        return a2a_audit_store(url, root)
    import importlib

    _, module, name, filename = FACTORIES[store]
    kwargs: dict[str, Any] = {"config": storage_config(url, root), "database_path": root / filename}
    if name == "build_module_session_store":
        kwargs["env"] = {}
    return getattr(importlib.import_module(module), name)(**kwargs)


def close(obj) -> None:
    fn = getattr(obj, "close", None)
    if callable(fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


def open_close(store: str, url: str, root: Path) -> str:
    obj = build_store(store, url, root)
    close(obj)
    return type(obj).__name__


def attempt(fn) -> tuple[bool, str]:
    """(ok, message) of one call; the message is the error's text on failure."""
    try:
        result = fn()
        return True, "" if result is None else str(result)
    except BaseException as exc:  # noqa: BLE001 - SystemExit included
        return False, f"{type(exc).__name__}: {' '.join(str(exc).split())}"


def short(message: str, n: int = 600) -> str:
    return message if len(message) <= n else message[:n] + " ..."


# ---------------------------------------------------------------------------- children

def child(python: str, probe: Path, action: str, *args: str, root: Path, timeout: int = 900) -> dict:
    """Run one builder in another interpreter (the published 0.1.16 for layouts it writes)."""
    root.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([python, "-I", "-X", "utf8", str(probe), "--child", action, *args], cwd=root,
                          capture_output=True, text=True, timeout=timeout, env={**os.environ})
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("CHILD_RESULT "):
            return json.loads(line[len("CHILD_RESULT "):])
    return {"ok": False, "message": f"child exit {proc.returncode}: {proc.stderr[-800:]}"}
