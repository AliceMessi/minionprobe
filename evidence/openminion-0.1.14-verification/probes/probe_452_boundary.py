"""#452 on the installed 0.1.14: the older-package/backup rollback boundary and the
typed-save versus turn-start-reset distinction, as docs/runtime-database-upgrades.md
at tag v0.1.14 describes them.

The guidance says: keep a pre-upgrade backup (the databases of the runtime home as
in "SQLite runtime home backup", and with the local session store a copy of its
`sessions` directory), upgrade every process together, never run an older process
against working state saved by the newer package, and roll back by restoring the
matching pre-upgrade backup with the matching older package. On a session last
saved before `turn_evidence`/`turn_evidence_trace_id`, the first runtime turn after
the upgrade can write the turn-start reset's row without them, the next typed save
adds them, and once added the rewrites keep them; a package from before the fields
rejects a working state saved with them (`extra_forbidden`).

Run with the 0.1.14 interpreter; the older package (its interpreter is the first
argument) runs its own turns in its own processes:
  1 the older package creates the home (default layout, `echo` agent): its own
    config, one SDK turn, `brainctl run` in local adapter mode (local session store
    under <home>/brain-local, outside the data root) and an `artifactctl ingest`
  2 every process stopped; the pre-upgrade backup as documented (with the 0.1.14
    CLI and interpreter), and a copy of the local `sessions` directory
  3 upgrade: two 0.1.14 SDK turns in the same conversation, with a recorder on
    SessctlAdapter.put_working_state, and 0.1.14 `brainctl step`
  4 the older package against the upgraded state, on a raw copy only (documented
    as unsupported): how it reads the rows and the local file, a turn, a step
  5 rollback as documented with the pre-upgrade backup (0.1.14 CLI), and the local
    `sessions` directory put back from its copy
  6 the older package on the restored home: it reads the state and continues

Usage: python -I -X utf8 probe_452_boundary.py <older_python> <work_dir> <out.json>
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, sanitize_text, write_result  # noqa: E402

SID, CONV = "s-452b", "c-452b"
BRAIN_SID = f"{SID}::conv:{CONV}"
LOCAL_SID = "local-452b"
NEW_FIELDS = ("turn_evidence", "turn_evidence_trace_id")
PLAIN_DICT_WRITERS = ("_reset_state_for_new_input", "_inject_gateway_system_context", "_inject_resume_task_hints")


# ---------------------------------------------------------------- step mode (any version)
def step_main(name: str, args: dict[str, Any]) -> None:
    quiet()
    clear_override_env()
    import openminion

    result: dict[str, Any] = {"step": name, "version": openminion.__version__,
                              "openminion_file": sanitize_text(openminion.__file__)}
    home = Path(args["home"])
    if name == "init":
        from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config

        config = OpenMinionConfig()
        config.agents = {"openminion": AgentProfileConfig(name="openminion", provider="echo",
                                                          default_channel="console")}
        config.default_agent = "openminion"
        config.runtime.process_mode = "single-process"
        config.runtime.daemon_auto_start = False
        home.mkdir(parents=True, exist_ok=True)
        save_config(config, str(home / "agents.json"))
    elif name == "turn":
        from openminion import APIRuntime

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
                    "plain_dict_writer": next((f.name for f in reversed(frames) if f.name in PLAIN_DICT_WRITERS),
                                              None),
                    "typed_save_state": any(f.name == "save_state" and f.filename.replace("\\", "/").endswith(
                        "openminion/modules/brain/state.py") for f in frames),
                    "frames": [f"{Path(f.filename).resolve().as_posix().replace(pkg + '/', '')}:{f.lineno} {f.name}"
                               for f in frames[-3:]]})
                return version

            SessctlAdapter.put_working_state = recording_put  # type: ignore[method-assign]
        runtime = APIRuntime.from_config_path(str(home / "agents.json"), home_root=home)
        try:
            out = runtime.run_turn(payload={"message": args["message"], "session_id": SID, "conversation_id": CONV})
        finally:
            runtime.close()
        body = out.get("body")
        result.update({"run_state": out.get("run_state"), "body": str(body)[:300] if body is not None else None,
                       "puts": records})
    elif name == "inspect":
        from openminion.modules.brain.schemas import WorkingState

        def validate(state: dict[str, Any]) -> dict[str, Any]:
            try:
                st = WorkingState.model_validate(state)
                return {"ok": True, "turn_evidence": st.turn_evidence if "turn_evidence" in WorkingState.model_fields
                        else "<not a field>", "turn_evidence_trace_id": getattr(st, "turn_evidence_trace_id",
                                                                                "<not a field>")}
            except Exception as exc:  # noqa: BLE001 - recorded
                errors = getattr(exc, "errors", None)
                return {"ok": False, "error": type(exc).__name__,
                        "errors": sorted([e["type"], list(e["loc"])] for e in errors()) if callable(errors) else None}

        result["fields_defined"] = [f for f in NEW_FIELDS if f in WorkingState.model_fields]
        result["working_state_field_count"] = len(WorkingState.model_fields)
        db = home / ".openminion" / "state" / "brain" / "sessions.db"
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute("select version, state_inline_json from working_state where session_id=? "
                               "order by version", (BRAIN_SID,)).fetchall()
        finally:
            con.close()
        result["rows"] = {str(v): validate(json.loads(s or "{}")) for v, s in rows}
        local = home / "brain-local" / "sessions" / LOCAL_SID / "working_state.json"
        if local.exists():
            data = json.loads(local.read_text(encoding="utf-8"))
            result["local"] = {"version": data.get("version"), **validate(data.get("state_inline") or {})}
    print("STEP-RESULT " + json.dumps(result, default=str), flush=True)


if len(sys.argv) > 1 and sys.argv[1] == "--step":
    step_main(sys.argv[2], json.loads(sys.argv[3]))
    sys.exit(0)

# ---------------------------------------------------------------- orchestrator (0.1.14)
OLDER = sys.argv[1]
WORK = Path(sys.argv[2]).resolve()
OUT = Path(sys.argv[3]).resolve()
quiet()
clear_override_env()
import probe_452_lib as L  # noqa: E402

PY = sys.executable
# Not resolve(): on Linux the venv's bin/python is a symlink to the base interpreter.
L.EXTRA_LABELS.append((str(Path(OLDER).absolute().parent.parent), "<VENV_OLDER>"))
if WORK.exists():
    shutil.rmtree(WORK)
HOME = WORK / "home"
DATA = HOME / ".openminion"
LOCAL_ROOT = HOME / "brain-local"
SESSIONS = LOCAL_ROOT / "sessions"
BACKUP = WORK / "pre-upgrade-backup"
ASIDE = WORK / "aside"
COPY = WORK / "scratch-copy-of-upgraded-home"
CWD = WORK / "cwd"
SCRATCH = WORK / "scratch"
for d in (WORK, BACKUP, CWD, SCRATCH):
    d.mkdir(parents=True, exist_ok=True)
SH = L.Shell(CWD)
SELF = Path(__file__).resolve()
info = banner("probe_452_boundary", older_python=L.sanitize_text(OLDER),
              sqlite3_shell_version=SH.shell_version(), sha256sum=bool(SH.sha256sum))
from openminion.modules.storage.migrations.module_ids import MODULE_APPLICATION_IDS  # noqa: E402

MODULE_BY_ID: dict[int, list[str]] = {}
for _module, _app_id in MODULE_APPLICATION_IDS.items():
    MODULE_BY_ID.setdefault(_app_id, []).append(_module)
CFG = WORK / "brain-local.yaml"
CFG.write_text("brain:\n  agent_id: openminion\n  budgets:\n    max_ticks_per_user_turn: 20\n    max_tool_calls: 5\n"
               "    max_a2a_calls: 5\n    max_total_llm_tokens: 5000\n    max_elapsed_ms: 120000\n"
               "  adapters:\n    mode: local\n", encoding="utf-8")
c = Checks()
observed: dict[str, Any] = {}
scratch_n = [0]


def scratch(label: str) -> Path:
    scratch_n[0] += 1
    return SCRATCH / f"{scratch_n[0]:02d}-{label}"


# ======================================================== 0. what the published text says
DOC, doc_info = L.load_doc()
FLAT = " ".join(DOC.split())
CMDS = L.doc_commands(DOC)
PHRASES = (
    "Rolling back means restoring the matching pre-upgrade backup with the matching older package.",
    "Never run a process from before the upgrade against the migrated database.",
    "Downgrading in place is not supported",
    "and with the local session store a copy of its `sessions` directory",
    "never run an older process against working state saved by the newer package",
    "So on a session last saved before the fields, the first runtime turn after the upgrade can write the reset's "
    "row without them, and the brain's next typed save in that turn adds them. Once a typed save has added them, the "
    "rewrites keep them.",
    "Packages with the fields read a working state without them, including those rows, with both unset.",
    "A package from before the fields rejects any working state saved with them: validation fails with "
    "`extra_forbidden` for both.",
)
observed["0_guidance"] = {"source": doc_info, "phrases": {p: p in FLAT for p in PHRASES}}
c.check("the published text (tag v0.1.14, SHA-256 equal to the #453 head) states the rollback boundary (matching "
        "pre-upgrade backup with the matching older package; never run an older process against the newer state) and "
        "the typed-save versus turn-start-reset behavior that this probe exercises",
        doc_info["sha256"] == L.DOC_SHA256 and all(observed["0_guidance"]["phrases"].values())
        and all(CMDS["verbatim"].values()), observed["0_guidance"]["phrases"])


def step(py: str, name: str, home: Path = HOME, **args: Any) -> dict[str, Any]:
    res = SH.step(py, SELF, name, home=str(home), **args)
    if py == OLDER and str(res.get("openminion_file", "")).startswith("<VENV>"):
        # sanitized in the older process, where its own venv is <VENV>
        res["openminion_file"] = "<VENV_OLDER>" + res["openminion_file"][len("<VENV>"):]
    return res


def brainctl(py: str, command: str, root: Path, message: str) -> dict[str, Any]:
    proc = SH.run([L.bin_of(py, "brainctl"), command, "--input", message, "--session-id", LOCAL_SID,
                   "--config", str(CFG), "--root", str(root)])
    try:
        status = json.loads(proc.stdout).get("status")
    except ValueError:
        status = None
    return {"argv": ["brainctl", command, "--session-id", LOCAL_SID], "rc": proc.returncode, "status": status,
            "stderr_tail": [L.sanitize_text(x) for x in proc.stderr.strip().splitlines()[-4:]],
            "extra_forbidden_in_stderr": "extra_forbidden" in proc.stderr}


def artifactctl(py: str, home: Path, *args: str) -> dict[str, Any]:
    proc = SH.run([L.bin_of(py, "artifactctl"), "--home-root", str(home), *args])
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = None
    return {"rc": proc.returncode, "json": payload}


def ws_rows(db: Path) -> list[tuple]:
    return L.query(db, "select version, state_inline_json from working_state where session_id=? order by version",
                   (BRAIN_SID,))


def messages(db: Path) -> list[tuple]:
    return L.query(db, "select role, body from messages where session_id=? order by created_at, append_seq", (SID,))


def keys_of(row: tuple) -> list[str]:
    return sorted(json.loads(row[1] or "{}"))


def fields_of(row: tuple) -> list[str]:
    return [f for f in NEW_FIELDS if f in json.loads(row[1] or "{}")]


def local_state(root: Path = LOCAL_ROOT) -> dict[str, Any]:
    data = json.loads((root / "sessions" / LOCAL_SID / "working_state.json").read_text(encoding="utf-8"))
    st = data.get("state_inline") or {}
    return {"version": data.get("version"), "has_fields": [f for f in NEW_FIELDS if f in st],
            "turn_evidence": st.get("turn_evidence", "<absent>")}


DB_SESS = DATA / "state" / "brain" / "sessions.db"
DB_RT = DATA / "state" / "openminion.db"
TEXT = {"A": "turn A, written by the older package", "B1": "turn B1, after the upgrade to 0.1.14",
        "B2": "turn B2, 0.1.14 again", "C": "turn C, the older package after the rollback"}

# ======================================================== 1. the older package creates the home
init = step(OLDER, "init")
tA = step(OLDER, "turn", message=TEXT["A"])
runA = brainctl(OLDER, "run", LOCAL_ROOT, "local step, written by the older package")
artA = artifactctl(OLDER, HOME, "ingest", str(WORK / "brain-local.yaml"), "--label", "artifact-a", "--session", SID)
older = step(OLDER, "inspect")
OLDER_VERSION = older.get("version")
OLDER_HAS_FIELDS = older.get("fields_defined") == list(NEW_FIELDS)
rows_A = ws_rows(DB_SESS)
msgs_A = messages(DB_RT)
local_A = local_state()
observed["1_older_home"] = {"init": init, "turn": tA, "brainctl_run": runA, "artifact_a": artA, "older": older,
                            "working_state_rows": [(r[0], fields_of(r)) for r in rows_A], "messages": msgs_A,
                            "local": local_A, "sqlite_files": L.sqlite_files(DATA)}
c.check(f"the older package ({OLDER_VERSION}, its own venv and processes) writes the home's config, completes an SDK "
        f"turn, `brainctl run` (local session store) and `artifactctl ingest`; its WorkingState "
        f"{'defines' if OLDER_HAS_FIELDS else 'does not define'} turn_evidence/turn_evidence_trace_id, and every row "
        f"it saved {'has' if OLDER_HAS_FIELDS else 'lacks'} them",
        init.get("rc") == 0 and tA.get("rc") == 0 and tA.get("run_state") == "completed" and runA["rc"] == 0
        and artA["rc"] == 0 and bool(rows_A) and OLDER_VERSION not in (None, info["openminion.__version__"])
        and all(fields_of(r) == (list(NEW_FIELDS) if OLDER_HAS_FIELDS else []) for r in rows_A)
        and local_A["has_fields"] == (list(NEW_FIELDS) if OLDER_HAS_FIELDS else []),
        {"older": {k: older.get(k) for k in ("version", "openminion_file", "fields_defined",
                                             "working_state_field_count")},
         "rows": [(r[0], fields_of(r)) for r in rows_A], "local": local_A})

# ======================================================== 2. pre-upgrade backup
users = L.processes_using(HOME)
B = L.backup_home(SH, PY, DATA, BACKUP, CMDS["python_backup_code"], module_by_id=MODULE_BY_ID, shell_copy=False)
shutil.copytree(SESSIONS, BACKUP / "local-sessions")
backup_rows = ws_rows(BACKUP / "db" / "state" / "brain" / "sessions.db")
observed["2_backup"] = {"processes_using_home": users,
                        "db": {f: {k: e.get(k) for k in ("accepted", "method", "python_rc", "sha256")}
                               for f, e in B["db"].items()},
                        "other_files_copied": B["other_files_copied"],
                        "local_sessions_copy": sorted(L.tree_sha(BACKUP / "local-sessions"))}
c.check("pre-upgrade backup with every process stopped (no process uses the home): `openminion storage backup` for "
        "the accepted files and the Python one-liner for the refused ones, all with a recorded SHA-256; the other "
        "files without companions; a copy of the local `sessions` directory. The working_state backup holds the older "
        "package's rows exactly",
        users["processes_using"] == [] and len(B["db"]) == 17
        and all((e["accepted"] or e.get("python_rc") == 0) for e in B["db"].values())
        and sorted(f for f, e in B["db"].items() if e["accepted"]) == sorted(
            f for f, v in L.doc_table(DOC).items() if v["storage_backup"] == "accepted")
        and B["sha256_recorded"] == 17 and backup_rows == rows_A
        and L.tree_sha(BACKUP / "local-sessions") == L.tree_sha(SESSIONS),
        {"accepted": sorted(f for f, e in B["db"].items() if e["accepted"]), "rows": [r[0] for r in backup_rows]})

# ======================================================== 3. upgrade to 0.1.14
tB1 = step(PY, "turn", message=TEXT["B1"], record=True)
tB2 = step(PY, "turn", message=TEXT["B2"], record=True)
stepB = brainctl(PY, "step", LOCAL_ROOT, "local step, after the upgrade")
newer = step(PY, "inspect")
rows_B = ws_rows(DB_SESS)
local_B = local_state()
puts1 = [p for p in tB1.get("puts") or [] if p["session_id"] == BRAIN_SID]
puts2 = [p for p in tB2.get("puts") or [] if p["session_id"] == BRAIN_SID]
observed["3_upgrade"] = {
    "turns": {k: {x: t.get(x) for x in ("rc", "run_state", "version", "error", "stderr_tail")}
              for k, t in (("B1", tB1), ("B2", tB2))},
    "B1_puts": [{k: p[k] for k in ("version", "has_fields", "plain_dict_writer", "typed_save_state", "frames")}
                for p in puts1],
    "B2_puts": [{k: p[k] for k in ("version", "has_fields", "plain_dict_writer", "typed_save_state")} for p in puts2],
    "rows": [(r[0], fields_of(r)) for r in rows_B], "brainctl_step": stepB, "local": local_B,
    "0.1.14_reads": {"rows": newer.get("rows"), "local": newer.get("local")}}
first1 = puts1[0] if puts1 else {}
c.check("upgrade turn B1 (0.1.14): its first working-state write is the turn-start reset (_reset_state_for_new_input, "
        "not save_state), rewriting the stored state with the older package's keys "
        f"({'with' if OLDER_HAS_FIELDS else 'without'} the fields, as stored); every later write is the typed "
        "save_state with both fields",
        tB1.get("rc") == 0 and tB1.get("run_state") == "completed" and tB1.get("version") == info[
            "openminion.__version__"]
        and first1.get("plain_dict_writer") == "_reset_state_for_new_input" and not first1.get("typed_save_state")
        and first1.get("keys") == keys_of(rows_A[-1]) and first1.get("version") == rows_A[-1][0] + 1
        and first1.get("has_fields") == (list(NEW_FIELDS) if OLDER_HAS_FIELDS else [])
        and len(puts1) > 1 and all(p["typed_save_state"] and p["has_fields"] == list(NEW_FIELDS) for p in puts1[1:]),
        {"first": {k: first1.get(k) for k in ("version", "plain_dict_writer", "has_fields", "frames")},
         "later": [(p["version"], p["typed_save_state"], p["has_fields"]) for p in puts1[1:]]})
c.check("upgrade turn B2 (0.1.14): the reset rewrites the state the typed save left, so its row keeps both fields "
        "('once a typed save has added them, the rewrites keep them'); 0.1.14 reads every stored row (a row without "
        "the fields with both unset); 0.1.14 `brainctl step` saves the local working_state.json with both fields "
        "(null) at a higher version",
        tB2.get("run_state") == "completed" and bool(puts2)
        and puts2[0]["plain_dict_writer"] == "_reset_state_for_new_input" and puts2[0]["has_fields"] == list(NEW_FIELDS)
        and all(v.get("ok") for v in (newer.get("rows") or {}).values()) and bool(newer.get("rows"))
        and all((v.get("turn_evidence"), v.get("turn_evidence_trace_id")) == (None, None)
                for k, v in (newer.get("rows") or {}).items()
                if not fields_of(next(r for r in rows_B if str(r[0]) == k)))
        and stepB["rc"] == 0 and local_B["has_fields"] == list(NEW_FIELDS) and local_B["turn_evidence"] is None
        and local_B["version"] > local_A["version"],
        {"B2_first": {k: (puts2[0] if puts2 else {}).get(k) for k in ("version", "plain_dict_writer", "has_fields")},
         "rows_without_fields": [r[0] for r in rows_B if not fields_of(r)], "local": local_B})

# ======================================================== 4. the older package against the upgraded state (copy)
shutil.copytree(HOME, COPY)
rej = step(OLDER, "inspect", home=COPY)
rej_turn = step(OLDER, "turn", home=COPY, message="the older package on a copy of the upgraded home")
rej_step = brainctl(OLDER, "step", COPY / "brain-local", "older step on the upgraded local session")
saved_by_new = [str(r[0]) for r in rows_B if r[0] > rows_A[-1][0] and fields_of(r)]
observed["4_older_on_upgraded_copy"] = {
    "inspect": {"rows_saved_by_0.1.14": {v: (rej.get("rows") or {}).get(v) for v in saved_by_new},
                "local": rej.get("local")},
    "turn": {k: rej_turn.get(k) for k in ("rc", "run_state", "body", "error", "extra_forbidden_in_stderr",
                                          "stderr_last_line", "stderr_tail")},
    "brainctl_step": rej_step,
    "working_state_fields": {"older": older.get("working_state_field_count"),
                             "0.1.14": newer.get("working_state_field_count")}}
if not OLDER_HAS_FIELDS:
    expected = [["extra_forbidden", ["turn_evidence"]], ["extra_forbidden", ["turn_evidence_trace_id"]]]
    c.check(f"not supported, shown on a raw copy (the upgraded home itself is never opened by {OLDER_VERSION}): "
            f"{OLDER_VERSION}, from before the fields, rejects every working state 0.1.14 saved with them, in the "
            f"brain session store and the local file, with extra_forbidden for both; its SDK turn and its `brainctl "
            f"step` on the upgraded state exit non-zero with extra_forbidden",
            bool(saved_by_new) and all((rej.get("rows") or {}).get(v, {}).get("errors") == expected
                                       for v in saved_by_new)
            and (rej.get("local") or {}).get("errors") == expected and rej_step["rc"] != 0
            and rej_step["extra_forbidden_in_stderr"] and rej_turn.get("rc") != 0
            and rej_turn.get("extra_forbidden_in_stderr") is True,
            {"rows": {v: (rej.get("rows") or {}).get(v) for v in saved_by_new[-2:]}, "local": rej.get("local"),
             "sdk_turn": {k: rej_turn.get(k) for k in ("rc", "stderr_last_line")},
             "brainctl": {k: rej_step[k] for k in ("rc", "stderr_tail")}})
else:
    # Not claimed as supported: recorded only. The published text forbids this whatever the outcome.
    observed["4_older_on_upgraded_copy"]["note"] = (
        f"{OLDER_VERSION} defines both fields; the text's rejection applies to packages from before the fields. "
        "The outcome here is recorded, not certified: the guidance says never to run an older process against "
        "the newer state.")

# ======================================================== 5. rollback with the matching pre-upgrade backup
RB = L.rollback_home(SH, PY, HOME, DATA, BACKUP, B, ASIDE)
ASIDE_LOCAL = WORK / "aside-local-sessions"
shutil.move(str(SESSIONS), str(ASIDE_LOCAL))
shutil.copytree(BACKUP / "local-sessions", SESSIONS)
rows_R = ws_rows(DB_SESS)
after = {f: L.db_facts(DATA / f) for f in B["db"]}
backup_facts = {f: L.db_facts(BACKUP / "db" / f) for f in B["db"]}
aside_rows = L.read_set_copy(ASIDE / "state" / "brain" / "sessions.db", scratch("aside-sessions"), ws_rows)
aside_msgs = L.read_set_copy(ASIDE / "state" / "openminion.db", scratch("aside-runtime"), messages)
observed["5_rollback"] = {"steps": RB, "rows": [(r[0], fields_of(r)) for r in rows_R],
                          "aside_rows": [(r[0], fields_of(r)) for r in aside_rows], "aside_messages": len(aside_msgs),
                          "local_sessions_restored": L.tree_sha(SESSIONS) == L.tree_sha(BACKUP / "local-sessions")}
c.check("rollback (0.1.14 CLI, the documented steps 1-5): no process uses the home; every backup passes its checksum "
        "and integrity_check; every SQLite file moves aside with its companions, unchanged; accepted files restored "
        "with `openminion storage restore`, refused ones copied; no companion next to a restored file; every database "
        "equals its pre-upgrade backup; the local `sessions` directory is its copy again",
        "stopped" not in RB and RB["step1"]["processes_using"] == [] and RB["step2"]["ok"]
        and RB["step3"]["moved_sets_byte_identical"] and all(r["rc"] == 0 for r in RB["step4"]["restored"].values())
        and RB["step5"]["companions_next_to_restored_files"] == {} and RB["step5"]["restore_tmp_left"] == []
        and all(after[f]["digest"] == backup_facts[f]["digest"] for f in B["db"])
        and observed["5_rollback"]["local_sessions_restored"],
        {"not_equal": [f for f in B["db"] if after[f]["digest"] != backup_facts[f]["digest"]],
         "moved_companions": [m for m in RB.get("step3", {}).get("moved_aside", []) if L.is_companion(Path(m))]})
c.check("the restored working state is the pre-upgrade backup's, row for row; the set moved aside keeps the upgraded "
        "state (read through a copy: 0.1.14's rows with both fields, and turns B1/B2 in the runtime database)",
        rows_R == rows_A == backup_rows and aside_rows == rows_B
        and any(TEXT["B1"] in (b or "") for _r, b in aside_msgs),
        {"restored": [r[0] for r in rows_R], "aside": [(r[0], fields_of(r)) for r in aside_rows][-3:]})

# ======================================================== 6. the older package on the restored home
back = step(OLDER, "inspect")
tC = step(OLDER, "turn", message=TEXT["C"])
stepC = brainctl(OLDER, "step", LOCAL_ROOT, "local step, the older package after the rollback")
rows_C = ws_rows(DB_SESS)
msgs_C = messages(DB_RT)
local_C = local_state()
verify_C = artifactctl(OLDER, HOME, "verify", "--all")
new_C = [r for r in rows_C if r[0] > rows_A[-1][0]]
observed["6_older_after_rollback"] = {
    "inspect": {"rows": back.get("rows"), "local": back.get("local")},
    "turn": {k: tC.get(k) for k in ("rc", "run_state", "version", "error", "stderr_tail")},
    "rows": [(r[0], fields_of(r)) for r in rows_C], "messages": msgs_C, "brainctl_step": stepC, "local": local_C,
    "artifact_verify": verify_C["json"], "companions_after_exit": L.companion_files(DATA)}
c.check(f"rollback boundary: {OLDER_VERSION} reads the restored state (every row and the local file validate) and "
        f"continues it: turn C completes, its rows follow the backup's (first version = backup's last + 1) and are "
        f"{OLDER_VERSION}'s ({'with' if OLDER_HAS_FIELDS else 'without'} the fields); the messages are turn A's then "
        f"turn C's, without B1/B2; its `brainctl step` continues the local session; `artifactctl verify --all` ok",
        back.get("version") == OLDER_VERSION and all(v.get("ok") for v in (back.get("rows") or {}).values())
        and (back.get("local") or {}).get("ok") is True and tC.get("rc") == 0 and tC.get("run_state") == "completed"
        and rows_C[:len(rows_A)] == rows_A and bool(new_C) and new_C[0][0] == rows_A[-1][0] + 1
        and all(fields_of(r) == (list(NEW_FIELDS) if OLDER_HAS_FIELDS else []) for r in new_C)
        and msgs_C[:len(msgs_A)] == msgs_A and any(TEXT["C"] in (b or "") for _r, b in msgs_C)
        and not any(TEXT["B1"] in (b or "") or TEXT["B2"] in (b or "") for _r, b in msgs_C)
        and stepC["rc"] == 0 and local_C["version"] > local_A["version"]
        and local_C["has_fields"] == (list(NEW_FIELDS) if OLDER_HAS_FIELDS else [])
        and verify_C["rc"] == 0 and (((verify_C["json"] or {}).get("report") or {}).get("failed") == 0)
        and L.companion_files(DATA) == [],
        {"rows": [(r[0], fields_of(r)) for r in rows_C], "messages": len(msgs_C), "local": local_C,
         "verify": ((verify_C["json"] or {}).get("report") or {})})

observed["files_in_working_directory_at_end"] = sorted(L.rel(CWD, p) for p in CWD.rglob("*") if p.is_file())
s = c.summary()
write_result(OUT, {"info": info, "observed": sanitize(observed), "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
