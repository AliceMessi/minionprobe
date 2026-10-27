"""`openminion storage migrate` and the runtime migrations on one new PostgreSQL database.

    python -I -X utf8 probe_migrate_ledger.py --label <name> --work <dir> --out <out.json>

Needs PGURL (a PostgreSQL server URL) and the `postgres` extra. Every case uses a
new, empty database on that server. APIRuntime is not used: the runtime side is
`build_runtime_storage` with the record backend and options that a config file with
`storage.backend: postgres` resolves to (the call APIRuntime makes), so the cases do
not depend on #437. Cases:

  runtime_only          build_runtime_storage on a new database, then close (control)
  module_alone          every module the CLI migrates on PostgreSQL, each on its own new
                        database: `openminion storage migrate --backend postgres
                        --postgres-url <db> --module <id> --json` (control)
  cli_migrate_all       `openminion storage migrate --backend postgres --postgres-url <db> --json`
  runner_messages       the same modules in the same order, in process, through the CLI's
                        own helpers (_get_validated_module_ids, _get_postgres_engine,
                        _make_runner), recording each MigrationReport.error
  migrate_then_runtime  cli_migrate_all, then build_runtime_storage on the same database
  session_then_runtime  `... storage migrate --module session` only (the module named in the
                        first startup warning), then build_runtime_storage
  runtime_then_migrate  build_runtime_storage (closed), then the CLI for every module; then
                        build_runtime_storage again. A second database repeats the same
                        order with the in-process runner to record the messages.
  pair_session_then_artifact   `--module session`, then `--module artifact`, on one new database
  pair_artifact_then_registry  `--module artifact`, then `--module registry`, on one new database
  runtime_then_session         build_runtime_storage (closed), then `--module session`

When the CLI reports `error`, the probe runs the same module once more in process on
the same database (the failed attempt was rolled back) to read MigrationReport.error,
which the CLI output does not include. For every CLI module status `ok`, the probe
checks whether that module's own tables (the tables it creates alone on a new database,
from module_alone, without alembic_version and om_meta) exist afterwards.

After each step the probe records the database: the tables in `public`, the runtime
ledger (`migrations`), the Alembic ledger (`alembic_version`), `om_meta`, and the
columns of `sessions` and `events`.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import openminion

PGURL = os.environ.get("PGURL", "")
STRIP_ENV = (
    "OPENMINION_HOME",
    "OPENMINION_DATA_ROOT",
    "OPENMINION_GENERATED_ROOT",
    "OPENMINION_CONFIG_PATH",
    "OPENMINION_STORAGE_BACKEND",
    "OPENMINION_STORAGE_POSTGRES_URL",
)


# ---------------------------------------------------------------- sanitizing
def _prefixes(work: str | None) -> list[tuple[str, str]]:
    pairs = []
    for value, token in (
        (work, "<WORK>"),
        (sys.prefix, "<VENV>"),
        (os.environ.get("RUNNER_TEMP"), "<RUNNER_TEMP>"),
        (str(Path.home()), "<USER_HOME>"),
    ):
        if value:
            for form in {str(Path(value)), str(Path(value).resolve())}:
                pairs.append((form, token))
    return sorted(set(pairs), key=lambda item: len(item[0]), reverse=True)


PREFIXES: list[tuple[str, str]] = _prefixes(None)


def clean(text: str) -> str:
    for raw, token in PREFIXES:
        text = text.replace(raw, token)
    return re.sub(r"(postgresql(?:\+\w+)?://[^:/@\s]+):[^@\s]*@", r"\1:***@", text)


def clean_any(value: Any) -> Any:
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {k: clean_any(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_any(v) for v in value]
    return value


def log(line: str) -> None:
    print(clean(line), flush=True)


def first_line(text: str, limit: int = 400) -> str:
    return (text or "").strip().splitlines()[0][:limit] if (text or "").strip() else ""


# ---------------------------------------------------------------- PostgreSQL
def db_url(name: str) -> str:
    parts = urlsplit(PGURL)
    return urlunsplit(parts._replace(path=f"/{name}"))


def create_database(name: str) -> str:
    import sqlalchemy as sa

    engine = sa.create_engine(db_url("postgres"), isolation_level="AUTOCOMMIT", future=True)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return db_url(name)


def database_facts(name: str) -> dict[str, Any]:
    import sqlalchemy as sa

    gc.collect()
    facts: dict[str, Any] = {}
    engine = sa.create_engine(db_url(name), future=True)
    try:
        with engine.connect() as conn:
            tables = conn.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
            ).scalars().all()
            facts["public_tables"] = list(tables)
            facts["public_table_count"] = len(tables)
            if "migrations" in tables:
                rows = conn.execute(
                    sa.text("SELECT version, name FROM migrations ORDER BY version")
                ).all()
                facts["runtime_ledger"] = {
                    "exists": True,
                    "rows": len(rows),
                    "max_version": max((r[0] for r in rows), default=None),
                }
            else:
                facts["runtime_ledger"] = {"exists": False}
            if "alembic_version" in tables:
                facts["alembic_version_rows"] = list(
                    conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
                )
            else:
                facts["alembic_version_rows"] = None
            if "om_meta" in tables:
                facts["om_meta"] = {
                    r[0]: r[1]
                    for r in conn.execute(sa.text("SELECT key, value FROM om_meta ORDER BY key")).all()
                    if r[0] != "last_migrated_at"
                }
            else:
                facts["om_meta"] = None
            for table in ("sessions", "events"):
                if table in tables:
                    facts[f"{table}_columns"] = list(
                        conn.execute(
                            sa.text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = 'public' AND table_name = :t "
                                "ORDER BY ordinal_position"
                            ),
                            {"t": table},
                        ).scalars().all()
                    )
            facts["other_open_connections"] = conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": name},
            ).scalar_one()
    finally:
        engine.dispose()
    return facts


def table_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    b, a = set(before["public_tables"]), set(after["public_tables"])
    return {"added": sorted(a - b), "removed": sorted(b - a)}


# ---------------------------------------------------------------- the two migration paths
def write_config(root: Path, url: str) -> Path:
    config = {
        "default_agent": "first",
        "agents": {"first": {"provider": "echo", "default_channel": "console"}},
        "runtime": {"process_mode": "single-process", "daemon_auto_start": False},
        "storage": {
            "path": str(root / ".openminion" / "state.db"),
            "backend": "postgres",
            "postgres_url": url,
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    path = root / "agents.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def runtime_storage(root: Path, url: str) -> dict[str, Any]:
    """build_runtime_storage as APIRuntime calls it for this config (no telemetry hook)."""
    from openminion.base.config.parser import openminion_config_from_dict
    from openminion.modules.storage.runtime.context import build_runtime_storage

    cfg = write_config(root, url)
    config = openminion_config_from_dict(json.loads(cfg.read_text(encoding="utf-8")))
    result: dict[str, Any] = {
        "call": "build_runtime_storage(storage.path, env={}, record_backend="
        f"{config.storage.record_backend()!r}, record_backend_options=<storage.record_backend_options()>)",
    }
    try:
        storage = build_runtime_storage(
            config.storage.path,
            env={},
            record_backend=config.storage.record_backend(),
            record_backend_options=config.storage.record_backend_options(),
        )
    except Exception as exc:  # noqa: BLE001
        result["outcome"] = "failed"
        result["exception"] = f"{type(exc).__name__}: {first_line(str(exc))}"
        result["exception_full"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        return result
    try:
        result["outcome"] = "built"
        result["applied_versions"] = list(storage.migration_result.applied_versions)
        result["current_version"] = storage.migration_result.current_version
    finally:
        storage.close()
    return result


def run_cli(root: Path, url: str, extra: list[str]) -> dict[str, Any]:
    binary = str(Path(sys.executable).with_name("openminion"))
    if os.name == "nt":
        binary += ".exe"
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["OPENMINION_HOME"] = str(root)
    root.mkdir(parents=True, exist_ok=True)
    cmd = [binary, "storage", "migrate", "--backend", "postgres", "--postgres-url", url, *extra, "--json"]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=str(root), timeout=600,
    )
    out: dict[str, Any] = {
        "argv": ["openminion", *cmd[1:]],
        "exit_code": proc.returncode,
        "stderr_tail": proc.stderr[-1500:],
    }
    try:
        modules = json.loads(proc.stdout).get("modules", [])
    except (ValueError, AttributeError):
        out["stdout_tail"] = proc.stdout[-1500:]
        modules = []
    out["modules"] = modules
    out["status_by_module"] = {m.get("module_id"): m.get("status") for m in modules}
    counts: dict[str, int] = {}
    for m in modules:
        counts[m.get("status")] = counts.get(m.get("status"), 0) + 1
    out["status_counts"] = counts
    out["error_entries_without_message"] = sorted(
        m.get("module_id") for m in modules if m.get("status") == "error" and not m.get("error")
    )
    out["error_entry_keys"] = sorted({k for m in modules if m.get("status") == "error" for k in m})
    return out


def runner_loop(url: str) -> dict[str, Any]:
    """The CLI's own loop (cli/commands/storage.py run_storage_migrate), in process."""
    from openminion.cli.commands import storage as storage_cli
    from openminion.modules.storage.migrations.runner import MigrationRunner

    to_run, skipped = storage_cli._get_validated_module_ids("postgres", None)
    engine = storage_cli._get_postgres_engine(url)
    rows = []
    try:
        for module_id in to_run:
            runner: MigrationRunner = storage_cli._make_runner(
                module_id, ":memory:", backend_type="postgres", engine=engine
            )
            report = runner.migrate(target="head")
            rows.append(
                {
                    "module_id": module_id,
                    "success": report.success,
                    "rolled_back": report.rolled_back,
                    "alembic_revision_before": report.before.alembic_revision,
                    "alembic_revision_after": report.after.alembic_revision,
                    "error": first_line(report.error or ""),
                }
            )
    finally:
        engine.dispose()
    return {"order": to_run, "skipped": skipped, "reports": rows}


