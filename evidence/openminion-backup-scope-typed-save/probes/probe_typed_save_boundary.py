"""Where the brain working state gets `turn_evidence` and `turn_evidence_trace_id`
after an upgrade: the turn-start reset rewrites the stored dict as it was read,
and the typed `save_state` adds the two fields.

Runs on the installed packages only (no checkout, no repo test fixtures). Each
step is its own process. The runtime home uses one offline `echo` agent and
the default layout; turns go through `APIRuntime.run_turn` with a session id
and a conversation id, as the HTTP turns route passes them, so later turns
continue the same brain session `<session>::conv:<conversation>`.

Steps:
  1 seed with the published 0.1.12 (its own venv and process): one runtime turn
    in conversation c1, and `brainctl run` in local adapter mode
  2 the package under test: one runtime turn in c1. A read-only recorder on
    SessctlAdapter.put_working_state (records the writer frames, the payload's
    keys and the returned version, then calls the original) attributes every
    working-state write
  3 controls with the package under test: a second turn in c1 (the stored state
    now has both fields), and the first turn of a new conversation c2
  4 `brainctl step` (package under test) on the 0.1.12 local session

Usage: python -I -X utf8 probe_typed_save_boundary.py <python_0.1.12> <work_dir> <out.json>
       (run with the interpreter under test)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, sanitize_text, write_result  # noqa: E402

SID, LOCAL_SID = "s-typed", "local-typed"
NEW_FIELDS = ("turn_evidence", "turn_evidence_trace_id")
PLAIN_DICT_WRITERS = ("_reset_state_for_new_input", "_inject_gateway_system_context", "_inject_resume_task_hints")


def brain_sid(conversation: str) -> str:
    return f"{SID}::conv:{conversation}"


# ---------------------------------------------------------------- step mode (any version)
def step_main(name: str, args: dict[str, Any]) -> None:
    quiet()
    clear_override_env()
    import openminion
    from openminion import APIRuntime

    result: dict[str, Any] = {"step": name, "version": openminion.__version__}
    records: list[dict[str, Any]] = []
    if args.get("record"):
        from openminion.modules.brain.adapters.session.runtime import SessctlAdapter

        original = SessctlAdapter.put_working_state
        pkg = str(Path(openminion.__file__).resolve().parent.parent)

        def recording_put(self: Any, session_id: str, *, state_ref: Any = None, state_inline: Any = None) -> Any:
            frames = [f for f in traceback.extract_stack()[:-1] if "openminion" in f.filename.replace("\\", "/")]
            version = original(self, session_id, state_ref=state_ref, state_inline=state_inline)
            payload = state_inline if isinstance(state_inline, dict) else {}
            records.append({
                "session_id": session_id, "version": version,
                "has_fields": [f for f in NEW_FIELDS if f in payload], "key_count": len(payload),
                "keys": sorted(payload),
                "plain_dict_writer": next((f.name for f in reversed(frames) if f.name in PLAIN_DICT_WRITERS), None),
                "typed_save_state": any(f.name == "save_state" and f.filename.replace("\\", "/").endswith(
                    "openminion/modules/brain/state.py") for f in frames),
                "frames": [f"{Path(f.filename).resolve().as_posix().replace(pkg.replace(chr(92), '/') + '/', '')}"
                           f":{f.lineno} {f.name}" for f in frames[-3:]],
            })
            return version

        SessctlAdapter.put_working_state = recording_put  # type: ignore[method-assign]
    home = Path(args["home"])
    runtime = APIRuntime.from_config_path(str(home / "agents.json"), home_root=home)
    try:
        out = runtime.run_turn(payload={"message": args["message"], "session_id": SID,
                                        "conversation_id": args["conversation"]})
    finally:
        runtime.close()
    result.update({"run_state": out.get("run_state"), "puts": records})
    if args.get("record"):
        from openminion.modules.brain.schemas import WorkingState

        # how the package under test reads each payload it wrote
        db = home / ".openminion" / "state" / "brain" / "sessions.db"
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute("select version, state_inline_json from working_state where session_id=? "
                               "order by version", (brain_sid(args["conversation"]),)).fetchall()
        finally:
            con.close()
        loaded = {}
        for version, inline in rows:
            try:
                st = WorkingState.model_validate(json.loads(inline))
                loaded[version] = {"ok": True, "turn_evidence": st.turn_evidence,
                                   "turn_evidence_trace_id": st.turn_evidence_trace_id}
            except Exception as exc:  # noqa: BLE001 - recorded
                loaded[version] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        result["model_validate"] = loaded
    print("STEP-RESULT " + json.dumps(result, default=str), flush=True)


if len(sys.argv) > 1 and sys.argv[1] == "--step":
    step_main(sys.argv[2], json.loads(sys.argv[3]))
    sys.exit(0)

# ---------------------------------------------------------------- orchestrator
PY12 = sys.argv[1]
PY = sys.executable
WORK = Path(sys.argv[2]).resolve()
OUT = Path(sys.argv[3]).resolve()
quiet()
clear_override_env()
info = banner("probe_typed_save_boundary", python_0112=sanitize_text(PY12))
from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config  # noqa: E402

c = Checks()
observed: dict[str, Any] = {}
if WORK.exists():
    shutil.rmtree(WORK)
HOME = WORK / "home"
LOCAL_ROOT = WORK / "brain-local"
CWD = WORK / "cwd"
for d in (HOME, CWD):
    d.mkdir(parents=True)
DB = HOME / ".openminion" / "state" / "brain" / "sessions.db"
CFG = WORK / "brain-local.yaml"
CFG.write_text("brain:\n  agent_id: openminion\n  budgets:\n    max_ticks_per_user_turn: 20\n    max_tool_calls: 5\n"
               "    max_a2a_calls: 5\n    max_total_llm_tokens: 5000\n    max_elapsed_ms: 120000\n"
               "  adapters:\n    mode: local\n", encoding="utf-8")
ENV = {k: v for k, v in os.environ.items() if k not in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT",
                                                        "OPENMINION_GENERATED_ROOT", "OPENMINION_CONFIG_PATH")}
config = OpenMinionConfig()
config.agents = {"openminion": AgentProfileConfig(name="openminion", provider="echo", default_channel="console")}
config.default_agent = "openminion"
config.runtime.process_mode = "single-process"
config.runtime.daemon_auto_start = False
save_config(config, str(HOME / "agents.json"))


def step(py: str, name: str, **args: Any) -> dict[str, Any]:
    proc = subprocess.run([py, "-I", "-X", "utf8", str(Path(__file__).resolve()), "--step", name, json.dumps(args)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV, cwd=str(CWD))
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("STEP-RESULT "):
            res = json.loads(line[len("STEP-RESULT "):])
            res["rc"] = proc.returncode
            return res
    return {"step": name, "rc": proc.returncode, "error": "no STEP-RESULT",
            "stderr_tail": sanitize_text(proc.stderr.strip())[-1500:]}


def brainctl(py: str, command: str, message: str) -> dict[str, Any]:
    exe = Path(py).parent / ("brainctl.exe" if os.name == "nt" else "brainctl")
    proc = subprocess.run([str(exe), command, "--input", message, "--session-id", LOCAL_SID, "--config", str(CFG),
                           "--root", str(LOCAL_ROOT)], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, cwd=str(CWD))
    try:
        status = json.loads(proc.stdout).get("status")
    except ValueError:
        status = None
    return {"argv": ["brainctl", command, "--session-id", LOCAL_SID], "rc": proc.returncode, "status": status,
            "stderr_tail": [sanitize_text(x) for x in proc.stderr.strip().splitlines()[-4:]]}


def rows(conversation: str) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(DB))
    try:
        found = con.execute("select version, state_inline_json from working_state where session_id=? order by version",
                            (brain_sid(conversation),)).fetchall()
    finally:
        con.close()
    out = []
    for version, inline in found:
        st = json.loads(inline) if inline else {}
        out.append({"version": version, "has_fields": [f for f in NEW_FIELDS if f in st], "keys": sorted(st)})
    return out


def local_state() -> dict[str, Any]:
    data = json.loads((LOCAL_ROOT / "sessions" / LOCAL_SID / "working_state.json").read_text(encoding="utf-8"))
    st = data.get("state_inline") or {}
    return {"version": data.get("version"), "has_fields": [f for f in NEW_FIELDS if f in st],
            "turn_evidence": st.get("turn_evidence", "<absent>"),
            "turn_evidence_trace_id": st.get("turn_evidence_trace_id", "<absent>")}


def brief(puts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: p[k] for k in ("version", "has_fields", "plain_dict_writer", "typed_save_state", "frames")}
            for p in puts]


# ======================================================== 1. seed with 0.1.12
seed = step(PY12, "turn", home=str(HOME), message="first turn, written by 0.1.12", conversation="c1")
seed_cli = brainctl(PY12, "run", "first local step, written by 0.1.12")
seed_rows = rows("c1")
seed_local = local_state()
observed["1_seed_0112"] = {"turn": seed, "rows": [(r["version"], r["has_fields"]) for r in seed_rows],
                           "brainctl": seed_cli, "local": seed_local}
c.check("seed: the published 0.1.12 (own process) completes one runtime turn in conversation c1 and `brainctl run`; "
        "none of the working states it saved has either field",
        seed.get("version") == "0.1.12" and seed.get("rc") == 0 and seed.get("run_state") == "completed"
        and bool(seed_rows) and all(r["has_fields"] == [] for r in seed_rows) and seed_cli["rc"] == 0
        and seed_local["has_fields"] == [],
        {"rows": [(r["version"], r["has_fields"]) for r in seed_rows], "local": seed_local})
seed_last = seed_rows[-1]

# ======================================================== 2. package under test, next turn in c1
up = step(PY, "turn", home=str(HOME), message="second turn, after the upgrade", conversation="c1", record=True)
up_rows = rows("c1")
new_rows = [r for r in up_rows if r["version"] > seed_last["version"]]
puts = [p for p in up.get("puts") or [] if p["session_id"] == brain_sid("c1")]
observed["2_upgrade_turn"] = {"version": up.get("version"), "rc": up.get("rc"), "run_state": up.get("run_state"),
                              "puts": brief(puts), "rows": [(r["version"], r["has_fields"]) for r in new_rows],
                              "model_validate": up.get("model_validate"),
                              "error": up.get("error"), "stderr_tail": up.get("stderr_tail")}
first = puts[0] if puts else {}
c.check("upgrade turn: completes; the recorder saw every row it wrote (same versions as the stored rows)",
        up.get("rc") == 0 and up.get("run_state") == "completed" and bool(puts)
        and [p["version"] for p in puts] == [r["version"] for r in new_rows],
        {"puts": [p["version"] for p in puts], "rows": [r["version"] for r in new_rows]})
c.check("upgrade turn: its first working-state write is the turn-start reset (_reset_state_for_new_input -> "
        "_write_working_state_inline -> put_working_state), version = 0.1.12's last + 1, and it writes the stored "
        "0.1.12 dict back with the same key set, without either field",
        first.get("plain_dict_writer") == "_reset_state_for_new_input" and not first.get("typed_save_state")
        and first.get("version") == seed_last["version"] + 1 and first.get("has_fields") == []
        and first.get("keys") == seed_last["keys"],
        {"first_put": {k: first.get(k) for k in ("version", "plain_dict_writer", "frames", "has_fields")},
         "same_key_set_as_0112": first.get("keys") == seed_last["keys"]})
later = puts[1:]
c.check("upgrade turn: every later write comes from the typed save_state (WorkingState.model_dump) and has both "
        "fields; no other plain-dict writer ran",
        bool(later) and all(p["typed_save_state"] and p["plain_dict_writer"] is None
                            and p["has_fields"] == list(NEW_FIELDS) for p in later),
        brief(later))
mv = up.get("model_validate") or {}
c.check("the package under test reads the reset's old-shape row with both fields unset",
        (mv.get(str(first.get("version"))) or {}).get("ok") is True
        and (mv[str(first["version"])]["turn_evidence"], mv[str(first["version"])]["turn_evidence_trace_id"])
        == (None, None), mv.get(str(first.get("version"))))

# ======================================================== 3. controls
again = step(PY, "turn", home=str(HOME), message="third turn, same conversation", conversation="c1", record=True)
again_puts = [p for p in again.get("puts") or [] if p["session_id"] == brain_sid("c1")]
fresh = step(PY, "turn", home=str(HOME), message="first turn of a new conversation", conversation="c2", record=True)
fresh_puts = [p for p in fresh.get("puts") or [] if p["session_id"] == brain_sid("c2")]
observed["3_controls"] = {"same_conversation_again": {"rc": again.get("rc"), "run_state": again.get("run_state"),
                                                      "puts": brief(again_puts)},
                          "new_conversation": {"rc": fresh.get("rc"), "run_state": fresh.get("run_state"),
                                               "puts": brief(fresh_puts)}}
c.check("control: the next turn in c1 starts with the reset again, and its rewrite now has both fields (the "
        "stored state it copies was saved by save_state); every write has both fields",
        again.get("run_state") == "completed" and bool(again_puts)
        and again_puts[0]["plain_dict_writer"] == "_reset_state_for_new_input"
        and all(p["has_fields"] == list(NEW_FIELDS) for p in again_puts), brief(again_puts[:2]))
c.check("control: the first turn of a new conversation (no stored state) has no reset write; its first write is "
        "save_state with both fields",
        fresh.get("run_state") == "completed" and bool(fresh_puts) and fresh_puts[0]["typed_save_state"]
        and all(p["plain_dict_writer"] is None and p["has_fields"] == list(NEW_FIELDS) for p in fresh_puts),
        brief(fresh_puts[:2]))

# ======================================================== 4. brainctl, local session store
step_cli = brainctl(PY, "step", "second local step, after the upgrade")
up_local = local_state()
observed["4_brainctl_local"] = {"brainctl": step_cli, "local": up_local}
c.check("brainctl step (package under test, local session store) on the 0.1.12 session: its saved "
        "working_state.json has both fields (null) at a higher version",
        step_cli["rc"] == 0 and up_local["has_fields"] == list(NEW_FIELDS) and up_local["turn_evidence"] is None
        and up_local["turn_evidence_trace_id"] is None and up_local["version"] > seed_local["version"],
        {"brainctl": step_cli, "local": up_local, "seed_version": seed_local["version"]})

s = c.summary()
write_result(OUT, {"info": info, "observed": sanitize(observed), "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']}")
sys.exit(0 if c.all_pass else 1)
