"""#494 PR evidence: on SQLite, the branch's migrations against the shipped (dev) ones.

    python -I probe_494_sqlite_shipped_vs_head.py <dev checkout> <out dir>

Run with the branch's environment (its `openminion` package). For `controlplane`
and `telemetry` it migrates new SQLite files to head with dev's migration scripts
(`<dev checkout>/src/.../storage/migrations`) and with the branch's own, through
`MigrationRunner`, and compares `sqlite_master`, `alembic_version`,
`application_id` and `user_version`. Then it upgrades the file migrated with dev's
scripts with the branch's scripts and compares again. Nothing outside a temporary
directory is written.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

from openminion.modules.storage.migrations.alembic import (
    discover_module_alembic_paths,
)
from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
from openminion.modules.storage.migrations.runner import MigrationRunner

MODULE_DIRS = {"controlplane": "controlplane", "telemetry": "telemetry"}


def facts(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        master = [
            list(row)
            for row in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            )
        ]
        return {
            "sqlite_master": master,
            "sqlite_master_sha256": hashlib.sha256(
                json.dumps(master).encode("utf-8")
            ).hexdigest(),
            "alembic_version": [
                row[0] for row in conn.execute("SELECT version_num FROM alembic_version")
            ],
            "application_id": conn.execute("PRAGMA application_id").fetchone()[0],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
        }
    finally:
        conn.close()


def migrate(module_id: str, db_path: Path, ini: Path, location: Path) -> dict:
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(db_path).close()
    report = MigrationRunner(
        module_id=module_id,
        db_path=db_path,
        module_application_id=MODULE_APPLICATION_IDS[module_id],
        alembic_ini_path=ini,
        alembic_script_location=location,
    ).migrate(target="head")
    return {"success": report.success, "error": report.error}


def main() -> int:
    dev_root = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="probe494-"))
    results: dict = {"dev_root": dev_root.name}
    ok = True
    for module_id, module_dir in MODULE_DIRS.items():
        head_ini, head_location = discover_module_alembic_paths(module_id)
        dev_storage = dev_root / "src" / "openminion" / "modules" / module_dir / "storage"
        dev_ini, dev_location = dev_storage / "alembic.ini", dev_storage / "migrations"
        assert head_ini is not None and head_location is not None
        dev_file = work / f"{module_id}-dev.db"
        head_file = work / f"{module_id}-head.db"
        dev_run = migrate(module_id, dev_file, dev_ini, dev_location)
        head_run = migrate(module_id, head_file, head_ini, head_location)
        dev_facts, head_facts = facts(dev_file), facts(head_file)
        upgrade_run = migrate(module_id, dev_file, head_ini, head_location)
        upgraded_facts = facts(dev_file)
        new_same = dev_facts == head_facts
        upgrade_same = upgraded_facts == dev_facts
        ok = ok and dev_run["success"] and head_run["success"] and upgrade_run["success"]
        ok = ok and new_same and upgrade_same
        print(
            f"CASE {module_id}: new file, dev scripts success={dev_run['success']} "
            f"head scripts success={head_run['success']}; identical "
            f"(sqlite_master, alembic_version, application_id, user_version)={new_same}; "
            f"alembic_version={head_facts['alembic_version']} "
            f"sqlite_master sha256={head_facts['sqlite_master_sha256']}",
            flush=True,
        )
        print(
            f"CASE {module_id}: file migrated with dev scripts, then upgraded with head "
            f"scripts: success={upgrade_run['success']}; unchanged={upgrade_same}; "
            f"alembic_version {dev_facts['alembic_version']} -> "
            f"{upgraded_facts['alembic_version']}",
            flush=True,
        )
        results[module_id] = {
            "dev_scripts": dev_run,
            "head_scripts": head_run,
            "new_file_identical": new_same,
            "upgrade_with_head_scripts": upgrade_run,
            "upgraded_file_unchanged": upgrade_same,
            "dev": dev_facts,
            "head": head_facts,
            "upgraded": upgraded_facts,
        }
    (out_dir / "probe_494_sqlite_shipped_vs_head.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    print("RESULT", "ok" if ok else "DIFFERENT", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
