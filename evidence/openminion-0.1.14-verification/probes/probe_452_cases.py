"""#452 on the installed 0.1.14: the published backup and rollback steps on single
databases with -journal/-wal/-shm companions, including the maintainer's
hot-DELETE-journal reproduction from the #453 review.

Based on round 3's probe_rollback_companions.py (evidence/openminion-backup-scope-
typed-save/round3). The steps now come from docs/runtime-database-upgrades.md at tag
v0.1.14 (downloaded, SHA-256 checked; every command appears verbatim in it), and the
control is the 0.1.13 guidance, which says to restore the matching backup and says
nothing about companions or checks. Each case builds `data/module.db` (table
`marker`, value `backup`) and `data/notes.json`, backs them up as documented, then:

  M  the maintainer's reproduction, run verbatim (control), and the same setup
     rolled back with the documented steps, by copy and by `openminion storage restore`
  J  a hot DELETE -journal after a post-backup commit (the review's crashed writer)
  W  a -wal/-shm with a commit, left by a WAL writer that exited without closing
  O  a -wal/-shm with a newer commit among the other-files backup
  B  companions next to the database while the backup is taken (whether a backup
     method leaves companions behind is recorded, not asserted), and the zero-byte
     -wal/-shm the macOS sqlite3 shell leaves, present at the other-files copy
  V  a backup that fails its recorded SHA-256, and one that matches it but fails
     `PRAGMA integrity_check`: rollback stops before anything moves

Cases J, W, O, V run for an accepted file (application_id 0: `openminion storage
backup` / `openminion storage restore`) and a refused one (the artifact module's
application_id: the documented Python one-liner / a copy of the backup).

Usage: python -I -X utf8 probe_452_cases.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, sanitize_text, write_result  # noqa: E402
import probe_452_lib as L  # noqa: E402

MODULE_APP_ID = 0x4F4D0002  # artifact: `openminion storage backup` refuses it

# The reproduction in the maintainer's #453 review (CHANGES_REQUESTED, 2026-10-28T02:53:37Z),
# verbatim. It runs as its own process from a new directory and creates its database there.
MAINTAINER_REPRO = '''"""Run only against this probe's new temporary SQLite databases."""
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix='journal-repro-', dir='.'))
db, backup = root / 'module.db', root / 'backup.db'
with sqlite3.connect(db) as connection:
    connection.execute('CREATE TABLE marker (value TEXT)')
    connection.execute("INSERT INTO marker VALUES ('backup')")
with sqlite3.connect(db) as source, sqlite3.connect(backup) as target:
    source.backup(target)
with sqlite3.connect(db) as connection:
    connection.execute("UPDATE marker SET value = 'after-backup'")
