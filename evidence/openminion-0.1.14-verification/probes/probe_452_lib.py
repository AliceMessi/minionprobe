"""Helpers for the #452 probes: the published guidance text at the v0.1.14 tag,
and its SQLite runtime home backup and rollback steps, run on installed packages.

The guidance (docs/runtime-database-upgrades.md) is not in the wheel or the sdist.
`load_doc()` downloads it from the tag on GitHub and checks its SHA-256 against
the blob of the #453 head, so every command the probes run is taken from the
published text and checked to appear in it verbatim.

The probes run the installed console scripts and interpreters as subprocesses, from
an empty working directory; the orchestrator process imports openminion only for the
banner and the module application ids.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import sanitize_text as _sanitize_text  # noqa: E402

# Extra (path, token) pairs applied before _common's sanitizing, e.g. the older package's venv,
# which would otherwise read as "<VENV>0113" (its path starts with the 0.1.14 venv's).
EXTRA_LABELS: list[tuple[str, str]] = []


def sanitize_text(text: str) -> str:
    for raw, token in EXTRA_LABELS:
        for variant in {raw, raw.replace("\\", "/"), raw.replace("\\", "\\\\")}:
            text = re.sub(re.escape(variant), lambda _m, t=token: t, text, flags=re.IGNORECASE)
    return _sanitize_text(text)

REPO = "openminion/openminion"
DOC_PATH = "docs/runtime-database-upgrades.md"
DOC_URL = f"https://raw.githubusercontent.com/{REPO}/v0.1.14/{DOC_PATH}"
DOC_0113_URL = f"https://raw.githubusercontent.com/{REPO}/v0.1.13/{DOC_PATH}"
# sha256 of the file at tag v0.1.14, equal to the #453 head 2d8fd446 (git blob 62a77d88).
DOC_SHA256 = "8e4af607481ce9a0b14d08e29b5568a61895541710e9a077e473e3f46dc278f2"

COMPANIONS = ("-journal", "-wal", "-shm")
STORAGE_APP_ID = 0x4F4D0000
SQLITE_HEADER = b"SQLite format 3\x00"

# The documented commands, as the published text writes them. `doc_commands()` checks
# that each one appears verbatim in the text before a probe runs it.
CMD_BACKUP = "openminion storage backup --sqlite <file> --output <backup>"
CMD_SHELL_BACKUP = "sqlite3 <file> \".backup '<backup>'\""
CMD_INTEGRITY = "sqlite3 <backup> \"PRAGMA integrity_check\""
CMD_RESTORE = "openminion storage restore --sqlite <file> --snapshot <backup> --yes"
REFUSAL = "application_id mismatch for module 'storage': expected 1330446336, found <id>"
PY_ONELINER_RE = re.compile(r'python -c "([^"]+)" <file> <backup>')

# The crashed DELETE-journal writer of the maintainer's #453 review (verbatim, with the
# table name as a parameter so it also runs on runtime-home databases; "-" skips the
# UPDATE, so the hot journal only undoes the uncommitted spill): a one-page cache,
# so the uncommitted transaction spills pages into the database file, then exit without
# rollback. The hot <file>-journal is the only way back to the last commit.
CRASH_HOT_JOURNAL = '''
import os, sqlite3, sys
table = sys.argv[2] if len(sys.argv) > 2 else 'marker'
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=DELETE')
c.execute('PRAGMA cache_size=1')
c.execute('BEGIN IMMEDIATE')
if table != '-':
    c.execute(f"UPDATE {table} SET value = 'crashed'")
c.execute('CREATE TABLE spill (value BLOB)')
for _ in range(100):
    c.execute('INSERT INTO spill VALUES (zeroblob(4096))')
os._exit(0)
'''
# A WAL writer that commits with checkpoints off and exits without closing: the commit is
# only in <file>-wal, and <file>-shm stays.
CRASH_WAL = '''
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute(sys.argv[2])
c.commit()
os._exit(0)
'''


# ---------------------------------------------------------------- the published text
def fetch(url: str, attempts: int = 4) -> bytes:
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "minionprobe-452"})
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed https URLs
                return r.read()
        except Exception as exc:  # noqa: BLE001 - retried, then raised
            last = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"fetch failed: {url}: {last}")


def load_doc() -> tuple[str, dict[str, Any]]:
    raw = fetch(DOC_URL)
    digest = hashlib.sha256(raw).hexdigest()
    info = {"url": DOC_URL, "sha256": digest, "sha256_expected": DOC_SHA256, "bytes": len(raw)}
    if digest != DOC_SHA256:
        raise RuntimeError(f"the guidance at the tag is not the reviewed text: {info}")
    return raw.decode("utf-8"), info


def section(text: str, heading: str) -> str:
    start = text.index(f"## {heading}\n")
    nxt = text.find("\n## ", start + 4)
    return text[start: nxt if nxt != -1 else len(text)]


def doc_table(text: str) -> dict[str, dict[str, str]]:
    """The table of SQLite files in "SQLite runtime home backup": file -> owner, result."""
    rows: dict[str, dict[str, str]] = {}
    for line in section(text, "SQLite runtime home backup").splitlines():
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        for name in re.findall(r"`([^`]+)`", cells[0]):
            rows[name] = {"owner": cells[1], "storage_backup": cells[2]}
    return rows


def doc_commands(text: str) -> dict[str, Any]:
    """The commands the probes run, each checked to appear verbatim in the text."""
    body = section(text, "SQLite runtime home backup")
    flat = " ".join(body.split())  # the refusal message wraps over two lines in the text
    found = {name: " ".join(cmd.split()) in flat for name, cmd in (
        ("backup", CMD_BACKUP), ("shell_backup", CMD_SHELL_BACKUP), ("integrity", CMD_INTEGRITY),
        ("restore", CMD_RESTORE), ("refusal_message", REFUSAL), ("sha256sum", "sha256sum"))}
    m = PY_ONELINER_RE.search(body)
    return {"verbatim": found, "python_backup_code": m.group(1) if m else None}


# ---------------------------------------------------------------- processes
def bin_of(py: str, name: str) -> str:
    return str(Path(py).parent / (f"{name}.exe" if os.name == "nt" else name))


def clean_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in (
        "OPENMINION_HOME", "OPENMINION_DATA_ROOT", "OPENMINION_GENERATED_ROOT", "OPENMINION_CONFIG_PATH")}


class Shell:
    """Runs installed console scripts, interpreters and the sqlite3 shell from one cwd."""

    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.env = clean_env()
        self.sqlite3 = shutil.which("sqlite3")
        self.sha256sum = shutil.which("sha256sum")
        self.started = 0

    def run(self, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        self.started += 1
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=self.env, cwd=str(cwd or self.cwd))

    def cli(self, py: str, *args: str) -> dict[str, Any]:
        proc = self.run([bin_of(py, "openminion"), *args])
        return {"rc": proc.returncode, "stdout": sanitize_text(proc.stdout.strip())[-600:],
                "stderr_tail": sanitize_text(proc.stderr.strip())[-600:] or None}

    def shell_version(self) -> str | None:
        if not self.sqlite3:
            return None
        return self.run([self.sqlite3, "--version"]).stdout.strip()[:40]

    def step(self, py: str, script: Path, name: str, **args: Any) -> dict[str, Any]:
        """Run `<script> --step <name> <json>` in a new process of interpreter `py`."""
        proc = self.run([py, "-I", "-X", "utf8", str(script), "--step", name, json.dumps(args)])
        for line in reversed(proc.stdout.splitlines()):
            if line.startswith("STEP-RESULT "):
                res = json.loads(line[len("STEP-RESULT "):])
                res["rc"] = proc.returncode
                return res
        return {"step": name, "rc": proc.returncode, "error": "no STEP-RESULT",
                "extra_forbidden_in_stderr": "extra_forbidden" in proc.stderr,
                "stderr_last_line": sanitize_text((proc.stderr.strip().splitlines() or [""])[-1])[:300],
                "stderr_tail": sanitize_text(proc.stderr.strip())[-1500:]}


def processes_using(root: Path) -> dict[str, Any]:
    """Rollback step 1: every process (readers included) whose command line, open files or
    working directory is under `root`, other than this one. psutil is an openminion
    dependency."""
    import psutil

    me = os.getpid()
    root_n = os.path.normcase(str(root.resolve()))
    hits, scanned, denied = [], 0, 0
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        if p.pid == me:
            continue
        scanned += 1
        try:
            name = (p.info.get("name") or "").lower()
            cmd = os.path.normcase(" ".join(p.info.get("cmdline") or []))
            uses = root_n in cmd
            if not uses and (name.startswith(("python", "openminion", "sqlite3", "brainctl", "artifactctl"))):
                uses = (any(os.path.normcase(f.path).startswith(root_n) for f in p.open_files())
                        or os.path.normcase(p.cwd()).startswith(root_n))
            if uses:
                hits.append({"pid": p.pid, "name": name})
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            denied += 1
    return {"processes_using": hits, "scanned": scanned, "not_inspectable": denied}


# ---------------------------------------------------------------- files
def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_sqlite(p: Path) -> bool:
    with p.open("rb") as fh:
        return fh.read(16) == SQLITE_HEADER


def is_companion(p: Path) -> bool:
    return p.name.endswith(COMPANIONS)


def in_bak_dir(data: Path, p: Path) -> bool:
    return any(part.endswith(".bak") for part in p.relative_to(data).parts[:-1])


def rel(data: Path, p: Path) -> str:
    return p.relative_to(data).as_posix()


def sqlite_files(data: Path) -> list[str]:
    """The databases of the runtime home: SQLite files (by header, any extension) under the
    data root, outside the `<file>.bak/` snapshot directories the storage tools keep."""
    return [rel(data, p) for p in sorted(data.rglob("*")) if p.is_file() and not is_companion(p)
            and not in_bak_dir(data, p) and is_sqlite(p)]


def other_files(data: Path, dbs: list[str]) -> list[str]:
    return [rel(data, p) for p in sorted(data.rglob("*")) if p.is_file() and not is_companion(p)
            and rel(data, p) not in dbs]


def companion_files(data: Path) -> list[str]:
    return [rel(data, p) for p in sorted(data.rglob("*")) if p.is_file() and is_companion(p)]


def companions_of(db: Path) -> dict[str, int]:
    return {s: Path(str(db) + s).stat().st_size for s in COMPANIONS if Path(str(db) + s).exists()}


def tree_sha(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {rel(root, p): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}


def db_facts(path: Path, module_by_id: dict[int, list[str]] | None = None) -> dict[str, Any]:
    """Open with a normal connection, as any process would (SQLite rolls back a hot
    journal and reads a -wal here): use it only where that is intended, or on a copy."""
    con = sqlite3.connect(str(path))
    try:
        app_id = con.execute("pragma application_id").fetchone()[0]
        dump = "\n".join(con.iterdump())
        names = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
        out = {"application_id": app_id, "application_id_hex": f"{app_id:#010x}",
               "journal_mode": con.execute("pragma journal_mode").fetchone()[0],
               "integrity": con.execute("pragma integrity_check").fetchone()[0],
               "tables": len(names), "probe_tables": [n for n in names if n.startswith(("probe_", "spill"))],
               "digest": hashlib.sha256(f"{app_id}|{dump}".encode()).hexdigest()}
        if module_by_id is not None:
            out["owner"] = ",".join(module_by_id.get(app_id, ["none"] if app_id == 0 else ["(unknown)"]))
        return out
    finally:
        con.close()


def read_set_copy(db: Path, scratch: Path, reader: Callable[[Path], Any]) -> Any:
    """Copy a database with its companions into a new scratch directory and read the copy,
    so the set itself stays as it is (opening it would roll back a hot journal or
    checkpoint a -wal)."""
    scratch.mkdir(parents=True, exist_ok=False)
    target = scratch / db.name
    for s in ("",) + COMPANIONS:
        src = Path(str(db) + s)
        if src.exists():
            shutil.copyfile(src, Path(str(target) + s))
    return reader(target)


def query(db: Path, sql: str, args: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(str(db))
    try:
        return [tuple(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def integrity_check(sh: Shell, path: Path) -> dict[str, Any]:
    """Rollback step 2's documented form, `sqlite3 <backup> "PRAGMA integrity_check"`, where
    the shell is installed; the same pragma through Python's sqlite3 otherwise."""
    if sh.sqlite3:
        proc = sh.run([sh.sqlite3, str(path), "PRAGMA integrity_check"])
        return {"via": "sqlite3 shell", "rc": proc.returncode, "out": proc.stdout.strip()[:300],
                "err": sanitize_text(proc.stderr.strip())[:300] or None}
    try:
        con = sqlite3.connect(str(path))
        try:
            out = "\n".join(r[0] for r in con.execute("pragma integrity_check"))
        finally:
            con.close()
        return {"via": "python sqlite3", "rc": 0, "out": out[:300]}
    except sqlite3.Error as exc:
        return {"via": "python sqlite3", "rc": 1, "out": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- documented backup
def backup_home(sh: Shell, py: str, data: Path, dest: Path, py_code: str, *,
                module_by_id: dict[int, list[str]], before_copy: Callable[[], Any] | None = None,
                shell_copy: bool = True) -> dict[str, Any]:
    """"To back up the runtime home" steps 1-4, with every writer stopped.

    Step 1 runs `openminion storage backup --sqlite <file> --output <backup>` on every
    SQLite file: it writes a backup for the accepted ones and refuses the others. Step 2
    backs up each refused file with the documented Python one-liner in the interpreter
    that runs OpenMinion (`py`); where the sqlite3 shell is installed, its `.backup` makes
    a second copy that is compared, not used. Step 3 copies the other files under the
    data root without -journal/-wal/-shm. Step 4 records a SHA-256 of each backup in
    `SHA256SUMS` (sha256sum format)."""
    dbs = sqlite_files(data)
    out: dict[str, Any] = {"files": dbs, "db": {}, "companions_before_backup": companion_files(data)}
    for f in dbs:
        target = dest / "db" / f
        target.parent.mkdir(parents=True, exist_ok=True)
        bak = Path(str(data / f) + ".bak")
        bak_before = {p.name for p in bak.iterdir()} if bak.exists() else set()
        res = sh.cli(py, "storage", "backup", "--sqlite", str(data / f), "--output", str(target))
        entry: dict[str, Any] = {"storage_backup": res}
        added = sorted({p.name for p in bak.iterdir()} - bak_before) if bak.exists() else []
        entry["bak_copy_added"] = [rel(data, bak / n) for n in added]
        if res["rc"] == 0 and target.exists():
            entry.update({"accepted": True, "method": CMD_BACKUP})
        else:
            entry.update({"accepted": False, "backup_written_by_refused_command": target.exists()})
            if target.exists():
                target.unlink()
            proc = sh.run([py, "-c", py_code, str(data / f), str(target)])
            entry.update({"method": "python -c <documented one-liner> <file> <backup>", "python_rc": proc.returncode,
                          "python_stderr": sanitize_text(proc.stderr.strip())[-300:] or None,
                          "companions_left_by_python_backup": companions_of(data / f)})
            if shell_copy and sh.sqlite3:
                twin = dest / "shell-copy" / f
                twin.parent.mkdir(parents=True, exist_ok=True)
                sp = sh.run([sh.sqlite3, str(data / f), f".backup '{twin}'"])
                entry["shell_backup"] = {"rc": sp.returncode, "stderr": sanitize_text(sp.stderr.strip())[-300:] or None,
                                         "companions_left": companions_of(data / f), "path": twin}
        entry["companions_left_after_step"] = companions_of(data / f)
        entry["backup"] = target
        out["db"][f] = entry
    out["before_copy"] = before_copy() if before_copy else None
    out["companions_at_other_files_copy"] = companion_files(data)
    copied, skipped = [], []
    for p in sorted(data.rglob("*")):
        if not p.is_file() or rel(data, p) in dbs:
            continue
        if is_companion(p):
            skipped.append(rel(data, p))
            continue
        target = dest / "other" / rel(data, p)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        copied.append(rel(data, p))
    out["other_files_copied"] = copied
    out["companions_left_out_of_copy"] = skipped
    lines = []
    for f, entry in out["db"].items():
        entry["sha256"] = sha256(entry["backup"])
        lines.append(f"{entry['sha256']}  db/{f}\n")
    (dest / "SHA256SUMS").write_text("".join(lines), encoding="utf-8", newline="\n")
    out["sha256_recorded"] = len(lines)
    return out


# ---------------------------------------------------------------- documented rollback
def check_backups(sh: Shell, dest: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Rollback step 2: each backup matches its recorded checksum and integrity_check is ok."""
    recorded = {}
    for line in (dest / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        recorded[name[len("db/"):]] = digest
    per: dict[str, Any] = {}
    for f in manifest["db"]:
        b = dest / "db" / f
        before = sha256(b)
        integ = integrity_check(sh, b)
        per[f] = {"sha256_matches_recorded": before == recorded.get(f), "integrity_check": integ,
                  "unchanged_by_check": sha256(b) == before}
    out: dict[str, Any] = {"per_backup": per}
    if sh.sha256sum:
        proc = sh.run([sh.sha256sum, "-c", "SHA256SUMS"], cwd=dest)
        out["sha256sum_c"] = {"rc": proc.returncode,
                              "failed_lines": [x for x in proc.stdout.splitlines() if not x.endswith(": OK")]}
    out["ok"] = (all(v["sha256_matches_recorded"] and v["integrity_check"]["out"] == "ok" and v["unchanged_by_check"]
                     for v in per.values())
                 and (out.get("sha256sum_c") or {"rc": 0})["rc"] == 0)
    return out


def rollback_home(sh: Shell, py: str, home: Path, data: Path, dest: Path, manifest: dict[str, Any],
                  aside: Path) -> dict[str, Any]:
    """"To roll back" steps 1-5. Stops before anything is moved if step 1 finds a process
    using the home or step 2 finds a backup that fails its check."""
    out: dict[str, Any] = {}
    out["step1"] = processes_using(home)
    if out["step1"]["processes_using"]:
        out["stopped"] = "step 1: a process still uses the home; nothing was moved"
        return out
    out["step2"] = check_backups(sh, dest, manifest)
    if not out["step2"]["ok"]:
        out["stopped"] = "step 2: a backup failed its check; nothing was moved"
        return out
    moved, intact = [], {}
    for f in sqlite_files(data):
        for s in ("",) + COMPANIONS:
            p = Path(str(data / f) + s)
            if p.exists():
                before = sha256(p)
                target = aside / (f + s)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(target))
                moved.append(f + s)
                intact[f + s] = sha256(target) == before
    out["step3"] = {"moved_aside": moved, "moved_sets_byte_identical": all(intact.values()),
                    "aside_outside_data_root": not str(aside.resolve()).startswith(str(data.resolve()))}
    restored: dict[str, Any] = {}
    for f, entry in manifest["db"].items():
        if entry["accepted"]:
            res = sh.cli(py, "storage", "restore", "--sqlite", str(data / f), "--snapshot", str(dest / "db" / f),
                         "--yes")
            restored[f] = {"method": CMD_RESTORE, **res}
        else:
            (data / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dest / "db" / f, data / f)
            restored[f] = {"method": "copy the backup to the file's path", "rc": 0}
    copied, skipped = [], []
    for p in sorted((dest / "other").rglob("*")):
        if not p.is_file():
            continue
        r = rel(dest / "other", p)
        if is_companion(p):
            skipped.append(r)
            continue
        target = data / r
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        copied.append(r)
    out["step4"] = {"restored": restored, "other_files_copied_back": copied,
                    "companions_in_other_files_backup_left_out": skipped}
    out["step5"] = {"companions_next_to_restored_files": {f: companions_of(data / f) for f in manifest["db"]
                                                          if companions_of(data / f)},
                    "restore_tmp_left": [rel(data, p) for p in data.rglob("*.restore.tmp")]}
    return out


def corrupt_one_page(sh: Shell, src: Path, dst: Path) -> dict[str, Any]:
    """A copy of `src` that still opens as a database but fails integrity_check: the
    b-tree page type byte of one page set to an invalid value (first page that does it)."""
    data = src.read_bytes()
    page = int.from_bytes(data[16:18], "big") or 65536
    for n in range(2, len(data) // page + 1):
        bad = bytearray(data)
        bad[(n - 1) * page] = 0x00
        dst.write_bytes(bytes(bad))
        res = integrity_check(sh, dst)
        if res["out"] != "ok":
            return {"page": n, "page_size": page, "integrity_check": res}
    return {"page": None, "integrity_check": integrity_check(sh, dst)}
