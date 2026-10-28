"""#446 reporting probe: what `openminion storage migrate` prints for a failed module report.

Runs the CLI in a subprocess (`python -m openminion`, from the tree on PYTHONPATH):
- sqlite_unknown_revision: a new SQLite file; `--module secret` once, then its
  alembic_version is set to a revision no module has, then `--module secret` again;
- pg_session_then_artifact: a new PostgreSQL database; `--module session`, then
  `--module artifact` (the shared alembic_version case from #446);
- pg_unknown_revision: a new PostgreSQL database; as the SQLite case.
Each step prints the exit code, the JSON entries and the text output. The
password in the URL is masked. Creates only new temporary files and databases.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

BASE = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "")


def mask(text: str) -> str:
    return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", text)


def cli(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "openminion", "storage", "migrate", *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout


def step(case: str, name: str, args: list[str]) -> None:
    code, out = cli(*args, "--json")
    try:
        entries = json.loads(out)["modules"]
    except Exception:  # noqa: BLE001
        entries = out.strip()[-400:]
    text_code, text_out = cli(*args)
    print(
        f"CASE {case} {name}: json exit={code} entries={mask(json.dumps(entries))} | "
        f"text exit={text_code} output={mask(text_out.strip())!r}"
    )


def sqlite_case(root: Path) -> None:
    db = root / "secret.db"
    sqlite3.connect(db).close()
    args = ["--sqlite", str(db), "--module", "secret"]
    code, _ = cli(*args)
    print(f"CASE sqlite_unknown_revision setup: --module secret exit={code}")
    connection = sqlite3.connect(db)
    with connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999_not_a_revision'")
    connection.close()
    step("sqlite_unknown_revision", "after setting 9999_not_a_revision", args)


def new_database() -> str:
    import sqlalchemy as sa

    name = f"r446_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(BASE, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return BASE.rsplit("/", 1)[0] + "/" + name


def pg_cases() -> None:
    import sqlalchemy as sa

    url = new_database()
    pg = ["--backend", "postgres", "--postgres-url", url]
    code, _ = cli(*pg, "--module", "session")
    print(f"CASE pg_session_then_artifact setup: --module session exit={code}")
    step("pg_session_then_artifact", "--module artifact", pg + ["--module", "artifact"])

    url = new_database()
    pg = ["--backend", "postgres", "--postgres-url", url]
    code, _ = cli(*pg, "--module", "secret")
    print(f"CASE pg_unknown_revision setup: --module secret exit={code}")
    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE alembic_version SET version_num = '9999_not_a_revision'"))
    engine.dispose()
    step("pg_unknown_revision", "after setting 9999_not_a_revision", pg + ["--module", "secret"])


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="r446-"))
    os.environ["OPENMINION_HOME"] = str(root / "home")
    sqlite_case(root)
    if BASE:
        pg_cases()
    else:
        print("CASE pg: skipped, OPENMINION_TEST_POSTGRES_URL is not set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
