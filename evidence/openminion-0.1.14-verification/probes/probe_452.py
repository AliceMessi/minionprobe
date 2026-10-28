"""#452 on the installed 0.1.14: the "SQLite runtime home backup" and "To roll back"
steps of docs/runtime-database-upgrades.md at tag v0.1.14, step by step, on a
runtime home that the installed package created and used.

The guidance is not in the wheel or the sdist. The probe downloads it from the tag
(probe_452_lib.load_doc, SHA-256 checked against the #453 head), reads the file
table from it, and runs only commands that appear verbatim in it.

The home: one offline `echo` agent (the package's built-in local provider), the
default layout (data root `<home>/.openminion`). Turns go through the installed SDK
(`APIRuntime.run_turn` with a session id and a conversation id), each turn in its
own process that exits before the next step. Non-database files come from the
installed `artifactctl ingest` (blob and `.meta.json` under `artifact/artifacts/`)
and from the `<file>.bak/` snapshots the storage tools keep.

Steps:
  1 turn 1 and artifact A on a fresh home
  2 inventory against the published table: every SQLite file (by header), owner
    (application_id), journal mode; the non-database files
  3 companions present while the backup is taken: a crashed writer's hot -journal
    on ops/plans.db (nothing committed, so the backup must equal the database), a
    committed -wal on memory/memory.audit.db, and (between backup steps 2 and 3) a
    zero-byte -wal/-shm next to artifact/index.db, as the macOS sqlite3 shell leaves
  4 backup steps 1-4 with no process using the home: `openminion storage backup`
    on every file (accepted / refused), the documented Python one-liner for the
    refused files (the sqlite3 shell's `.backup` as a cross-check where installed),
    the other files without companions, SHA256SUMS
  5 after the backup: turn 2 and artifact B, then writers that did not finish: a
    commit and a hot DELETE -journal on ops/evidence.db (accepted) and on
    state/brain/brain.db (refused, restored by copying), a committed -wal/-shm on
    memory/memory.audit.db (accepted) and artifact/index.db (refused); and a
    -wal/-shm with a newer commit among the other-files backup
  6 control: the 0.1.13 guidance (back up "the database" with
    `openminion storage backup --sqlite <path>`, roll back by restoring it), on a
    raw copy of the home
  7 a backup that fails its checksum, and one that matches its checksum but fails
    integrity_check: rollback stops at step 2, nothing in the home moves
  8 rollback steps 1-5 with the good backup; restored data and the sets moved aside
    are checked separately (the sets through copies, so they stay as moved)
  9 the runtime on the restored home: sessions, messages, working state and
    artifacts are the backup's; turn 3 continues from them (a recorder on
    SessctlAdapter.put_working_state attributes each working-state write to the
    turn-start reset or the typed save_state)

Usage: python -I -X utf8 probe_452.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, sanitize_text, write_result  # noqa: E402

SID, CONV = "s-452", "c-452"
BRAIN_SID = f"{SID}::conv:{CONV}"
NEW_FIELDS = ("turn_evidence", "turn_evidence_trace_id")
PLAIN_DICT_WRITERS = ("_reset_state_for_new_input", "_inject_gateway_system_context", "_inject_resume_task_hints")
TURN_TEXT = {1: "first turn, before the backup", 2: "second turn, after the backup",
             3: "third turn, after the rollback"}


# ---------------------------------------------------------------- step mode
def step_main(name: str, args: dict[str, Any]) -> None:
    quiet()
    clear_override_env()
    import openminion
    from openminion import APIRuntime

    result: dict[str, Any] = {"step": name, "version": openminion.__version__,
                              "openminion_file": sanitize_text(openminion.__file__)}
    records: list[dict[str, Any]] = []
    if args.get("record"):
        from openminion.modules.brain.adapters.session.runtime import SessctlAdapter

        original = SessctlAdapter.put_working_state
        pkg = str(Path(openminion.__file__).resolve().parent.parent).replace("\\", "/")

        def recording_put(self: Any, session_id: str, *, state_ref: Any = None, state_inline: Any = None) -> Any:
            frames = [f for f in traceback.extract_stack()[:-1] if "openminion" in f.filename.replace("\\", "/")]
            version = original(self, session_id, state_ref=state_ref, state_inline=state_inline)
            payload = state_inline if isinstance(state_inline, dict) else {}
            records.append({
                "session_id": session_id, "version": version, "keys": sorted(payload),
                "has_fields": [f for f in NEW_FIELDS if f in payload],
                "plain_dict_writer": next((f.name for f in reversed(frames) if f.name in PLAIN_DICT_WRITERS), None),
                "typed_save_state": any(f.name == "save_state" and f.filename.replace("\\", "/").endswith(
                    "openminion/modules/brain/state.py") for f in frames),
                "frames": [f"{Path(f.filename).resolve().as_posix().replace(pkg + '/', '')}:{f.lineno} {f.name}"
                           for f in frames[-3:]]})
            return version

        SessctlAdapter.put_working_state = recording_put  # type: ignore[method-assign]
    home = Path(args["home"])
    runtime = APIRuntime.from_config_path(str(home / "agents.json"), home_root=home)
    try:
        out = runtime.run_turn(payload={"message": args["message"], "session_id": SID, "conversation_id": CONV})
    finally:
        runtime.close()
    result.update({"run_state": out.get("run_state"), "session_id": out.get("session_id"), "puts": records})
    print("STEP-RESULT " + json.dumps(result, default=str), flush=True)


if len(sys.argv) > 1 and sys.argv[1] == "--step":
    step_main(sys.argv[2], json.loads(sys.argv[3]))
    sys.exit(0)

# ---------------------------------------------------------------- orchestrator
WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
quiet()
clear_override_env()
import probe_452_lib as L  # noqa: E402

PY = sys.executable
if WORK.exists():
    shutil.rmtree(WORK)
HOME = WORK / "home"
DATA = HOME / ".openminion"
BACKUP = WORK / "backup"
ASIDE = WORK / "aside"
CWD = WORK / "cwd"
SCRATCH = WORK / "scratch"
for d in (HOME, BACKUP, CWD, SCRATCH):
    d.mkdir(parents=True)
SH = L.Shell(CWD)
SELF = Path(__file__).resolve()
info = banner("probe_452", sqlite3_shell=sanitize_text(SH.sqlite3) if SH.sqlite3 else None,
              sqlite3_shell_version=SH.shell_version(), sha256sum=bool(SH.sha256sum))
from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config  # noqa: E402
from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS  # noqa: E402

MODULE_BY_ID: dict[int, list[str]] = {}
for _module, _app_id in MODULE_APPLICATION_IDS.items():
    MODULE_BY_ID.setdefault(_app_id, []).append(_module)

c = Checks()
observed: dict[str, Any] = {}
scratch_n = [0]


def scratch(label: str) -> Path:
    scratch_n[0] += 1
    return SCRATCH / f"{scratch_n[0]:02d}-{label}"


# ======================================================== 0. the published guidance
DOC, doc_info = L.load_doc()
TABLE = L.doc_table(DOC)
CMDS = L.doc_commands(DOC)
PY_CODE = CMDS["python_backup_code"]
# The 0.1.13 text, for the control in section 6 (whitespace normalized for the phrase checks).
DOC13 = " ".join(L.fetch(L.DOC_0113_URL).decode("utf-8").split())
DOC13_PHRASES = {p: p in DOC13 for p in (
    "Keep a pre-upgrade backup of the database. For SQLite, `openminion storage backup --sqlite <path>` takes a "
    "snapshot",
    "The brain saves each session's working state as JSON: in the runtime database's `working_state` table",
    "Rolling back means restoring the matching pre-upgrade backup with the matching older package.")}
DOC13_PHRASES["(absent) ## SQLite runtime home backup"] = "## SQLite runtime home backup" not in DOC13
observed["0_guidance"] = {"source": doc_info, "table": TABLE, "commands": CMDS, "v0.1.13_phrases": DOC13_PHRASES}
c.check("the guidance followed is docs/runtime-database-upgrades.md at tag v0.1.14 (SHA-256 equal to the #453 head); "
        "its table lists 17 SQLite files, and every command this probe runs appears in it verbatim",
        doc_info["sha256"] == L.DOC_SHA256 and len(TABLE) == 17 and all(CMDS["verbatim"].values())
        and PY_CODE is not None,
        {"sha256": doc_info["sha256"], "table_files": len(TABLE), "verbatim": CMDS["verbatim"]})

config = OpenMinionConfig()
config.agents = {"openminion": AgentProfileConfig(name="openminion", provider="echo", default_channel="console")}
config.default_agent = "openminion"
config.runtime.process_mode = "single-process"
config.runtime.daemon_auto_start = False
save_config(config, str(HOME / "agents.json"))
observed["config"] = {"agents": "openminion: provider echo (built in, offline)", "storage.path": config.storage.path,
                      "home_root": "<home>", "data_root": "<home>/.openminion (default)"}


def turn(n: int, home: Path = HOME, record: bool = False) -> dict[str, Any]:
    return SH.step(PY, SELF, "turn", home=str(home), message=TURN_TEXT[n], record=record)


def artifactctl(home: Path, *args: str) -> dict[str, Any]:
    proc = SH.run([L.bin_of(PY, "artifactctl"), "--home-root", str(home), *args])
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = None
    return {"rc": proc.returncode, "json": payload, "stderr_tail": sanitize_text(proc.stderr.strip())[-300:] or None}


def sessions_show(home: Path = HOME) -> dict[str, Any]:
    proc = SH.run([L.bin_of(PY, "openminion"), "--home-root", str(home), "--config", str(home / "agents.json"),
                   "sessions", "show", SID, "--json"])
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = {}
    return {"rc": proc.returncode, "turns": payload.get("turns"), "id": payload.get("id"),
            "stderr_tail": sanitize_text(proc.stderr.strip())[-300:] or None}


def ws_rows(db: Path) -> list[tuple]:
    return L.query(db, "select version, state_inline_json from working_state where session_id=? order by version",
                   (BRAIN_SID,))


def messages(db: Path) -> list[tuple]:
    return L.query(db, "select role, body from messages where session_id=? order by created_at, append_seq", (SID,))


def keys_of(row: tuple) -> list[str]:
    return sorted(json.loads(row[1] or "{}"))


def ingest(home: Path, label: str, body: str) -> dict[str, Any]:
    src = WORK / f"{label}.txt"
    src.write_text(body, encoding="utf-8", newline="\n")
    res = artifactctl(home, "ingest", str(src), "--label", label, "--session", SID)
    return {"rc": res["rc"], "ref": ((res["json"] or {}).get("artifact") or {}).get("ref"),
            "stderr_tail": res["stderr_tail"]}


# ======================================================== 1. turn 1 and artifact A
t1 = turn(1)
art_a = ingest(HOME, "artifact-a", "artifact A, ingested before the backup\n")
verify_t1 = artifactctl(HOME, "verify", "--all")
show_t1 = sessions_show()
rows_t1 = ws_rows(DATA / "state" / "brain" / "sessions.db")
msgs_t1 = messages(DATA / "state" / "openminion.db")
observed["1_turn1"] = {"turn": t1, "artifact_a": art_a, "artifact_verify": verify_t1["json"],
                       "sessions_show": show_t1, "working_state_versions": [r[0] for r in rows_t1],
                       "messages": msgs_t1}
c.check("turn 1 (installed SDK, own process, echo provider) completes on a fresh home and the process exits; "
        "`artifactctl ingest` stores artifact A and `artifactctl verify --all` is ok",
        t1.get("rc") == 0 and t1.get("run_state") == "completed" and t1.get("version") == info["openminion.__version__"]
        and show_t1["rc"] == 0 and bool(rows_t1) and art_a["rc"] == 0 and bool(art_a["ref"])
        and verify_t1["rc"] == 0 and ((verify_t1["json"] or {}).get("report") or {}).get("failed") == 0,
        {"turn": {k: t1.get(k) for k in ("rc", "run_state", "version", "openminion_file")},
         "sessions_show_turns": show_t1["turns"], "working_state_versions": [r[0] for r in rows_t1],
         "artifact_a": art_a["ref"], "verify": (verify_t1["json"] or {}).get("report")})

# ======================================================== 2. inventory against the published table
DBS = L.sqlite_files(DATA)
FACTS = {f: L.db_facts(DATA / f, MODULE_BY_ID) for f in DBS}
OTHERS = L.other_files(DATA, DBS)
inventory = {f: {k: FACTS[f][k] for k in ("application_id_hex", "owner", "journal_mode", "integrity", "tables")}
             for f in DBS}
observed["2_inventory"] = {"sqlite_files": inventory, "other_files": OTHERS,
                           "companions_after_writers_exit": L.companion_files(DATA),
                           "files_in_home_root_outside_data_root": sorted(p.name for p in HOME.iterdir()
                                                                          if p.is_file()),
                           "files_in_working_directory": sorted(p.name for p in CWD.iterdir())}
table_owner_ok = {f: (TABLE.get(f, {}).get("owner") == FACTS[f]["owner"]) for f in DBS}
c.check("inventory: the SQLite files of the home (by header, any extension, outside <file>.bak/) are exactly the 17 "
        "files of the published table, each with the table's owner (application_id 0 = none, otherwise the module "
        "of MODULE_APPLICATION_IDS); no -journal/-wal/-shm after the writers exited; integrity ok",
        sorted(DBS) == sorted(TABLE) and all(table_owner_ok.values()) and observed["2_inventory"][
            "companions_after_writers_exit"] == [] and all(FACTS[f]["integrity"] == "ok" for f in DBS),
        {f: f"{FACTS[f]['application_id_hex']} {FACTS[f]['owner']} {FACTS[f]['journal_mode']}" for f in DBS})
blobs = [f for f in OTHERS if f.startswith("artifact/artifacts/")]
c.check("inventory: the non-database files under the data root are artifact A's blobs and .meta.json files under "
        "artifact/artifacts/ and the <file>.bak/ snapshots the storage tools keep",
        len(blobs) >= 2 and any(f.endswith(".meta.json") for f in blobs)
        and all(f in blobs or ".bak/" in f for f in OTHERS),
        {"other_files": OTHERS})

# ======================================================== 3. companions present while the backup is taken
PRE_HOT = "ops/plans.db"            # accepted, DELETE journal
PRE_WAL = "memory/memory.audit.db"  # accepted, WAL
ZERO = "artifact/index.db"          # refused, WAL
digest_before_crash = FACTS[PRE_HOT]["digest"]
pre = {PRE_HOT: {"journal_mode": FACTS[PRE_HOT]["journal_mode"],
                 "crash_rc": SH.run([PY, "-c", L.CRASH_HOT_JOURNAL, str(DATA / PRE_HOT), "-"]).returncode,
                 "companions": L.companions_of(DATA / PRE_HOT)},
       PRE_WAL: {"journal_mode": FACTS[PRE_WAL]["journal_mode"],
                 "writer_rc": SH.run([PY, "-c", L.CRASH_WAL, str(DATA / PRE_WAL),
                                      "create table probe_wal_before_backup as select 'committed-in-wal' as value"]
                                     ).returncode,
                 "companions": L.companions_of(DATA / PRE_WAL)}}
def file_alone(p: Path) -> list[str]:
    for s in ("-wal", "-shm"):
        Path(str(p) + s).unlink(missing_ok=True)
    return L.db_facts(p)["probe_tables"]


pre[PRE_WAL]["file_alone"] = L.read_set_copy(DATA / PRE_WAL, scratch("pre-wal-file-alone"), file_alone)
pre[PRE_WAL]["with_wal"] = L.read_set_copy(DATA / PRE_WAL, scratch("pre-wal-set"), lambda p: L.db_facts(p)["probe_tables"])
observed["3_companions_at_backup"] = pre
c.check("before the backup: a crashed DELETE-journal writer leaves a hot -journal on ops/plans.db (nothing committed), "
        "and a WAL writer leaves memory/memory.audit.db with -wal/-shm holding a commit the file alone lacks",
        pre[PRE_HOT]["crash_rc"] == 0 and "-journal" in pre[PRE_HOT]["companions"] and pre[PRE_WAL]["writer_rc"] == 0
        and {"-wal", "-shm"} <= set(pre[PRE_WAL]["companions"]) and pre[PRE_WAL]["file_alone"] == []
        and pre[PRE_WAL]["with_wal"] == ["probe_wal_before_backup"], pre)


def leave_zero_byte_companions() -> dict[str, Any]:
    """Between backup steps 2 and 3: what the macOS sqlite3 shell's `.backup` leaves."""
    made = []
    for s in ("-wal", "-shm"):
        p = Path(str(DATA / ZERO) + s)
        if not p.exists():
            p.write_bytes(b"")
            made.append(s)
    return {"db": ZERO, "made": made, "companions": L.companions_of(DATA / ZERO)}


