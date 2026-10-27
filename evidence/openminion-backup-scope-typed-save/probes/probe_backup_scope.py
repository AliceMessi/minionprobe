"""Which SQLite files of a runtime home `openminion storage backup` accepts, and
a backup/rollback of the whole home with every writer stopped.

Runs on the installed package only (no checkout, no repo test fixtures). The
runtime home uses the default layout: one offline `echo` agent, no
`storage.path`, data root `<home>/.openminion`. Every runtime step is its own
process that exits before the next step, so no writer is running while files
are copied.

Steps:
  1 turn 1 (APIRuntime.run_turn, session + conversation id) on a fresh home
  2 inventory: every SQLite file under the data root (by file header, any
    extension), with its application_id, journal mode and side files
  3 `openminion storage backup --sqlite <file> --output <backup> --json` for
    every file: accepted vs refused, refusal message, snapshot content
  4 the other files: SQLite's backup API, from Python (the documented one-line
    command) and from the sqlite3 shell (`.backup`) where it is installed
  5 turn 2 changes the home
  6 rollback with every process stopped: delete <file>-wal/-shm, then
    `openminion storage restore` for the accepted files and a file copy for
    the others; every database equals its backup
  7 the runtime starts on the restored home: `openminion sessions show`, the
    stored working state, turn 3 continues from the restored state
  8 a -wal left by a writer that did not close (scratch copy): why the
    rollback deletes it first

Usage: python -I -X utf8 probe_backup_scope.py <work_dir> <out.json>
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

SID, CONV = "s-backup", "c-backup"
BRAIN_SID = f"{SID}::conv:{CONV}"
STORAGE_APP_ID = 0x4F4D0000
# The documented Python form of SQLite's backup API (docs/runtime-database-upgrades.md).
PY_BACKUP = ("import sqlite3, sys; src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2]); "
             "src.backup(dst); dst.close(); src.close()")
SIDE = ("-wal", "-shm", "-journal")


# ---------------------------------------------------------------- step mode
def step_main(name: str, args: dict[str, Any]) -> None:
    quiet()
    clear_override_env()
    result: dict[str, Any] = {"step": name}
    if name == "turn":
        from openminion import APIRuntime

        home = Path(args["home"])
        runtime = APIRuntime.from_config_path(str(home / "agents.json"), home_root=home)
        try:
            out = runtime.run_turn(payload={"message": args["message"], "session_id": SID, "conversation_id": CONV})
        finally:
            runtime.close()
        result.update({"run_state": out.get("run_state"), "session_id": out.get("session_id")})
    elif name == "dirty-wal":
        # A writer that does not close: commit into WAL with checkpoints off, then exit
        # without closing the connection.
        con = sqlite3.connect(args["db"])
        con.execute("pragma wal_autocheckpoint=0")
        con.execute("create table stale_wal_marker(x text)")
        con.execute("insert into stale_wal_marker values ('written after the backup')")
        con.commit()
        print("STEP-RESULT " + json.dumps({"step": name, "journal_mode": con.execute("pragma journal_mode").fetchone()[0]}),
              flush=True)
        os._exit(0)
    print("STEP-RESULT " + json.dumps(result, default=str), flush=True)


if len(sys.argv) > 1 and sys.argv[1] == "--step":
    step_main(sys.argv[2], json.loads(sys.argv[3]))
    sys.exit(0)

# ---------------------------------------------------------------- orchestrator
WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
quiet()
clear_override_env()
info = banner("probe_backup_scope")
from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config  # noqa: E402
from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS  # noqa: E402

MODULE_BY_ID: dict[int, list[str]] = {}
for _module, _app_id in MODULE_APPLICATION_IDS.items():
    MODULE_BY_ID.setdefault(_app_id, []).append(_module)

c = Checks()
observed: dict[str, Any] = {}
if WORK.exists():
    shutil.rmtree(WORK)
HOME = WORK / "home"
DATA = HOME / ".openminion"
BACKUP = WORK / "backup"
CWD = WORK / "cwd"
SCRATCH = WORK / "scratch"
for d in (HOME, BACKUP, CWD, SCRATCH):
    d.mkdir(parents=True)
BIN = Path(sys.executable).parent
OM = str(BIN / ("openminion.exe" if os.name == "nt" else "openminion"))
ENV = {k: v for k, v in os.environ.items() if k not in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT",
                                                        "OPENMINION_GENERATED_ROOT", "OPENMINION_CONFIG_PATH")}
SQLITE3 = shutil.which("sqlite3")

config = OpenMinionConfig()
config.agents = {"openminion": AgentProfileConfig(name="openminion", provider="echo", default_channel="console")}
config.default_agent = "openminion"
config.runtime.process_mode = "single-process"
config.runtime.daemon_auto_start = False
save_config(config, str(HOME / "agents.json"))
observed["config"] = {"agents": "openminion: provider echo", "storage.path": config.storage.path,
                      "home_root": "<home>", "data_root": "<home>/.openminion (default)"}


def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
                          cwd=str(CWD), **kw)


def step(name: str, **args: Any) -> dict[str, Any]:
    proc = run([sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--step", name, json.dumps(args)])
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("STEP-RESULT "):
            res = json.loads(line[len("STEP-RESULT "):])
            res["rc"] = proc.returncode
            return res
    return {"step": name, "rc": proc.returncode, "error": "no STEP-RESULT",
            "stderr_tail": sanitize_text(proc.stderr.strip())[-1500:]}


def om_json(*args: str) -> dict[str, Any]:
    proc = run([OM, *args])
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
    except ValueError:
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            payload = None
    return {"rc": proc.returncode, "json": payload, "stdout_tail": sanitize_text(proc.stdout.strip())[-400:],
            "stderr_tail": sanitize_text(proc.stderr.strip())[-400:]}


def sessions_show() -> dict[str, Any]:
    proc = run([OM, "--home-root", str(HOME), "--config", str(HOME / "agents.json"), "sessions", "show", SID, "--json"])
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = {}
    return {"rc": proc.returncode, "turns": payload.get("turns"), "id": payload.get("id"),
            "stderr_tail": sanitize_text(proc.stderr.strip())[-400:]}


def rel(p: Path) -> str:
    return p.relative_to(DATA).as_posix()


def is_sqlite(p: Path) -> bool:
    with p.open("rb") as fh:
        return fh.read(16) == b"SQLite format 3\x00"


def in_bak_dir(p: Path) -> bool:
    return any(part.endswith(".bak") for part in p.relative_to(DATA).parts[:-1])


def sqlite_files() -> list[str]:
    return [rel(p) for p in sorted(DATA.rglob("*")) if p.is_file() and not p.name.endswith(SIDE)
            and not in_bak_dir(p) and is_sqlite(p)]


def other_files() -> list[str]:
    return [rel(p) for p in sorted(DATA.rglob("*")) if p.is_file() and not p.name.endswith(SIDE)
            and not in_bak_dir(p) and not is_sqlite(p)]


def bak_files() -> list[str]:
    return [rel(p) for p in sorted(DATA.rglob("*")) if p.is_file() and in_bak_dir(p)]


def side_files() -> list[str]:
    return [rel(p) for p in sorted(DATA.rglob("*")) if p.is_file() and p.name.endswith(SIDE)]


def db_facts(path: Path) -> dict[str, Any]:
    # A normal (read-write) connection: a read-only one leaves empty -wal/-shm files behind
    # on a WAL database. Nothing is written.
    con = sqlite3.connect(str(path))
    try:
        app_id = con.execute("pragma application_id").fetchone()[0]
        dump = "\n".join(con.iterdump())
        return {"application_id": app_id, "application_id_hex": f"{app_id:#010x}",
                "module": MODULE_BY_ID.get(app_id, ["(none)"] if app_id == 0 else ["(unknown)"]),
                "user_version": con.execute("pragma user_version").fetchone()[0],
                "journal_mode": con.execute("pragma journal_mode").fetchone()[0],
                "tables": con.execute("select count(*) from sqlite_master where type='table'").fetchone()[0],
                "integrity": con.execute("pragma integrity_check").fetchone()[0],
                "digest": hashlib.sha256(f"{app_id}|{dump}".encode()).hexdigest()}
    finally:
        con.close()


def digest(path: Path) -> str:
    return db_facts(path)["digest"]


def ws_versions() -> list[int]:
    con = sqlite3.connect(str(DATA / "state" / "brain" / "sessions.db"))
    try:
        return [r[0] for r in con.execute("select version from working_state where session_id=? order by version",
                                          (BRAIN_SID,))]
    finally:
        con.close()


def backup_name(rel_path: str) -> Path:
    return BACKUP / (rel_path.replace("/", "__") + ".backup")


# ======================================================== 1. turn 1 on a fresh home
t1 = step("turn", home=str(HOME), message="first turn, before the backup")
show_t1 = sessions_show()
versions_t1 = ws_versions()
observed["1_turn1"] = {"turn": t1, "sessions_show": show_t1, "working_state_versions": versions_t1}
c.check("turn 1: APIRuntime.run_turn with the echo agent completes on a fresh home; the process exited",
        t1.get("rc") == 0 and t1.get("run_state") == "completed" and show_t1["rc"] == 0 and bool(versions_t1),
        {"turn": t1, "sessions_show_turns": show_t1["turns"], "working_state_versions": versions_t1})

# ======================================================== 2. inventory
files = sqlite_files()
facts = {f: db_facts(DATA / f) for f in files}
observed["2_inventory"] = {
    "sqlite_files": {f: {k: v for k, v in facts[f].items() if k != "digest"} for f in files},
    "other_files_under_data_root": other_files(),
    "files_in_bak_dirs": bak_files(),
    "side_files_after_writer_exit": side_files(),
    "files_in_working_directory": sorted(p.name for p in CWD.iterdir()),
    "non_sqlite_files_in_home_root": sorted(p.name for p in HOME.iterdir() if p.is_file()),
}
c.check("inventory: the SQLite files under the data root (any extension, outside *.bak dirs); no -wal, -shm or "
        "-journal next to any of them after the writer exited; integrity ok",
        len(files) > 0 and observed["2_inventory"]["side_files_after_writer_exit"] == []
        and all(facts[f]["integrity"] == "ok" for f in files),
        {f: f"{facts[f]['application_id_hex']} {','.join(facts[f]['module'])} {facts[f]['journal_mode']}"
         for f in files})

# ======================================================== 3. openminion storage backup, every file
cmd_backup: dict[str, Any] = {}
for f in files:
    target = backup_name(f)
    bak_before = set(bak_files())
    res = om_json("storage", "backup", "--sqlite", str(DATA / f), "--output", str(target), "--json")
    entry = {"rc": res["rc"], "reply": res["json"], "stderr_tail": res["stderr_tail"] or None,
             "bak_copies_added": sorted(set(bak_files()) - bak_before)}
    if res["rc"] == 0 and target.exists():
        entry["snapshot"] = {k: v for k, v in db_facts(target).items() if k in ("digest", "integrity",
                                                                                 "application_id_hex")}
    cmd_backup[f] = entry
accepted = [f for f in files if cmd_backup[f]["rc"] == 0 and (cmd_backup[f]["reply"] or {}).get("ok") is True]
refused = [f for f in files if f not in accepted]
expected_accepted = [f for f in files if facts[f]["application_id"] in (0, STORAGE_APP_ID)]
observed["3_storage_backup"] = {"results": cmd_backup, "accepted": accepted, "refused": refused}
c.check("`openminion storage backup --sqlite <file> --output <backup> --json` accepts exactly the files whose "
        "application_id is 0 or the storage module's 0x4F4D0000, and refuses every other file",
        accepted == expected_accepted and len(refused) > 0,
        {"accepted": {f: facts[f]["application_id_hex"] for f in accepted},
         "refused": {f: facts[f]["application_id_hex"] + " " + ",".join(facts[f]["module"]) for f in refused}})
c.check("every refusal exits 1 with {\"ok\": false, \"error\": \"application_id mismatch for module 'storage': "
        "expected 1330446336, found <the file's id>\"} and writes no backup",
        all(cmd_backup[f]["rc"] == 1 and cmd_backup[f]["reply"] == {
            "ok": False, "error": f"application_id mismatch for module 'storage': expected {STORAGE_APP_ID}, "
                                  f"found {facts[f]['application_id']}"} and not backup_name(f).exists()
            for f in refused),
        {f: cmd_backup[f]["reply"] for f in refused})
c.check("every accepted backup equals its database (schema, rows and application_id) and passes integrity_check; "
        "the command also leaves a copy in <file>.bak/ next to the database",
        all(cmd_backup[f]["snapshot"]["digest"] == facts[f]["digest"] and cmd_backup[f]["snapshot"]["integrity"] == "ok"
            and len(cmd_backup[f]["bak_copies_added"]) == 1 for f in accepted),
        {f: cmd_backup[f]["bak_copies_added"] for f in accepted})

# ======================================================== 4. the refused files: SQLite's backup API
api_backup: dict[str, Any] = {}
for f in refused:
    proc = run([sys.executable, "-c", PY_BACKUP, str(DATA / f), str(backup_name(f))])
    snap = db_facts(backup_name(f))
    api_backup[f] = {"python_rc": proc.returncode, "stderr": sanitize_text(proc.stderr.strip())[-300:] or None,
                     "equal": snap["digest"] == facts[f]["digest"], "integrity": snap["integrity"],
                     "application_id_hex": snap["application_id_hex"]}
c.check("python -c \"<documented sqlite3 backup one-liner>\" <file> <backup> backs up every refused file: exit 0, "
        "the backup equals the database (application_id kept) and passes integrity_check",
        all(api_backup[f]["python_rc"] == 0 and api_backup[f]["equal"] and api_backup[f]["integrity"] == "ok"
            for f in refused), api_backup)
shell: dict[str, Any] = {"sqlite3": sanitize_text(SQLITE3) if SQLITE3 else None}
if SQLITE3:
    shell["version"] = run([SQLITE3, "--version"]).stdout.strip()[:60]
    for f in refused:
        target = SCRATCH / ("shell__" + f.replace("/", "__"))
        proc = run([SQLITE3, str(DATA / f), f".backup '{target}'"])
        shell[f] = {"rc": proc.returncode, "equal": target.exists() and digest(target) == facts[f]["digest"],
                    "stderr": sanitize_text(proc.stderr.strip())[-300:] or None}
    c.check("sqlite3 <file> \".backup '<backup>'\" gives the same backup of every refused file",
            all(shell[f]["rc"] == 0 and shell[f]["equal"] for f in refused),
            {f: shell[f] for f in refused})
observed["4_backup_api"] = {"python": api_backup, "sqlite3_shell": shell,
                            "side_files_after_backups": side_files()}
backup_digest = {f: digest(backup_name(f)) for f in files}

# ======================================================== 5. turn 2 changes the home
t2 = step("turn", home=str(HOME), message="second turn, after the backup")
show_t2 = sessions_show()
versions_t2 = ws_versions()
changed = [f for f in files if digest(DATA / f) != backup_digest[f]]
added = [f for f in sqlite_files() if f not in files]
observed["5_turn2"] = {"turn": t2, "sessions_show": show_t2, "working_state_versions": versions_t2,
                       "changed_since_backup": changed, "sqlite_files_added_since_backup": added,
                       "side_files_after_writer_exit": side_files()}
c.check("turn 2 completes and changes the home after the backup: accepted and refused files differ from their "
        "backups, the session has more turns and working-state rows",
        t2.get("rc") == 0 and t2.get("run_state") == "completed" and any(f in changed for f in accepted)
        and any(f in changed for f in refused) and show_t2["turns"] > show_t1["turns"]
        and len(versions_t2) > len(versions_t1),
        {"changed": changed, "added": added, "turns": [show_t1["turns"], show_t2["turns"]],
         "working_state_rows": [len(versions_t1), len(versions_t2)]})

# ======================================================== 6. rollback, every process stopped
removed_side = []
restore: dict[str, Any] = {}
for f in added:  # a database that did not exist at backup time
    for suffix in ("",) + SIDE:
        p = Path(str(DATA / f) + suffix)
        if p.exists():
            p.unlink()
for f in files:
    for suffix in ("-wal", "-shm"):
        p = Path(str(DATA / f) + suffix)
        if p.exists():
            removed_side.append(rel(p))
            p.unlink()
    if f in accepted:
        res = om_json("storage", "restore", "--sqlite", str(DATA / f), "--snapshot", str(backup_name(f)),
                      "--yes", "--json")
        restore[f] = {"method": "openminion storage restore --sqlite <file> --snapshot <backup> --yes --json",
                      "rc": res["rc"], "reply_ok": (res["json"] or {}).get("ok")}
    else:
        shutil.copyfile(backup_name(f), DATA / f)
        restore[f] = {"method": "file copy of the backup over the database"}
after = {f: db_facts(DATA / f) for f in files}
for f in files:
    restore[f].update({"equal_to_backup": after[f]["digest"] == backup_digest[f], "integrity": after[f]["integrity"]})
observed["6_rollback"] = {"restore": restore, "side_files_removed": removed_side,
                          "sqlite_files_removed_as_added_after_backup": added,
                          "left_restore_tmp": [rel(p) for p in DATA.rglob("*.restore.tmp")]}
c.check("rollback with every process stopped: `openminion storage restore ... --yes --json` (exit 0, ok) for the "
        "accepted files and a file copy for the refused ones; every database equals its backup and passes "
        "integrity_check",
        all(restore[f].get("rc", 0) == 0 and restore[f].get("reply_ok", True) is True for f in files)
        and all(restore[f]["equal_to_backup"] and restore[f]["integrity"] == "ok" for f in files)
        and observed["6_rollback"]["left_restore_tmp"] == [],
        {f: {k: restore[f].get(k) for k in ("method", "rc", "equal_to_backup")} for f in files})

# ======================================================== 7. the runtime on the restored home
show_rb = sessions_show()
versions_rb = ws_versions()
c.check("the restored home reads as it was at the backup: `openminion sessions show` reports turn 1's count, and "
        "the stored working state has turn 1's rows",
        show_rb["rc"] == 0 and show_rb["turns"] == show_t1["turns"] and versions_rb == versions_t1,
        {"turns": {"turn1": show_t1["turns"], "turn2": show_t2["turns"], "restored": show_rb["turns"]},
         "working_state_versions": {"turn1": versions_t1, "turn2": versions_t2, "restored": versions_rb}})
t3 = step("turn", home=str(HOME), message="third turn, after the rollback")
show_t3 = sessions_show()
versions_t3 = ws_versions()
final = {f: db_facts(DATA / f) for f in files}
observed["7_after_rollback"] = {"sessions_show_restored": show_rb, "turn3": t3, "sessions_show_turn3": show_t3,
                                "working_state_versions": versions_t3,
                                "integrity_after_turn3": {f: final[f]["integrity"] for f in files},
                                "changed_by_turn3": [f for f in files if final[f]["digest"] != backup_digest[f]],
                                "side_files_after_writer_exit": side_files()}
c.check("the runtime starts on the restored home: turn 3 completes in the same conversation, continuing from "
        "turn 1's stored working state (its first row is version last-of-turn-1 + 1), the session count grows from "
        "turn 1's, and every database passes integrity_check",
        t3.get("rc") == 0 and t3.get("run_state") == "completed" and versions_t3[:len(versions_t1)] == versions_t1
        and len(versions_t3) > len(versions_t1) and versions_t3[len(versions_t1)] == versions_t1[-1] + 1
        and show_t3["turns"] == show_t1["turns"] + (show_t2["turns"] - show_t1["turns"])
        and all(final[f]["integrity"] == "ok" for f in files),
        {"turn3": t3, "turns": show_t3["turns"], "versions": versions_t3})

# ======================================================== 8. a -wal left behind (scratch copy)
demo_db_rel = next((f for f in refused if facts[f]["journal_mode"] == "wal"), None)
demo: dict[str, Any] = {"database": demo_db_rel}
if demo_db_rel:
    results = {}
    for remove_first in (False, True):
        d = SCRATCH / ("wal-demo-" + ("removed" if remove_first else "kept"))
        d.mkdir()
        db = d / Path(demo_db_rel).name
        shutil.copyfile(DATA / demo_db_rel, db)
        w = step("dirty-wal", db=str(db))
        wal = Path(str(db) + "-wal")
        wal_size = wal.stat().st_size if wal.exists() else None
        if remove_first:
            for suffix in ("-wal", "-shm"):
                Path(str(db) + suffix).unlink(missing_ok=True)
        shutil.copyfile(backup_name(demo_db_rel), db)
        try:
            got = db_facts(db)
            con = sqlite3.connect(str(db))
            try:
                marker = con.execute("select count(*) from sqlite_master where name='stale_wal_marker'").fetchone()[0]
            finally:
                con.close()
            results["removed" if remove_first else "kept"] = {
                "writer": w, "wal_bytes_left_by_writer": wal_size, "equal_to_backup": got["digest"] == backup_digest[demo_db_rel],
                "stale_marker_table_present": bool(marker), "integrity": got["integrity"]}
        except sqlite3.Error as exc:
            results["removed" if remove_first else "kept"] = {"writer": w, "wal_bytes_left_by_writer": wal_size,
                                                              "error": f"{type(exc).__name__}: {exc}"}
    demo.update(results)
    c.check("a -wal left by a writer that did not close is applied to a backup copied over the database: with the "
            "-wal kept the result is not the backup; deleting <file>-wal and <file>-shm first gives the backup "
            "(scratch copy of one refused database)",
            (results["kept"].get("wal_bytes_left_by_writer") or 0) > 0
            and not results["kept"].get("equal_to_backup", False)
            and results["removed"].get("equal_to_backup") is True
            and results["removed"].get("stale_marker_table_present") is False, results)
observed["8_wal_left_behind"] = demo
observed["files_in_working_directory_at_end"] = sorted(p.name for p in CWD.iterdir())

s = c.summary()
write_result(OUT, {"info": info, "observed": sanitize(observed), "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
