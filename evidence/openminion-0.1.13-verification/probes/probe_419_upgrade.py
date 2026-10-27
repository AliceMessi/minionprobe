"""#419 coordinated upgrade on the installed packages: working state written by
0.1.12 in both locations, pre-upgrade backup, upgrade to 0.1.13, rollback.

Follows docs/runtime-database-upgrades.md at the v0.1.13 tag ("Brain working
state fields" -> "Coordinated upgrade"; the document is not in the wheel):
stop every writer, keep a pre-upgrade backup (for SQLite
`openminion storage backup --sqlite <path>`; with the local session store a
copy of its `sessions` directory), upgrade every process together, never run
an older process against working state saved by the newer package, and roll
back by restoring the matching pre-upgrade backup with the matching older
package. Mixed old/new readers are not certified and not claimed here.

Writers (each its own process, which exits before the next step):
  - SDK runtime: `APIRuntime` + `Agent` (conversation "upgrade-chat") over a
    scripted OpenAI-compatible provider on 127.0.0.1 (see probe_419_lib.py).
    It saves the working state in <home>/brain/sessions.db (SessctlAdapter).
  - `brainctl` in local adapter mode (the installed console script), session
    local-419 under <home>/brain-local/sessions (LocalSessionStore).

Steps:
  1 seed with 0.1.12 (its own venv and processes): one SDK turn, `brainctl run`
  2 writers stopped; pre-upgrade backup with the 0.1.12 CLI of every SQLite file
    of the runtime home, and a copy of the local `sessions` directory
  3 upgrade: 0.1.13 reads the 0.1.12 states (both fields unset), then continues
    the SDK conversation and the brainctl session
  4 0.1.12 against the newer state, on a scratch copy only (documented as
    unsupported; shown, not certified): rejection
  5 rollback: restore the matching backup files and directory, then 0.1.12
    reads them and continues both sessions

Usage: python -I -X utf8 probe_419_upgrade.py <python_0.1.12> <work_dir> <out.json>
       (run with the 0.1.13 interpreter; each step runs in a new process)
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

SDK_SID, CONVERSATION, LOCAL_SID = "s419up", "upgrade-chat", "local-419"
BRAIN_SID = f"{SDK_SID}::conv:{CONVERSATION}"


# ---------------------------------------------------------------- step mode (any version)
def step_main(name: str, args: dict[str, Any]) -> None:
    quiet()
    clear_override_env()
    import openminion

    import probe_419_lib as L

    result: dict[str, Any] = {"step": name, "version": openminion.__version__}
    if name == "sdk-turn":
        plan = L.TurnPlan(classifier=L.LIVE_NO_DATE, first_entry="tool", judges=("continue",))
        out = L.run_scenario(Path(args["home"]), L.Script([plan]), inputs=[args["message"]],
                             forced_tools=["lookup_order"], session_id=SDK_SID, conversation_id=CONVERSATION)
        result.update({"results": out.get("results"), "lookups": len(out["lookup_order_calls"]),
                       "respond_verify": [{k: r.get(k) for k in ("answer", "result", "state_at_respond")}
                                          for r in out.get("respond_verify") or []],
                       "blocked": list(L.BLOCKED)})
    elif name == "inspect":
        home = Path(args["home"])
        sq = L.load_through_owner("sqlite", L.brain_sessions_db(home), BRAIN_SID)
        lo = L.load_through_owner("local", home / "brain-local" / "sessions", LOCAL_SID)
        result.update({"sqlite": L.public(sq), "local": L.public(lo)})
    print("STEP-RESULT " + json.dumps(result, default=str), flush=True)


if len(sys.argv) > 1 and sys.argv[1] == "--step":
    step_main(sys.argv[2], json.loads(sys.argv[3]))
    sys.exit(0)

# ---------------------------------------------------------------- orchestrator (0.1.13)
PY12 = sys.argv[1]
PY13 = sys.executable
WORK = Path(sys.argv[2]).resolve()
OUT = Path(sys.argv[3]).resolve()
quiet()
clear_override_env()
info = banner("probe_419_upgrade", python_0112=sanitize_text(PY12))
import probe_419_lib as L  # noqa: E402

c = Checks()
observed: dict[str, Any] = {}
HOME = WORK / "home"
LOCAL_ROOT = HOME / "brain-local"
SESSIONS = LOCAL_ROOT / "sessions"
BACKUP = WORK / "pre-upgrade-backup"
NEWER_COPY = WORK / "scratch-copy-of-upgraded-home"
if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
CFG = WORK / "brain-local.yaml"
CFG.write_text("brain:\n  agent_id: openminion\n  budgets:\n    max_ticks_per_user_turn: 20\n    max_tool_calls: 5\n"
               "    max_a2a_calls: 5\n    max_total_llm_tokens: 5000\n    max_elapsed_ms: 120000\n"
               "  adapters:\n    mode: local\n", encoding="utf-8")
ENV = {k: v for k, v in os.environ.items() if k not in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT",
                                                        "OPENMINION_GENERATED_ROOT", "OPENMINION_CONFIG_PATH")}


def bin_of(py: str, name: str) -> str:
    return str(Path(py).parent / (f"{name}.exe" if os.name == "nt" else name))


def run_step(py: str, name: str, **args: Any) -> dict[str, Any]:
    proc = subprocess.run([py, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--step", name, json.dumps(args)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
                          cwd=str(WORK))
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("STEP-RESULT "):
            res = json.loads(line[len("STEP-RESULT "):])
            res["rc"] = proc.returncode
            return res
    return {"step": name, "rc": proc.returncode, "error": "no STEP-RESULT",
            "stderr_tail": sanitize_text(proc.stderr.strip())[-1500:]}


def brainctl(py: str, command: str, root: Path, message: str) -> dict[str, Any]:
    proc = subprocess.run([bin_of(py, "brainctl"), command, "--input", message, "--session-id", LOCAL_SID,
                           "--config", str(CFG), "--root", str(root)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=ENV, cwd=str(WORK))
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = {}
    err = proc.stderr.strip().splitlines()
    return {"argv": ["brainctl", command, "--session-id", LOCAL_SID], "rc": proc.returncode,
            "status": payload.get("status"),
            "stderr_tail": [sanitize_text(x) for x in err[-6:]],
            "extra_forbidden_in_stderr": "extra_forbidden" in proc.stderr
            or "Extra inputs are not permitted" in proc.stderr}


def ws_rows(db: Path) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in con.execute(
            "select session_id, version, state_inline_json from working_state order by session_id, version")]
    finally:
        con.close()


def digest_rows(rows: list[tuple[Any, ...]]) -> str:
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


def row_keys(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    out = []
    for sid, version, inline in rows:
        st = json.loads(inline) if inline else {}
        out.append({"session_id": sid, "version": version,
                    "keys": [f for f in L.NEW_FIELDS if f in st],
                    "turn_evidence_trace_id": st.get("turn_evidence_trace_id", "<absent>"),
                    "trace_id": st.get("trace_id"), "has_evidence": isinstance(st.get("turn_evidence"), dict)})
    return out


def tree_digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def local_state() -> dict[str, Any]:
    path = SESSIONS / LOCAL_SID / "working_state.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    st = data.get("state_inline") or {}
    return {"version": data.get("version"), "keys": [f for f in L.NEW_FIELDS if f in st],
            "turn_evidence": st.get("turn_evidence", "<absent>"),
            "turn_evidence_trace_id": st.get("turn_evidence_trace_id", "<absent>"), "trace_id": st.get("trace_id"),
            "goal": st.get("goal")}


def sqlite_files() -> list[str]:
    out = []
    for p in sorted(HOME.rglob("*.db")):
        if not p.is_file() or any(part.endswith(".bak") for part in p.relative_to(HOME).parts[:-1]):
            continue
        with p.open("rb") as fh:
            if fh.read(16) == b"SQLite format 3\x00":
                out.append(str(p.relative_to(HOME)).replace("\\", "/"))
    return out


def text0(res: dict[str, Any]) -> str:
    r = (res.get("results") or [{}])[0] or {}
    return str(r.get("text") or r.get("error") or "")


def run_state0(res: dict[str, Any]) -> Any:
    return ((res.get("results") or [{}])[0] or {}).get("run_state")


DB = L.brain_sessions_db(HOME)

# ======================================================== 1. seed with 0.1.12
seed_sdk = run_step(PY12, "sdk-turn", home=str(HOME), message="Check order A-104")
seed_cli = brainctl(PY12, "run", LOCAL_ROOT, "Check order A-104")
observed["1_seed"] = {"sdk": seed_sdk, "brainctl": seed_cli}
c.check("seed: the published 0.1.12 (own venv, own processes) ran one SDK turn of conversation upgrade-chat and "
        "`brainctl run` in local adapter mode; both processes exited",
        seed_sdk.get("version") == "0.1.12" and seed_sdk.get("rc") == 0 and run_state0(seed_sdk) == "completed"
        and seed_cli["rc"] == 0, {"sdk": {k: seed_sdk.get(k) for k in ("version", "rc", "results")},
                                  "brainctl": {k: seed_cli[k] for k in ("rc", "status")}})
seed_rows = ws_rows(DB)
seed_local = local_state()
c.check("seed: every working state 0.1.12 saved lacks both fields (the old payload): all rows of the SQLite store "
        "for the conversation, and the local working_state.json",
        bool(seed_rows) and all(r["keys"] == [] for r in row_keys(seed_rows)) and seed_local["keys"] == []
        and any(r[0] == BRAIN_SID for r in seed_rows),
        {"sqlite_rows": [(r["session_id"], r["version"], r["keys"]) for r in row_keys(seed_rows)],
         "local": seed_local})

# ======================================================== 2. writers stopped; pre-upgrade backup (0.1.12 CLI)
db_files = sqlite_files()
observed["2_sqlite_files"] = db_files
backups = {}
GUIDE_FILES = ("state.db", "brain/sessions.db")  # the runtime database and the brain's working-state store
for rel in db_files:
    target = BACKUP / (rel.replace("/", "__") + ".backup")
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([PY12, "-I", "-m", "openminion.cli.main", "storage", "backup", "--sqlite", str(HOME / rel),
                           "--output", str(target), "--json"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, cwd=str(WORK))
    try:
        reply = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        reply = {}
    entry = {"storage_backup_rc": proc.returncode, "storage_backup_ok": reply.get("ok"),
             "storage_backup_error": sanitize_text(str(reply.get("error") or ""))[:200] or None}
    if proc.returncode == 0 and reply.get("ok") is True and target.exists():
        entry["method"] = "openminion storage backup --sqlite (0.1.12)"
    else:
        # `storage backup` checks the storage module's application_id and refuses the other
        # modules' databases; those get a snapshot with sqlite3's online backup API.
        if target.exists():
            target.unlink()
        src = sqlite3.connect(f"file:{(HOME / rel).as_posix()}?mode=ro", uri=True)
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        entry["method"] = "sqlite3 Connection.backup"
    entry["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    backups[rel] = entry
shutil.copytree(SESSIONS, BACKUP / "sessions")
observed["2_backup"] = {"sqlite": backups, "sessions_copy_files": len(tree_digest(BACKUP / "sessions"))}
c.check("backup: with every writer stopped, `openminion storage backup --sqlite <file> --output <backup> --json` "
        "(the guide's command, 0.1.12 CLI) succeeds for the runtime database (state.db) and the brain's working-state "
        "store (brain/sessions.db); every other SQLite file of the home has a snapshot too",
        all(rel in db_files for rel in GUIDE_FILES)
        and all(backups[rel]["method"].startswith("openminion storage backup") for rel in GUIDE_FILES)
        and all(b.get("sha256") for b in backups.values()),
        {rel: {k: b.get(k) for k in ("method", "storage_backup_error")} for rel, b in backups.items()})
backup_sessions_db = BACKUP / ("brain__sessions.db.backup")
c.check("backup: the brain/sessions.db snapshot holds exactly the seeded working-state rows; the copy of the local "
        "`sessions` directory equals the original",
        backup_sessions_db.exists() and digest_rows(ws_rows(backup_sessions_db)) == digest_rows(seed_rows)
        and tree_digest(BACKUP / "sessions") == tree_digest(SESSIONS),
        {"rows": len(seed_rows), "sessions_files": sorted(tree_digest(SESSIONS))})

# ======================================================== 3. upgrade to 0.1.13
pre = run_step(PY13, "inspect", home=str(HOME))
observed["3_0113_reads_0112_state"] = pre
seed_last = row_keys([r for r in seed_rows if r[0] == BRAIN_SID])[-1]
c.check("upgrade: 0.1.13 loads the 0.1.12 working state through the SQLite owner (SessctlAdapter + "
        "load_or_init_state) with both fields unset; trace id kept; _same_turn_evidence returns nothing",
        pre.get("version") == "0.1.13" and (pre.get("sqlite") or {}).get("ok") is True
        and (pre["sqlite"]["state"]["turn_evidence"], pre["sqlite"]["state"]["turn_evidence_trace_id"]) == (None, None)
        and pre["sqlite"]["state"]["trace_id"] == seed_last["trace_id"]
        and pre["sqlite"]["state"]["same_turn_evidence"] is None, pre.get("sqlite"))
c.check("upgrade: 0.1.13 loads the 0.1.12 local working_state.json through LocalSessionStore with both fields unset; "
        "trace id and goal kept",
        (pre.get("local") or {}).get("ok") is True
        and (pre["local"]["state"]["turn_evidence"], pre["local"]["state"]["turn_evidence_trace_id"]) == (None, None)
        and pre["local"]["state"]["trace_id"] == seed_local["trace_id"]
        and pre["local"]["state"]["goal"] == seed_local["goal"], {"loaded": pre.get("local"), "seed": seed_local})
up_sdk = run_step(PY13, "sdk-turn", home=str(HOME), message="Is order A-104 still ready?")
up_cli = brainctl(PY13, "step", LOCAL_ROOT, "Is order A-104 still ready?")
observed["3_upgrade_continue"] = {"sdk": up_sdk, "brainctl": up_cli}
up_rows = ws_rows(DB)
new_rows = [r for r in row_keys(up_rows) if (r["session_id"], r["version"]) not in
            {(s[0], s[1]) for s in seed_rows}]
old_rows_after = [r for r in up_rows if (r[0], r[1]) in {(s[0], s[1]) for s in seed_rows}]
rv = (up_sdk.get("respond_verify") or [{}])[0] or {}
c.check("upgrade: 0.1.13 continues the SDK conversation (new turn): the respond route verifies the continuation "
        "text with this turn's lookup (trace ids equal, no reasons) and keeps it",
        up_sdk.get("version") == "0.1.13" and run_state0(up_sdk) == "completed"
        and text0(up_sdk).endswith(L.CONTINUATION_TEXT) and rv.get("result") == []
        and (rv.get("state_at_respond") or {}).get("trace_ids_match") is True,
        {"results": up_sdk.get("results"), "respond_verify": up_sdk.get("respond_verify")})
last_new = [r for r in new_rows if r["session_id"] == BRAIN_SID]
seed_keyset = sorted(json.loads(seed_rows[-1][2]).keys())
new_raw = {(r[0], r[1]): json.loads(r[2]) for r in up_rows if (r[0], r[1]) in
           {(n["session_id"], n["version"]) for n in new_rows}}
old_shape = [k for k, st in new_raw.items() if not any(f in st for f in L.NEW_FIELDS)]
observed["3_rows_written_by_0113"] = {
    "rows": [(r["session_id"], r["version"], r["keys"]) for r in new_rows],
    "rows_without_the_fields": old_shape,
    "their_key_set_equals_the_0112_payload": all(sorted(new_raw[k].keys()) == seed_keyset for k in old_shape),
    "note": "the brain bridge's turn-start reset reads the stored dict and writes it back as a new version "
            "(services/brain/post_execution/reset.py _write_working_state_inline); on a 0.1.12 payload that "
            "rewrite has no new fields"}
first_new_version = min((n["version"] for n in new_rows if n["session_id"] == BRAIN_SID), default=None)
c.check("upgrade: in the upgraded SQLite store, 0.1.13's first write to the conversation is the turn-start reset "
        "rewriting the stored 0.1.12 dict (same key set, no new fields); every later row 0.1.13 wrote (save_state) "
        "has both fields, the latest pairs the turn's evidence with its trace id, and the 0.1.12 rows are unchanged",
        bool(new_rows) and old_shape == [(BRAIN_SID, first_new_version)]
        and all(sorted(new_raw[k].keys()) == seed_keyset for k in old_shape)
        and all(r["keys"] == list(L.NEW_FIELDS) for r in new_rows if (r["session_id"], r["version"]) not in old_shape)
        and bool(last_new) and last_new[-1]["has_evidence"]
        and last_new[-1]["turn_evidence_trace_id"] == last_new[-1]["trace_id"]
        and digest_rows(old_rows_after) == digest_rows(seed_rows),
        {"new_rows": [(r["version"], r["keys"]) for r in new_rows], "rows_without_the_fields": old_shape,
         "latest": last_new[-1] if last_new else None,
         "old_rows_unchanged": digest_rows(old_rows_after) == digest_rows(seed_rows)})
up_local = local_state()
c.check("upgrade: `brainctl step` (0.1.13) continues the local session; its working_state.json now has both fields "
        "(null) at a higher version",
        up_cli["rc"] == 0 and up_local["keys"] == list(L.NEW_FIELDS) and up_local["turn_evidence"] is None
        and up_local["turn_evidence_trace_id"] is None and up_local["version"] > seed_local["version"],
        {"brainctl": {k: up_cli[k] for k in ("rc", "status")}, "local": up_local,
         "seed_version": seed_local["version"]})
observed["3_sqlite_files_after_upgrade"] = sqlite_files()

# ======================================================== 4. 0.1.12 against the newer state (scratch copy only)
shutil.copytree(HOME, NEWER_COPY)
rej = run_step(PY12, "inspect", home=str(NEWER_COPY))
rej_sdk = run_step(PY12, "sdk-turn", home=str(NEWER_COPY), message="Is order A-104 still ready?")
rej_cli = brainctl(PY12, "step", NEWER_COPY / "brain-local", "Is order A-104 still ready?")
observed["4_0112_on_newer_state_copy"] = {"inspect": rej, "sdk": rej_sdk, "brainctl": rej_cli}


def extra_forbidden(view: dict[str, Any] | None) -> bool:
    errs = (view or {}).get("errors") or []
    return ((view or {}).get("ok") is False and (view or {}).get("error") == "ValidationError"
            and sorted((e["type"], tuple(e["loc"])) for e in errs)
            == [("extra_forbidden", ("turn_evidence",)), ("extra_forbidden", ("turn_evidence_trace_id",))])


c.check("not supported (shown on a scratch copy, the upgraded home is never opened by 0.1.12): 0.1.12's "
        "load_or_init_state rejects the 0.1.13 working state in both locations with extra_forbidden for "
        "turn_evidence and turn_evidence_trace_id",
        rej.get("version") == "0.1.12" and extra_forbidden(rej.get("sqlite")) and extra_forbidden(rej.get("local")),
        {"sqlite": rej.get("sqlite"), "local": rej.get("local")})
c.check("not supported (scratch copy): the 0.1.12 SDK turn on the upgraded conversation fails with the "
        "extra_forbidden validation error instead of answering, and 0.1.12 `brainctl step` on the upgraded local "
        "session exits non-zero with extra_forbidden",
        not text0(rej_sdk).endswith(L.CONTINUATION_TEXT) and "extra_forbidden" in text0(rej_sdk)
        and rej_cli["rc"] != 0 and rej_cli["extra_forbidden_in_stderr"],
        {"sdk": {k: rej_sdk.get(k) for k in ("rc", "results", "error", "stderr_tail")},
         "brainctl": {k: rej_cli[k] for k in ("rc", "status", "stderr_tail")}})

# ======================================================== 5. rollback: restore the matching backup, run 0.1.12
restored = {}
added_by_upgrade = [rel for rel in observed["3_sqlite_files_after_upgrade"] if rel not in db_files]
for rel in added_by_upgrade:
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(HOME / rel) + suffix)
        if p.exists():
            p.unlink()
observed["5_removed_files_created_after_backup"] = added_by_upgrade
for rel in db_files:
    target = HOME / rel
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(target) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(BACKUP / (rel.replace("/", "__") + ".backup"), target)
    restored[rel] = hashlib.sha256(target.read_bytes()).hexdigest() == backups[rel]["sha256"]
shutil.rmtree(SESSIONS)
shutil.copytree(BACKUP / "sessions", SESSIONS)
observed["5_restored"] = restored
c.check("rollback: with every process stopped, each SQLite file is replaced by its pre-upgrade snapshot (file copy; "
        "0.1.12's `storage restore` is not used, openminion#412) and the `sessions` directory by its copy; the "
        "restored working-state rows and local files equal the pre-upgrade state",
        all(restored.values()) and digest_rows(ws_rows(DB)) == digest_rows(seed_rows)
        and tree_digest(SESSIONS) == tree_digest(BACKUP / "sessions"), restored)
back = run_step(PY12, "inspect", home=str(HOME))
rb_sdk = run_step(PY12, "sdk-turn", home=str(HOME), message="Is order A-104 still ready?")
rb_cli = brainctl(PY12, "step", LOCAL_ROOT, "Is order A-104 still ready?")
observed["5_rollback_0112"] = {"inspect": back, "sdk": rb_sdk, "brainctl": rb_cli}
c.check("rollback: 0.1.12 loads the restored working state in both locations (no new fields; the post-upgrade turn "
        "is gone with the backup)",
        back.get("version") == "0.1.12" and (back.get("sqlite") or {}).get("ok") is True
        and (back.get("local") or {}).get("ok") is True
        and back["sqlite"]["state"]["trace_id"] == seed_last["trace_id"]
        and back["sqlite"]["state"]["turn_evidence"] == "<absent>",
        {"sqlite": back.get("sqlite"), "local": back.get("local")})
rb_rows = row_keys(ws_rows(DB))
c.check("rollback: 0.1.12 continues the SDK conversation and the local session on the restored backup "
        "(turn completed, brainctl rc 0); what it writes has no new fields",
        run_state0(rb_sdk) == "completed" and text0(rb_sdk).endswith(L.CONTINUATION_TEXT) and rb_cli["rc"] == 0
        and all(r["keys"] == [] for r in rb_rows) and local_state()["keys"] == [],
        {"sdk": {k: rb_sdk.get(k) for k in ("version", "rc", "results")},
         "brainctl": {k: rb_cli[k] for k in ("rc", "status")}, "rows": len(rb_rows),
         "local": local_state()})
blocked = [x for res in (seed_sdk, up_sdk, rej_sdk, rb_sdk) for x in res.get("blocked") or []]
c.check("Network: only loopback connections in every SDK step", blocked == [], blocked)

observed["guide"] = {"source": "docs/runtime-database-upgrades.md at v0.1.13 (not in the wheel)",
                     "stop": "each writer is a separate process that exited before the next step",
                     "backup": "0.1.12 `openminion storage backup --sqlite <file> --output <file> --json` for every "
                               "SQLite file of the runtime home; copy of <home>/brain-local/sessions",
                     "rollback": "file copy of the snapshots and the sessions copy; then 0.1.12",
                     "mixed_readers": "not certified, not tested beyond the documented rejection"}
s = c.summary()
write_result(OUT, {"info": info, "observed": sanitize(observed), "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