# ======================================================== 4. backup steps 1-4
users_before_backup = L.processes_using(HOME)
B = L.backup_home(SH, PY, DATA, BACKUP, PY_CODE, module_by_id=MODULE_BY_ID, before_copy=leave_zero_byte_companions)
ACCEPTED = [f for f, e in B["db"].items() if e["accepted"]]
REFUSED = [f for f, e in B["db"].items() if not e["accepted"]]
BACKUP_FACTS = {f: L.db_facts(BACKUP / "db" / f) for f in DBS}
observed["4_backup"] = {"processes_using_home": users_before_backup,
                        "storage_backup": {f: B["db"][f]["storage_backup"] for f in DBS},
                        "accepted": ACCEPTED, "refused": REFUSED,
                        "python_backup": {f: {k: B["db"][f].get(k) for k in ("python_rc", "python_stderr")}
                                          for f in REFUSED},
                        "shell_backup": {f: B["db"][f].get("shell_backup") for f in REFUSED},
                        "companions_left_by_each_method": {f: B["db"][f]["companions_left_after_step"] for f in DBS
                                                           if B["db"][f]["companions_left_after_step"]},
                        "before_copy": B["before_copy"], "companions_at_other_files_copy": B["companions_at_other_files_copy"],
                        "other_files_copied": B["other_files_copied"],
                        "companions_left_out_of_copy": B["companions_left_out_of_copy"],
                        "sha256_recorded": B["sha256_recorded"]}
