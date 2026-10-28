#!/bin/bash
set -u
DEV="988ac4ec54ae803f4ac937a6fc2fc1c04bb5f590"
F="tests/cli/test_storage_migrate_report_fields.py tests/cli/test_storage_migration_commands.py"
PGX="tests/integration/test_storage_migrate_report_postgres.py tests/integration/test_postgres_tier1_promotion.py tests/integration/test_postgres_tier2_promotion.py tests/integration/test_postgres_tier3_memory_promotion.py"
RM="tests/cli tests/storage"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
PROBE="/tmp/ev/probe_446_report.py"
P=.venv/bin/python; DEV=${DEV:-a41487ab1b51f7fd526ff29cd526f0b98dc01fc7}
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph|typer) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
mkdir -p /tmp/ev
cat > /tmp/ev/probe_446_report.py <<'PYEOF_X'
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
PYEOF_X
[ -n "${PROBE:-}" ] && sha256sum /tmp/ev/*
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=line "$@" 2>&1 | grep -E "^FAILED|^ERROR|passed|failed|error" | sed -E 's/^(.{240}).*/\1 .../'; }
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"; fi
  sec "[$tree] pytest <focused>"; pyt $F
  if [ -n "${R:-}" ]; then sec "[$tree] pytest <related>"; pyt $R; fi
  if [ -n "${RM:-}" ]; then sec "[$tree] pytest -m \"$RMARK\" $RM"; pyt -m "$RMARK" $RM; fi
  if [ -n "${PGX:-}" ]; then sec "[$tree] pytest -m postgres <postgres>"; pyt -m postgres $PGX; fi
  if [ -n "${PROBE:-}" ]; then sec "[$tree] the issue's probe: PYTHONPATH=src:. python $(basename $PROBE)"; PYTHONPATH=src:. $P $PROBE 2>&1 | grep -v "^20[0-9][0-9]-"; fi
  if [ -n "${PROBE2:-}" ]; then sec "[$tree] PYTHONPATH=src:. python $(basename $PROBE2)"; PYTHONPATH=src:. $P $PROBE2 2>&1 | grep -v "^20[0-9][0-9]-"; fi
done
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -40; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
