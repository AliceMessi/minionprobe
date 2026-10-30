"""#493 on the installed package: `storage migrate --module compress` (PR #519).

    python -I -X utf8 probe_493.py <work_dir> <out.json> --backend sqlite|postgres

Run from an empty directory outside any checkout. The CLI is the installed
`openminion` console script next to this interpreter, run as a subprocess from
the current (empty) directory with OPENMINION_HOME / OPENMINION_DATA_ROOT under
<work_dir>. Every SQLite case uses a new file in a new directory; every
PostgreSQL case a new database (PGURL / OPENMINION_TEST_POSTGRES_URL is the
server URL), so compress is the only module in its schema: the shared-schema
migration of #446 is not exercised.

Checklist (openminion#493, comment 6490537757):
  item 1: fresh SQLite file: `storage migrate --sqlite <file> --module compress`
          succeeds, creates the compress tables and the module metadata, reaches
          0001_baseline, and reruns successfully.            (--backend sqlite)
  item 2: fresh isolated PostgreSQL schema: the same, with the correct tables,
          module metadata and version; the rerun is clean. A new database (its
          `public` schema) and a named schema selected through the URL's
          search_path, each holding only compress.         (--backend postgres)
  item 3: compress's own migration entry point (`run_migrations`) and ordinary
          module-discovery controls (both backends; the CLI controls run on the
          backend selected).
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
import traceback
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, quiet, sanitize, write_result  # noqa: E402

AP = argparse.ArgumentParser()
AP.add_argument("work")
AP.add_argument("out")
AP.add_argument("--backend", choices=("sqlite", "postgres"), required=True)
ARGS = AP.parse_args()
WORK = Path(ARGS.work).resolve()
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
OUT = Path(ARGS.out)
PGURL = os.environ.get("PGURL") or os.environ.get("OPENMINION_TEST_POSTGRES_URL") or ""

import openminion  # noqa: E402

VERSION = getattr(openminion, "__version__", "x")
TAG = "v" + VERSION.replace(".", "_")
PKG = Path(openminion.__file__).resolve().parent
BIN = Path(sys.executable).parent
OM = shutil.which("openminion", path=str(BIN)) or str(BIN / "openminion")

CHECKS = Checks()
CASES: dict = {}

# The compress baseline's DDL in the package (0001_baseline.py, DDL / POSTGRES_DDL).
COMPRESS_TABLES = ["compression_failures", "compression_runs", "dropped_reasons", "om_meta"]
SQLITE_COLUMNS = {
    "compression_runs": [
        ("run_id", "TEXT"), ("request_id", "TEXT"), ("method_id", "TEXT"), ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"), ("ratio", "REAL"), ("compression_hash", "TEXT"),
        ("empty_augmentation", "INTEGER"), ("fallback_used", "INTEGER"), ("policy_hash", "TEXT"),
        ("input_hash", "TEXT"), ("output_hash", "TEXT"), ("engine_version", "TEXT"), ("tokenizer_id", "TEXT"),
        ("scorer_version", "TEXT"), ("warnings", "TEXT"),
    ],
    "dropped_reasons": [("id", "INTEGER"), ("run_id", "TEXT"), ("reason", "TEXT"), ("count", "INTEGER")],
    "compression_failures": [("failure_id", "TEXT"), ("request_id", "TEXT"), ("error_code", "TEXT"), ("message", "TEXT")],
    "om_meta": [("key", "TEXT"), ("value", "TEXT")],
}
PG_COLUMNS = {
    table: [(name, {"TEXT": "text", "INTEGER": "integer", "REAL": "double precision"}[kind]) for name, kind in cols]
    for table, cols in SQLITE_COLUMNS.items()
}
BASELINE = "0001_baseline"


def _mask_prefixes() -> list[tuple[str, str]]:
    import tempfile

    pairs = []
    for raw, token in ((str(WORK), "<CASE_WORK>"), (sys.prefix, "<VENV>"), (os.environ.get("VERIFY_WORK"), "<WORK>"),
                       (os.environ.get("RUNNER_TEMP"), "<RUNNER_TEMP>"), (tempfile.gettempdir(), "<TMP>"),
                       (str(Path.home()), "<USER_HOME>")):
        if raw:
            pairs.append((raw, token))
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


MASK_PREFIXES = _mask_prefixes()


def mask(value):
    """Mask URL passwords and local path prefixes, also inside repr()/JSON-escaped text
    (Windows paths appear with 1, 2, 4 or 8 backslashes per separator)."""
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    text = re.sub(r"(postgresql(\+[a-z0-9]+)?://[^:/@\s\"]+):[^@\s\"]*@", r"\1:***@", text)
    for raw, token in MASK_PREFIXES:
        for variant in {raw.replace("\\", "/")} | {raw.replace("\\", "\\" * n) for n in (8, 4, 2, 1)}:
            text = re.sub(re.escape(variant), lambda _m, t=token: t, text, flags=re.IGNORECASE)
    return text if isinstance(value, str) else json.loads(text)


def say(line: str) -> None:
    print(sanitize(mask(line)), flush=True)


def check(name: str, ok: bool, observed=None, expected=None) -> bool:
    return CHECKS.check(name, ok, mask(observed) if observed is not None else None,
                        mask(expected) if expected is not None else None)


def env() -> dict:
    e = dict(os.environ)
    for key in ("OPENMINION_CONFIG_PATH", "OPENMINION_GENERATED_ROOT", "OPENMINION_STORAGE_BACKEND",
                "OPENMINION_STORAGE_POSTGRES_URL"):
        e.pop(key, None)
    e["OPENMINION_HOME"] = str(WORK / "home")
    e["OPENMINION_DATA_ROOT"] = str(WORK / "data")
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def cli(*args: str) -> dict:
    argv = [OM, *args]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env(),
                          cwd=os.getcwd(), timeout=600)
    entries = None
    if "--json" in args:
        try:
            entries = json.loads(proc.stdout)["modules"]
        except Exception:  # noqa: BLE001
            entries = None
    shown = "openminion " + " ".join(args)
    step = {"argv": shown, "exit": proc.returncode, "entries": entries,
            "stdout_tail": proc.stdout[-1500:], "stderr_tail": proc.stderr[-1500:]}
    step = mask(step)
    say(f"$ {step['argv']}")
    say(f"  exit={proc.returncode} " + (f"entries={json.dumps(step['entries'])}" if entries is not None
                                         else f"stdout={step['stdout_tail'].strip()!r}"))
    if proc.returncode != 0 and proc.stderr.strip():
        say(f"  stderr tail: {step['stderr_tail'].strip()[-400:]!r}")
    return step


def entry_ok(step: dict, module_id: str) -> bool:
    entries = step.get("entries") or []
    return (step["exit"] == 0 and len(entries) == 1 and entries[0].get("module_id") == module_id
            and entries[0].get("status") == "ok" and entries[0].get("success") is True and "error" not in entries[0])


def plan_revision(step: dict):
    entries = step.get("entries") or []
    return entries[0].get("current_revision") if entries else None


# ---------------------------------------------------------------- SQLite facts
def sqlite_facts(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        objects = sorted(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex%'"))
        tables = sorted(r[1] for r in objects if r[0] == "table")
        alembic = [r[0] for r in conn.execute("SELECT version_num FROM alembic_version")] if "alembic_version" in tables else None
        meta = dict(conn.execute("SELECT key, value FROM om_meta")) if "om_meta" in tables else None
        columns = {t: [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{t}")')] for t in tables}
        fks = {t: [tuple(r[2:5]) for r in conn.execute(f'PRAGMA foreign_key_list("{t}")')] for t in tables}
        counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    signature = hashlib.sha256(json.dumps(objects).encode()).hexdigest()
    return {"tables": tables, "alembic_version": alembic, "om_meta": meta, "columns": columns, "foreign_keys": fks,
            "row_counts": counts, "application_id": hex(app_id), "user_version": user_version,
            "schema_sha256": signature}


def sqlite_insert_probe_rows(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO compression_runs VALUES ('probe-run-1','probe-req-1','probe-method',10,5,0.5,'ch',0,0,"
            "'ph','ih','oh','ev','tok','sv','')"
        )
        conn.execute("INSERT INTO dropped_reasons(run_id, reason, count) VALUES ('probe-run-1','probe',2)")
        conn.commit()
    finally:
        conn.close()


def new_sqlite(name: str, *, touch: bool = True) -> Path:
    path = WORK / "sqlite" / name / f"{name}.db"
    path.parent.mkdir(parents=True)
    if touch:
        sqlite3.connect(path).close()
    return path


# ---------------------------------------------------------------- PostgreSQL
def pg_url(dbname: str, schema: str | None = None) -> str:
    base = urlsplit(PGURL)
    query = f"options={quote('-csearch_path=' + schema)}" if schema else ""
    return urlunsplit(base._replace(path="/" + dbname, query=query))


def admin_engine():
    import sqlalchemy as sa

    return sa.create_engine(pg_url("postgres"), isolation_level="AUTOCOMMIT")


def new_db(name: str) -> str:
    import sqlalchemy as sa

    dbname = f"p493_{TAG}_{name}".lower()
    engine = admin_engine()
    with engine.connect() as conn:
        conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
        conn.execute(sa.text(f'CREATE DATABASE "{dbname}"'))
    engine.dispose()
    return dbname


def pg_facts(dbname: str, schema: str = "public") -> dict:
    import sqlalchemy as sa

    engine = sa.create_engine(pg_url(dbname))
    try:
        with engine.connect() as conn:
            q = lambda sql, **kw: conn.execute(sa.text(sql), kw).all()  # noqa: E731
            per_schema = {r[0]: r[1] for r in q(
                "SELECT table_schema, count(*) FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema') GROUP BY 1 ORDER BY 1")}
            tables = [r[0] for r in q("SELECT table_name FROM information_schema.tables WHERE table_schema=:s ORDER BY 1", s=schema)]
            cols = q(
                "SELECT table_name, column_name, data_type, is_nullable, column_default, is_identity, identity_generation "
                "FROM information_schema.columns WHERE table_schema=:s ORDER BY table_name, ordinal_position", s=schema)
            columns: dict = {}
            for t, c, dt, *_ in cols:
                columns.setdefault(t, []).append((c, dt))
            identity = {f"{t}.{c}": gen for t, c, _dt, _n, _d, ident, gen in cols if ident == "YES"}
            constraints = [tuple(r) for r in q(
                "SELECT tc.table_name, tc.constraint_type, kcu.column_name, ccu.table_name AS ref_table, ccu.column_name AS ref_col "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema "
                "LEFT JOIN information_schema.constraint_column_usage ccu ON tc.constraint_type='FOREIGN KEY' "
                "  AND tc.constraint_name=ccu.constraint_name AND tc.table_schema=ccu.table_schema "
                "WHERE tc.table_schema=:s ORDER BY 1,2,3", s=schema)]
            alembic = [r[0] for r in q(f'SELECT version_num FROM "{schema}".alembic_version')] if "alembic_version" in tables else None
            meta = {r[0]: r[1] for r in q(f'SELECT key, value FROM "{schema}".om_meta')} if "om_meta" in tables else None
            counts = {t: q(f'SELECT count(*) FROM "{schema}"."{t}"')[0][0] for t in tables}
            others = q("SELECT count(*) FROM pg_stat_activity WHERE datname=:d AND pid <> pg_backend_pid()", d=dbname)[0][0]
    finally:
        engine.dispose()
    signature = hashlib.sha256(json.dumps([cols, constraints], default=str).encode()).hexdigest()
    return {"tables_per_schema": per_schema, "schema": schema, "tables": tables,
            "columns": {t: [list(x) for x in v] for t, v in columns.items()}, "identity_columns": identity,
            "constraints": [list(c) for c in constraints], "alembic_version": alembic, "om_meta": meta,
            "row_counts": counts, "other_connections_to_db": others, "schema_sha256": signature}


def pg_insert_probe_rows(dbname: str, schema: str) -> None:
    import sqlalchemy as sa

    engine = sa.create_engine(pg_url(dbname))
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(
                f'INSERT INTO "{schema}".compression_runs VALUES (\'probe-run-1\',\'probe-req-1\',\'probe-method\',10,5,0.5,'
                "'ch',0,0,'ph','ih','oh','ev','tok','sv','')"))
            conn.execute(sa.text(f'INSERT INTO "{schema}".dropped_reasons(run_id, reason, count) VALUES (\'probe-run-1\',\'probe\',2)'))
    finally:
        engine.dispose()


# ---------------------------------------------------------------- shared assertions
def assert_compress_fresh(item: str, facts: dict, *, postgres: bool) -> None:
    expected_tables = sorted(COMPRESS_TABLES + ["alembic_version"] + ([] if postgres else ["sqlite_sequence"]))
    check(f"{item}: the compress tables exist, and only they (with alembic_version{'' if postgres else ' and sqlite_sequence'})",
          facts["tables"] == expected_tables, facts["tables"], expected_tables)
    expected_cols = PG_COLUMNS if postgres else SQLITE_COLUMNS
    got = {t: [tuple(c) for c in facts["columns"].get(t, [])] for t in expected_cols}
    check(f"{item}: each compress table has the baseline's columns and types",
          got == expected_cols, {t: [list(c) for c in v] for t, v in got.items()})
    if postgres:
        check(f"{item}: dropped_reasons.id is an identity column (GENERATED ALWAYS), as the PostgreSQL DDL declares",
              facts["identity_columns"] == {"dropped_reasons.id": "ALWAYS"}, facts["identity_columns"])
        fk = [c for c in facts["constraints"] if c[1] == "FOREIGN KEY"]
        check(f"{item}: dropped_reasons.run_id references compression_runs.run_id",
              fk == [["dropped_reasons", "FOREIGN KEY", "run_id", "compression_runs", "run_id"]], fk)
    else:
        check(f"{item}: dropped_reasons.run_id references compression_runs.run_id",
              facts["foreign_keys"].get("dropped_reasons") == [("compression_runs", "run_id", "run_id")]
              or facts["foreign_keys"].get("dropped_reasons") == [["compression_runs", "run_id", "run_id"]],
              facts["foreign_keys"].get("dropped_reasons"))
    check(f"{item}: alembic_version is 0001_baseline", facts["alembic_version"] == [BASELINE], facts["alembic_version"])
    meta = facts["om_meta"] or {}
    check(f"{item}: module metadata (om_meta) names compress at 0001_baseline, with last_migrated_at",
          meta.get("module_id") == "compress" and meta.get("schema_head") == BASELINE and bool(meta.get("last_migrated_at")),
          meta)
    if not postgres:
        from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS

        check(f"{item}: PRAGMA application_id is compress's module id (MODULE_APPLICATION_IDS['compress'])",
              facts["application_id"] == hex(MODULE_APPLICATION_IDS["compress"]), facts["application_id"],
              hex(MODULE_APPLICATION_IDS["compress"]))


def assert_rerun(item: str, step: dict, text_step: dict, before: dict, after: dict) -> None:
    check(f"{item}: rerun (--json) exits 0 with the compress entry ok", entry_ok(step, "compress"),
          {"exit": step["exit"], "entries": step["entries"]})
    check(f"{item}: rerun in text mode exits 0 and prints `ok  compress`",
          text_step["exit"] == 0 and "ok  compress" in text_step["stdout_tail"],
          {"exit": text_step["exit"], "stdout": text_step["stdout_tail"].strip()})
    check(f"{item}: after both reruns the schema is unchanged (same objects and columns)",
          before["schema_sha256"] == after["schema_sha256"] and before["tables"] == after["tables"],
          {"before": before["schema_sha256"], "after": after["schema_sha256"], "tables": after["tables"]})
    check(f"{item}: after both reruns alembic_version is still 0001_baseline",
          after["alembic_version"] == [BASELINE], after["alembic_version"])
    bm, am = before["om_meta"] or {}, after["om_meta"] or {}
    check(f"{item}: after both reruns om_meta still names compress at 0001_baseline",
          am.get("module_id") == "compress" and am.get("schema_head") == BASELINE and set(am) == set(bm),
          {"before": bm, "after": am})
    check(f"{item}: the rows the probe inserted before the reruns are kept (1 compression_runs, 1 dropped_reasons)",
          after["row_counts"].get("compression_runs") == 1 and after["row_counts"].get("dropped_reasons") == 1,
          after["row_counts"])


# ---------------------------------------------------------------- item 1
def case_item1_sqlite() -> None:
    item = "item 1"
    db = new_sqlite("compress_cli")
    say(f"CASE {item}: new SQLite file <CASE_WORK>/sqlite/compress_cli/compress_cli.db")
    before_any = sqlite_facts(db)
    plan0 = cli("storage", "migrate", "--sqlite", str(db), "--module", "compress", "--plan", "--json")
    check(f"{item}: `--plan` before the migration shows compress pending (no revision)",
          plan0["exit"] == 0 and plan_revision(plan0) is None and (plan0["entries"] or [{}])[0].get("pending") is True,
          {"exit": plan0["exit"], "entries": plan0["entries"]})
    first = cli("storage", "migrate", "--sqlite", str(db), "--module", "compress", "--json")
    after1 = sqlite_facts(db)
    check(f"{item}: `storage migrate --sqlite <new file> --module compress --json` exits 0 with the compress entry ok",
          entry_ok(first, "compress"), {"exit": first["exit"], "entries": first["entries"]})
    assert_compress_fresh(item, after1, postgres=False)
    plan1 = cli("storage", "migrate", "--sqlite", str(db), "--module", "compress", "--plan", "--json")
    check(f"{item}: `--plan` after the migration shows compress at 0001_baseline, not pending",
          plan1["exit"] == 0 and plan_revision(plan1) == BASELINE and (plan1["entries"] or [{}])[0].get("pending") is False,
          {"exit": plan1["exit"], "entries": plan1["entries"]})
    verify = cli("storage", "verify", "--sqlite", str(db), "--module", "compress", "--json")
    ventries = verify["entries"] or [{}]
    check(f"{item}: `storage verify --sqlite <file> --module compress` passes",
          verify["exit"] == 0 and ventries[0].get("status") == "passed",
          {"exit": verify["exit"], "entries": verify["entries"]})
    if first["exit"] != 0:
        CASES["item 1 failed run"] = {"file_unchanged": after1["schema_sha256"] == before_any["schema_sha256"]
                                      and after1["tables"] == before_any["tables"], "tables_after": after1["tables"],
                                      "application_id_after": after1["application_id"]}
        say(f"  the failed run left the file unchanged: {CASES['item 1 failed run']}")
    if "compression_runs" in after1["tables"]:
        sqlite_insert_probe_rows(db)
    else:
        say("  probe rows not inserted: the compress tables do not exist")
    before_rerun = sqlite_facts(db)
    rerun = cli("storage", "migrate", "--sqlite", str(db), "--module", "compress", "--json")
    rerun_text = cli("storage", "migrate", "--sqlite", str(db), "--module", "compress")
    after2 = sqlite_facts(db)
    assert_rerun(item, rerun, rerun_text, before_rerun, after2)
    files = sorted(str(p.relative_to(db.parent)).replace("\\", "/") for p in db.parent.rglob("*"))
    CASES[item] = {"file_before": before_any, "plan_before": plan0, "migrate": first, "after_migrate": after1,
                   "plan_after": plan1, "verify": verify, "before_rerun": before_rerun, "rerun_json": rerun,
                   "rerun_text": rerun_text, "after_reruns": after2, "files_in_db_dir": files}
    say(f"  after: tables={after2['tables']} alembic={after2['alembic_version']} om_meta={after2['om_meta']} "
        f"application_id={after2['application_id']} rows={after2['row_counts']}")


# ---------------------------------------------------------------- item 2
def case_item2_postgres(label: str, schema: str | None) -> None:
    item = "item 2"
    dbname = new_db(label)
    target_schema = schema or "public"
    if schema:
        import sqlalchemy as sa

        engine = sa.create_engine(pg_url(dbname))
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        engine.dispose()
    url = pg_url(dbname, schema)
    where = f"new database {dbname}, schema {target_schema}" + (" (selected by the URL's search_path)" if schema else "")
    say(f"CASE {item} [{label}]: {where}")
    sub = f"{item} [{target_schema} schema of a new database]"
    plan0 = cli("storage", "migrate", "--backend", "postgres", "--postgres-url", url, "--module", "compress", "--plan", "--json")
    check(f"{sub}: `--plan` before the migration shows compress pending (no revision)",
          plan0["exit"] == 0 and plan_revision(plan0) is None, {"exit": plan0["exit"], "entries": plan0["entries"]})
    first = cli("storage", "migrate", "--backend", "postgres", "--postgres-url", url, "--module", "compress", "--json")
    after1 = pg_facts(dbname, target_schema)
    check(f"{sub}: `storage migrate --backend postgres --postgres-url <url> --module compress --json` exits 0 with the compress entry ok",
          entry_ok(first, "compress"), {"exit": first["exit"], "entries": first["entries"]})
    check(f"{sub}: the migration wrote only to the {target_schema} schema (tables per schema)",
          after1["tables_per_schema"] == {target_schema: 5}, after1["tables_per_schema"], {target_schema: 5})
    assert_compress_fresh(sub, after1, postgres=True)
    check(f"{sub}: no connection to the database is left after the CLI exits",
          after1["other_connections_to_db"] == 0, after1["other_connections_to_db"])
    plan1 = cli("storage", "migrate", "--backend", "postgres", "--postgres-url", url, "--module", "compress", "--plan", "--json")
    check(f"{sub}: `--plan` after the migration shows compress at 0001_baseline",
          plan1["exit"] == 0 and plan_revision(plan1) == BASELINE, {"exit": plan1["exit"], "entries": plan1["entries"]})
    verify = cli("storage", "verify", "--backend", "postgres", "--postgres-url", url, "--module", "compress", "--json")
    if first["exit"] != 0:
        CASES[f"{item} [{label}] failed run"] = {"tables_per_schema_after": after1["tables_per_schema"]}
        say(f"  the failed run left the database without tables: {after1['tables_per_schema'] == {}}")
    if "compression_runs" in after1["tables"]:
        pg_insert_probe_rows(dbname, target_schema)
    else:
        say("  probe rows not inserted: the compress tables do not exist")
    before_rerun = pg_facts(dbname, target_schema)
    rerun = cli("storage", "migrate", "--backend", "postgres", "--postgres-url", url, "--module", "compress", "--json")
    rerun_text = cli("storage", "migrate", "--backend", "postgres", "--postgres-url", url, "--module", "compress")
    after2 = pg_facts(dbname, target_schema)
    assert_rerun(sub, rerun, rerun_text, before_rerun, after2)
    check(f"{sub}: after the reruns the database still holds only the {target_schema} schema's 5 tables",
          after2["tables_per_schema"] == {target_schema: 5}, after2["tables_per_schema"])
    CASES[f"{item} [{label}]"] = {"database": dbname, "schema": target_schema, "url": url, "plan_before": plan0,
                                  "migrate": first, "after_migrate": after1, "plan_after": plan1,
                                  "verify_observed": verify, "before_rerun": before_rerun, "rerun_json": rerun,
                                  "rerun_text": rerun_text, "after_reruns": after2}
    say(f"  after: tables={after2['tables']} alembic={after2['alembic_version']} om_meta={after2['om_meta']} "
        f"identity={after2['identity_columns']} rows={after2['row_counts']} per_schema={after2['tables_per_schema']}")
    say(f"  observed only: storage verify --backend postgres --module compress: exit={verify['exit']} entries={verify['entries']}")


# ---------------------------------------------------------------- item 3
def case_item3_entry_point() -> None:
    item = "item 3"
    from openminion.modules.context.compress.storage import migrations as cm
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS

    db = new_sqlite("compress_own", touch=False)
    say(f"CASE {item}: openminion.modules.context.compress.storage.migrations.run_migrations(<new file>)")
    results = []
    facts = []
    for _ in range(2):
        try:
            cm.run_migrations(db)
            results.append("returned")
        except Exception as exc:  # noqa: BLE001
            results.append(f"{type(exc).__name__}: {exc}")
        facts.append(sqlite_facts(db) if db.exists() else {"exists": False})
    say(f"  results={results}")
    check(f"{item}: compress's own run_migrations(<new file>) returns, twice",
          results == ["returned", "returned"], results)
    f1, f2 = facts
    ok1 = (isinstance(f1.get("tables"), list) and f1.get("alembic_version") == [BASELINE]
           and (f1.get("om_meta") or {}).get("module_id") == "compress"
           and f1.get("application_id") == hex(MODULE_APPLICATION_IDS["compress"])
           and f1.get("user_version") == cm.TARGET_USER_VERSION
           and all(t in f1.get("tables", []) for t in COMPRESS_TABLES))
    check(f"{item}: after run_migrations the file has the compress tables, 0001_baseline, om_meta module_id compress, "
          "application_id 0x4f4d000e and user_version TARGET_USER_VERSION",
          ok1, {k: f1.get(k) for k in ("tables", "alembic_version", "om_meta", "application_id", "user_version")})
    check(f"{item}: the second run_migrations leaves the schema and revision unchanged",
          f1.get("schema_sha256") == f2.get("schema_sha256") and f2.get("alembic_version") == [BASELINE],
          {"first": f1.get("schema_sha256"), "second": f2.get("schema_sha256"), "alembic": f2.get("alembic_version")})
    check(f"{item}: compress's list_migrations() and MODULE_ID",
          cm.list_migrations() == [BASELINE] and cm.MODULE_ID == "compress",
          {"list_migrations": cm.list_migrations(), "MODULE_ID": cm.MODULE_ID})
    CASES[f"{item} entry point"] = {"results": results, "after_first": f1, "after_second": f2}


def case_item3_discovery() -> None:
    import importlib

    item = "item 3"
    from openminion.modules.storage.migrations import alembic as om_alembic
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS

    rel = lambda p: str(Path(p).resolve().relative_to(PKG.parent)).replace("\\", "/") if p else None  # noqa: E731
    by_module = {}
    for module_id in MODULE_APPLICATION_IDS:
        root = om_alembic.discover_module_storage_root(module_id)
        ini, script = om_alembic.discover_module_alembic_paths(module_id)
        by_module[module_id] = {"storage_root": rel(root), "alembic_ini": rel(ini), "script_location": rel(script)}
    packages = []
    for env_py in sorted(PKG.rglob("migrations/env.py")):
        storage_dir = env_py.parent.parent
        dotted = ".".join(storage_dir.relative_to(PKG.parent).parts)
        try:
            declared = getattr(importlib.import_module(dotted + ".migrations"), "MODULE_ID", None)
        except Exception as exc:  # noqa: BLE001
            declared = f"<import failed: {exc}>"
        root = om_alembic.discover_module_storage_root(str(declared))
        packages.append({"package": dotted, "module_id": declared,
                         "discovered": root is not None and Path(root).resolve() == storage_dir.resolve()})
    for p in packages:
        say(f"  package {p['package']} MODULE_ID={p['module_id']!r} found_by_discovery={p['discovered']}")
    comp = by_module["compress"]
    alembic_py = Path(om_alembic.__file__).resolve()
    nested = dict(getattr(om_alembic, "_NESTED_MODULE_STORAGE_IMPORT_PATHS", {}))
    say(f"  installed {rel(alembic_py)} sha256={hashlib.sha256(alembic_py.read_bytes()).hexdigest()} nested map={nested}")
    check(f"{item}: discovery resolves compress to openminion/modules/context/compress/storage with its migrations "
          "(script location; the wheel ships no alembic.ini there)",
          comp["storage_root"] == "openminion/modules/context/compress/storage"
          and comp["script_location"] == "openminion/modules/context/compress/storage/migrations",
          {**comp, "nested_module_storage_import_paths": nested,
           "installed_alembic_py_sha256": hashlib.sha256(alembic_py.read_bytes()).hexdigest()})
    missing = [p["module_id"] for p in packages if not p["discovered"]]
    check(f"{item}: every installed `*/storage/migrations/env.py` package is found by discovery for the MODULE_ID it declares",
          not missing, {"packages": len(packages), "not_found": missing})
    others_missing = [p["module_id"] for p in packages if not p["discovered"] and p["module_id"] != "compress"]
    check(f"{item}: control: every such package other than compress is found by discovery ({len(packages) - 1} packages)",
          not others_missing, {"not_found_other_than_compress": others_missing})
    expected_roots = {
        "secret": "openminion/modules/secret/storage",
        "session": "openminion/modules/session/storage",
        "authoring": "openminion/modules/tool/authoring/storage",
        "commerce": "openminion/tools/commerce/storage",
        "controlplane_telegram": "openminion/modules/controlplane/channels/telegram/storage",
    }
    got = {m: by_module[m]["storage_root"] for m in expected_roots}
    check(f"{item}: control: ordinary (`openminion.modules.<id>.storage`), nested (authoring, commerce) and channel "
          "(controlplane_telegram) modules resolve to their packages",
          got == expected_roots and all(by_module[m]["script_location"] for m in expected_roots), got, expected_roots)
    no_script = sorted(m for m, v in by_module.items() if not v["script_location"])
    check(f"{item}: only the module ids with no migrations package (storage, brain) have no script location",
          no_script == ["brain", "storage"], no_script, ["brain", "storage"])
    CASES[f"{item} discovery"] = {"by_module": by_module, "packages": packages}


def case_item3_cli_controls_sqlite() -> None:
    item = "item 3"
    from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS

    rows = {}
    for module_id in sorted(MODULE_APPLICATION_IDS):
        if module_id in ("storage", "brain"):
            continue
        db = new_sqlite(f"alone_{module_id}")
        step = cli("storage", "migrate", "--sqlite", str(db), "--module", module_id, "--json")
        facts = sqlite_facts(db)
        rows[module_id] = {"exit": step["exit"], "entries": step["entries"], "alembic_version": facts["alembic_version"],
                           "application_id": facts["application_id"],
                           "om_meta_module_id": (facts["om_meta"] or {}).get("module_id")}
    for module_id in ("secret", "authoring", "commerce"):
        r = rows[module_id]
        check(f"{item}: control: `storage migrate --sqlite <new file> --module {module_id}` exits 0, ok, at its head, "
              "with its application_id and om_meta module_id",
              r["exit"] == 0 and (r["entries"] or [{}])[0].get("status") == "ok" and bool(r["alembic_version"])
              and r["application_id"] == hex(MODULE_APPLICATION_IDS[module_id]) and r["om_meta_module_id"] == module_id,
              r)
    config_missing = [m for m, r in rows.items() if "Alembic configuration missing" in json.dumps(r["entries"])]
    check(f"{item}: no module (each alone on a new SQLite file, the 17 with a migrations package) reports "
          "`Alembic configuration missing`", not config_missing, config_missing)
    others = [m for m in config_missing if m != "compress"]
    check(f"{item}: control: no module other than compress reports `Alembic configuration missing` on SQLite",
          not others, others)
    status = {m: ((r["entries"] or [{}])[0].get("status"), r["alembic_version"]) for m, r in rows.items()}
    say(f"  each module alone on SQLite: {status}")
    CASES[f"{item} sqlite each module alone"] = rows


def case_item3_cli_controls_postgres() -> None:
    item = "item 3"
    from openminion.modules.storage.migrations.registry import POSTGRES_VALIDATED_MODULES

    rows = {}
    for module_id in sorted(POSTGRES_VALIDATED_MODULES):
        dbname = new_db(f"alone_{module_id}")
        step = cli("storage", "migrate", "--backend", "postgres", "--postgres-url", pg_url(dbname), "--module", module_id, "--json")
        facts = pg_facts(dbname)
        rows[module_id] = {"exit": step["exit"], "entries": step["entries"], "alembic_version": facts["alembic_version"],
                           "om_meta_module_id": (facts["om_meta"] or {}).get("module_id"), "tables": len(facts["tables"])}
    r = rows["secret"]
    check(f"{item}: control: `storage migrate --backend postgres --module secret` on a new database exits 0, ok, at its head",
          r["exit"] == 0 and (r["entries"] or [{}])[0].get("status") == "ok" and bool(r["alembic_version"])
          and r["om_meta_module_id"] == "secret", r)
    config_missing = [m for m, r in rows.items() if "Alembic configuration missing" in json.dumps(r["entries"])]
    check(f"{item}: no PostgreSQL-validated module (each alone on a new database) reports `Alembic configuration missing`",
          not config_missing, config_missing)
    others = [m for m in config_missing if m != "compress"]
    check(f"{item}: control: no PostgreSQL-validated module other than compress reports `Alembic configuration missing`",
          not others, others)
    status = {m: ((r["entries"] or [{}])[0].get("status"), r["alembic_version"]) for m, r in rows.items()}
    say(f"  each validated module alone on PostgreSQL (observed; telemetry/controlplane are #494): {status}")
    CASES[f"{item} postgres each module alone"] = rows


def main() -> int:
    quiet()
    info = banner("probe_493", backend=ARGS.backend, cli=OM.replace(sys.prefix, "<VENV>"))
    server = None
    if ARGS.backend == "postgres":
        if not PGURL:
            print("NOT RUN: PGURL / OPENMINION_TEST_POSTGRES_URL is not set")
            return 2
        import sqlalchemy as sa

        engine = admin_engine()
        with engine.connect() as conn:
            server = conn.execute(sa.text("select version()")).scalar()
        engine.dispose()
        say(f"postgres: {server}")
    cases = [case_item3_discovery, case_item3_entry_point]
    if ARGS.backend == "sqlite":
        cases = [case_item1_sqlite] + cases + [case_item3_cli_controls_sqlite]
    else:
        cases = [lambda: case_item2_postgres("newdb", None), lambda: case_item2_postgres("schema", "om_compress")] + cases + [
            case_item3_cli_controls_postgres]
    for case in cases:
        try:
            case()
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            say(f"CASE raised:\n{tb}")
            check(f"case {getattr(case, '__name__', 'lambda')} ran without an exception in the probe", False, tb[-1500:])
    summary = CHECKS.summary()
    write_result(OUT, mask({"info": info, "postgres_version": server, "checks": CHECKS.items, "summary": summary,
                            "cases": CASES}))
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}", flush=True)
    return 0 if CHECKS.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