c.check("backup: no process uses the home (psutil: command line, open files, working directory)",
        users_before_backup["processes_using"] == [], users_before_backup)
c.check("backup step 1: `openminion storage backup --sqlite <file> --output <backup>` accepts exactly the files the "
        "table marks accepted (exit 0, 'Backup created'); for every refused one it exits 1 with 'application_id "
        "mismatch for module 'storage': expected 1330446336, found <the file's id>' and writes no backup",
        sorted(ACCEPTED) == sorted(f for f in TABLE if TABLE[f]["storage_backup"] == "accepted")
        and sorted(REFUSED) == sorted(f for f in TABLE if TABLE[f]["storage_backup"] == "refused")
        and all("Backup created" in B["db"][f]["storage_backup"]["stdout"] for f in ACCEPTED)
        and all(B["db"][f]["storage_backup"]["rc"] == 1 and not B["db"][f]["backup_written_by_refused_command"]
                and f"application_id mismatch for module 'storage': expected 1330446336, found "
                    f"{FACTS[f]['application_id']}" in (B["db"][f]["storage_backup"]["stdout"]
                                                         + str(B["db"][f]["storage_backup"]["stderr_tail"]))
                for f in REFUSED),
        {"accepted": ACCEPTED, "refused": {f: B["db"][f]["storage_backup"]["stdout"] for f in REFUSED}})
