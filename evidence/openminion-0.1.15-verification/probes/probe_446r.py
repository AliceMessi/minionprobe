"""#446, reporting part (PR #457), on the installed package: what the real
`openminion storage migrate` CLI prints for a successful and a failed module
migration, on isolated SQLite and PostgreSQL test databases.

    python -I -X utf8 probe_446r.py <work_dir> <out.json> [--backend sqlite|postgres]
                                    [--baseline <0.1.14 control json>]

Every migrate command is a subprocess of the installed `openminion` console
script (<venv>/Scripts/openminion.exe or <venv>/bin/openminion), from a new
empty working directory, with OPENMINION_HOME / OPENMINION_DATA_ROOT inside the
case folder and OPENMINION_STORAGE_POSTGRES_URL unset. Module `secret`:

  db1  a new, empty test database: `storage migrate --module secret` (text),
       then again with `--json` (already at head);
  db2  a new, empty test database: `--json` first, then text;
  db1  its alembic_version set to `9999_not_a_revision` (a deliberately invalid
       revision), then `--json`, text, `--verify --json` and `--verify`.

SQLite: two new files under the work dir. PostgreSQL: two new databases
(CREATE DATABASE) on the server in OPENMINION_TEST_POSTGRES_URL, dropped at the
end; the URL's password is masked in every output. The databases are read back
independently of openminion (sqlite3, or SQLAlchemy on the server). The actual
revision error is also taken from the installed MigrationRunner in process
(cross-check only; the verified output is the CLI's).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, quiet, sanitize_text, write_result  # noqa: E402

MODULE = "secret"
BAD = "9999_not_a_revision"
CASES: dict[str, Any] = {}
STRIP_ENV = (
    "OPENMINION_HOME",
    "OPENMINION_DATA_ROOT",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
    "OPENMINION_STORAGE_POSTGRES_URL",
    "OPENMINION_TEST_POSTGRES_URL",
    "PGURL",
)


def mask(text: str) -> str:
    return sanitize_text(re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1***@", str(text)))


def console_script() -> Path:
    name = "openminion.exe" if os.name == "nt" else "openminion"
    return Path(sys.executable).parent / name


def entry_point() -> str:
    from importlib import metadata

    for ep in metadata.distribution("openminion").entry_points:
        if ep.group == "console_scripts" and ep.name == "openminion":
            return ep.value
    return ""


class Cli:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.n = 0
        self.runs: list[dict[str, Any]] = []
        self.exe = console_script()

    def __call__(self, label: str, args: list[str]) -> dict[str, Any]:
        self.n += 1
        case = self.work / "cli" / f"{self.n:02d}"
        cwd = case / "cwd"
        cwd.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env["OPENMINION_HOME"] = str(case / "home")
        env["OPENMINION_DATA_ROOT"] = str(case / "data")
        env["PYTHONIOENCODING"] = "utf-8"
        argv = [str(self.exe), "storage", "migrate", *args]
        started = time.monotonic()
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, timeout=600)
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        left = sorted(str(p.relative_to(cwd)).replace("\\", "/") for p in cwd.rglob("*"))
        run = {
            "label": label,
            "argv": [mask(a) for a in ["openminion", *argv[1:]]],
            "exit_code": proc.returncode,
            # Windows text-mode stdout ends lines with CRLF; compared with LF.
            "stdout": mask(out.replace("\r\n", "\n")),
            "stdout_line_endings": "crlf" if "\r\n" in out else "lf",
            "stderr_tail": mask(err[-1500:]),
            "stderr_has_traceback": "Traceback (most recent call last)" in err,
            "seconds": round(time.monotonic() - started, 2),
            "files_left_in_cwd": left,
        }
        try:
            run["json"] = json.loads(out)
        except ValueError:
            run["json"] = None
        self.runs.append(run)
        print(f"--- {label}: exit={proc.returncode}\n{run['stdout']}", flush=True)
        if err.strip():
            print(f"    stderr (tail): {run['stderr_tail'][-400:]!r}", flush=True)
        return run


def head_revision() -> str | None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from openminion.modules.storage.migrations.alembic import discover_module_alembic_paths

    _ini, location = discover_module_alembic_paths(MODULE)
    cfg = Config()
    cfg.set_main_option("script_location", str(location))
    return ScriptDirectory.from_config(cfg).get_current_head()


# ----------------------------------------------------------------- databases
class SqliteDb:
    def __init__(self, work: Path, name: str) -> None:
        self.path = work / "sqlite" / f"{name}.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(self.path).close()  # a new, empty file, as the PR's test does
        self.args = ["--sqlite", str(self.path)]
        self.label = f"sqlite:{name}"

    def _q(self, sql: str) -> list[tuple]:
        conn = sqlite3.connect(self.path)
        try:
            return [tuple(r) for r in conn.execute(sql).fetchall()]
        finally:
            conn.close()

    def revision(self) -> list[str]:
        try:
            return [r[0] for r in self._q("SELECT version_num FROM alembic_version")]
        except sqlite3.OperationalError as exc:
            return [f"<{exc}>"]

    def set_revision(self, value: str) -> None:
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute("UPDATE alembic_version SET version_num = ?", (value,))
        conn.close()

    def tables(self) -> list[str]:
        return [r[0] for r in self._q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

    def om_meta(self) -> dict[str, str]:
        try:
            return {r[0]: r[1] for r in self._q("SELECT key, value FROM om_meta")}
        except sqlite3.OperationalError:
            return {}

    def runner(self):
        from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
        from openminion.modules.storage.migrations.runner import MigrationRunner

        return MigrationRunner(
            module_id=MODULE, db_path=str(self.path),
            module_application_id=MODULE_APPLICATION_IDS.get(MODULE, 0),
        )

    def drop(self) -> None:
        pass


class PgDb:
    def __init__(self, base: str, name: str) -> None:
        import sqlalchemy as sa

        self.sa = sa
        self.name = f"p446_{name}_{uuid.uuid4().hex[:10]}"
        admin = sa.create_engine(base, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{self.name}"'))
        admin.dispose()
        self.base = base
        self.url = sa.engine.make_url(base).set(database=self.name).render_as_string(hide_password=False)
        self.args = ["--backend", "postgres", "--postgres-url", self.url]
        self.label = f"postgres:{self.name}"

    def _q(self, sql: str) -> list[tuple]:
        engine = self.sa.create_engine(self.url)
        try:
            with engine.connect() as conn:
                return [tuple(r) for r in conn.execute(self.sa.text(sql)).fetchall()]
        finally:
            engine.dispose()

    def revision(self) -> list[str]:
        try:
            return [r[0] for r in self._q("SELECT version_num FROM alembic_version")]
        except Exception as exc:  # noqa: BLE001
            return [f"<{type(exc).__name__}>"]

    def set_revision(self, value: str) -> None:
        engine = self.sa.create_engine(self.url)
        with engine.begin() as conn:
            conn.execute(self.sa.text("UPDATE alembic_version SET version_num = :v"), {"v": value})
        engine.dispose()

    def tables(self) -> list[str]:
        return [r[0] for r in self._q(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() ORDER BY 1")]

    def om_meta(self) -> dict[str, str]:
        try:
            return {r[0]: r[1] for r in self._q("SELECT key, value FROM om_meta")}
        except Exception:  # noqa: BLE001
            return {}

    def server(self) -> dict[str, Any]:
        rows = self._q("SELECT version(), current_database(), current_schema()")
        return {"version": rows[0][0], "database": rows[0][1], "schema": rows[0][2]}

    def runner(self):
        from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS
        from openminion.modules.storage.migrations.runner import MigrationRunner

        self._engine = self.sa.create_engine(self.url)
        return MigrationRunner(
            module_id=MODULE, db_path=":memory:",
            module_application_id=MODULE_APPLICATION_IDS.get(MODULE, 0),
            backend_type="postgres", engine=self._engine,
        )

    def drop(self) -> None:
        engine = getattr(self, "_engine", None)
        if engine is not None:
            engine.dispose()
        admin = self.sa.create_engine(self.base, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(self.sa.text(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)'))
        admin.dispose()


# ----------------------------------------------------------------- checks
def entries(run: dict[str, Any]) -> Any:
    data = run.get("json")
    return data.get("modules") if isinstance(data, dict) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--backend", default="sqlite", choices=("sqlite", "postgres"))
    ap.add_argument("--baseline", default="")
    a = ap.parse_args()
    quiet()
    base = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
    if a.backend == "postgres" and not base:
        print("OPENMINION_TEST_POSTGRES_URL is not set: PostgreSQL not run")
        return 2
    work = Path(a.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    exe = console_script()
    info = banner("probe_446r", backend=a.backend, console_script=str(exe),
                  console_script_exists=exe.exists(), console_script_entry_point=entry_point())
    checks = Checks()
    L = a.backend
    head = head_revision()
    CASES["secret_head_revision"] = head
    cli = Cli(work)
    dbs: list[Any] = []
    try:
        if a.backend == "sqlite":
            db1, db2 = SqliteDb(work, "db1"), SqliteDb(work, "db2")
        else:
            db1, db2 = PgDb(base, "db1"), PgDb(base, "db2")
            CASES["postgres"] = db1.server()
        dbs = [db1, db2]
        CASES["databases"] = [mask(d.label) for d in dbs]
        checks.check(f"{L}: the probe runs the installed console script `openminion` (entry point openminion.cli.main:main)",
                     exe.exists() and entry_point() == "openminion.cli.main:main",
                     observed={"script": str(exe), "entry_point": entry_point()})
        checks.check(f"{L}: both test databases start with no alembic_version table",
                     all(d.tables() == [] for d in dbs), observed={d.label: d.tables() for d in dbs})

        # --------------------------------------------------- successful migration
        ok_entry = [{"module_id": MODULE, "status": "ok", "success": True}]
        t1 = cli("db1 new database, text", [*db1.args, "--module", MODULE])
        rev1 = db1.revision()
        j1 = cli("db1 at head, --json", [*db1.args, "--module", MODULE, "--json"])
        j2 = cli("db2 new database, --json", [*db2.args, "--module", MODULE, "--json"])
        rev2 = db2.revision()
        t2 = cli("db2 at head, text", [*db2.args, "--module", MODULE])
        CASES["after_success"] = {
            "db1": {"alembic_version": rev1, "om_meta": db1.om_meta(), "tables": db1.tables()},
            "db2": {"alembic_version": rev2, "om_meta": db2.om_meta(), "tables": db2.tables()},
        }
        checks.check(f"{L} success: the migrations really ran: both new databases are at the module's head revision",
                     rev1 == [head] and rev2 == [head], observed={"db1": rev1, "db2": rev2, "head": head})
        for run in (t1, t2):
            checks.check(f"{L} success ({run['label']}): exit 0, stdout exactly '  ok  secret', no traceback",
                         run["exit_code"] == 0 and run["stdout"] == f"  ok  {MODULE}\n" and not run["stderr_has_traceback"],
                         observed={"exit": run["exit_code"], "stdout": run["stdout"]})
        for run in (j2, j1):
            checks.check(f"{L} success ({run['label']}): exit 0 and the JSON entries are exactly "
                         "[{module_id: secret, status: ok, success: true}]",
                         run["exit_code"] == 0 and entries(run) == ok_entry and not run["stderr_has_traceback"],
                         observed={"exit": run["exit_code"], "json": run["json"]})
        checks.check(f"{L} success: no entry carries an `applied` list (nor an `error`)",
                     all(isinstance(entries(r), list) and all("applied" not in e and "error" not in e for e in entries(r))
                         for r in (j1, j2)),
                     observed=[entries(r) for r in (j1, j2)])

        # --------------------------------------------------- invalid revision
        db1.set_revision(BAD)
        before = {"alembic_version": db1.revision(), "tables": db1.tables(), "om_meta": db1.om_meta()}
        CASES["db1_invalid_revision_before"] = before
        fj = cli("db1 invalid revision, --json", [*db1.args, "--module", MODULE, "--json"])
        ft = cli("db1 invalid revision, text", [*db1.args, "--module", MODULE])
        vj = cli("db1 invalid revision, --verify --json", [*db1.args, "--module", MODULE, "--verify", "--json"])
        vt = cli("db1 invalid revision, --verify", [*db1.args, "--module", MODULE, "--verify"])
        after = {"alembic_version": db1.revision(), "tables": db1.tables(), "om_meta": db1.om_meta()}
        CASES["db1_invalid_revision_after"] = after
        # The actual error, from the installed MigrationRunner on the same database (cross-check).
        report = db1.runner().migrate(target="head")
        actual = report.error
        CASES["migration_report_in_process"] = {"success": report.success, "error": mask(str(actual)),
                                                "has_applied_versions": hasattr(report, "applied_versions")}
        locate = f"Can't locate revision identified by '{BAD}'"
        checks.check(f"{L} failure: the installed MigrationRunner's report for this database fails with the revision error",
                     report.success is False and isinstance(actual, str) and locate in actual,
                     observed=mask(str(actual)))
        for run in (fj, vj):
            ent = entries(run)
            one = ent[0] if isinstance(ent, list) and len(ent) == 1 else {}
            checks.check(f"{L} failure ({run['label']}): the JSON entry carries the actual revision error "
                         "(identical to MigrationReport.error), status error, success false",
                         one.get("status") == "error" and one.get("success") is False
                         and one.get("module_id") == MODULE and one.get("error") == mask(str(actual))
                         and locate in str(one.get("error")),
                         observed=ent)
            checks.check(f"{L} failure ({run['label']}): no `applied` list in the entry",
                         isinstance(ent, list) and all("applied" not in e for e in ent), observed=ent)
            checks.check(f"{L} failure ({run['label']}): exit status nonzero (1)",
                         run["exit_code"] == 1, observed=run["exit_code"])
        for run in (ft, vt):
            checks.check(f"{L} failure ({run['label']}): stdout is '  FAIL  secret: <the actual revision error>', exit 1",
                         run["stdout"] == f"  FAIL  {MODULE}: {mask(str(actual))}\n" and run["exit_code"] == 1,
                         observed={"exit": run["exit_code"], "stdout": run["stdout"]})
        checks.check(f"{L} failure: the failed runs left the database as it was (revision {BAD}, same tables and om_meta)",
                     after == before and after["alembic_version"] == [BAD], observed={"before": before, "after": after})

        # --------------------------------------------------- 0.1.14 baseline (exit codes)
        if a.baseline and Path(a.baseline).exists():
            base_runs = {r["label"]: r for r in json.loads(Path(a.baseline).read_text(encoding="utf-8"))["cases"]["cli_runs"]}
            pairs = [(r["label"], r["exit_code"], base_runs.get(r["label"], {}).get("exit_code")) for r in cli.runs]
            CASES["exit_codes_vs_baseline"] = pairs
            checks.check(f"{L}: every command's exit status is the same as on the 0.1.14 control (failures 1, successes 0)",
                         all(cur == old for _l, cur, old in pairs), observed=pairs)
            same = []
            for run in (t1, j1, j2, t2):
                old = base_runs.get(run["label"], {})
                if run["json"] is None:
                    same.append((run["label"], run["stdout"] == old.get("stdout"), run["stdout"], old.get("stdout")))
                else:
                    old_entries = [{k: v for k, v in e.items() if k != "applied"}
                                   for e in ((old.get("json") or {}).get("modules") or [])]
                    same.append((run["label"], entries(run) == old_entries, entries(run), (old.get("json") or {}).get("modules")))
            CASES["success_output_vs_baseline"] = same
            checks.check(f"{L} success: the output is the 0.1.14 control's, byte for byte in text, and in JSON "
                         "the same entry without the always-empty `applied` list",
                         all(flag for _l, flag, _c, _o in same), observed=same)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        checks.check(f"{L}: the probe completes without an exception", False, observed=mask(f"{type(exc).__name__}: {exc}"))
    finally:
        for d in dbs:
            try:
                d.drop()
            except Exception as exc:  # noqa: BLE001
                print("drop failed:", mask(str(exc)))
    CASES["cli_runs"] = cli.runs
    summary = checks.summary()
    write_result(Path(a.out), {"banner": info, "summary": summary, "checks": checks.items, "cases": CASES})
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
