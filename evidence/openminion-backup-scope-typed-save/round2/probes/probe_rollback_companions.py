"""Rollback of a SQLite file from its backup when -journal/-wal/-shm companions are
next to the database: the old documented steps against the corrected ones.

Runs on the installed package only (no checkout, no repo test fixtures), in new
temporary directories under <work_dir>. Each case builds one small database
`data/module.db`, backs it up as documented, changes it, leaves companions the
way a writer that did not finish leaves them, then rolls back with:

  OLD  the steps of the PR's first version: delete <file>-wal and <file>-shm if
       they exist, put the backup in place, copy the other files back
  NEW  the corrected steps: check the backup (recorded SHA-256 and
       `PRAGMA integrity_check`), move the file with its -journal/-wal/-shm
       aside as a set, put the backup in place, copy the other files back
       without companions, check that no companion is next to the file

Each case runs for an accepted file (application_id 0: `openminion storage
backup` / `openminion storage restore`) and for a refused one (a module
application_id: the documented Python backup one-liner / a file copy).

Cases:
  J  hot DELETE journal (the maintainer's reproduction in the #453 review)
  W  stale -wal/-shm from a WAL writer that committed and exited without closing
  O  the other-files copy holds a -wal: the old steps put it back after deleting
  B  companions present while the backup is taken: SQLite consumes them
  V  a backup that does not match its recorded checksum stops the rollback
     before anything is moved

Usage: python -I -X utf8 probe_rollback_companions.py <work_dir> <out.json>
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, sanitize_text, write_result  # noqa: E402

COMPANIONS = ("-journal", "-wal", "-shm")
MODULE_APP_ID = 0x4F4D0002  # artifact: `openminion storage backup` refuses it
# The documented Python form of SQLite's backup API (docs/runtime-database-upgrades.md).
PY_BACKUP = ("import sqlite3, sys; src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); "
             "src.backup(dst); dst.close(); src.close()")

# The maintainer's crashed writer (#453 review): DELETE journal, a one-page cache so the
# uncommitted transaction spills pages into the database file, then exit without rollback.
CRASH_HOT_JOURNAL = '''
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('PRAGMA journal_mode=DELETE')
c.execute('PRAGMA cache_size=1')
c.execute('BEGIN IMMEDIATE')
c.execute("UPDATE marker SET value = 'crashed'")
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
c.execute("UPDATE marker SET value = ?", (sys.argv[2],))
c.commit()
os._exit(0)
'''

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
quiet()
clear_override_env()
info = banner("probe_rollback_companions")
c = Checks()
observed: dict[str, Any] = {}
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
CWD = WORK / "cwd"
CWD.mkdir()
BIN = Path(sys.executable).parent
OM = str(BIN / ("openminion.exe" if os.name == "nt" else "openminion"))
ENV = {k: v for k, v in os.environ.items() if k not in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT",
                                                        "OPENMINION_GENERATED_ROOT", "OPENMINION_CONFIG_PATH")}
SQLITE3 = shutil.which("sqlite3")
observed["sqlite3_shell"] = sanitize_text(SQLITE3) if SQLITE3 else None


def run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
                          cwd=str(CWD))


def om_json(*args: str) -> dict[str, Any]:
    proc = run([OM, *args])
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
    except ValueError:
        payload = None
    return {"rc": proc.returncode, "json": payload, "stderr_tail": sanitize_text(proc.stderr.strip())[-300:] or None}


def companions(db: Path) -> dict[str, int]:
    return {s: Path(str(db) + s).stat().st_size for s in COMPANIONS if Path(str(db) + s).exists()}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_marker(db: Path) -> dict[str, Any]:
    # A normal connection, as any process would open the database: SQLite rolls back a
    # hot journal and reads a -wal here.
    try:
        con = sqlite3.connect(str(db))
        try:
            value = con.execute("select value from marker").fetchone()[0]
            integrity = con.execute("pragma integrity_check").fetchone()[0]
            spill = con.execute("select count(*) from sqlite_master where name='spill'").fetchone()[0]
        finally:
            con.close()
        return {"value": value, "integrity": integrity, "spill_table": bool(spill)}
    except sqlite3.Error as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def integrity_check(path: Path) -> dict[str, Any]:
    if SQLITE3:  # the documented form: sqlite3 <backup> "PRAGMA integrity_check"
        proc = run([SQLITE3, str(path), "PRAGMA integrity_check"])
        return {"via": "sqlite3 shell", "rc": proc.returncode, "out": proc.stdout.strip()}
    con = sqlite3.connect(str(path))
    try:
        return {"via": "python sqlite3", "rc": 0, "out": con.execute("pragma integrity_check").fetchone()[0]}
    finally:
        con.close()


class Case:
    """One database `data/module.db` with its backup, recorded checksum and other files."""

    def __init__(self, name: str, variant: str, journal_mode: str) -> None:
        self.root = WORK / f"{name}-{variant}"
        self.variant = variant
        self.data = self.root / "data"
        self.db = self.data / "module.db"
        self.backup_dir = self.root / "backup"
        self.backup = self.backup_dir / "module.db.backup"
        self.other_backup = self.backup_dir / "other"
        self.aside = self.root / "aside"
        for d in (self.data, self.backup_dir, self.other_backup):
            d.mkdir(parents=True)
        con = sqlite3.connect(str(self.db))
        con.execute(f"pragma journal_mode={journal_mode}")
        if variant == "refused":
            con.execute(f"pragma application_id={MODULE_APP_ID}")
        con.execute("create table marker (value text)")
        con.execute("insert into marker values ('backup')")
        con.commit()
        con.close()
        (self.data / "notes.json").write_text('{"other": "file"}', encoding="utf-8")

    def take_backup(self, shell: bool = False) -> dict[str, Any]:
        """Backup steps 1-4: the database, the other files without companions, a checksum."""
        if shell:
            proc = run([SQLITE3, str(self.db), f".backup '{self.backup}'"])
            how = {"method": "sqlite3 <file> \".backup '<backup>'\"", "rc": proc.returncode}
        elif self.variant == "accepted":
            res = om_json("storage", "backup", "--sqlite", str(self.db), "--output", str(self.backup), "--json")
            how = {"method": "openminion storage backup --sqlite <file> --output <backup> --json", **res}
        else:
            proc = run([sys.executable, "-c", PY_BACKUP, str(self.db), str(self.backup)])
            how = {"method": "python -c <documented backup one-liner> <file> <backup>", "rc": proc.returncode}
        copied = []
        for p in sorted(self.data.iterdir()):
            if p.is_file() and p != self.db and not p.name.endswith(COMPANIONS):
                shutil.copy2(p, self.other_backup / p.name)
                copied.append(p.name)
        self.recorded_sha = sha256(self.backup)
        return {**how, "other_files_copied": copied, "companions_left_next_to_db": companions(self.db)}

    def restore_file(self) -> dict[str, Any]:
        if self.variant == "accepted":
            res = om_json("storage", "restore", "--sqlite", str(self.db), "--snapshot", str(self.backup),
                          "--yes", "--json")
            return {"method": "openminion storage restore --sqlite <file> --snapshot <backup> --yes --json",
                    "rc": res["rc"], "ok": (res["json"] or {}).get("ok")}
        shutil.copyfile(self.backup, self.db)
        return {"method": "file copy of the backup over the database", "rc": 0, "ok": True}

    def old_steps(self) -> dict[str, Any]:
        deleted = []
        for s in ("-wal", "-shm"):
            p = Path(str(self.db) + s)
            if p.exists():
                deleted.append(s)
                p.unlink()
        restored = self.restore_file()
        copied_back = []
        for p in sorted(self.other_backup.iterdir()):
            shutil.copy2(p, self.data / p.name)
            copied_back.append(p.name)
        return {"deleted": deleted, "companions_left_before_restore": companions(self.db), "restore": restored,
                "other_files_copied_back": copied_back, "companions_after": companions(self.db),
                "read": read_marker(self.db)}

    def new_steps(self) -> dict[str, Any]:
        step2 = {"recorded_sha256_matches": sha256(self.backup) == self.recorded_sha,
                 "integrity_check": integrity_check(self.backup)}
        step2["sha256_unchanged_by_check"] = sha256(self.backup) == self.recorded_sha
        if not (step2["recorded_sha256_matches"] and step2["integrity_check"]["out"] == "ok"):
            return {"step2_check": step2, "stopped": "the backup failed its check; nothing was moved",
                    "companions_now": companions(self.db)}
        moved = []
        self.aside.mkdir()
        for s in ("",) + COMPANIONS:
            p = Path(str(self.db) + s)
            if p.exists():
                shutil.move(str(p), str(self.aside / p.name))
                moved.append(p.name)
        restored = self.restore_file()
        copied_back = []
        for p in sorted(self.other_backup.iterdir()):
            if not p.name.endswith(COMPANIONS):
                shutil.copy2(p, self.data / p.name)
                copied_back.append(p.name)
        left = companions(self.db)
        return {"step2_check": step2, "moved_aside_as_a_set": moved, "restore": restored,
                "other_files_copied_back": copied_back, "step5_no_companion_next_to_file": left == {},
                "companions_after": left, "read": read_marker(self.db),
                "aside_set_read": read_marker(self.aside / "module.db")}


def crash(script: str, db: Path, *extra: str) -> int:
    return run([sys.executable, "-c", script, str(db), *extra]).returncode


def twin(name: str, variant: str, journal_mode: str, prepare) -> tuple[Case, Case, dict[str, Any]]:
    """Two identical cases: one rolled back with the old steps, one with the new steps."""
    out = {}
    cases = []
    for label in ("old", "new"):
        case = Case(f"{name}-{label}", variant, journal_mode)
        out[f"{label}_backup"] = case.take_backup()
        out[f"{label}_prepare"] = prepare(case)
        cases.append(case)
    return cases[0], cases[1], out


# ======================================================== J: hot DELETE journal
def prepare_hot_journal(case: Case) -> dict[str, Any]:
    con = sqlite3.connect(str(case.db))
    con.execute("update marker set value = 'after-backup'")
    con.commit()
    con.close()
    rc = crash(CRASH_HOT_JOURNAL, case.db)
    return {"after_backup_committed": "after-backup", "crashed_writer_rc": rc, "companions": companions(case.db)}


for variant in ("accepted", "refused"):
    old, new, ctx = twin("J", variant, "delete", prepare_hot_journal)
    ctx["old"] = old.old_steps()
    ctx["new"] = new.new_steps()
    observed[f"J_hot_journal_{variant}"] = ctx
    c.check(f"J {variant}: a crashed DELETE-journal writer leaves a hot <file>-journal; with the old steps "
            f"(no -wal/-shm to delete, backup put in place, journal left) the first read is 'after-backup', "
            f"not the backup",
            "-journal" in ctx["old_prepare"]["companions"] and ctx["old"]["companions_left_before_restore"].get("-journal")
            and ctx["old"]["read"].get("value") == "after-backup",
            {"companions": ctx["old_prepare"]["companions"], "restore": ctx["old"]["restore"],
             "read": ctx["old"]["read"]})
    n = ctx["new"]
    c.check(f"J {variant}: the new steps restore the backup: the backup matches its recorded SHA-256 and passes "
            f"integrity_check (unchanged by the check), module.db and module.db-journal move aside as a set, the "
            f"backup is put in place with no companion next to it, and the first read is 'backup'",
            n["step2_check"]["recorded_sha256_matches"] and n["step2_check"]["sha256_unchanged_by_check"]
            and n["step2_check"]["integrity_check"]["out"] == "ok"
            and n["moved_aside_as_a_set"] == ["module.db", "module.db-journal"] and n["restore"]["ok"] is True
            and n["step5_no_companion_next_to_file"] and n["read"] == {"value": "backup", "integrity": "ok",
                                                                        "spill_table": False},
            {k: n[k] for k in ("step2_check", "moved_aside_as_a_set", "restore", "read")})
    c.check(f"J {variant}: the set moved aside keeps the database's current content: opened together, SQLite "
            f"rolls the hot journal back and reads 'after-backup'",
            n["aside_set_read"] == {"value": "after-backup", "integrity": "ok", "spill_table": False},
            n["aside_set_read"])


# ======================================================== W: stale -wal/-shm
def prepare_stale_wal(case: Case) -> dict[str, Any]:
    rc = crash(CRASH_WAL, case.db, "after-backup")
    # What the database file alone holds: copy it without its companions.
    alone = case.root / "file-alone.db"
    shutil.copyfile(case.db, alone)
    return {"writer_rc": rc, "companions": companions(case.db), "file_alone_reads": read_marker(alone)}


for variant in ("accepted", "refused"):
    old, new, ctx = twin("W", variant, "wal", prepare_stale_wal)
    # The naive restore: put the backup in place and keep the companions.
    naive = Case("W-naive", variant, "wal")
    ctx["naive_backup"] = naive.take_backup()
    ctx["naive_prepare"] = prepare_stale_wal(naive)
    ctx["naive_restore"] = naive.restore_file()
    ctx["naive_read"] = read_marker(naive.db)
    ctx["old"] = old.old_steps()
    ctx["new"] = new.new_steps()
    observed[f"W_stale_wal_{variant}"] = ctx
    c.check(f"W {variant}: a WAL writer that commits and exits without closing leaves <file>-wal and <file>-shm; "
            f"the commit is only in the -wal (the file alone reads 'backup'); a backup put in place next to them "
            f"reads 'after-backup'",
            {"-wal", "-shm"} <= set(ctx["naive_prepare"]["companions"])
            and ctx["naive_prepare"]["file_alone_reads"].get("value") == "backup"
            and ctx["naive_read"].get("value") == "after-backup",
            {"companions": ctx["naive_prepare"]["companions"], "file_alone": ctx["naive_prepare"]["file_alone_reads"],
             "read": ctx["naive_read"]})
    c.check(f"W {variant}: the old steps delete -wal and -shm, so the restored file reads 'backup', but the "
            f"deleted -wal was the only copy of the 'after-backup' commit",
            ctx["old"]["deleted"] == ["-wal", "-shm"] and ctx["old"]["read"].get("value") == "backup"
            and ctx["old_prepare"]["file_alone_reads"].get("value") == "backup",
            {"deleted": ctx["old"]["deleted"], "read": ctx["old"]["read"]})
    n = ctx["new"]
    c.check(f"W {variant}: the new steps restore the backup ('backup', integrity ok, no companion next to the "
            f"file) and keep module.db, -wal and -shm aside as a set that still reads 'after-backup'",
            n["step2_check"]["recorded_sha256_matches"] and n["step2_check"]["integrity_check"]["out"] == "ok"
            and sorted(n["moved_aside_as_a_set"]) == ["module.db", "module.db-shm", "module.db-wal"]
            and n["step5_no_companion_next_to_file"] and n["read"].get("value") == "backup"
            and n["read"].get("integrity") == "ok" and n["aside_set_read"].get("value") == "after-backup",
            {k: n[k] for k in ("moved_aside_as_a_set", "restore", "read", "aside_set_read")})


# ======================================================== O: a -wal among the copied other files
def prepare_other_files_wal(case: Case) -> dict[str, Any]:
    # The old step 3 copied the other files "as they are": model a copy taken while a -wal
    # with a newer commit was next to the database.
    rc = crash(CRASH_WAL, case.db, "after-backup")
    copied = []
    for s in ("-wal", "-shm"):
        p = Path(str(case.db) + s)
        if p.exists():
            shutil.copy2(p, case.other_backup / p.name)
            copied.append(p.name)
    return {"writer_rc": rc, "companions_in_other_files_backup": copied}


for variant in ("accepted", "refused"):
    old, new, ctx = twin("O", variant, "wal", prepare_other_files_wal)
    ctx["old"] = old.old_steps()
    ctx["new"] = new.new_steps()
    observed[f"O_other_files_wal_{variant}"] = ctx
    c.check(f"O {variant}: with a -wal among the copied other files, the old steps delete the current -wal and "
            f"-shm, then copy the old ones back next to the restored file, and the first read is 'after-backup'",
            ctx["old"]["deleted"] == ["-wal", "-shm"] and "module.db-wal" in ctx["old"]["other_files_copied_back"]
            and ctx["old"]["read"].get("value") == "after-backup",
            {"copied_back": ctx["old"]["other_files_copied_back"], "read": ctx["old"]["read"]})
    n = ctx["new"]
    c.check(f"O {variant}: the new steps copy the other files back without the -wal/-shm, and the first read is "
            f"'backup'",
            n["other_files_copied_back"] == ["notes.json"] and n["step5_no_companion_next_to_file"]
            and n["read"].get("value") == "backup" and n["read"].get("integrity") == "ok",
            {k: n[k] for k in ("other_files_copied_back", "companions_after", "read")})


# ======================================================== B: companions present while the backup is taken
backup_time: dict[str, Any] = {}
for variant in ("accepted", "refused") + (("refused-shell",) if SQLITE3 else ()):
    for kind, mode in (("hot-journal", "delete"), ("wal", "wal")):
        if variant == "refused-shell":
            case = Case(f"B-shell-{kind}", "refused", mode)
        else:
            case = Case(f"B-{kind}", variant, mode)
        con = sqlite3.connect(str(case.db))
        con.execute("update marker set value = 'committed'")
        con.commit()
        con.close()
        rc = crash(CRASH_HOT_JOURNAL, case.db) if kind == "hot-journal" else crash(CRASH_WAL, case.db,
                                                                                    "committed-in-wal")
        before = companions(case.db)
        took = case.take_backup(shell=variant == "refused-shell")
        backup_time[f"{variant}/{kind}"] = {
            "writer_rc": rc, "companions_before_backup": before, "backup": took,
            "backup_reads": read_marker(case.backup), "companions_after_backup": companions(case.db),
            "database_reads_after_backup": read_marker(case.db)}
observed["B_backup_time"] = backup_time
c.check("B: when a hot -journal or a -wal is next to the database while the backup is taken, `openminion storage "
        "backup`, the Python one-liner and `sqlite3 <file> \".backup\"` (where the shell is installed) open it "
        "through SQLite: the backup holds the committed content "
        "('committed' / 'committed-in-wal', integrity ok) and no companion is left next to the database to copy",
        all(v["companions_before_backup"] and v["backup"].get("rc") == 0 and v["companions_after_backup"] == {}
            and v["backup_reads"].get("integrity") == "ok"
            and v["backup_reads"].get("value") == ("committed" if k.endswith("hot-journal") else "committed-in-wal")
            for k, v in backup_time.items()),
        {k: {"before": v["companions_before_backup"], "backup": v["backup_reads"].get("value"),
             "after": v["companions_after_backup"]} for k, v in backup_time.items()})


# ======================================================== V: a backup that does not match its checksum
verify: dict[str, Any] = {}
for variant in ("accepted", "refused"):
    case = Case("V", variant, "delete")
    took = case.take_backup()
    prep = prepare_hot_journal(case)
    data = bytearray(case.backup.read_bytes())
    data[-100] ^= 0xFF  # one changed byte in the backup's last page
    case.backup.write_bytes(bytes(data))
    files = [case.db, Path(str(case.db) + "-journal")]
    before = {p.name: sha256(p) for p in files}
    new_run = case.new_steps()
    after = {p.name: sha256(p) for p in files if p.exists()}
    verify[variant] = {"backup": took, "prepare": prep, "new": new_run, "unchanged": before == after,
                       "read_current_afterwards": read_marker(case.db)}
observed["V_backup_check"] = verify
c.check("V: a backup that no longer matches its recorded SHA-256 stops the new steps at step 2: nothing is moved, "
        "the database and its hot journal are byte-for-byte unchanged, and opening them afterwards reads "
        "'after-backup'",
        all(v["new"].get("stopped") and v["new"]["step2_check"]["recorded_sha256_matches"] is False
            and "-journal" in v["prepare"]["companions"] and not (WORK / f"V-{k}" / "aside").exists()
            and v["unchanged"] and v["read_current_afterwards"].get("value") == "after-backup"
            for k, v in verify.items()),
        {k: {"stopped": v["new"].get("stopped"), "unchanged": v["unchanged"],
             "read_current_afterwards": v["read_current_afterwards"]} for k, v in verify.items()})

observed["files_in_working_directory_at_end"] = sorted(p.name for p in CWD.iterdir())
s = c.summary()
write_result(OUT, {"info": info, "observed": sanitize(observed), "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