c.check("backup step 1: each accepted backup also leaves one copy of the snapshot in <file>.bak/ next to the "
        "database, with the backup's content; the refused command adds none",
        all(len(B["db"][f]["bak_copy_added"]) == 1
            and L.db_facts(DATA / B["db"][f]["bak_copy_added"][0])["digest"] == BACKUP_FACTS[f]["digest"]
            for f in ACCEPTED) and all(B["db"][f]["bak_copy_added"] == [] for f in REFUSED),
        {f: B["db"][f]["bak_copy_added"] for f in ACCEPTED})
c.check("backup step 2: the documented Python one-liner, in the interpreter that runs OpenMinion, backs up every "
        "refused file (exit 0, the backup keeps the module's application_id); where the sqlite3 shell is installed, "
        "its `.backup` gives the same content",
        all(B["db"][f]["python_rc"] == 0 and BACKUP_FACTS[f]["application_id"] == FACTS[f]["application_id"]
            for f in REFUSED)
        and (not SH.sqlite3 or all(B["db"][f]["shell_backup"]["rc"] == 0
                                   and L.db_facts(B["db"][f]["shell_backup"]["path"])["digest"]
                                   == BACKUP_FACTS[f]["digest"] for f in REFUSED)),
        {"sqlite3_shell": info.get("sqlite3_shell_version"), "refused": len(REFUSED)})
unchanged = [f for f in DBS if f not in (PRE_HOT, PRE_WAL)]
c.check("backups hold the committed content: every backup passes integrity_check; ops/plans.db's backup equals the "
        "database before the crashed writer (its hot -journal undone), memory/memory.audit.db's holds the commit that "
        "was only in its -wal, and every other backup equals its database",
        all(BACKUP_FACTS[f]["integrity"] == "ok" for f in DBS)
        and BACKUP_FACTS[PRE_HOT]["digest"] == digest_before_crash and "spill" not in BACKUP_FACTS[PRE_HOT]["probe_tables"]
        and BACKUP_FACTS[PRE_WAL]["probe_tables"] == ["probe_wal_before_backup"]
        and all(BACKUP_FACTS[f]["digest"] == FACTS[f]["digest"] for f in unchanged),
        {PRE_HOT: {"equal_to_db_before_crash": BACKUP_FACTS[PRE_HOT]["digest"] == digest_before_crash},
         PRE_WAL: BACKUP_FACTS[PRE_WAL]["probe_tables"],
         "not_equal": [f for f in unchanged if BACKUP_FACTS[f]["digest"] != FACTS[f]["digest"]]})
copied_other = B["other_files_copied"]
c.check("backup step 3: the other files under the data root are copied as they are (artifact A's blob and "
        ".meta.json, the <file>.bak/ snapshots), leaving out every -journal/-wal/-shm present at the copy, including "
        "the zero-byte -wal/-shm next to artifact/index.db; companions left by the backup methods are recorded, "
        "not assumed absent",
        all(f in copied_other for f in OTHERS) and not any(L.is_companion(Path(f)) for f in copied_other)
        and {f"{ZERO}-wal", f"{ZERO}-shm"} <= set(B["companions_left_out_of_copy"])
        and sorted(B["companions_left_out_of_copy"]) == sorted(B["companions_at_other_files_copy"]),
        {"copied": len(copied_other), "companions_at_copy": B["companions_at_other_files_copy"],
         "left_out": B["companions_left_out_of_copy"]})
c.check("backup step 4: a SHA-256 of each of the 17 backups is recorded (SHA256SUMS, sha256sum format)",
        B["sha256_recorded"] == len(DBS) == 17, {"recorded": B["sha256_recorded"]})

# The 0.1.13 guidance's backup, for the control in section 6: `openminion storage backup --sqlite <path>`
# for "the database" (it writes into <file>.bak/; the snapshot is moved out of the data root at once).
bak_dir = DATA / "state" / "openminion.db.bak"
bak_before = set(p.name for p in bak_dir.iterdir()) if bak_dir.exists() else set()
ctl_backup = SH.cli(PY, "storage", "backup", "--sqlite", str(DATA / "state" / "openminion.db"))
new_snaps = sorted(set(p.name for p in bak_dir.iterdir()) - bak_before) if bak_dir.exists() else []
CONTROL_SNAPSHOT = WORK / "control-0113" / "openminion.db.snapshot"
CONTROL_SNAPSHOT.parent.mkdir(parents=True)
if len(new_snaps) == 1:
    shutil.move(str(bak_dir / new_snaps[0]), str(CONTROL_SNAPSHOT))