subprocess.run([sys.executable, '-c', \'\'\'
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
\'\'\', str(db)], check=True)
journal = Path(str(db) + '-journal')
assert journal.is_file()
shutil.copyfile(backup, db)
with sqlite3.connect(db) as connection:
    actual = connection.execute('SELECT value FROM marker').fetchone()[0]
print(json.dumps({'journal_existed': True, 'expected': 'backup', 'actual': actual, 'root': str(root)}, indent=2))
assert actual == 'after-backup', 'Expected reproduction did not occur'
'''

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
quiet()
clear_override_env()
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
CWD = WORK / "cwd"
CWD.mkdir()
SH = L.Shell(CWD)
PY = sys.executable
info = banner("probe_452_cases", sqlite3_shell=sanitize_text(SH.sqlite3) if SH.sqlite3 else None,
              sqlite3_shell_version=SH.shell_version(), sha256sum=bool(SH.sha256sum))
DOC, doc_info = L.load_doc()
CMDS = L.doc_commands(DOC)
PY_CODE = CMDS["python_backup_code"]
c = Checks()
observed: dict[str, Any] = {"guidance": {"source": doc_info, "commands": CMDS}}
c.check("the steps followed are docs/runtime-database-upgrades.md at tag v0.1.14 (SHA-256 equal to the #453 head); "
        "every command run appears in it verbatim",
        doc_info["sha256"] == L.DOC_SHA256 and all(CMDS["verbatim"].values()) and PY_CODE is not None,
        {"sha256": doc_info["sha256"], "verbatim": CMDS["verbatim"]})


def read_marker(db: Path) -> dict[str, Any]:
    """A normal connection, as any process opens the database: SQLite rolls back a hot
    journal and reads a -wal here."""
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


class Case:
    """`data/module.db` and `data/notes.json`, their backup, recorded checksum and other files."""

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

    def take_backup(self, shell: bool = False, before_copy=None) -> dict[str, Any]:
        """Backup steps 1-4 for this database: the backup, the other files without
        companions, a recorded SHA-256. `before_copy` runs between the database backup and
        the other-files copy."""
        if shell:
            proc = SH.run([SH.sqlite3, str(self.db), f".backup '{self.backup}'"])
            how = {"method": L.CMD_SHELL_BACKUP, "rc": proc.returncode}
        elif self.variant == "accepted":
            res = SH.cli(PY, "storage", "backup", "--sqlite", str(self.db), "--output", str(self.backup))
            how = {"method": L.CMD_BACKUP, **res}
        else:
            proc = SH.run([PY, "-c", PY_CODE, str(self.db), str(self.backup)])
            how = {"method": "python -c <documented one-liner> <file> <backup>", "rc": proc.returncode}
        if before_copy is not None:
            how["before_copy"] = before_copy(self)
        copied = []  # the other files under data/, as they are, without companions
        for p in sorted(self.data.rglob("*")):
            if p.is_file() and p != self.db and not L.is_companion(p):
                target = self.other_backup / L.rel(self.data, p)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, target)
                copied.append(L.rel(self.data, p))
        self.recorded_sha = L.sha256(self.backup)
        return {**how, "other_files_copied": copied, "companions_left_next_to_db": L.companions_of(self.db)}

    def restore_file(self) -> dict[str, Any]:
        if self.variant == "accepted":
            res = SH.cli(PY, "storage", "restore", "--sqlite", str(self.db), "--snapshot", str(self.backup), "--yes")
            return {"method": L.CMD_RESTORE, "rc": res["rc"], "stdout": res["stdout"][:120],
                    "ok": res["rc"] == 0 and "Restored" in res["stdout"]}
        shutil.copyfile(self.backup, self.db)
        return {"method": "copy the backup to the file's path", "rc": 0, "ok": True}

    def control_0113(self) -> dict[str, Any]:
        """The 0.1.13 guidance: restore the matching backup. No check, nothing about companions."""
        before = L.companions_of(self.db)
        restored = self.restore_file()
        return {"companions_next_to_file": before, "restore": restored, "companions_after": L.companions_of(self.db),
                "first_read": read_marker(self.db)}

    def documented(self) -> dict[str, Any]:
        """Rollback steps 1-5 for this database."""
        step1 = L.processes_using(self.root)
        if step1["processes_using"]:
            return {"step1": step1, "stopped": "step 1"}
        step2 = {"recorded_sha256_matches": L.sha256(self.backup) == self.recorded_sha,
                 "integrity_check": L.integrity_check(SH, self.backup)}
        step2["sha256_unchanged_by_check"] = L.sha256(self.backup) == self.recorded_sha
        if not (step2["recorded_sha256_matches"] and step2["integrity_check"]["out"] == "ok"):
            return {"step1": step1, "step2": step2, "stopped": "step 2: the backup failed its check; nothing moved",
                    "aside_exists": self.aside.exists()}
        before = {Path(str(self.db) + s).name: L.sha256(Path(str(self.db) + s)) for s in ("",) + L.COMPANIONS
                  if Path(str(self.db) + s).exists()}
        moved = []
        self.aside.mkdir()
        for s in ("",) + L.COMPANIONS:
            p = Path(str(self.db) + s)
            if p.exists():
                shutil.move(str(p), str(self.aside / p.name))
                moved.append(p.name)
        intact = {n: L.sha256(self.aside / n) == before[n] for n in moved}
        restored = self.restore_file()
        copied_back, left_out = [], []
        for p in sorted(self.other_backup.rglob("*")):
            if not p.is_file():
                continue
            r = L.rel(self.other_backup, p)
            if L.is_companion(p):
                left_out.append(r)
                continue
            (self.data / r).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, self.data / r)
            copied_back.append(r)
        left = L.companions_of(self.db)
        return {"step1": {"processes_using": step1["processes_using"]}, "step2": step2,
                "step3_moved_aside_as_a_set": moved, "step3_set_byte_identical": all(intact.values()),
                "step4_restore": restored, "step4_other_files_copied_back": copied_back,
                "step4_companions_left_out": left_out, "step5_no_companion_next_to_file": left == {},
                "first_read": read_marker(self.db),
                "aside_set_read_through_copy": L.read_set_copy(self.aside / "module.db", self.root / "aside-copy",
                                                               read_marker)}


def crash(script: str, db: Path, *extra: str) -> int:
    return SH.run([PY, "-c", script, str(db), *extra]).returncode


def good(n: dict[str, Any]) -> bool:
    s2 = n.get("step2") or {}
    return (not n.get("stopped") and n["step1"]["processes_using"] == [] and s2.get("recorded_sha256_matches")
            and s2.get("sha256_unchanged_by_check") and s2["integrity_check"]["out"] == "ok"
            and n["step3_set_byte_identical"] and n["step4_restore"]["ok"] is True
            and n["step5_no_companion_next_to_file"] and n["first_read"] == {"value": "backup", "integrity": "ok",
                                                                             "spill_table": False})


# ======================================================== M: the maintainer's reproduction
mdir = WORK / "M-control"
mdir.mkdir()
proc = SH.run([PY, "-c", MAINTAINER_REPRO], cwd=mdir)
try:
    m_out = json.loads(proc.stdout)
except ValueError:
    m_out = {"stdout": sanitize_text(proc.stdout)[-400:]}
m_control = {"rc": proc.returncode, "output": {k: v for k, v in m_out.items() if k != "root"},
             "stderr_tail": sanitize_text(proc.stderr.strip())[-400:] or None}
observed["M_control_verbatim"] = m_control
c.check("M control: the maintainer's reproduction, run verbatim (the backup copied over the database with the hot "
        "-journal left next to it), still reproduces: exit 0, the first read is 'after-backup', not 'backup'",
        proc.returncode == 0 and m_out.get("actual") == "after-backup" and m_out.get("expected") == "backup",
        m_control)


def maintainer_setup(root: Path) -> tuple[Path, Path, dict[str, Any]]:
    """The reproduction's statements up to `assert journal.is_file()`, in `root`, with each
    connection closed when its block ends (rollback step 1: no process, this one included,
    keeps the database open). Backup step 4 records the backup's SHA-256."""
    root.mkdir()
    db, backup = root / "module.db", root / "backup.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE marker (value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('backup')")
    connection.close()
    with sqlite3.connect(db) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    source.close()
    target.close()
    recorded = L.sha256(backup)
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE marker SET value = 'after-backup'")
    connection.close()
    rc = crash(L.CRASH_HOT_JOURNAL, db)
    return db, backup, {"crash_rc": rc, "journal_is_file": Path(str(db) + "-journal").is_file(),
                        "companions": L.companions_of(db), "recorded_sha256": recorded}


m_runs: dict[str, Any] = {}
for how in ("copy", "storage-restore"):
    root = WORK / f"M-documented-{how}"
    db, backup, setup = maintainer_setup(root)
    run: dict[str, Any] = {"setup": setup}
    run["step1"] = L.processes_using(root)
    run["step2"] = {"recorded_sha256_matches": L.sha256(backup) == setup["recorded_sha256"],
                    "integrity_check": L.integrity_check(SH, backup)}
    before = {n: L.sha256(root / n) for n in ("module.db", "module.db-journal")}
    aside = root / "aside"
    aside.mkdir()
    moved = []
    for s in ("",) + L.COMPANIONS:
        p = Path(str(db) + s)
        if p.exists():
            shutil.move(str(p), str(aside / p.name))
            moved.append(p.name)
    run["step3"] = {"moved_aside_as_a_set": moved,
                    "byte_identical": all(L.sha256(aside / n) == before[n] for n in moved)}
    if how == "copy":
        shutil.copyfile(backup, db)
        run["step4"] = {"method": "copy the backup to the file's path", "rc": 0, "ok": True}
    else:
        res = SH.cli(PY, "storage", "restore", "--sqlite", str(db), "--snapshot", str(backup), "--yes")
        run["step4"] = {"method": L.CMD_RESTORE, "rc": res["rc"], "ok": res["rc"] == 0 and "Restored" in res["stdout"]}
    run["step5_companions"] = L.companions_of(db)
    run["first_read"] = read_marker(db)
    run["aside_set_read_through_copy"] = L.read_set_copy(aside / "module.db", root / "aside-copy", read_marker)
    m_runs[how] = run
observed["M_documented"] = m_runs
c.check("M documented: the same setup rolled back with the published steps (no process keeps the database open; the "
        "backup matches its recorded SHA-256 and integrity_check is ok; module.db and its hot -journal move aside as a "
        "set; the backup is put in place by copy, or by `openminion storage restore` since application_id is 0; no "
        "companion is left): the first read is 'backup'",
        all(r["setup"]["journal_is_file"] and r["step1"]["processes_using"] == []
            and r["step2"]["recorded_sha256_matches"] and r["step2"]["integrity_check"]["out"] == "ok"
            and r["step3"]["moved_aside_as_a_set"] == ["module.db", "module.db-journal"] and r["step3"]["byte_identical"]
            and r["step4"]["ok"] and r["step5_companions"] == {}
            and r["first_read"] == {"value": "backup", "integrity": "ok", "spill_table": False}
            for r in m_runs.values()),
        {k: {"moved": r["step3"]["moved_aside_as_a_set"], "restore": r["step4"]["method"], "first_read": r["first_read"]}
         for k, r in m_runs.items()})
c.check("M documented: the set moved aside is unchanged and keeps the last commit: read through a copy, SQLite rolls "
        "the hot journal back and reads 'after-backup', without the crashed writer's table",
        all(r["aside_set_read_through_copy"] == {"value": "after-backup", "integrity": "ok", "spill_table": False}
            for r in m_runs.values()),
        {k: r["aside_set_read_through_copy"] for k, r in m_runs.items()})


# ======================================================== J: hot DELETE journal
def prepare_hot_journal(case: Case) -> dict[str, Any]:
    con = sqlite3.connect(str(case.db))
    con.execute("update marker set value = 'after-backup'")
    con.commit()
    con.close()
    rc = crash(L.CRASH_HOT_JOURNAL, case.db)
    return {"after_backup_committed": "after-backup", "crashed_writer_rc": rc, "companions": L.companions_of(case.db)}


for variant in ("accepted", "refused"):
    ctx: dict[str, Any] = {}
    ctl, new = Case("J-control", variant, "delete"), Case("J", variant, "delete")
    for label, case in (("control", ctl), ("documented", new)):
        ctx[f"{label}_backup"] = case.take_backup()
        ctx[f"{label}_prepare"] = prepare_hot_journal(case)
    ctx["control_0113"] = ctl.control_0113()
    ctx["documented"] = new.documented()
    observed[f"J_hot_journal_{variant}"] = ctx
    c.check(f"J {variant} control (0.1.13 guidance): the backup is restored with the hot -journal left next to it; the "
            f"first read is 'after-backup', not the backup",
            "-journal" in ctx["control_prepare"]["companions"]
            and "-journal" in ctx["control_0113"]["companions_next_to_file"]
            and ctx["control_0113"]["restore"]["ok"] is True
            and ctx["control_0113"]["first_read"].get("value") == "after-backup",
            {"restore": ctx["control_0113"]["restore"], "first_read": ctx["control_0113"]["first_read"]})
    n = ctx["documented"]
    c.check(f"J {variant} documented: the checked backup is restored, module.db and module.db-journal move aside as an "
            f"unchanged set, no companion is left, and the first read is 'backup'",
            good(n) and n["step3_moved_aside_as_a_set"] == ["module.db", "module.db-journal"],
            {k: n.get(k) for k in ("step3_moved_aside_as_a_set", "step4_restore", "first_read")})
    c.check(f"J {variant} documented: the set moved aside, read through a copy, rolls the hot journal back to the last "
            f"commit, 'after-backup'",
            n.get("aside_set_read_through_copy") == {"value": "after-backup", "integrity": "ok", "spill_table": False},
            n.get("aside_set_read_through_copy"))


# ======================================================== W: stale -wal/-shm
def prepare_stale_wal(case: Case) -> dict[str, Any]:
    rc = crash(L.CRASH_WAL, case.db, "update marker set value = 'after-backup'")
    alone = case.root / "file-alone.db"
    shutil.copyfile(case.db, alone)
    return {"writer_rc": rc, "companions": L.companions_of(case.db), "file_alone_reads": read_marker(alone)}


for variant in ("accepted", "refused"):
    ctx = {}
    ctl, new = Case("W-control", variant, "wal"), Case("W", variant, "wal")
    for label, case in (("control", ctl), ("documented", new)):
        ctx[f"{label}_backup"] = case.take_backup()
        ctx[f"{label}_prepare"] = prepare_stale_wal(case)
    ctx["control_0113"] = ctl.control_0113()
    ctx["documented"] = new.documented()
    observed[f"W_stale_wal_{variant}"] = ctx
    c.check(f"W {variant} control (0.1.13 guidance): a WAL writer left -wal/-shm whose commit the file alone lacks "
            f"(it reads 'backup'); the backup restored next to them reads 'after-backup'",
            {"-wal", "-shm"} <= set(ctx["control_prepare"]["companions"])
            and ctx["control_prepare"]["file_alone_reads"].get("value") == "backup"
            and ctx["control_0113"]["restore"]["ok"] is True
            and ctx["control_0113"]["first_read"].get("value") == "after-backup",
            {"companions": ctx["control_prepare"]["companions"], "first_read": ctx["control_0113"]["first_read"]})
    n = ctx["documented"]
    c.check(f"W {variant} documented: the checked backup is restored ('backup', no companion next to it), and "
            f"module.db, -wal and -shm move aside as an unchanged set that, read through a copy, holds 'after-backup'",
            good(n) and sorted(n["step3_moved_aside_as_a_set"]) == ["module.db", "module.db-shm", "module.db-wal"]
            and n["aside_set_read_through_copy"].get("value") == "after-backup",
            {k: n.get(k) for k in ("step3_moved_aside_as_a_set", "first_read", "aside_set_read_through_copy")})


# ======================================================== O: companions among the other-files backup
def prepare_other_files_wal(case: Case) -> dict[str, Any]:
    rc = crash(L.CRASH_WAL, case.db, "update marker set value = 'after-backup'")
    copied = []
    for s in ("-wal", "-shm"):
        p = Path(str(case.db) + s)
        if p.exists():
            shutil.copy2(p, case.other_backup / p.name)
            copied.append(p.name)
    return {"writer_rc": rc, "companions_put_in_other_files_backup": copied}


for variant in ("accepted", "refused"):
    case = Case("O", variant, "wal")
    ctx = {"backup": case.take_backup(), "prepare": prepare_other_files_wal(case)}
    ctx["documented"] = case.documented()
    observed[f"O_other_files_companions_{variant}"] = ctx
    n = ctx["documented"]
    c.check(f"O {variant}: with a -wal/-shm holding a newer commit among the other-files backup, the documented copy-back "
            f"leaves them out, copies notes.json, and the first read is 'backup'",
            ctx["prepare"]["companions_put_in_other_files_backup"] == ["module.db-wal", "module.db-shm"] and good(n)
            and "notes.json" in n["step4_other_files_copied_back"]
            and not any(L.is_companion(Path(f)) for f in n["step4_other_files_copied_back"])
            and sorted(n["step4_companions_left_out"]) == ["module.db-shm", "module.db-wal"],
            {k: n.get(k) for k in ("step4_other_files_copied_back", "step4_companions_left_out", "first_read")})

# ======================================================== B: companions while the backup is taken
backup_time: dict[str, Any] = {}
for variant in ("accepted", "refused") + (("refused-shell",) if SH.sqlite3 else ()):
    for kind, mode in (("hot-journal", "delete"), ("wal", "wal")):
        case = (Case(f"B-shell-{kind}", "refused", mode) if variant == "refused-shell"
                else Case(f"B-{kind}", variant, mode))
        con = sqlite3.connect(str(case.db))
        con.execute("update marker set value = 'committed'")
        con.commit()
        con.close()
        rc = (crash(L.CRASH_HOT_JOURNAL, case.db) if kind == "hot-journal"
              else crash(L.CRASH_WAL, case.db, "update marker set value = 'committed-in-wal'"))
        before = L.companions_of(case.db)
        took = case.take_backup(shell=variant == "refused-shell")
        backup_time[f"{variant}/{kind}"] = {
            "writer_rc": rc, "companions_before_backup": before, "backup": took,
            "backup_reads": read_marker(case.backup), "companions_after_backup": L.companions_of(case.db),
            "other_backup_dir": sorted(L.rel(case.other_backup, p) for p in case.other_backup.rglob("*")
                                       if p.is_file())}
observed["B_backup_time"] = backup_time
observed["B_companions_left_by_backup_method"] = {k: v["companions_after_backup"] for k, v in backup_time.items()}


def leave_zero_byte_wal_shm(case: Case) -> dict[str, Any]:
    made = []
    for s in ("-wal", "-shm"):
        p = Path(str(case.db) + s)
        if not p.exists():
            p.write_bytes(b"")
            made.append(s)
    return {"simulated_zero_byte": made, "companions_at_copy": L.companions_of(case.db)}


left_zero: dict[str, Any] = {}
for variant in ("accepted", "refused"):
    case = Case("B-zero-left", variant, "wal")
    con = sqlite3.connect(str(case.db))
    con.execute("update marker set value = 'committed'")
    con.commit()
    con.close()
    took = case.take_backup(before_copy=leave_zero_byte_wal_shm)
    left_zero[variant] = {"backup": took, "backup_reads": read_marker(case.backup),
                          "other_backup_dir": sorted(L.rel(case.other_backup, p) for p in case.other_backup.rglob("*")
                                                     if p.is_file())}
observed["B_zero_byte_companions_at_copy"] = left_zero
c.check("B: when a hot -journal or a committed -wal is next to the database while the backup is taken, `openminion "
        "storage backup`, the Python one-liner and `sqlite3 <file> \".backup\"` (where the shell is installed) exit 0 "
        "and the backup holds the committed content ('committed' / 'committed-in-wal', integrity ok); the other-files "
        "copy leaves out every companion still next to the database",
        all(v["companions_before_backup"] and v["backup"].get("rc") == 0 and v["backup_reads"].get("integrity") == "ok"
            and v["backup_reads"].get("value") == ("committed" if k.endswith("hot-journal") else "committed-in-wal")
            and "notes.json" in v["backup"]["other_files_copied"] and v["other_backup_dir"] == v["backup"][
                "other_files_copied"] and not any(L.is_companion(Path(f)) for f in v["other_backup_dir"])
            for k, v in backup_time.items()),
        {k: {"before": v["companions_before_backup"], "backup": v["backup_reads"].get("value"),
             "left_by_backup": v["companions_after_backup"]} for k, v in backup_time.items()})
c.check("B: with the zero-byte -wal and -shm the macOS sqlite3 shell leaves next to the database at the other-files copy, "
        "the backup holds 'committed' (exit 0, integrity ok) and the copy leaves out both",
        all(v["backup"].get("rc") == 0 and v["backup"]["before_copy"]["companions_at_copy"] == {"-wal": 0, "-shm": 0}
            and v["backup_reads"].get("value") == "committed" and v["backup_reads"].get("integrity") == "ok"
            and "notes.json" in v["backup"]["other_files_copied"] and v["other_backup_dir"] == v["backup"][
                "other_files_copied"] and not any(L.is_companion(Path(f)) for f in v["other_backup_dir"])
            for v in left_zero.values()),
        {k: {"at_copy": v["backup"]["before_copy"]["companions_at_copy"], "other_files": v["other_backup_dir"]}
         for k, v in left_zero.items()})

# ======================================================== V: a bad backup stops the rollback
verify: dict[str, Any] = {}
for variant in ("accepted", "refused"):
    for bad in ("checksum", "integrity"):
        case = Case(f"V-{bad}", variant, "delete")
        took = case.take_backup()
        prep = prepare_hot_journal(case)
        if bad == "checksum":
            data = bytearray(case.backup.read_bytes())
            data[-100] ^= 0xFF  # one changed byte in the backup's last page
            case.backup.write_bytes(bytes(data))
            how = {"changed": "one byte; the recorded checksum is kept"}
        else:
            good_copy = case.root / "good.backup"
            shutil.copyfile(case.backup, good_copy)
            how = L.corrupt_one_page(SH, good_copy, case.backup)
            case.recorded_sha = L.sha256(case.backup)  # recorded for the corrupt file
            how["checksum"] = "recorded for the corrupt file, so only integrity_check can catch it"
        files = [case.db, Path(str(case.db) + "-journal")]
        before = {p.name: L.sha256(p) for p in files}
        run = case.documented()
        after = {p.name: L.sha256(p) for p in files if p.exists()}
        verify[f"{variant}/{bad}"] = {"how": how, "prepare": prep, "documented": run, "unchanged": before == after,
                                      "read_current_afterwards": read_marker(case.db)}
observed["V_backup_check"] = verify
c.check("V: rollback step 2 stops before anything moves when the backup no longer matches its recorded SHA-256, and "
        "when it matches but `PRAGMA integrity_check` is not ok: no aside directory, the database and its hot journal "
        "are byte-for-byte unchanged, and opening them afterwards reads 'after-backup'",
        all(v["documented"].get("stopped", "").startswith("step 2") and not v["documented"].get("aside_exists")
            and v["unchanged"] and "-journal" in v["prepare"]["companions"]
            and v["read_current_afterwards"].get("value") == "after-backup"
            and (v["documented"]["step2"]["recorded_sha256_matches"] is False if k.endswith("checksum")
                 else (v["documented"]["step2"]["recorded_sha256_matches"] is True
                       and v["documented"]["step2"]["integrity_check"]["out"] != "ok"))
            for k, v in verify.items()),
        {k: {"stopped": v["documented"].get("stopped"), "sha": v["documented"].get("step2", {}).get(
            "recorded_sha256_matches"), "integrity": str(v["documented"].get("step2", {}).get(
                "integrity_check", {}).get("out"))[:100], "unchanged": v["unchanged"]} for k, v in verify.items()})

observed["files_in_working_directory_at_end"] = sorted(p.name for p in CWD.iterdir())
s = c.summary()
write_result(OUT, {"info": info, "observed": sanitize(observed), "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