def rerun_message(url: str, module_id: str) -> dict[str, Any]:
    """One in-process MigrationRunner.migrate() for a module the CLI reported as `error`."""
    from openminion.cli.commands import storage as storage_cli

    engine = storage_cli._get_postgres_engine(url)
    try:
        runner = storage_cli._make_runner(module_id, ":memory:", backend_type="postgres", engine=engine)
        report = runner.migrate(target="head")
        return {
            "success": report.success,
            "rolled_back": report.rolled_back,
            "alembic_revision_before": report.before.alembic_revision,
            "error": first_line(report.error or ""),
        }
    finally:
        engine.dispose()


def cli_with_messages(root: Path, url: str, extra: list[str]) -> dict[str, Any]:
    out = run_cli(root, url, extra)
    out["in_process_rerun_for_errors"] = {
        m: rerun_message(url, m) for m, status in out["status_by_module"].items() if status == "error"
    }
    return out


# ---------------------------------------------------------------- cases
def case_runtime_only(work: Path, label: str) -> dict[str, Any]:
    name = f"om_{label}_rt_only"
    url = create_database(name)
    result = runtime_storage(work / "runtime_only", url)
    result["database_after"] = database_facts(name)
    return result


def case_module_alone(work: Path, label: str) -> dict[str, Any]:
    from openminion.cli.commands import storage as storage_cli

    to_run, _ = storage_cli._get_validated_module_ids("postgres", None)
    out: dict[str, Any] = {}
    for module_id in to_run:
        name = f"om_{label}_alone_{module_id}"[:60]
        url = create_database(name)
        cli = run_cli(work / f"alone_{module_id}", url, ["--module", module_id])
        facts = database_facts(name)
        out[module_id] = {
            "exit_code": cli["exit_code"],
            "status": cli["status_by_module"].get(module_id),
            "cli_entry": (cli["modules"] or [None])[0],
            "alembic_version_rows": facts["alembic_version_rows"],
            "public_tables": facts["public_tables"],
        }
        if out[module_id]["status"] == "error":
            out[module_id]["in_process_rerun"] = rerun_message(url, module_id)
    return out