observed["4_control_0113_backup"] = {"command": "openminion storage backup --sqlite <data>/state/openminion.db",
                                     "result": ctl_backup, "snapshot_written_to_bak_dir": new_snaps}

# ======================================================== 5. after the backup
t2 = turn(2)
art_b = ingest(HOME, "artifact-b", "artifact B, ingested after the backup\n")
show_t2 = sessions_show()
rows_t2 = ws_rows(DATA / "state" / "brain" / "sessions.db")
msgs_t2 = messages(DATA / "state" / "openminion.db")
changed = [f for f in DBS if L.db_facts(DATA / f)["digest"] != BACKUP_FACTS[f]["digest"]]
c.check("after the backup, turn 2 and artifact B change the home: accepted and refused files differ from their "
        "backups, the session has more turns, messages and working-state rows",
        t2.get("rc") == 0 and t2.get("run_state") == "completed" and art_b["rc"] == 0
        and any(f in changed for f in ACCEPTED) and any(f in changed for f in REFUSED)
        and show_t2["turns"] > show_t1["turns"] and len(rows_t2) > len(rows_t1) and len(msgs_t2) > len(msgs_t1),
        {"changed": changed, "turns": [show_t1["turns"], show_t2["turns"]],
         "working_state_rows": [len(rows_t1), len(rows_t2)], "artifact_b": art_b["ref"]})

HOT_ACC, HOT_REF = "ops/evidence.db", "state/brain/brain.db"
WAL_ACC, WAL_REF = "memory/memory.audit.db", "artifact/index.db"
writers: dict[str, Any] = {}
for f in (HOT_ACC, HOT_REF):
    # As in the #453 review: a commit after the backup, then a crashed DELETE-journal writer.
    con = sqlite3.connect(str(DATA / f))
    mode = con.execute("pragma journal_mode=delete").fetchone()[0]
    con.execute("create table probe_after_backup(value text)")
    con.execute("insert into probe_after_backup values ('after-backup')")
    con.commit()
    con.close()
    rc = SH.run([PY, "-c", L.CRASH_HOT_JOURNAL, str(DATA / f), "probe_after_backup"]).returncode
    writers[f] = {"journal_mode": mode, "crash_rc": rc, "companions": L.companions_of(DATA / f)}
for f in (WAL_ACC, WAL_REF):
    rc = SH.run([PY, "-c", L.CRASH_WAL, str(DATA / f),
                 "create table probe_wal_after_backup as select 'after-backup' as value"]).returncode
    writers[f] = {"writer_rc": rc, "companions": L.companions_of(DATA / f)}
# A careless copy of the other files: -wal/-shm with the newer commit among the other-files backup.
for s in ("-wal", "-shm"):
    shutil.copyfile(Path(str(DATA / WAL_REF) + s), BACKUP / "other" / (WAL_REF + s))
observed["5_after_backup"] = {"turn2": t2, "artifact_b": art_b, "sessions_show": show_t2,
                              "working_state_versions": [r[0] for r in rows_t2], "changed_since_backup": changed,
                              "writers": writers, "companions_in_home": L.companion_files(DATA),
                              "companions_planted_in_other_files_backup": [WAL_REF + "-wal", WAL_REF + "-shm"]}
c.check("writers that did not finish leave companions after the backup: a commit then a hot -journal on "
        "ops/evidence.db (accepted) and on state/brain/brain.db (refused, switched to DELETE as in the review's "
        "reproduction); -wal/-shm with a commit on memory/memory.audit.db (accepted) and artifact/index.db (refused); "
        "and that -wal/-shm also sits among the other-files backup",
        all(writers[f]["crash_rc"] == 0 and "-journal" in writers[f]["companions"] for f in (HOT_ACC, HOT_REF))
        and all(writers[f]["writer_rc"] == 0 and {"-wal", "-shm"} <= set(writers[f]["companions"])
                for f in (WAL_ACC, WAL_REF)),
        {f: w["companions"] for f, w in writers.items()})


def marker_reader(p: Path) -> dict[str, Any]:
    facts = L.db_facts(p)
    out = {"integrity": facts["integrity"], "probe_tables": facts["probe_tables"]}
    if "probe_after_backup" in facts["probe_tables"]:
        out["probe_after_backup"] = L.query(p, "select value from probe_after_backup")[0][0]
    return out


# What each current set holds, read through copies (the sets in the home stay untouched).
current_sets = {f: L.read_set_copy(DATA / f, scratch("current-" + f.replace("/", "_")), marker_reader)
                for f in (HOT_ACC, HOT_REF, WAL_ACC, WAL_REF)}
observed["5_current_sets_read_through_copies"] = current_sets

# ======================================================== 6. control: the 0.1.13 guidance, on a raw copy
CTL_HOME = WORK / "control-0113" / "home"
shutil.copytree(HOME, CTL_HOME)
CTL_DATA = CTL_HOME / ".openminion"
ctl_restore = SH.cli(PY, "storage", "restore", "--sqlite", str(CTL_DATA / "state" / "openminion.db"), "--snapshot",
                     str(CONTROL_SNAPSHOT), "--yes")
ctl_show = sessions_show(CTL_HOME)
ctl_rows = ws_rows(CTL_DATA / "state" / "brain" / "sessions.db")
ctl_art_b = artifactctl(CTL_HOME, "show", art_b["ref"] or "-")
ctl_companions = L.companion_files(CTL_DATA)
ctl_evidence = marker_reader(CTL_DATA / HOT_ACC)
observed["6_control_0113_guidance"] = {
    "text_0113": DOC13_PHRASES,
    "runtime_database_has_working_state_table": bool(L.query(
        CTL_DATA / "state" / "openminion.db", "select count(*) from sqlite_master where name='working_state'")[0][0]),
    "backup": observed["4_control_0113_backup"], "restore": ctl_restore, "sessions_show": ctl_show,
    "working_state_versions": [r[0] for r in ctl_rows], "artifact_b_show_rc": ctl_art_b["rc"],
    "companions_left": ctl_companions, "ops_evidence_first_read": ctl_evidence}
