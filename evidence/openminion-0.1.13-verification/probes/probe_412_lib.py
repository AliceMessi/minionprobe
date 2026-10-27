"""Helpers for probe_412.py: run storagectl, read and compare SQLite files.

The probe reads every database through `file:...?immutable=1` connections,
so reading never takes a lock, writes a -wal/-shm file or changes a byte of
the target or the snapshot, and it closes each connection explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CHILD = HERE / "probe_412_child.py"
STORAGECTL = ["-m", "openminion.modules.storage"]
OVERRIDE_ENV_KEYS = ("OPENMINION_HOME", "OPENMINION_DATA_ROOT", "OPENMINION_GENERATED_ROOT", "OPENMINION_CONFIG_PATH")
IDENTITY_PRAGMAS = ("application_id", "user_version", "schema_version", "page_size", "encoding", "auto_vacuum")


# ---------------------------------------------------------------- processes
def case_env(case: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in OVERRIDE_ENV_KEYS}
    env["OPENMINION_HOME"] = str(case / "home")
    env["OPENMINION_DATA_ROOT"] = str(case / "data")
    return env


def _run(cmd: list[str], env: dict[str, str]) -> dict[str, Any]:
    done = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    return {"cmd": cmd, "exit_code": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


def storagectl(env: dict[str, str], args: list[str]) -> dict[str, Any]:
    """The installed storagectl, as its own process: python -m openminion.modules.storage <args>."""
    return _run([sys.executable, "-I", "-X", "utf8", *STORAGECTL, *args], env)


def child(env: dict[str, str], mode: str, result: Path, target: Path, args: list[str] | None = None) -> dict[str, Any]:
    cmd = [sys.executable, "-I", "-X", "utf8", str(CHILD), mode, str(result), str(target)]
    if args is not None:
        cmd += ["--", *args]
    out = _run(cmd, env)
    out["result"] = json.loads(result.read_text(encoding="utf-8")) if result.exists() else None
    return out


def shown_cmd(cmd: list[str]) -> str:
    parts = ["python" if i == 0 else part for i, part in enumerate(cmd)]
    parts = ["probe_412_child.py" if part == str(CHILD) else part for part in parts]
    return " ".join(parts)


def json_payload(stdout: str) -> tuple[dict | None, str | None]:
    try:
        value = json.loads(stdout)
    except ValueError as exc:
        return None, f"stdout is not one JSON document: {exc}"
    return (value, None) if isinstance(value, dict) else (None, "stdout JSON is not an object")


def last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return ""
    line = lines[-1]
    match = re.search(r"\[WinError \d+\]", line)
    if match:  # Windows error text is localized; keep the error class and code only.
        line = line[: match.end()] + " (localized message trimmed)"
    return line


# ---------------------------------------------------------------- files
def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def files_in(folder: Path) -> list[str]:
    return sorted(p.name for p in Path(folder).iterdir() if p.is_file()) if Path(folder).exists() else []


def restore_tmp_files(folder: Path) -> list[str]:
    return sorted(p.name for p in Path(folder).glob("*.restore.tmp"))


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?immutable=1", uri=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    if isinstance(value, float):
        return repr(value)
    return value


def header(path: Path) -> dict[str, Any]:
    raw = Path(path).read_bytes()[:100]

    def be(offset: int, size: int = 4) -> int:
        return int.from_bytes(raw[offset:offset + size], "big")

    page_size = be(16, 2)
    return {
        "page_size": 65536 if page_size == 1 else page_size,
        "write_version (2 = WAL)": raw[18],
        "read_version (2 = WAL)": raw[19],
        "schema_cookie": be(40),
        "text_encoding": be(56),
        "user_version": be(60),
        "application_id": be(68),
    }


def content(path: Path) -> dict[str, Any]:
    """Everything the probe compares: bytes, header, pragmas, schema, rows, metadata, marker."""
    path = Path(path)
    out: dict[str, Any] = {"sha256": sha256(path), "bytes": path.stat().st_size, "header": header(path)}
    with closing(_ro(path)) as conn:
        out["pragmas"] = {name: conn.execute(f"PRAGMA {name}").fetchone()[0] for name in IDENTITY_PRAGMAS}
        out["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        schema = [list(row) for row in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")]
        out["schema objects"] = len(schema)
        out["schema sha256"] = hashlib.sha256(repr(schema).encode()).hexdigest()
        tables = [name for kind, name, _tbl, _sql in schema if kind == "table"]
        out["tables"] = tables
        rows: dict[str, dict[str, Any]] = {}
        for table in tables:
            data = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            rows[table] = {"rows": len(data), "sha256": hashlib.sha256(repr(sorted(map(repr, data))).encode()).hexdigest()}
        out["rows"] = rows
        out["total rows"] = sum(item["rows"] for item in rows.values())
        out["iterdump sha256"] = hashlib.sha256("\n".join(conn.iterdump()).encode()).hexdigest()
        out["om_meta"] = [list(r) for r in conn.execute("SELECT * FROM om_meta ORDER BY 1")] if "om_meta" in tables else None
        out["alembic_version"] = [list(r) for r in conn.execute("SELECT * FROM alembic_version")] if "alembic_version" in tables else None
        out["probe_marker"] = (
            [[_jsonable(v) for v in r] for r in conn.execute("SELECT * FROM probe_marker ORDER BY id")]
            if "probe_marker" in tables else None
        )
    return out


def identity(c: dict[str, Any]) -> dict[str, Any]:
    return {"pragmas": c["pragmas"], "header": c["header"], "om_meta": c["om_meta"], "alembic_version": c["alembic_version"]}


def mutate_after_backup(db: Path) -> None:
    """Make the live database differ from the snapshot in rows and schema."""
    with closing(sqlite3.connect(str(db))) as conn, conn:
        conn.execute("UPDATE probe_marker SET v = 'after-backup' WHERE id = 1")
        conn.execute("DELETE FROM probe_marker WHERE id = 2")
        conn.execute("DELETE FROM turns WHERE rowid = (SELECT max(rowid) FROM turns)")
        conn.execute("CREATE TABLE probe_after_backup(v TEXT)")
        conn.execute("INSERT INTO probe_after_backup(v) VALUES ('only in the live database')")


def differences(target: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """What differs between two content() results (empty when identical)."""
    diff: dict[str, Any] = {}
    missing = sorted(set(snapshot["tables"]) - set(target["tables"]))
    extra = sorted(set(target["tables"]) - set(snapshot["tables"]))
    if missing:
        diff["snapshot tables missing"] = missing
    if extra:
        diff["tables not in the snapshot"] = extra
    rows = {t: [target["rows"][t]["rows"], snapshot["rows"][t]["rows"]] for t in snapshot["tables"]
            if t in target["rows"] and target["rows"][t] != snapshot["rows"][t]}
    if rows:
        diff["tables whose rows differ [target rows, snapshot rows]"] = rows
    for key in ("schema sha256", "iterdump sha256", "sha256"):
        if target[key] != snapshot[key]:
            diff[key] = "differs"
    if identity(target) != identity(snapshot):
        diff["identity"] = {"target": identity(target), "snapshot": identity(snapshot)}
    if target["probe_marker"] != snapshot["probe_marker"]:
        diff["probe_marker [target, snapshot]"] = [target["probe_marker"], snapshot["probe_marker"]]
    return diff