def case_cli_migrate_all(work: Path, label: str) -> dict[str, Any]:
    name = f"om_{label}_cli_all"
    url = create_database(name)
    result = {"cli": run_cli(work / "cli_all", url, [])}
    result["database_after"] = database_facts(name)
    return result


def case_runner_messages(work: Path, label: str) -> dict[str, Any]:
    name = f"om_{label}_runner"
    url = create_database(name)
    result = runner_loop(url)
    result["database_after"] = database_facts(name)
    return result


def case_migrate_then_runtime(work: Path, label: str) -> dict[str, Any]:
    name = f"om_{label}_mig_rt"
    url = create_database(name)
    result: dict[str, Any] = {"cli": run_cli(work / "mig_rt", url, [])}
    before = database_facts(name)
    result["database_after_cli"] = before
    result["runtime_storage"] = runtime_storage(work / "mig_rt", url)
    after = database_facts(name)
    result["database_after_runtime_storage"] = after
    result["tables_changed_by_runtime_storage"] = table_diff(before, after)
    return result


def case_session_then_runtime(work: Path, label: str) -> dict[str, Any]:
    name = f"om_{label}_ses_rt"
    url = create_database(name)
    result: dict[str, Any] = {"cli": run_cli(work / "ses_rt", url, ["--module", "session"])}
    before = database_facts(name)
    result["database_after_cli"] = before
    result["runtime_storage"] = runtime_storage(work / "ses_rt", url)
    after = database_facts(name)
    result["database_after_runtime_storage"] = after
    result["tables_changed_by_runtime_storage"] = table_diff(before, after)
    return result


