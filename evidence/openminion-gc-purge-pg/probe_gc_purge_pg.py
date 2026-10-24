"""`memctl gc` and `run_gc` with a PostgreSQL memory store.

Each case starts from a fresh store seeded with two facts in `agent:a`:
- `old`: confidence 0.8, last updated 28 days ago, so confidence decay lowers it
  to 0.6 (default rate 0.05 per 7 days);
- `deleted`: soft-deleted with store.delete(), so the purge removes the row.

Cases:
1. SQLite memory store (control): the real `memctl gc` command
   (`python -m openminion.modules.memory gc`) in a subprocess, with a memctl
   config file (MEMCTL_CONFIG) that selects the SQLite store.
2. PostgreSQL memory store: the same command, with a config file that selects
   the PostgreSQL store.
3. PostgreSQL: `run_gc` called in-process exactly as `memctl gc` calls it
   (`modules/memory/cli.py`, `cmd_gc`): on the store of the service that
   `_get_service()` builds, with `load_config().retention`.
4. PostgreSQL: the same call on the PostgresMemoryStore inside that store.

After each case the probe reads both rows back. The PostgreSQL cases need
OPENMINION_TEST_POSTGRES_URL and the postgres extra; without it they are skipped.

Run from an openminion source checkout: PYTHONPATH=src:. python probe_gc_purge_pg.py
(PYTHONPATH="src;." on Windows)
"""

import contextlib
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

from openminion.modules.memory import cli as memory_cli
from openminion.modules.memory.config import load_config
from openminion.modules.memory.models import MemoryRecord
from openminion.modules.memory.runtime.gc import run_gc
from openminion.modules.memory.storage.sqlite.store import SQLiteMemoryStore

POSTGRES_BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
OLD = (
    datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=28)
).isoformat()
MEMCTL = {
    "version": 1,
    "memctl": {
        "defaults": {
            "confidence": {
                "user_said": 0.7,
                "tool_output": 0.8,
                "agent_inferred": 0.4,
                "validated": 0.95,
                "imported": 0.9,
            }
        },
        "promotion": {
            "require_approval_for_global": True,
            "auto_promote_agent_procedures": True,
            "allowlisted_auto_rules": [],
        },
        "retrieval": {"max_results": 25, "min_confidence_default": 0.55, "pin_first": True},
        "retention": {"enable_soft_delete": True, "gc_enabled": True, "gc_batch_size": 500},
    },
}


def seed(store):
    for record_id in ("old", "deleted"):
        store.put(
            MemoryRecord(
                id=record_id,
                scope="agent:a",
                type="fact",
                title=record_id,
                content="c",
                created_at=OLD,
                updated_at=OLD,
                confidence=0.8,
            )
        )
    store.delete("deleted")


def memctl_environment(root, store_config):
    config = json.loads(json.dumps(MEMCTL))
    config["memctl"]["store"] = store_config
    root.mkdir(parents=True, exist_ok=True)
    path = root / "memory.yaml"
    path.write_text(json.dumps(config), encoding="utf-8")  # JSON is valid YAML
    return {
        "MEMCTL_CONFIG": str(path),
        "OPENMINION_HOME": str(root / "home"),
        "OPENMINION_DATA_ROOT": str(root / "data"),
    }


@contextlib.contextmanager
def environ(values):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def describe(rows):
    described = {}
    for record_id in ("old", "deleted"):
        if record_id not in rows:
            described[record_id] = "row purged"
            continue
        confidence, is_deleted = rows[record_id]
        state = "soft-deleted" if is_deleted else "active"
        described[record_id] = f"confidence {float(confidence):.3f}, {state}"
    return described


def openminion_frames(exc):
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__):
        name = frame.filename.replace("\\", "/")
        index = name.rfind("/openminion/")
        if index >= 0:
            frames.append(f"{name[index + 1:]}:{frame.lineno} {frame.name}")
    return frames[-3:]


def memctl_gc(env, tmp):
    completed = subprocess.run(
        [sys.executable, "-m", "openminion.modules.memory", "gc"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=600,
    )
    output = (completed.stdout + completed.stderr).replace(tmp, "<tmp>")
    if POSTGRES_BASE:
        output = output.replace(POSTGRES_BASE, "<postgres-url>")
    lines = output.splitlines()
    shown = [line for line in lines if line.startswith(("GC:", "Error:"))]
    return {"exit code": completed.returncode, "output": shown or lines[-1:]}


def run_gc_as_memctl(env, *, inner):
    with environ(env):
        service = memory_cli._get_service(None)
        retention = load_config(env=dict(os.environ)).retention
    audited = service._store
    target = audited._store if inner else audited
    row = {
        "store passed to run_gc": (
            type(target).__name__
            if inner
            else f"{type(audited).__name__} wrapping {type(audited._store).__name__}"
        )
    }
    try:
        result = run_gc(target, batch_size=500, retention_config=retention)
        row["result"] = (
            f"ok (decayed_records={result.decayed_records}, "
            f"deleted_records={result.deleted_records})"
        )
    except Exception as exc:
        row["result"] = f"raised {type(exc).__name__}: {exc}"
        row["raised at"] = openminion_frames(exc)
    finally:
        audited._sink.close()
        audited._store.close()
    return row


@contextlib.contextmanager
def postgres_schema():
    import sqlalchemy as sa

    from tests.storage.postgres_test_utils import schema_url

    schema = f"probe_gcpurge_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(POSTGRES_BASE, future=True)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    url = schema_url(POSTGRES_BASE, schema)
    engine = sa.create_engine(url, future=True)
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def postgres_rows(engine):
    import sqlalchemy as sa

    with engine.connect() as conn:
        result = conn.execute(sa.text("SELECT id, confidence, is_deleted FROM memory_records"))
        return {row[0]: (row[1], row[2]) for row in result}


def sqlite_rows(db_path):
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        result = conn.execute("SELECT id, confidence, is_deleted FROM memory_records")
        return {row[0]: (row[1], row[2]) for row in result}


def main():
    report = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp) / "sqlite"
        db_path = root / "data" / "memory" / "memory.db"
        seed(SQLiteMemoryStore(db_path, artifactctl=None))
        env = memctl_environment(root, {"backend": "sqlite", "sqlite_path": str(db_path)})
        row = memctl_gc(env, tmp)
        row["rows after"] = describe(sqlite_rows(db_path))
        report["SQLite memory store (control)"] = {"memctl gc": row}

        if not POSTGRES_BASE:
            report["PostgreSQL memory store"] = "skipped: OPENMINION_TEST_POSTGRES_URL not set"
        else:
            from openminion.modules.memory.storage.postgres.store import PostgresMemoryStore

            cases = (
                ("memctl gc", lambda env: memctl_gc(env, tmp)),
                (
                    "run_gc as memctl gc calls it",
                    lambda env: run_gc_as_memctl(env, inner=False),
                ),
                (
                    "run_gc on the PostgresMemoryStore inside it",
                    lambda env: run_gc_as_memctl(env, inner=True),
                ),
            )
            postgres = {}
            for number, (label, action) in enumerate(cases):
                case_root = Path(tmp) / f"postgres-{number}"
                with postgres_schema() as (url, engine):
                    store = PostgresMemoryStore(
                        engine, database_path=case_root / "seed.db", artifactctl=None
                    )
                    seed(store)
                    store.close()
                    env = memctl_environment(
                        case_root, {"backend": "postgres", "postgres": {"url": url}}
                    )
                    row = action(env)
                    row["rows after"] = describe(postgres_rows(engine))
                postgres[label] = row
            report["PostgreSQL memory store"] = postgres
    print(json.dumps(report, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
