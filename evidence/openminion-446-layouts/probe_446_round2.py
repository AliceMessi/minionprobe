"""#446 round 2: PostgreSQL layouts, a read-only detection prototype, relocation rehearsals, PostgreSQL facts.

This is evidence for a proposal, not the fix. It runs against the installed openminion and a PostgreSQL
server at OPENMINION_TEST_POSTGRES_URL (a superuser URL), and every case uses a new database.

Parts:
1. manifest: every owner (runtime storage, the bare storage engine, each of the 14 modules that
   `storage migrate` runs on PostgreSQL, and the memory / A2A audit / A2A state / session module stores)
   built alone in its own schema through a URL whose `options` sets search_path. Its tables and columns
   become the owner manifest the detection prototype uses.
2. layouts: each layout is built with the current code. Then the prototype classifies it for runtime,
   memory, a2a, session, artifact and registry inside one READ ONLY transaction, and a full snapshot
   (schemas, tables, columns, row counts, md5 of all rows, functions, triggers, ledgers) is compared
   before and after the classification.
3. claims: what A2A's ledger does to runtime storage's `idempotency_keys` (state store writes; downgrade).
4. relocation rehearsals: R1 (move a dedicated history intact), R1b (move memory's history out of a
   memory-then-runtime layout), R2 (park, recreate, copy) for runtime-then-A2A.
5. facts: search_path / options precedence, PGOPTIONS, current_schema(), identifier length, quoting,
   transactional and concurrent CREATE SCHEMA, permissions of a non-superuser role, and the visibility
   of another schema's alembic_version to MigrationRunner.detect().
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import text

BASE = os.environ["OPENMINION_TEST_POSTGRES_URL"]
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("probe_446_round2.json")
ROOT = Path(tempfile.mkdtemp(prefix="p446r2-"))
os.environ["OPENMINION_HOME"] = str(ROOT / "home")
os.environ.pop("PGOPTIONS", None)
RESULTS: dict = {"layouts": {}, "manifest": {}, "claims": {}, "relocation": {}, "facts": {}}
MODULES_UNDER_TEST = ["memory", "a2a", "session", "artifact", "registry"]
SAFE_NAME = re.compile(r"[A-Za-z0-9_]+")


def say(kind: str, case: str, msg: str) -> None:
    print(f"{kind} {case} | {msg}", flush=True)


def short_exc(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    loc = f" (at {Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    first = str(exc).splitlines()[0][:300] if str(exc) else ""
    return f"{type(exc).__name__}: {first}{loc}"


def attempt(fn) -> str:
    try:
        return "ok: " + str(fn())
    except Exception as exc:  # noqa: BLE001
        return "FAILED " + short_exc(exc)


def url_for(dbname: str, *, user: str | None = None, password: str | None = None) -> str:
    u = sa.engine.make_url(BASE).set(database=dbname)
    if user:
        u = u.set(username=user, password=password)
    return u.render_as_string(hide_password=False)


def new_db(tag: str) -> str:
    name = f"p446_{tag}_{uuid.uuid4().hex[:6]}"[:63]
    admin = sa.create_engine(BASE, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return url_for(name)


def with_options(url: str, options: str, **extra: str) -> str:
    query = {"options": options, **extra}
    return sa.engine.make_url(url).update_query_dict(query).render_as_string(hide_password=False)


def derived_url(url: str, schema: str) -> str:
    """Prototype of the proposed helper: keep every query parameter and every existing option token
    (or PGOPTIONS when the URL has none) and append the derived search_path last."""
    if not SAFE_NAME.fullmatch(schema):
        raise ValueError(f"unsafe schema name {schema!r}")
    if len(schema.encode("utf-8")) > 63:
        raise ValueError(f"schema name {schema!r} is longer than 63 bytes")
    u = sa.engine.make_url(url)
    query = dict(u.query)
    existing = query.get("options")
    if isinstance(existing, tuple):
        raise ValueError("repeated options parameter")
    if existing is None:
        existing = os.environ.get("PGOPTIONS", "")
    query["options"] = f'{existing} -c search_path="{schema}"'.strip()
    return u.set(query=query).render_as_string(hide_password=False)


def admin_exec(url: str, *statements: str) -> None:
    eng = sa.create_engine(url)
    with eng.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    eng.dispose()


def scalar(url: str, sql: str, **params):
    eng = sa.create_engine(url)
    try:
        with eng.connect() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        eng.dispose()


# ---------------------------------------------------------------- snapshots

SYSTEM_SCHEMA = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"


def snapshot(url: str) -> dict:
    eng = sa.create_engine(url)
    snap: dict = {}
    with eng.connect() as conn:
        schemas = [r[0] for r in conn.execute(text(
            f"SELECT n.nspname FROM pg_namespace n WHERE {SYSTEM_SCHEMA} ORDER BY 1"))]
        for schema in schemas:
            rels = conn.execute(text(
                "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :s AND c.relkind IN ('r','p','v','m','S','f') ORDER BY 1"), {"s": schema}).all()
            tables: dict = {}
            for name, kind in rels:
                if kind not in ("r", "p"):
                    continue
                cols = [r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = :s AND table_name = :t "
                    "ORDER BY ordinal_position"), {"s": schema, "t": name})]
                count, digest = conn.execute(text(
                    f"SELECT count(*), md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) "
                    f'FROM "{schema}"."{name}" x')).one()
                tables[name] = {"columns": cols, "rows": int(count), "md5": digest}
            ledgers: dict = {}
            if "alembic_version" in tables:
                ledgers["alembic_version"] = [r[0] for r in conn.execute(text(
                    f'SELECT version_num FROM "{schema}".alembic_version ORDER BY 1'))]
            if "om_meta" in tables:
                ledgers["om_meta"] = {r[0]: r[1] for r in conn.execute(text(
                    f'SELECT key, value FROM "{schema}".om_meta')) if r[0] in ("module_id", "schema_head")}
            if "migrations" in tables:
                ledgers["migrations"] = [f"{r[0]}:{r[1]}" for r in conn.execute(text(
                    f'SELECT version, name FROM "{schema}".migrations ORDER BY version'))]
            snap[schema] = {
                "tables": tables,
                "other_relations": {name: kind for name, kind in rels if kind not in ("r", "p")},
                "functions": sorted(r[0] for r in conn.execute(text(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = :s"), {"s": schema})),
                "indexes": sorted(f"{r[0]} on {r[1]}" for r in conn.execute(text(
                    "SELECT indexname, tablename FROM pg_indexes WHERE schemaname = :s"), {"s": schema})),
                "triggers": sorted(f"{r[0]} on {r[1]}" for r in conn.execute(text(
                    "SELECT t.tgname, cl.relname FROM pg_trigger t JOIN pg_class cl ON cl.oid = t.tgrelid "
                    "JOIN pg_namespace n ON n.oid = cl.relnamespace WHERE n.nspname = :s AND NOT t.tgisinternal"),
                    {"s": schema})),
                "ledgers": ledgers,
            }
    eng.dispose()
    return snap


def digest(snap: dict) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()


def compact(snap: dict) -> dict:
    return {
        schema: {
            "tables": {name: info["rows"] for name, info in body["tables"].items()},
            "ledgers": body["ledgers"],
            **({"functions": body["functions"]} if body["functions"] else {}),
        }
        for schema, body in snap.items()
        if body["tables"] or body["functions"]
    }


def snapshot_diff(before: dict, after: dict) -> list[str]:
    changes = []
    for schema in sorted(set(before) | set(after)):
        b, a = before.get(schema), after.get(schema)
        if b is None or a is None:
            changes.append(f"schema {schema} {'added' if b is None else 'removed'}")
            continue
        for name in sorted(set(b["tables"]) | set(a["tables"])):
            tb, ta = b["tables"].get(name), a["tables"].get(name)
            if tb != ta:
                if tb is None or ta is None:
                    changes.append(f"{schema}.{name} {'added' if tb is None else 'removed'}")
                else:
                    what = [k for k in ("columns", "rows", "md5") if tb[k] != ta[k]]
                    added = sorted(set(ta["columns"]) - set(tb["columns"]))
                    changes.append(f"{schema}.{name} changed {what}" + (f" +columns {added}" if added else ""))
        for key in ("functions", "triggers", "ledgers", "other_relations"):
            if b[key] != a[key]:
                changes.append(f"{schema} {key} changed")
        added = sorted(set(a["indexes"]) - set(b["indexes"]))
        removed = sorted(set(b["indexes"]) - set(a["indexes"]))
        if added or removed:
            changes.append(f"{schema} indexes +{added} -{removed}")
    return changes


# ---------------------------------------------------------------- owners

def storage_config(url: str, root: Path):
    from openminion.modules.storage.engine import StorageEngineConfig

    root.mkdir(parents=True, exist_ok=True)
    return StorageEngineConfig(
        root_dir=root, sqlite_path=root / "state.db", fallback_root=root,
        record_backend="record.postgres", record_backend_options={"url": url},
    )


def close_quietly(obj) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        close()


def build_runtime(url: str, tag: str) -> str:
    from openminion.modules.storage.runtime.context import build_runtime_storage

    ctx = build_runtime_storage(
        ROOT / tag / "state.db", env={}, record_backend="record.postgres",
        record_backend_options={"url": url},
    )
    try:
        return f"runtime version {ctx.migration_result.current_version}"
    finally:
        ctx.close()


def runtime_rows(url: str) -> str:
    admin_exec(
        url,
        "INSERT INTO sessions(id, session_key, channel, target) VALUES ('s-probe', 'k-probe', 'cli', 'probe')",
        "INSERT INTO idempotency_keys(method, idempotency_key) VALUES ('probe.method', 'idem-1')",
    )
    return "1 session row, 1 idempotency row"


def build_infra(url: str, tag: str) -> str:
    from openminion.modules.storage.engine import StorageEngine

    engine = StorageEngine.from_config(config=storage_config(url, ROOT / tag))
    close_quietly(engine)
    return "storage engine built"


def memory_store(url: str, tag: str, *, put: str | None = None, get: str | None = None) -> str:
    from openminion.modules.memory.models import MemoryRecord
    from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

    store = PostgresMemoryStore(sa.create_engine(url, future=True),
                                database_path=ROOT / tag / "memory" / "memory.db", owns_engine=True)
    try:
        notes = ["built"]
        if put:
            now = "2026-10-28T00:00:00+00:00"
            store.put(MemoryRecord(id=put, scope="agent:a", type="fact", title="t", content=f"c {put}",
                                   created_at=now, updated_at=now))
            notes.append(f"put {put}")
        if get:
            record = store.get(get)
            notes.append(f"get {get}: {'found' if record is not None else 'missing'}")
        return "; ".join(notes)
    finally:
        store.close()


def a2a_audit(url: str, tag: str, *, append: str | None = None, query: bool = False) -> str:
    from openminion.modules.a2a.models import AuditRecord
    from openminion.modules.a2a.storage.factory import build_a2a_audit_store

    root = ROOT / tag
    store = build_a2a_audit_store(config=storage_config(url, root), audit_root=root / "a2a-audit")
    try:
        notes = ["built"]
        if append:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            store.append_audit(AuditRecord(ts=now, msg_id=append, trace_id="t-probe", from_agent="a",
                                           to_agent="b", to_capability=None, type="request",
                                           method="probe.method", status="ok"))
            notes.append(f"appended {append}")
        if query:
            notes.append(f"query_audit -> {[r.msg_id for r in store.query_audit({})]}")
        return "; ".join(notes)
    finally:
        close_quietly(store)


def a2a_state(url: str, tag: str) -> str:
    from openminion.modules.a2a.storage.factory import build_a2a_state_store

    root = ROOT / tag
    store = build_a2a_state_store(config=storage_config(url, root), database_path=root / "a2a-state.db")
    try:
        reserved, _ = store.reserve_idempotency("probe-key", "probe-scope")
        return f"built; reserve_idempotency -> {reserved}"
    finally:
        close_quietly(store)


def session_store(url: str, tag: str) -> str:
    from openminion.modules.session.runtime.factory import build_module_session_store

    root = ROOT / tag
    store = build_module_session_store(config=storage_config(url, root), database_path=root / "sessions.db", env={})
    close_quietly(store)
    return "built"


def alembic_module(url: str, module_id: str, tag: str) -> str:
    from openminion.modules.storage.migrations.module_ids import get_module_application_id
    from openminion.modules.storage.migrations.runner import MigrationRunner

    eng = sa.create_engine(url)
    try:
        report = MigrationRunner(
            module_id=module_id, db_path=ROOT / tag / f"{module_id}.db",
            module_application_id=get_module_application_id(module_id), backend_type="postgres", engine=eng,
        ).migrate(target="head")
        error = report.error.splitlines()[0][:220] if report.error else None
        return (f"success={report.success} revision {report.before.alembic_revision} -> "
                f"{report.after.alembic_revision}; error={error}")
    finally:
        eng.dispose()


def cli_order() -> list[str]:
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
    from openminion.modules.storage.migrations.registry import POSTGRES_VALIDATED_MODULES

    return [m for m in MODULE_APPLICATION_IDS if m in POSTGRES_VALIDATED_MODULES]


def migrate_all(url: str, tag: str) -> str:
    return "; ".join(f"{m}: {attempt(lambda m=m: alembic_module(url, m, tag))[:120]}" for m in cli_order())


# ---------------------------------------------------------------- manifest and detection prototype

MANIFEST: dict[str, dict[str, set[str]]] = {}
INFRA: set[str] = set()
CHAINS: dict[str, set[str]] = {}
RUNTIME_VERSIONS: dict[int, str] = {}
RUNTIME_CREATES: dict[int, set[str]] = {}
LEDGER_TABLES = {"alembic_version", "om_meta"}


def module_chain(module_id: str) -> set[str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

    _, location = discover_module_alembic_paths(module_id)
    if location is None:
        return set()
    cfg = Config()
    cfg.set_main_option("script_location", str(location))
    return {script.revision for script in ScriptDirectory.from_config(cfg).walk_revisions()}


def build_manifest() -> None:
    from openminion.modules.storage.runtime.migrations import DEFAULT_MIGRATIONS

    for migration in DEFAULT_MIGRATIONS:
        RUNTIME_VERSIONS[migration.version] = migration.name
        RUNTIME_CREATES[migration.version] = {
            m.group(1) for statement in migration.statements
            for m in re.finditer(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"?(\w+)', statement, re.I)
        }
    url = new_db("manifest")
    builders = {
        "infra": lambda u: build_infra(u, "mf-infra"),
        "runtime": lambda u: build_runtime(u, "mf-runtime"),
        "memory_store": lambda u: memory_store(u, "mf-memory-store"),
        "a2a_audit": lambda u: a2a_audit(u, "mf-a2a-audit"),
        "a2a_state": lambda u: a2a_state(u, "mf-a2a-state"),
        "session_store": lambda u: session_store(u, "mf-session-store"),
    }
    for module_id in cli_order():
        builders[f"alembic_{module_id}"] = lambda u, m=module_id: alembic_module(u, m, f"mf-{m}")
    for owner, build in builders.items():
        schema = f"mf_{owner}"
        admin_exec(url, f'CREATE SCHEMA "{schema}"')
        result = attempt(lambda: build(derived_url(url, schema)))
        tables = snapshot(url)[schema]["tables"]
        RESULTS["manifest"][owner] = {"result": result, "tables": {n: t["columns"] for n, t in tables.items()}}
        say("MANIFEST", owner, f"{result} | tables {sorted(tables)}")
    INFRA.update(RESULTS["manifest"]["infra"]["tables"])
    merged: dict[str, dict[str, set[str]]] = {}
    for owner, body in RESULTS["manifest"].items():
        if owner == "infra":
            continue
        name = owner.replace("alembic_", "").replace("_store", "").replace("a2a_audit", "a2a").replace("a2a_state", "a2a")
        for table, cols in body["tables"].items():
            if table in LEDGER_TABLES or table in INFRA:
                continue
            merged.setdefault(name, {}).setdefault(table, set()).update(cols)
    MANIFEST.update(merged)
    for module_id in cli_order():
        CHAINS[module_id] = module_chain(module_id)
    shared = {}
    for owner, tables in MANIFEST.items():
        for table in tables:
            shared.setdefault(table, []).append(owner)
    RESULTS["manifest_shared_names"] = {t: sorted(o) for t, o in shared.items() if len(o) > 1}
    RESULTS["manifest_infra"] = sorted(INFRA)
    RESULTS["chains"] = {m: sorted(c) for m, c in CHAINS.items()}
    say("MANIFEST", "shared table names", json.dumps(RESULTS["manifest_shared_names"], sort_keys=True))
    say("MANIFEST", "storage engine infrastructure", str(sorted(INFRA)))


def read_schema(conn, schema: str) -> dict:
    tables: dict[str, set[str]] = {}
    for table, column in conn.execute(text(
        "SELECT c.table_name, c.column_name FROM information_schema.columns c "
        "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
        "WHERE c.table_schema = :s AND t.table_type = 'BASE TABLE'"), {"s": schema}):
        tables.setdefault(table, set()).add(column)
    layout = {"tables": tables, "alembic": [], "om_module": None, "migrations": []}
    if "alembic_version" in tables:
        layout["alembic"] = [r[0] for r in conn.execute(text(f'SELECT version_num FROM "{schema}".alembic_version'))]
    if "om_meta" in tables:
        layout["om_module"] = conn.execute(text(
            f"SELECT value FROM \"{schema}\".om_meta WHERE key = 'module_id'")).scalar()
    if "migrations" in tables:
        layout["migrations"] = [(int(r[0]), str(r[1])) for r in conn.execute(text(
            f'SELECT version, name FROM "{schema}".migrations'))]
    return layout


def attribute(layout: dict) -> dict[str, str]:
    owners = {}
    for table, cols in layout["tables"].items():
        if table in LEDGER_TABLES:
            owners[table] = "ledger"
        elif table in INFRA:
            owners[table] = "infra"
        elif table == "migrations":
            rows = layout["migrations"]
            if not rows:
                owners[table] = "neutral"
            elif all(RUNTIME_VERSIONS.get(v) == n for v, n in rows):
                owners[table] = "runtime"
            else:
                owners[table] = "unattributed"
        else:
            candidates = sorted(o for o, man in MANIFEST.items() if table in man and cols <= man[table])
            if len(candidates) > 1 and layout["om_module"] in candidates:
                candidates = [layout["om_module"]]
            if len(candidates) == 1:
                owners[table] = candidates[0]
            elif candidates:
                owners[table] = "ambiguous:" + "/".join(candidates)
            elif any(table in man for man in MANIFEST.values()):
                owners[table] = "unattributed"
            else:
                owners[table] = "foreign"
    return owners


NEUTRAL = {"ledger", "infra", "neutral", "foreign"}


def decide_runtime(base: str, layout: dict, perms: dict) -> str:
    rows = layout["migrations"]
    foreign_rows = [f"{v}:{n}" for v, n in rows if RUNTIME_VERSIONS.get(v) != n]
    if foreign_rows:
        return f"REJECT runtime ledger in {base} has rows that are not runtime versions: {foreign_rows}"
    applied = {v for v, _ in rows}
    pending = [v for v in sorted(RUNTIME_VERSIONS) if v not in applied]
    owners = attribute(layout)
    conflicts = sorted(f"{t}({owners.get(t)})" for v in pending for t in RUNTIME_CREATES[v] if t in layout["tables"])
    if conflicts:
        return f"REJECT pending runtime versions {pending[0]}..{pending[-1]} would create existing tables {conflicts}"
    if pending and not perms["base_create"]:
        return f"REJECT no CREATE privilege on schema {base}"
    if not pending:
        return "PROCEED as is (no pending runtime versions)"
    return f"PROCEED apply runtime versions {pending[0]}..{pending[-1]}"


def decide_module(module_id: str, base: str, base_layout: dict, derived: str, derived_layout: dict | None,
                  perms: dict) -> str:
    owners_b = attribute(base_layout)
    om_b = base_layout["om_module"]
    mine = sorted(t for t, o in owners_b.items() if o == module_id)
    others = sorted(f"{t}({o})" for t, o in owners_b.items() if o not in NEUTRAL and o != module_id)
    evidence = bool(mine) or om_b == module_id
    if derived_layout is not None:
        if evidence:
            return (f"REJECT both {base} (tables {mine}, om_meta {om_b}) and {derived} exist; "
                    "nothing selected")
        owners_d = attribute(derived_layout)
        foreign = sorted(f"{t}({o})" for t, o in owners_d.items() if o not in NEUTRAL and o != module_id)
        alembic_d = derived_layout["alembic"]
        if foreign or derived_layout["om_module"] not in (None, module_id) or len(alembic_d) > 1 or (
                alembic_d and alembic_d[0] not in CHAINS.get(module_id, set())):
            return f"REJECT {derived} holds objects that are not {module_id}'s: {foreign} om_meta={derived_layout['om_module']} alembic={alembic_d}"
        if not (perms["derived_usage"] and perms["derived_create"]):
            return f"REJECT no USAGE/CREATE privilege on schema {derived}"
        state = "empty (pre-created)" if not derived_layout["tables"] else f"history alembic={alembic_d} om_meta={derived_layout['om_module']}"
        return f"USE {derived} ({state})"
    if not evidence:
        if not SAFE_NAME.fullmatch(derived) or len(derived.encode()) > 63:
            return f"REJECT derived name {derived!r} is unsafe or longer than 63 bytes"
        if not perms["db_create"]:
            return f"REJECT no CREATE privilege on the database to create {derived}"
        return f"CREATE {derived} (no {module_id} objects in {base})"
    alembic_b = base_layout["alembic"]
    dedicated = (om_b == module_id and len(alembic_b) <= 1
                 and (not alembic_b or alembic_b[0] in CHAINS.get(module_id, set())) and not others)
    if dedicated:
        return f"USE {base} in place (dedicated {module_id} history alembic={alembic_b}, tables {len(mine)})"
    return (f"REJECT mixed layout in {base}: {module_id} tables {mine}, om_meta={om_b}, alembic={alembic_b}, "
            f"other owners {others}")


def classify(url: str) -> dict:
    decisions = {}
    eng = sa.create_engine(url)
    with eng.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        base = conn.execute(text("SELECT current_schema()")).scalar()
        if base is None:
            conn.rollback()
            eng.dispose()
            return {"*": "REJECT search_path names no existing schema"}
        base_layout = read_schema(conn, base)
        db_create = conn.execute(text("SELECT has_database_privilege(current_database(), 'CREATE')")).scalar()
        base_create = conn.execute(text("SELECT has_schema_privilege(:s, 'CREATE')"), {"s": base}).scalar()
        decisions["runtime"] = decide_runtime(base, base_layout, {"base_create": base_create})
        for module_id in MODULES_UNDER_TEST:
            derived = f"{base}__{module_id}"
            exists = conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :n"), {"n": derived}).scalar()
            derived_layout = read_schema(conn, derived) if exists else None
            perms = {"db_create": db_create, "derived_usage": True, "derived_create": True}
            if exists:
                perms["derived_usage"] = conn.execute(text("SELECT has_schema_privilege(:s, 'USAGE')"), {"s": derived}).scalar()
                perms["derived_create"] = conn.execute(text("SELECT has_schema_privilege(:s, 'CREATE')"), {"s": derived}).scalar()
            decisions[module_id] = decide_module(module_id, base, base_layout, derived, derived_layout, perms)
        conn.rollback()
    eng.dispose()
    return decisions


def record_layout(case: str, url: str, steps: list[tuple[str, object]]) -> dict:
    body: dict = {"steps": []}
    for name, fn in steps:
        before = snapshot(url)
        result = attempt(fn)
        after = snapshot(url)
        body["steps"].append({"step": name, "result": result, "changes": snapshot_diff(before, after)})
        say("STEP", case, f"{name}: {result}")
    snap = snapshot(url)
    body["state"] = snap
    say("STATE", case, json.dumps(compact(snap), sort_keys=True))
    before_digest = digest(snap)
    body["decisions"] = classify(url)
    after_digest = digest(snapshot(url))
    body["unchanged_by_detection"] = before_digest == after_digest
    for owner, decision in body["decisions"].items():
        say("DECIDE", case, f"{owner}: {decision}")
    say("UNCHANGED", case, f"detection read-only: {body['unchanged_by_detection']} (sha256 {before_digest[:16]})")
    RESULTS["layouts"][case] = body
    return body


# ---------------------------------------------------------------- parts 2-4

def layouts() -> None:
    url = new_db("fresh")
    record_layout("fresh", url, [])

    url = new_db("rt")
    record_layout("runtime_only", url, [("runtime storage", lambda: build_runtime(url, "l-rt")),
                                        ("runtime rows", lambda: runtime_rows(url))])

    url = new_db("memded")
    record_layout("memory_dedicated", url, [("memory store + record", lambda: memory_store(url, "l-md", put="rec-1"))])

    url = new_db("sesded")
    record_layout("session_dedicated_cli_all", url, [("storage migrate, every module in CLI order", lambda: migrate_all(url, "l-sd"))])

    url = new_db("a2aded")
    record_layout("a2a_dedicated", url, [("a2a audit store + record", lambda: a2a_audit(url, "l-ad", append="msg-1"))])

    url = new_db("rtmem")
    record_layout("runtime_then_memory", url, [("runtime storage", lambda: build_runtime(url, "l-rm")),
                                               ("runtime rows", lambda: runtime_rows(url)),
                                               ("memory store + record", lambda: memory_store(url, "l-rm", put="rec-1"))])

    url = new_db("memrt")
    record_layout("memory_then_runtime", url, [("memory store + record", lambda: memory_store(url, "l-mr", put="rec-1")),
                                               ("runtime storage", lambda: build_runtime(url, "l-mr"))])

    url = new_db("rta2a")
    record_layout("runtime_then_a2a", url, [("runtime storage", lambda: build_runtime(url, "l-ra")),
                                            ("runtime rows", lambda: runtime_rows(url)),
                                            ("a2a audit store + record", lambda: a2a_audit(url, "l-ra", append="msg-1")),
                                            ("a2a audit store reopened", lambda: a2a_audit(url, "l-ra", query=True))])

    url = new_db("a2art")
    record_layout("a2a_then_runtime", url, [("a2a audit store + record", lambda: a2a_audit(url, "l-ar", append="msg-1")),
                                            ("runtime storage", lambda: build_runtime(url, "l-ar"))])

    url = new_db("rtses")
    record_layout("runtime_then_session_store", url, [("runtime storage", lambda: build_runtime(url, "l-rs")),
                                                      ("runtime rows", lambda: runtime_rows(url)),
                                                      ("session module store (as api/queries/sessions.py builds it)",
                                                       lambda: session_store(url, "l-rs"))])

    url = new_db("artreg")
    record_layout("artifact_then_registry", url, [("migrate artifact", lambda: alembic_module(url, "artifact", "l-ag")),
                                                  ("migrate registry", lambda: alembic_module(url, "registry", "l-ag"))])

    url = new_db("rtall")
    record_layout("runtime_then_migrate_all", url, [("runtime storage", lambda: build_runtime(url, "l-rall")),
                                                    ("runtime rows", lambda: runtime_rows(url)),
                                                    ("storage migrate, every module", lambda: migrate_all(url, "l-rall"))])

    url = new_db("both")
    admin_exec(url, 'CREATE SCHEMA "public__memory"')
    record_layout("both_base_and_derived_memory", url, [
        ("memory store + record in public", lambda: memory_store(url, "l-b1", put="rec-base")),
        ("memory store + record in public__memory", lambda: memory_store(derived_url(url, "public__memory"), "l-b2", put="rec-derived"))])

    url = new_db("steady")
    for m in MODULES_UNDER_TEST:
        admin_exec(url, f'CREATE SCHEMA "public__{m}"')
    record_layout("derived_steady_state", url, [
        ("runtime storage in public", lambda: build_runtime(url, "l-s")),
        ("runtime rows", lambda: runtime_rows(url)),
        ("memory store + record in public__memory", lambda: memory_store(derived_url(url, "public__memory"), "l-s", put="rec-1")),
        ("a2a audit store + record in public__a2a", lambda: a2a_audit(derived_url(url, "public__a2a"), "l-s", append="msg-1")),
        ("a2a state store in public__a2a", lambda: a2a_state(derived_url(url, "public__a2a"), "l-s")),
        ("session module store in public__session", lambda: session_store(derived_url(url, "public__session"), "l-s")),
        ("migrate artifact in public__artifact", lambda: alembic_module(derived_url(url, "public__artifact"), "artifact", "l-s")),
        ("migrate registry in public__registry", lambda: alembic_module(derived_url(url, "public__registry"), "registry", "l-s")),
        ("runtime storage reopened", lambda: build_runtime(url, "l-s2")),
        ("memory store reopened in public__memory", lambda: memory_store(derived_url(url, "public__memory"), "l-s", get="rec-1"))])

    url = new_db("emptyder")
    admin_exec(url, 'CREATE SCHEMA "public__memory"')
    record_layout("empty_precreated_derived", url, [("runtime storage", lambda: build_runtime(url, "l-e"))])

    url = new_db("foreign")
    admin_exec(url, "CREATE TABLE public.alembic_version (version_num VARCHAR(32) PRIMARY KEY)",
               "INSERT INTO public.alembic_version VALUES ('othertool_0007')", "CREATE SCHEMA app")
    app_url = with_options(url, "-c search_path=app,public")

    def detect_memory():
        from openminion.modules.storage.migrations.module_ids import get_module_application_id
        from openminion.modules.storage.migrations.runner import MigrationRunner

        eng = sa.create_engine(app_url)
        try:
            state = MigrationRunner(module_id="memory", db_path=ROOT / "fa" / "m.db",
                                    module_application_id=get_module_application_id("memory"),
                                    backend_type="postgres", engine=eng).detect()
            return f"MigrationRunner.detect() alembic_revision={state.alembic_revision}"
        finally:
            eng.dispose()

    record_layout("foreign_alembic_in_public", app_url, [
        ("detect memory (search_path app,public)", detect_memory),
        ("memory store (search_path app,public)", lambda: memory_store(app_url, "l-fa"))])


def claims() -> None:
    url = new_db("claim")
    build_runtime(url, "c-r")
    runtime_rows(url)
    a2a_audit(url, "c-a", append="msg-1")
    before = snapshot(url)
    result = attempt(lambda: a2a_state(url, "c-s"))
    changes = snapshot_diff(before, snapshot(url))
    RESULTS["claims"]["a2a_state_store_on_runtime_then_a2a"] = {"result": result, "changes": changes}
    say("CLAIM", "a2a state store on runtime_then_a2a", f"{result} | changes {changes}")
    say("CLAIM", "idempotency_keys columns", str(before["public"]["tables"]["idempotency_keys"]["columns"]))

    def downgrade_a2a():
        from alembic import command
        from alembic.config import Config
        from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

        _, location = discover_module_alembic_paths("a2a")
        cfg = Config()
        cfg.set_main_option("script_location", str(location))
        eng = sa.create_engine(url)
        try:
            with eng.begin() as conn:
                cfg.attributes["connection"] = conn
                command.downgrade(cfg, "base")
            return "a2a downgraded to base"
        finally:
            eng.dispose()

    before = snapshot(url)
    result = attempt(downgrade_a2a)
    changes = snapshot_diff(before, snapshot(url))
    RESULTS["claims"]["a2a_downgrade_base_on_runtime_then_a2a"] = {"result": result, "changes": changes}
    say("CLAIM", "a2a alembic downgrade base on runtime_then_a2a (throwaway database)", f"{result} | changes {changes}")


def move_tables(url: str, target: str, tables: list[str], functions: list[str], *, create: bool = True) -> str:
    statements = [f'CREATE SCHEMA "{target}"'] if create else []
    statements += [f'ALTER TABLE public."{t}" SET SCHEMA "{target}"' for t in tables]
    statements += [f'ALTER FUNCTION public."{f}"() SET SCHEMA "{target}"' for f in functions]
    admin_exec(url, *statements)
    return f"moved {len(tables)} tables and {len(functions)} functions to {target} in one transaction"


def relocation() -> None:
    # R1: dedicated memory history moves intact; then runtime storage gets public.
    url = new_db("r1")
    memory_store(url, "r1", put="rec-1")
    snap = snapshot(url)["public"]
    ledger_before = snap["ledgers"]
    tables = sorted(snap["tables"])
    body = record_layout("R1_memory_dedicated_after_move", url, [
        ("move every table and function of public to public__memory", lambda: move_tables(url, "public__memory", tables, snap["functions"])),
        ("memory store opened in public__memory: get rec-1, put rec-2",
         lambda: memory_store(derived_url(url, "public__memory"), "r1", put="rec-2", get="rec-1")),
        ("runtime storage in public", lambda: build_runtime(url, "r1"))])
    body["memory_ledger_before"] = ledger_before
    RESULTS["relocation"]["R1"] = "R1_memory_dedicated_after_move"

    # R1b: memory-then-runtime layout; move memory's objects only, runtime keeps its ledger rows.
    url = new_db("r1b")
    memory_store(url, "r1b", put="rec-1")
    build_runtime_result = attempt(lambda: build_runtime(url, "r1b"))
    snap = snapshot(url)["public"]
    eng = sa.create_engine(url)
    with eng.connect() as conn:
        owners = attribute(read_schema(conn, "public"))
    eng.dispose()
    memory_tables = sorted(t for t, o in owners.items() if o == "memory") + ["alembic_version", "om_meta"]
    body = record_layout("R1b_memory_then_runtime_after_move", url, [
        ("move memory tables, alembic_version, om_meta and memory functions to public__memory",
         lambda: move_tables(url, "public__memory", memory_tables, snap["functions"])),
        ("runtime storage resumes in public", lambda: build_runtime(url, "r1b")),
        ("memory store opened in public__memory: get rec-1", lambda: memory_store(derived_url(url, "public__memory"), "r1b", get="rec-1"))])
    body["runtime_before_move"] = build_runtime_result
    body["moved"] = memory_tables
    RESULTS["relocation"]["R1b"] = "R1b_memory_then_runtime_after_move"

    # R2: runtime-then-A2A; park A2A's tables and the ledger, let A2A create public__a2a, copy rows.
    url = new_db("r2")
    build_runtime(url, "r2")
    runtime_rows(url)
    a2a_audit(url, "r2", append="msg-1")
    eng = sa.create_engine(url)
    with eng.connect() as conn:
        owners = attribute(read_schema(conn, "public"))
    eng.dispose()
    a2a_tables = sorted(t for t, o in owners.items() if o == "a2a")
    runtime_idem_before = snapshot(url)["public"]["tables"]["idempotency_keys"]

    def copy_rows() -> str:
        notes = []
        eng = sa.create_engine(url)
        with eng.begin() as conn:
            for table in a2a_tables:
                cols_hold = [r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = 'a2a_hold' AND table_name = :t "
                    "ORDER BY ordinal_position"), {"t": table})]
                cols_new = {r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public__a2a' AND table_name = :t"),
                    {"t": table})}
                common = ", ".join(f'"{c}"' for c in cols_hold if c in cols_new)
                moved = conn.execute(text(
                    f'INSERT INTO "public__a2a"."{table}" ({common}) SELECT {common} FROM a2a_hold."{table}"')).rowcount
                sequence = conn.execute(text("SELECT pg_get_serial_sequence(:t, 'id')"),
                                        {"t": f'"public__a2a"."{table}"'}).scalar() if "id" in cols_new else None
                if sequence and moved:
                    conn.execute(text(f'SELECT setval(:seq, (SELECT max(id) FROM "public__a2a"."{table}"))'), {"seq": sequence})
                notes.append(f"{table}: {moved}" + (f" (setval {sequence})" if sequence and moved else ""))
        eng.dispose()
        return "copied rows " + ", ".join(notes)

    body = record_layout("R2_runtime_then_a2a_after_relocation", url, [
        ("park A2A tables, alembic_version and om_meta in a2a_hold",
         lambda: move_tables(url, "a2a_hold", a2a_tables + ["alembic_version", "om_meta"], [])),
        ("create public__a2a (what the helper does)", lambda: admin_exec(url, 'CREATE SCHEMA "public__a2a"') or "created"),
        ("a2a audit store in public__a2a (fresh, head)", lambda: a2a_audit(derived_url(url, "public__a2a"), "r2b")),
        ("copy A2A rows from a2a_hold with explicit column lists", copy_rows),
        ("a2a audit store reopened in public__a2a: append msg-2, query",
         lambda: a2a_audit(derived_url(url, "public__a2a"), "r2b", append="msg-2", query=True)),
        ("a2a state store in public__a2a", lambda: a2a_state(derived_url(url, "public__a2a"), "r2b")),
        ("runtime storage reopened in public", lambda: build_runtime(url, "r2c"))])
    body["parked"] = a2a_tables + ["alembic_version", "om_meta"]
    body["runtime_idempotency_keys_unchanged"] = (
        snapshot(url)["public"]["tables"]["idempotency_keys"] == runtime_idem_before)
    say("RELOCATE", "R2", f"runtime idempotency_keys unchanged: {body['runtime_idempotency_keys_unchanged']}")
    RESULTS["relocation"]["R2"] = "R2_runtime_then_a2a_after_relocation"


def show(url: str, *names: str) -> dict:
    eng = sa.create_engine(url)
    try:
        with eng.connect() as conn:
            values = {n: conn.execute(text(f"SHOW {n}")).scalar() for n in names}
            values["current_schema"] = conn.execute(text("SELECT current_schema()")).scalar()
            return values
    finally:
        eng.dispose()


def facts() -> None:
    f = RESULTS["facts"]
    url = new_db("facts")
    dbname = sa.engine.make_url(url).database
    admin_exec(url, "CREATE SCHEMA a", "CREATE SCHEMA b", 'CREATE SCHEMA "public__memory"')
    f["server_version"] = scalar(url, "SHOW server_version")
    f["default"] = show(url, "search_path")
    f["repeated_search_path_last_wins"] = attempt(lambda: show(with_options(url, "-c search_path=a -c search_path=b"), "search_path"))
    base_with_opts = with_options(url, "-c statement_timeout=1234", application_name="probe446", connect_timeout="7")
    f["derived_url_rendered_query"] = sa.engine.make_url(derived_url(base_with_opts, "public__memory")).query
    f["derived_keeps_unrelated_options"] = attempt(lambda: show(derived_url(base_with_opts, "public__memory"),
                                                                 "search_path", "statement_timeout", "application_name"))
    os.environ["PGOPTIONS"] = "-c statement_timeout=4321"
    try:
        f["pgoptions_plain_url"] = attempt(lambda: show(url, "statement_timeout"))
        f["pgoptions_url_with_options_overrides_env"] = attempt(lambda: show(with_options(url, "-c search_path=a"), "statement_timeout"))
        f["pgoptions_derived_url_merges_env"] = attempt(lambda: show(derived_url(url, "public__memory"), "statement_timeout"))
    finally:
        os.environ.pop("PGOPTIONS", None)
    f["missing_schema_in_search_path"] = attempt(lambda: show(with_options(url, "-c search_path=missing_xyz"), "search_path"))
    f["missing_then_public"] = attempt(lambda: show(with_options(url, "-c search_path=missing_xyz,public"), "search_path"))
    admin_exec(url, 'CREATE SCHEMA "postgres"')
    f["schema_named_after_role"] = attempt(lambda: show(url, "search_path"))
    admin_exec(url, 'DROP SCHEMA "postgres"', "CREATE SCHEMA app")
    admin = sa.create_engine(BASE, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'ALTER DATABASE "{dbname}" SET search_path = app'))
    f["database_default_search_path"] = attempt(lambda: show(url, "search_path"))
    with admin.connect() as conn:
        conn.execute(text(f'ALTER DATABASE "{dbname}" RESET search_path'))
    admin_exec(url, 'CREATE SCHEMA "AppBase"', 'CREATE SCHEMA "AppBase__memory"')
    f["mixed_case_base"] = attempt(lambda: show(with_options(url, '-c search_path="AppBase"'), "search_path"))
    f["mixed_case_derived_quoted"] = attempt(lambda: show(derived_url(with_options(url, '-c search_path="AppBase"'), "AppBase__memory"), "search_path"))
    f["mixed_case_derived_unquoted"] = attempt(lambda: show(with_options(url, "-c search_path=AppBase__memory"), "search_path"))
    long64 = "a" * 64
    f["create_schema_64_bytes"] = attempt(lambda: admin_exec(url, f'CREATE SCHEMA "{long64}"') or "created")
    f["stored_name_bytes"] = scalar(url, "SELECT octet_length(nspname) FROM pg_namespace WHERE nspname LIKE 'aaaa%'")
    f["create_second_64_byte_name_same_prefix"] = attempt(lambda: admin_exec(url, f'CREATE SCHEMA "{"a" * 63}b"') or "created")
    f["search_path_65_bytes_resolves_to"] = attempt(lambda: show(with_options(url, f"-c search_path={'a' * 63}zz"), "search_path"))
    f["multibyte_create"] = attempt(lambda: admin_exec(url, 'CREATE SCHEMA "' + "é" * 32 + '"') or "created")
    f["multibyte_stored"] = attempt(lambda: scalar(url, "SELECT octet_length(nspname) || ' bytes, ' || char_length(nspname) || ' chars' "
                                                      "FROM pg_namespace WHERE nspname LIKE :p", p="éé%"))

    def tx_rollback():
        eng = sa.create_engine(url)
        with eng.connect() as conn:
            tx = conn.begin()
            conn.execute(text('CREATE SCHEMA "tx_probe"'))
            conn.execute(text('CREATE TABLE "tx_probe".t (x int)'))
            tx.rollback()
        eng.dispose()
        return f"schema exists after rollback: {bool(scalar(url, 'SELECT 1 FROM pg_namespace WHERE nspname = :n', n='tx_probe'))}"

    f["create_schema_rolls_back"] = attempt(tx_rollback)

    def race():
        eng = sa.create_engine(url)
        outcome = {}
        c1 = eng.connect()
        t1 = c1.begin()
        c1.execute(text('CREATE SCHEMA "race_probe"'))

        def second():
            try:
                with eng.connect() as c2:
                    with c2.begin():
                        c2.execute(text("SET LOCAL lock_timeout = '20s'"))
                        c2.execute(text('CREATE SCHEMA "race_probe"'))
                outcome["second"] = "created"
            except Exception as exc:  # noqa: BLE001
                outcome["second"] = short_exc(exc)

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(1.5)
        t1.commit()
        c1.close()
        thread.join()
        eng.dispose()
        return f"second CREATE SCHEMA after the first committed: {outcome['second']}"

    f["concurrent_create_schema"] = attempt(race)

    role = f"om_app_{uuid.uuid4().hex[:6]}"
    with admin.connect() as conn:
        conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'probe446'"))
    app = url_for(dbname, user=role, password="probe446")
    privs = ("SELECT current_user, has_database_privilege(current_database(), 'CREATE') AS db_create, "
             "has_schema_privilege('public', 'USAGE') AS public_usage, has_schema_privilege('public', 'CREATE') AS public_create")

    def privileges(u: str) -> dict:
        eng = sa.create_engine(u)
        try:
            with eng.connect() as conn:
                return dict(conn.execute(text(privs)).mappings().one())
        finally:
            eng.dispose()

    f["role_privileges"] = attempt(lambda: privileges(app))
    f["role_create_schema"] = attempt(lambda: admin_exec(app, 'CREATE SCHEMA "public__a2a"') or "created")
    f["role_runtime_storage_in_public"] = attempt(lambda: build_runtime(app, "perm-rt"))
    admin_exec(url, 'DROP SCHEMA "public__memory" CASCADE', f'CREATE SCHEMA "public__memory" AUTHORIZATION {role}')
    f["role_precreated_schema_privileges"] = attempt(lambda: scalar(
        app, "SELECT has_schema_privilege('public__memory', 'USAGE') AND has_schema_privilege('public__memory', 'CREATE')"))
    f["role_memory_store_in_precreated_schema"] = attempt(lambda: memory_store(derived_url(app, "public__memory"), "perm-m", put="rec-1"))
    admin_exec(url, "CREATE SCHEMA usage_only", f"GRANT USAGE ON SCHEMA usage_only TO {role}")
    f["role_usage_only_schema_create_privilege"] = attempt(lambda: scalar(app, "SELECT has_schema_privilege('usage_only', 'CREATE')"))
    f["role_create_table_in_usage_only_schema"] = attempt(lambda: admin_exec(app, "CREATE TABLE usage_only.t (x int)") or "created")
    with admin.connect() as conn:
        conn.execute(text(f'GRANT CREATE ON DATABASE "{dbname}" TO {role}'))
    f["role_after_grant_db_create"] = attempt(lambda: privileges(app))
    f["role_create_schema_after_grant"] = attempt(lambda: admin_exec(app, 'CREATE SCHEMA "public__a2a"') or "created")
    admin.dispose()
    for key, value in f.items():
        say("FACT", key, json.dumps(value, default=str))


def main() -> int:
    import importlib.metadata as md

    RESULTS["versions"] = {p: md.version(p) for p in ("openminion", "sqlalchemy", "psycopg", "alembic")}
    say("VERSIONS", "packages", json.dumps(RESULTS["versions"]))
    for part in (build_manifest, layouts, claims, relocation, facts):
        try:
            part()
        except Exception as exc:  # noqa: BLE001
            say("ERROR", part.__name__, short_exc(exc))
            RESULTS.setdefault("errors", {})[part.__name__] = traceback.format_exc()
    OUT.write_text(json.dumps(RESULTS, indent=1, sort_keys=True, default=lambda o: sorted(o) if isinstance(o, set) else str(o)),
                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