c.check("control (0.1.13 guidance, raw copy of the home): the v0.1.13 text backs up 'the database' with `openminion "
        "storage backup --sqlite <path>`, places the working state in the runtime database and has no 'SQLite runtime "
        "home backup' section; but the runtime database has no working_state table, and restoring it from that "
        "snapshot gives turn 1's session count while the working state stays turn 2's, artifact B stays indexed, the "
        "-journal/-wal companions stay next to their files, and ops/evidence.db's first read is the post-backup commit",
        all(DOC13_PHRASES.values()) and ctl_backup["rc"] == 0 and len(new_snaps) == 1 and ctl_restore["rc"] == 0
        and not observed["6_control_0113_guidance"]["runtime_database_has_working_state_table"]
        and ctl_show["turns"] == show_t1["turns"] and [r[0] for r in ctl_rows] == [r[0] for r in rows_t2]
        and ctl_rows != rows_t1 and ctl_art_b["rc"] == 0 and f"{HOT_ACC}-journal" in ctl_companions
        and ctl_evidence.get("probe_after_backup") == "after-backup",
        {"turns": ctl_show["turns"], "working_state_versions": [r[0] for r in ctl_rows],
         "artifact_b_indexed": ctl_art_b["rc"] == 0, "companions": ctl_companions, "ops_evidence": ctl_evidence})

