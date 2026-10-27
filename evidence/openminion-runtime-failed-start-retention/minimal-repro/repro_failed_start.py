"""A failed in-process APIRuntime start: what is still open afterwards?

    python repro_failed_start.py <new empty dir> [<postgresql+psycopg URL of a new empty database>]

Without a URL: SQLite runtime storage, and OPENMINION_MEMORY_PROVIDER=not-a-provider
makes the start fail after the runtime storage was built. With a URL: the #437
config (storage.backend: postgres), which fails at api/core/bootstrap.py:126.
"""

import gc

gc.disable()  # so the result does not depend on when a collection happens to run

import json
import os
import sys
from pathlib import Path

import psutil

root = Path(sys.argv[1]).resolve()
pg_url = sys.argv[2] if len(sys.argv) > 2 else None
storage = {"path": str(root / ".openminion" / "state.db")}
if pg_url:
    storage.update(backend="postgres", postgres_url=pg_url)
else:
    os.environ["OPENMINION_MEMORY_PROVIDER"] = "not-a-provider"
root.mkdir(parents=True, exist_ok=True)
(root / "agents.json").write_text(json.dumps({
    "default_agent": "first",
    "agents": {"first": {"provider": "echo", "default_channel": "console"}},
    "runtime": {"process_mode": "single-process", "daemon_auto_start": False},
    "storage": storage,
}))

from openminion import APIRuntime

try:
    APIRuntime.from_config_path(
        str(root / "agents.json"), home_root=root, data_root=root / ".openminion"
    )
except Exception as exc:
    print("start failed:", type(exc).__name__, str(exc).splitlines()[0])


def check(when: str) -> None:
    files = []
    for item in psutil.Process().open_files():
        try:
            files.append(Path(item.path).resolve().relative_to(root).as_posix())
        except ValueError:
            pass
    dbs = sorted(f for f in files if f.endswith((".db", ".sqlite")))
    print(f"{when}: {len(files)} open files under the root, databases: {dbs}")
    if pg_url:
        import sqlalchemy as sa

        engine = sa.create_engine(pg_url)
        with engine.connect() as conn:
            other = conn.execute(sa.text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )).scalar_one()
        engine.dispose()
        print(f"{when}: {other} other connection(s) to the database")
    elif os.name == "nt":
        state = root / ".openminion" / "state.db"
        try:
            os.replace(state, state.with_name("state.db.moved"))
            os.replace(state.with_name("state.db.moved"), state)
            print(f"{when}: renaming state.db works")
        except OSError as err:
            print(f"{when}: renaming state.db: {type(err).__name__} [WinError {err.winerror}]")


check("after the exception")
gc.collect()
check("after gc.collect()")