def case_runtime_then_migrate(work: Path, label: str) -> dict[str, Any]:
    name = f"om_{label}_rt_mig"
    url = create_database(name)
    result: dict[str, Any] = {"runtime_storage_first": runtime_storage(work / "rt_mig", url)}
    before = database_facts(name)
    result["database_after_runtime_storage"] = before
    result["cli"] = run_cli(work / "rt_mig", url, [])
    after = database_facts(name)
    result["database_after_cli"] = after
    result["tables_changed_by_cli"] = table_diff(before, after)
    result["runtime_storage_again"] = runtime_storage(work / "rt_mig", url)
    name2 = f"om_{label}_rt_run"
    url2 = create_database(name2)
    runtime_storage(work / "rt_run", url2)
    result["runner_messages_same_order"] = runner_loop(url2)
    return result


def case_pair(work: Path, label: str, first: str, second: str) -> dict[str, Any]:
    name = f"om_{label}_pair_{first}_{second}"[:60]
    url = create_database(name)
    root = work / f"pair_{first}_{second}"
    result: dict[str, Any] = {"first": cli_with_messages(root, url, ["--module", first])}
    result["database_after_first"] = database_facts(name)
    result["second"] = cli_with_messages(root, url, ["--module", second])
    result["database_after_second"] = database_facts(name)
    result["tables_changed_by_second"] = table_diff(result["database_after_first"], result["database_after_second"])
    return result


def case_runtime_then_session(work: Path, label: str) -> dict[str, Any]:
    name = f"om_{label}_rt_ses"
    url = create_database(name)
    result: dict[str, Any] = {"runtime_storage_first": runtime_storage(work / "rt_ses", url)}
    before = database_facts(name)
    result["database_after_runtime_storage"] = before
    result["cli"] = cli_with_messages(work / "rt_ses", url, ["--module", "session"])
    after = database_facts(name)
    result["database_after_cli"] = after
    result["tables_changed_by_cli"] = table_diff(before, after)
    return result


def own_tables_check(report: dict[str, Any]) -> None:
    """For each CLI `ok`, are the module's own tables (from module_alone) present afterwards?"""
    alone = report["cases"].get("module_alone") or {}
    own = {
        m: sorted(set(v.get("public_tables") or []) - {"alembic_version", "om_meta"})
        for m, v in alone.items() if isinstance(v, dict) and v.get("status") == "ok"
    }
    report["module_own_tables"] = own
    for case in report["cases"].values():
        if not isinstance(case, dict):
            continue
        for cli_key, facts_key in (("cli", "database_after_cli"), ("cli", "database_after"),
                                   ("first", "database_after_first"), ("second", "database_after_second")):
            cli, facts = case.get(cli_key), case.get(facts_key)
            if not (isinstance(cli, dict) and isinstance(facts, dict) and "status_by_module" in cli):
                continue
            if "ok_modules_own_tables_check" in cli:
                continue
            present = set(facts["public_tables"])
            check = {}
            for module_id, status in cli["status_by_module"].items():
                if status == "ok" and module_id in own:
                    missing = [t for t in own[module_id] if t not in present]
                    check[module_id] = {"own_tables": len(own[module_id]), "missing": missing}
            cli["ok_modules_own_tables_check"] = check


def versions() -> dict[str, Any]:
    info: dict[str, Any] = {
        "openminion": getattr(openminion, "__version__", "?"),
        "openminion_file": openminion.__file__,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
    }
    for mod in ("sqlalchemy", "psycopg", "alembic"):
        try:
            info[mod] = __import__(mod).__version__
        except Exception as exc:  # noqa: BLE001
            info[mod] = f"unavailable: {type(exc).__name__}"
    import sqlalchemy as sa

    engine = sa.create_engine(db_url("postgres"), future=True)
    with engine.connect() as conn:
        info["postgres_server"] = conn.execute(sa.text("SHOW server_version")).scalar_one()
    engine.dispose()
    return info