# ======================================================== 7. a bad backup stops before anything moves
bad_runs: dict[str, Any] = {}
for label in ("checksum", "integrity"):
    bad = WORK / f"backup-bad-{label}"
    shutil.copytree(BACKUP, bad)
    if label == "checksum":
        target = bad / "db" / HOT_REF
        data = bytearray(target.read_bytes())
        data[len(data) // 2] ^= 0xFF
        target.write_bytes(bytes(data))
        how = {"file": HOT_REF, "changed": "one byte, recorded checksum kept"}
    else:
        target = bad / "db" / "state" / "brain" / "sessions.db"
        how = {"file": "state/brain/sessions.db", **L.corrupt_one_page(SH, BACKUP / "db" / "state" / "brain" /
                                                                       "sessions.db", target)}
        lines = (bad / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        lines = [f"{L.sha256(target)}  db/state/brain/sessions.db" if ln.endswith("db/state/brain/sessions.db")
                 else ln for ln in lines]
        (bad / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        how["checksum"] = "recorded for the corrupt file, so only integrity_check can catch it"
    before = L.tree_sha(DATA)
    aside_bad = WORK / f"aside-bad-{label}"
    res = L.rollback_home(SH, PY, HOME, DATA, bad, B, aside_bad)
    after = L.tree_sha(DATA)
    failing = {f: v for f, v in (res.get("step2") or {}).get("per_backup", {}).items()
               if not (v["sha256_matches_recorded"] and v["integrity_check"]["out"] == "ok")}
    bad_runs[label] = {"how": how, "stopped": res.get("stopped"), "failing_backups": failing,
                       "sha256sum_c": (res.get("step2") or {}).get("sha256sum_c"),
                       "home_unchanged_byte_for_byte": before == after, "files_in_home": len(after),
                       "companions_in_home": L.companion_files(DATA), "aside_created": aside_bad.exists()}
observed["7_bad_backup"] = bad_runs
c.check("rollback step 2 stops a bad backup before anything moves: a backup with one changed byte fails its recorded "
        "SHA-256, and a backup whose checksum matches but whose page is corrupt fails `PRAGMA integrity_check`; in both "
        "runs no file is moved (no aside directory), and every file of the home, companions included, is unchanged "
        "byte for byte",
        all(r["stopped"] == "step 2: a backup failed its check; nothing was moved" and not r["aside_created"]
            and r["home_unchanged_byte_for_byte"] for r in bad_runs.values())
        and list(bad_runs["checksum"]["failing_backups"]) == [HOT_REF]
        and bad_runs["checksum"]["failing_backups"][HOT_REF]["sha256_matches_recorded"] is False
        and list(bad_runs["integrity"]["failing_backups"]) == ["state/brain/sessions.db"]
        and bad_runs["integrity"]["failing_backups"]["state/brain/sessions.db"]["sha256_matches_recorded"] is True
        and bad_runs["integrity"]["failing_backups"]["state/brain/sessions.db"]["integrity_check"]["out"] != "ok"
        and f"{HOT_ACC}-journal" in bad_runs["integrity"]["companions_in_home"],
        {k: {"stopped": v["stopped"], "failing": {f: {"sha": x["sha256_matches_recorded"],
                                                      "integrity": x["integrity_check"]["out"][:120]}
                                                  for f, x in v["failing_backups"].items()},
             "home_unchanged": v["home_unchanged_byte_for_byte"]} for k, v in bad_runs.items()})

# ======================================================== 8. rollback with the good backup
home_before = L.tree_sha(DATA)
dbs_before_rollback = L.sqlite_files(DATA)
RB = L.rollback_home(SH, PY, HOME, DATA, BACKUP, B, ASIDE)
observed["8_rollback"] = RB
s2 = RB.get("step2") or {}
c.check("rollback step 1: no process uses the home, readers included (psutil scan); step 2: every backup matches its "
        "recorded SHA-256 (and `sha256sum -c SHA256SUMS` passes where sha256sum is installed) and `PRAGMA "
        "integrity_check` prints ok, without changing the backup",
        RB.get("step1", {}).get("processes_using") == [] and s2.get("ok") is True and "stopped" not in RB,
        {"step1": RB.get("step1"), "sha256sum_c": s2.get("sha256sum_c"),
         "integrity_via": sorted({v["integrity_check"]["via"] for v in s2.get("per_backup", {}).values()})})
s3, s4, s5 = RB.get("step3") or {}, RB.get("step4") or {}, RB.get("step5") or {}
expected_moved = sorted(f + s for f in dbs_before_rollback for s in ("",) + L.COMPANIONS
                        if (f + s) in home_before)
c.check("rollback step 3: every SQLite file moves outside the data root with its -journal/-wal/-shm, keeping its "
        "path relative to the data root; each set is byte-identical to what was in the home (nothing deleted)",
        sorted(s3.get("moved_aside", [])) == expected_moved and s3.get("moved_sets_byte_identical") is True
        and s3.get("aside_outside_data_root") is True
        and all(L.tree_sha(ASIDE).get(m) == home_before[m] for m in expected_moved),
        {"moved_companions": [m for m in s3.get("moved_aside", []) if L.is_companion(Path(m))],
         "moved_files": len(s3.get("moved_aside", []))})
restored = s4.get("restored", {})
c.check("rollback step 4: `openminion storage restore --sqlite <file> --snapshot <backup> --yes` restores each "
        "accepted file (exit 0, 'Restored'), a copy restores each refused one; the other files are copied back and "
        "the -wal/-shm among the other-files backup are left out",
        all(restored[f]["rc"] == 0 and "Restored" in restored[f]["stdout"] for f in ACCEPTED)
        and all(restored[f]["rc"] == 0 for f in REFUSED)
        and sorted(s4.get("companions_in_other_files_backup_left_out", [])) == sorted([WAL_REF + "-shm",
                                                                                      WAL_REF + "-wal"])
        and all(f in s4.get("other_files_copied_back", []) for f in copied_other),
        {"accepted": {f: restored[f]["stdout"][:80] for f in ACCEPTED},
         "left_out": s4.get("companions_in_other_files_backup_left_out")})
c.check("rollback step 5: no -journal, -wal or -shm is next to any restored file, and no *.restore.tmp is left",
        s5.get("companions_next_to_restored_files") == {} and s5.get("restore_tmp_left") == []
        and L.companion_files(DATA) == [], s5)
AFTER = {f: L.db_facts(DATA / f) for f in DBS}
other_now = L.tree_sha(DATA)
other_backup = L.tree_sha(BACKUP / "other")
other_restored = {f: other_now.get(f) == h for f, h in other_backup.items() if not L.is_companion(Path(f))}
observed["8_restored"] = {"equal_to_backup": {f: AFTER[f]["digest"] == BACKUP_FACTS[f]["digest"] for f in DBS},
                          "other_files_equal_to_backup": all(other_restored.values()),
                          "files_in_data_root_not_in_backup": sorted(
                              f for f in other_now if f not in other_backup and f not in DBS)}
c.check("restored data: every database equals its checked backup (schema, rows, application_id) and passes "
        "integrity_check; memory/memory.audit.db holds the commit that was in its -wal at backup time; the other "
        "files equal the backup's",
        all(AFTER[f]["digest"] == BACKUP_FACTS[f]["digest"] and AFTER[f]["integrity"] == "ok" for f in DBS)
        and AFTER[PRE_WAL]["probe_tables"] == ["probe_wal_before_backup"]
        and all(AFTER[f]["probe_tables"] == [] for f in (HOT_ACC, HOT_REF, WAL_REF))
        and all(other_restored.values()),
        {"not_equal": [f for f in DBS if AFTER[f]["digest"] != BACKUP_FACTS[f]["digest"]],
         "files_in_data_root_not_in_backup": observed["8_restored"]["files_in_data_root_not_in_backup"]})
aside_read = {f: L.read_set_copy(ASIDE / f, scratch("aside-" + f.replace("/", "_")), marker_reader)
              for f in (HOT_ACC, HOT_REF, WAL_ACC, WAL_REF)}
aside_rows = L.read_set_copy(ASIDE / "state" / "brain" / "sessions.db", scratch("aside-sessions"), ws_rows)
aside_msgs = L.read_set_copy(ASIDE / "state" / "openminion.db", scratch("aside-runtime"), messages)
aside_art_b = L.read_set_copy(ASIDE / WAL_REF, scratch("aside-artifact-index"),
                              lambda p: L.query(p, "select count(*) from artifacts where 'artifact://sha256/' || sha256 = ?",
                                                (art_b["ref"],))[0][0])
observed["8_moved_aside_read_through_copies"] = {"sets": aside_read, "working_state_versions": [r[0] for r in aside_rows],
                                                 "messages": len(aside_msgs), "artifact_b_rows": aside_art_b}
c.check("moved-aside data, read through copies of the sets: ops/evidence.db and state/brain/brain.db with their hot "
        "-journal roll back to their last commit ('after-backup', the crashed writer's table gone); "
        "memory/memory.audit.db and artifact/index.db with their -wal hold the post-backup commit; the runtime and "
        "brain session sets hold turn 2's messages and working state; artifact/index.db holds artifact B",
        all(aside_read[f].get("probe_after_backup") == "after-backup" and "spill" not in aside_read[f]["probe_tables"]
            for f in (HOT_ACC, HOT_REF))
        and all("probe_wal_after_backup" in aside_read[f]["probe_tables"] for f in (WAL_ACC, WAL_REF))
        and all(v["integrity"] == "ok" for v in aside_read.values())
        and aside_rows == rows_t2 and aside_msgs == msgs_t2 and aside_art_b == 1
        and current_sets == aside_read,
        {"sets": aside_read, "working_state_versions": [r[0] for r in aside_rows], "messages": len(aside_msgs)})

# ======================================================== 9. the runtime on the restored home
show_rb = sessions_show()
rows_rb = ws_rows(DATA / "state" / "brain" / "sessions.db")
msgs_rb = messages(DATA / "state" / "openminion.db")
backup_rows = ws_rows(BACKUP / "db" / "state" / "brain" / "sessions.db")
verify_rb = artifactctl(HOME, "verify", "--all")
verify_report = (verify_rb["json"] or {}).get("report") or {}
verify_t1_ok = ((verify_t1["json"] or {}).get("report") or {}).get("ok")
show_a = artifactctl(HOME, "show", art_a["ref"] or "-")
show_b = artifactctl(HOME, "show", art_b["ref"] or "-")
observed["9_restored_home"] = {"sessions_show": show_rb, "working_state_versions": [r[0] for r in rows_rb],
                               "messages": msgs_rb, "artifact_verify": verify_rb["json"],
                               "artifact_a_show_rc": show_a["rc"], "artifact_b_show": show_b["json"]}
c.check("the restored home is the chosen backup's: `openminion sessions show` reports turn 1's count, the messages are "
        "turn 1's (turn 2's text is absent), the brain session store's working_state rows equal the backup's row for "
        "row (version and JSON); artifact A is indexed and verifies, artifact B (ingested after the backup) is not "
        "indexed",
        show_rb["rc"] == 0 and show_rb["turns"] == show_t1["turns"] and msgs_rb == msgs_t1
        and not any(TURN_TEXT[2] in (b or "") for _r, b in msgs_rb) and rows_rb == backup_rows == rows_t1
        and show_a["rc"] == 0 and show_b["rc"] == 1
        and ((show_b["json"] or {}).get("error") or {}).get("code") == "NOT_FOUND"
        and verify_t1_ok == verify_report.get("ok"),
        {"turns": {"turn1": show_t1["turns"], "turn2": show_t2["turns"], "restored": show_rb["turns"]},
         "working_state_versions": {"turn1": [r[0] for r in rows_t1], "turn2": [r[0] for r in rows_t2],
                                    "restored": [r[0] for r in rows_rb]},
         "artifact_verify_ok": {"turn1": verify_t1_ok, "restored": verify_report.get("ok")},
         "artifact_b": ((show_b["json"] or {}).get("error") or {}).get("code")})
extra_files = observed["8_restored"]["files_in_data_root_not_in_backup"]
extra_blobs = sorted(Path(f).name for f in extra_files if f.startswith("artifact/artifacts/") and
                     not f.endswith(".meta.json"))
orphans = sorted(i.get("sha256") for i in verify_report.get("issues") or [])
c.check("recorded (outside the checklist): the documented steps copy the backup's other files back but leave the files "
        "created after the backup in the data root (artifact B's blobs and .meta.json, the <file>.bak/ snapshots "
        "turn 2 made); `artifactctl verify --all` reports exactly artifact B's blobs as orphan_blob and nothing else",
        bool(extra_blobs) and orphans == extra_blobs
        and all(i.get("issue") == "orphan_blob" for i in verify_report.get("issues") or [])
        and all(f.startswith("artifact/artifacts/") or ".bak/" in f for f in extra_files),
        {"files_in_data_root_not_in_backup": extra_files, "verify_issues": verify_report.get("issues")})
t3 = turn(3, record=True)
show_t3 = sessions_show()
rows_t3 = ws_rows(DATA / "state" / "brain" / "sessions.db")
msgs_t3 = messages(DATA / "state" / "openminion.db")
puts = [p for p in t3.get("puts") or [] if p["session_id"] == BRAIN_SID]
new_rows = [r for r in rows_t3 if r[0] > rows_t1[-1][0]]
final = {f: L.db_facts(DATA / f) for f in DBS}
observed["9_turn3"] = {"turn": {k: t3.get(k) for k in ("rc", "run_state", "version", "error", "stderr_tail")},
                       "puts": [{k: p[k] for k in ("version", "has_fields", "plain_dict_writer", "typed_save_state",
                                                    "frames")} for p in puts],
                       "sessions_show": show_t3, "working_state_versions": [r[0] for r in rows_t3],
                       "messages": msgs_t3, "integrity": {f: final[f]["integrity"] for f in DBS},
                       "companions_after_exit": L.companion_files(DATA)}
c.check("a new turn continues correctly: turn 3 completes in the same conversation; its rows follow the backup's "
        "(first version = backup's last + 1; the backup's rows unchanged); the session has turn 1's count plus one "
        "turn; its messages are turn 1's then turn 3's, without turn 2's; every database passes integrity_check and "
        "no companion is left after the process exits",
        t3.get("rc") == 0 and t3.get("run_state") == "completed" and rows_t3[:len(rows_t1)] == rows_t1
        and bool(new_rows) and new_rows[0][0] == rows_t1[-1][0] + 1
        and show_t3["turns"] == show_t1["turns"] + (show_t2["turns"] - show_t1["turns"])
        and msgs_t3[:len(msgs_t1)] == msgs_t1 and any(TURN_TEXT[3] in (b or "") for _r, b in msgs_t3)
        and not any(TURN_TEXT[2] in (b or "") for _r, b in msgs_t3)
        and all(final[f]["integrity"] == "ok" for f in DBS) and L.companion_files(DATA) == [],
        {"versions": [r[0] for r in rows_t3], "turns": show_t3["turns"], "messages": len(msgs_t3)})
first = puts[0] if puts else {}
c.check("typed save versus turn-start reset, after the restore: turn 3's first working-state write is the turn-start "
        "reset (_reset_state_for_new_input, not save_state), which rewrites the restored state with its keys (the "
        "backup's last row, which turn 1's typed save gave both fields); every later write is the typed save_state "
        "with both fields; the recorder saw every stored row",
        first.get("plain_dict_writer") == "_reset_state_for_new_input" and not first.get("typed_save_state")
        and first.get("keys") == keys_of(rows_t1[-1]) and set(NEW_FIELDS) <= set(keys_of(rows_t1[-1]))
        and len(puts) > 1 and all(p["typed_save_state"] and p["plain_dict_writer"] is None
                                  and p["has_fields"] == list(NEW_FIELDS) for p in puts[1:])
        and [p["version"] for p in puts] == [r[0] for r in new_rows],
        {"first": {k: first.get(k) for k in ("version", "plain_dict_writer", "frames", "has_fields")},
         "later": [(p["version"], p["typed_save_state"], p["has_fields"]) for p in puts[1:]]})
observed["files_in_working_directory_at_end"] = sorted(L.rel(CWD, p) for p in CWD.rglob("*") if p.is_file())
observed["processes_started"] = SH.started

s = c.summary()
write_result(OUT, {"info": info, "observed": sanitize(observed), "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