def summary(name: str, result: dict[str, Any]) -> list[str]:
    lines = []
    if "probe_error" in result:
        return [f"CASE {name}: probe_error {result['probe_error']}"]
    if name == "module_alone":
        return [f"CASE {name}: " + json.dumps({k: (v["exit_code"], v["status"], v["alembic_version_rows"]) for k, v in result.items()}, sort_keys=True)]
    if name == "runner_messages":
        for row in result["reports"]:
            lines.append(f"CASE {name}: {row['module_id']} success={row['success']} rolled_back={row['rolled_back']} before={row['alembic_revision_before']} after={row['alembic_revision_after']} error={row['error']!r}")
        return lines
    parts = []
    for key in ("first", "second"):
        if key in result:
            v = result[key]
            parts.append(f"{key}: {v['argv'][-2]} exit={v['exit_code']} status={v['status_by_module']} entry={v['modules']}")
    for key in ("database_after_first", "database_after_second"):
        if key in result:
            f = result[key]
            parts.append(f"{key}: tables={f['public_table_count']} alembic_version={f['alembic_version_rows']} om_meta={f['om_meta']}")
    for key in ("runtime_storage_first", "cli", "runtime_storage", "runtime_storage_again"):
        if key in result:
            v = result[key]
            if "exit_code" in v:
                parts.append(f"{key}: exit={v['exit_code']} counts={v['status_counts']} no_message={len(v['error_entries_without_message'])}")
            else:
                parts.append(f"{key}: {v.get('outcome')} {v.get('exception') or v.get('current_version')}")
    for key in ("database_after", "database_after_cli", "database_after_runtime_storage"):
        if key in result:
            f = result[key]
            parts.append(f"{key}: tables={f['public_table_count']} runtime_ledger={f['runtime_ledger']} alembic_version={f['alembic_version_rows']} om_meta={f['om_meta']} conns={f['other_open_connections']}")
    return [f"CASE {name}: " + " | ".join(parts)]


def main() -> int:
    global PREFIXES
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if not PGURL:
        print("PGURL is not set", file=sys.stderr)
        return 2
    for key in STRIP_ENV:
        os.environ.pop(key, None)
    label = re.sub(r"[^a-z0-9]", "_", args.label.lower())
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    PREFIXES = _prefixes(str(work))
    report: dict[str, Any] = {"label": args.label, "versions": versions(), "cases": {}}
    log(f"PROBE migrate_ledger label={args.label} {json.dumps(clean_any(report['versions']), sort_keys=True)}")
    for name, step in (
        ("runtime_only", case_runtime_only),
        ("module_alone", case_module_alone),
        ("cli_migrate_all", case_cli_migrate_all),
        ("runner_messages", case_runner_messages),
        ("migrate_then_runtime", case_migrate_then_runtime),
        ("session_then_runtime", case_session_then_runtime),
        ("runtime_then_migrate", case_runtime_then_migrate),
        ("pair_session_then_artifact", lambda w, l: case_pair(w, l, "session", "artifact")),
        ("pair_artifact_then_registry", lambda w, l: case_pair(w, l, "artifact", "registry")),
        ("runtime_then_session", case_runtime_then_session),
    ):
        try:
            result = step(work, label)
        except Exception as exc:  # noqa: BLE001
            result = {"probe_error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        report["cases"][name] = result
        for line in summary(name, result):
            log(line)
    try:
        own_tables_check(report)
    except Exception as exc:  # noqa: BLE001
        report["own_tables_check_error"] = f"{type(exc).__name__}: {exc}"
    for name, case in report["cases"].items():
        for key in ("cli", "first", "second"):
            cli = case.get(key) if isinstance(case, dict) else None
            if isinstance(cli, dict) and "ok_modules_own_tables_check" in cli:
                log(f"OWN-TABLES {name}.{key}: " + json.dumps(cli["ok_modules_own_tables_check"], sort_keys=True))
            if isinstance(cli, dict) and cli.get("in_process_rerun_for_errors"):
                for m, r in cli["in_process_rerun_for_errors"].items():
                    log(f"RERUN {name}.{key}: {m} success={r['success']} before={r['alembic_revision_before']} error={r['error']!r}")
    alone = report["cases"].get("module_alone") or {}
    for m, v in alone.items():
        if isinstance(v, dict) and v.get("in_process_rerun"):
            log(f"RERUN module_alone: {m} error={v['in_process_rerun']['error']!r}")
    Path(args.out).write_text(
        json.dumps(clean_any(report), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
