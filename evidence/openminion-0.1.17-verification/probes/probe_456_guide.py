"""#456 on the installed package: the published guide's commands on jobs created by older published packages.

The guide is `docs/runtime-database-upgrades.md` at v0.1.17 (git blob
0f6b9c63248a823abd79cf2187268d195d5cb97b, unchanged at v0.1.18), section
"Memory consolidation jobs from earlier packages". docs/ is not in the wheel,
so the probe reads the published byte copy given on the command line, checks
its git blob id, and takes the commands from the section's text: the fenced
bash blocks and the inline `openminion tasks cancel|pause TASK_ID --json`.
Each command is run as written, through a POSIX shell (bash -c, the guide's
syntax: `VAR=value cmd`, backslash continuations, single-quoted JSON), with
only these textual substitutions: TASK_ID and AGENT by the real id and agent
name, and the example settings `{"interval_hours": 12, "batch_limit": 8}` by
the old job's settings noted in step 1. Where the guide states a change in
prose instead of a full command (`add --agent-id AGENT`; for another agent's
replacement `add OPENMINION_AGENT_ID=AGENT and --runtime-source inproc as
above`), the probe composes it and marks it "composed".

`openminion` is the console script of the package under test: its venv's bin
(Scripts) directory is put first on PATH. The runtime home is selected with
OPENMINION_HOME (the documented variable; the config is the default
<home>/.openminion/agents.json); OPENMINION_NO_UPDATE_CHECK=1; cwd is the home.

Setup, not part of the guide (marked "setup"):
- homes and their echo agents, written with 0.1.14's own config API (in the
  0.1.14 venv's python), each with its own daemon IPC port;
- every pre-upgrade job created with 0.1.14's own CLI (`openminion tools run
  task.consolidate_memory`, as the published 0.1.14 exposes it), and the
  mixed-age home's later job with 0.1.16's CLI;
- to make a run happen: the package's own `openminion tasks run-now`, or the
  job's `cron_jobs.next_due_at` set to now (sqlite) before the daemon picks it;
- the real daemon: `openminion daemon start` / `daemon stop`;
- the raw cron API: `openminion api run` and one HTTP POST/GET (urllib).
Evidence reads (marked "evidence") open the home's sqlite files read-only:
task.db (the task kind, which the CLI does not show), the cron tables, the
turn.outcome events (capture_state of each scheduled turn) and the daemon log
(which daemon pid ran each run).

Homes (all with the package under test after the setup):
  A  upgrade from 0.1.14, default agent: two jobs never run; inspect, cancel
     one and pause the other, two replacements with their own settings; the
     real daemon runs the replacements and never runs the stopped jobs.
  B  a 0.1.14 job that ran on 0.1.14, then its first run on this package
     fails and pauses it; resume does not help; cancel on the paused job;
     replacement created while the daemon runs; raw cron API rejection.
  C  default agent `openminion` and non-default agent `helper`, one 0.1.14 job
     each: the default and the non-default command paths.
  D  the default agent is `main`: `tasks` act on main, `tools run` acts as
     `openminion`; main's replacement through OPENMINION_AGENT_ID + inproc.
  E  mixed age: X from 0.1.14, Y from 0.1.16; only X fails and is replaced, Y
     stays enabled and is not replaced.
  F  the replacement created while the old job is still enabled.

Usage: python -I -X utf8 probe_456_guide.py <guide.md> <bin_0114> <bin_0116> <bin_under_test> <bash> <work> <out.json>
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, sanitize, sanitize_text, write_result  # noqa: E402

GUIDE = Path(sys.argv[1]).resolve()
BIN14, BIN16, BIN = (Path(a).resolve() for a in sys.argv[2:5])
BASH = sys.argv[5]
WORK = Path(sys.argv[6]).resolve()
OUT = Path(sys.argv[7]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
clear_override_env()
os.environ.pop("OPENMINION_AGENT_ID", None)

GUIDE_BLOB = "0f6b9c63248a823abd79cf2187268d195d5cb97b"
SECTION = "## Memory consolidation jobs from earlier packages"
MESSAGE = ("memory consolidation job is missing trusted task provenance; it was paused and must be recreated "
           "with task.consolidate_memory")
INSTRUCTION = "Review the provided memory candidates, decide which to promote, discard, or defer"
EXAMPLE_SETTINGS = '{"interval_hours": 12, "batch_limit": 8}'
TERMINAL = ("finished", "failed", "timed_out", "cancelled")
EXE = ".exe" if os.name == "nt" else ""
PY14 = BIN14 / ("python" + EXE)

info = banner("probe_456_guide", guide=str(GUIDE), bin_0114=str(BIN14), bin_0116=str(BIN16), bin_under_test=str(BIN),
              shell=BASH)
c = Checks()
transcript: list[dict[str, Any]] = []
observed: dict[str, Any] = {}


# ---------------------------------------------------------------- the published guide
def git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


guide_bytes = GUIDE.read_bytes()
guide_text = guide_bytes.decode("utf-8")
section = guide_text.split(SECTION, 1)[1].split("\n## ", 1)[0] if SECTION in guide_text else ""
blocks = [textwrap.dedent(b) for b in re.findall(r"^[ ]*```bash\n(.*?)^[ ]*```", section, re.M | re.S)]
inspect_lines = blocks[0].strip("\n").split("\n") if blocks else []
G = {
    "list": inspect_lines[0] if len(inspect_lines) > 0 else "",
    "show": inspect_lines[1] if len(inspect_lines) > 1 else "",
    "task.list": inspect_lines[2] if len(inspect_lines) > 2 else "",
    "task.list other agent": blocks[1].strip("\n") if len(blocks) > 1 else "",
    "cancel": "openminion tasks cancel TASK_ID --json",
    "pause": "openminion tasks pause TASK_ID --json",
    "create": blocks[2].strip("\n") if len(blocks) > 2 else "",
}
flat = " ".join(section.split())
PROSE = {"agent-id": "add `--agent-id AGENT` for another agent's",
         "other create": "For another agent's job, add `OPENMINION_AGENT_ID=AGENT` and `--runtime-source inproc` as above.",
         "cancel": "`openminion tasks cancel TASK_ID --json`", "pause": "`openminion tasks pause TASK_ID --json`"}
observed["guide"] = {"path": str(GUIDE), "sha256": hashlib.sha256(guide_bytes).hexdigest(),
                     "git_blob": git_blob(guide_bytes), "commands": G,
                     "prose_found": {k: v in flat for k, v in PROSE.items()}}


def composed_agent_id(cmd: str) -> str:
    """The guide: 'The tasks commands act on the default agent's tasks; add --agent-id AGENT for another agent's.'"""
    return cmd + " --agent-id AGENT"


def composed_other_create() -> str:
    """The guide, step 3: 'For another agent's job, add OPENMINION_AGENT_ID=AGENT and --runtime-source inproc as
    above' (above: `OPENMINION_AGENT_ID=AGENT openminion tools run task.list \\ --runtime-source inproc --json ...`)."""
    return "OPENMINION_AGENT_ID=AGENT " + G["create"].replace("  --json", "  --runtime-source inproc --json", 1)


def settings_json(every_ms: Any, batch_limit: Any) -> str:
    hours = int(every_ms) // 3600000 if isinstance(every_ms, int) and int(every_ms) % 3600000 == 0 else every_ms
    return json.dumps({"interval_hours": hours, "batch_limit": batch_limit})


# ---------------------------------------------------------------- running commands
def run(home: Path, label: str, kind: str, template: str, *, bindir: Path = BIN, subs: dict[str, str] | None = None,
        timeout: int = 420) -> dict[str, Any]:
    text = template
    for key, value in (subs or {}).items():
        if re.fullmatch(r"[A-Z_]+", key):  # TASK_ID, AGENT: whole words only (not inside OPENMINION_AGENT_ID)
            text = re.sub(rf"(?<![A-Za-z0-9_]){key}(?![A-Za-z0-9_])", lambda _m, v=value: v, text)
        else:
            text = text.replace(key, value)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_")}
    env.update({"OPENMINION_HOME": str(home), "OPENMINION_NO_UPDATE_CHECK": "1", "PYTHONUTF8": "1",
                "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")})
    started = time.monotonic()
    try:
        proc = subprocess.run([BASH, "-c", text], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, cwd=str(home), timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out, err = "timeout", str(exc.stdout or ""), str(exc.stderr or "")
    data: Any = None
    try:
        data = json.loads(out)
    except ValueError:
        pass
    noise = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (INFO|DEBUG) ")
    err_lines = [ln for ln in err.splitlines() if ln.strip() and not noise.match(ln)]
    row = {"home": home.name, "label": label, "kind": kind,
           "package": {BIN14: "0.1.14", BIN16: "0.1.16"}.get(bindir, "under test"),
           "command": text, "template": template if text != template else None, "exit": rc,
           "seconds": round(time.monotonic() - started, 1), "json": data,
           "stdout_head": None if data is not None else sanitize_text(out[:1500]),
           "stderr_tail": sanitize_text("\n".join(err_lines[-15:])) or None}
    transcript.append(row)
    shown = text.replace("\n", "\n    ")
    print(f"\n[{home.name}] ({kind}{'' if row['package'] == 'under test' else ', ' + row['package']}) $ {shown}\n"
          f"    exit={rc} {row['seconds']}s" + (f" stderr: {row['stderr_tail'][:400]}" if row["stderr_tail"] else ""),
          flush=True)
    return row


def tool_data(row: dict[str, Any]) -> dict[str, Any]:
    d = row.get("json") or {}
    return dict(((d.get("tool") or {}).get("data")) or {}) if isinstance(d, dict) else {}


def created_id(row: dict[str, Any]) -> str:
    return str(tool_data(row).get("task_id") or "")


def consolidation_tasks(row: dict[str, Any]) -> list[dict[str, Any]]:
    d = row.get("json") or {}
    rows = d.get("tasks", []) if isinstance(d, dict) else []
    out = []
    for t in rows:
        if not str(t.get("instruction", "")).startswith(INSTRUCTION):
            continue
        last = t.get("last_run") or {}
        err = last.get("error")
        out.append({"id": t.get("id"), "agent_id": t.get("agent_id"), "lifecycle_state": t.get("lifecycle_state"),
                    "enabled": t.get("enabled"), "every_ms": (t.get("schedule") or {}).get("every_ms"),
                    "task_kind": t.get("task_kind"), "valid_actions": t.get("valid_actions"),
                    "last_run": {"state": last.get("state"), "summary": last.get("summary"),
                                 "error": err.get("message") if isinstance(err, dict) else err} if last else None})
    return out


def by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r.get("id")): r for r in rows}


def enabled_ids(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(str(r["id"]) for r in rows if r.get("enabled") is True)


def show_task(row: dict[str, Any]) -> dict[str, Any]:
    d = row.get("json") or {}
    t = dict(d.get("task") or {}) if isinstance(d, dict) else {}
    last = t.get("last_run") or {}
    err = last.get("error")
    return {"id": t.get("id"), "agent_id": t.get("agent_id"), "lifecycle_state": t.get("lifecycle_state"),
            "enabled": t.get("enabled"), "every_ms": (t.get("schedule") or {}).get("every_ms"),
            "instruction_is_consolidation": str(t.get("instruction", "")).startswith(INSTRUCTION),
            "valid_actions": t.get("valid_actions"),
            "last_run": {"state": last.get("state"), "summary": last.get("summary"),
                         "error": err.get("message") if isinstance(err, dict) else err} if last else None}


def task_list_rows(row: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for t in tool_data(row).get("tasks", []) or []:
        cons = t.get("consolidation")
        if not isinstance(cons, dict):
            continue
        out.append({"id": t.get("id") or t.get("task_id"), "agent_id": t.get("agent_id"),
                    "lifecycle_state": t.get("lifecycle_state"), "enabled": t.get("enabled"),
                    "every_ms": (t.get("schedule") or {}).get("every_ms"), "batch_limit": cons.get("batch_limit"),
                    "target_scope": cons.get("target_scope")})
    return out


def values_named(value: Any, needle: str) -> list[str]:
    """Paths of every string value equal to `needle` (the task kind is not shown by the CLI)."""
    hits: list[str] = []

    def walk(v: Any, path: str) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
        elif v == needle:
            hits.append(path)

    walk(value, "")
    return hits


# ---------------------------------------------------------------- evidence (read-only sqlite) and setup writes
def ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)


def ev_tasks(home: Path) -> dict[str, dict[str, Any]]:
    db = home / ".openminion" / "task" / "task.db"
    with closing(ro(db)) as con:
        rows = con.execute("SELECT task_id, state, agent_id, metadata FROM scheduled_tasks").fetchall()
    return {r[0]: {"state": r[1], "agent_id": r[2], "kind": (json.loads(r[3] or "{}") or {}).get("kind")}
            for r in rows}


def sessions_db(home: Path) -> Path:
    return home / ".openminion" / "state" / "brain" / "sessions.db"


def ev_jobs(home: Path) -> dict[str, dict[str, Any]]:
    with closing(ro(sessions_db(home))) as con:
        rows = con.execute("SELECT job_id, enabled, agent_id, payload_json FROM cron_jobs").fetchall()
    return {r[0]: {"enabled": bool(r[1]), "agent_id": r[2],
                   "reserved_payload": "_openminion_memory_consolidation" in (json.loads(r[3] or "{}") or {})}
            for r in rows}


def ev_runs(home: Path, job_id: str) -> list[dict[str, Any]]:
    with closing(ro(sessions_db(home))) as con:
        rows = con.execute("SELECT run_id, state, summary, isolated_session_id, created_at FROM cron_runs "
                           "WHERE job_id = ? ORDER BY created_at", (job_id,)).fetchall()
    return [{"run_id": r[0], "state": r[1], "summary": r[2], "session": r[3]} for r in rows]


def ev_outcomes(home: Path) -> list[dict[str, Any]]:
    db = home / ".openminion" / "state" / "openminion.db"
    if not db.exists():
        return []
    with closing(ro(db)) as con:
        rows = con.execute("SELECT payload_json FROM events WHERE event_type = 'turn.outcome' ORDER BY id").fetchall()
    out = []
    for (raw,) in rows:
        p = json.loads(raw or "{}")
        out.append({"session": p.get("runtime_session_id"), "agent_id": p.get("agent_id"),
                    "capture_state": p.get("capture_state"), "root_turn_id": p.get("root_turn_id")})
    return out


def daemon_pids_for(home: Path, run_ids: list[str]) -> dict[str, Any]:
    log = home / ".openminion" / "logs" / "openminiond.log"
    found: dict[str, Any] = {}
    if not log.exists():
        return found
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "event=cron.run.finished" not in line or "payload=" not in line:
            continue
        try:
            p = json.loads(line.split("payload=", 1)[1])
        except ValueError:
            continue
        if p.get("run_id") in run_ids:
            found[p["run_id"]] = p.get("daemon_pid")
    return found


def due_now(home: Path, job_ids: list[str]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(str(sessions_db(home)), timeout=30)) as con:
        for job in job_ids:
            con.execute("UPDATE cron_jobs SET next_due_at = ? WHERE job_id = ?", (now, job))
        con.commit()
    transcript.append({"home": home.name, "kind": "setup", "label": "cron_jobs.next_due_at set to now (sqlite)",
                       "jobs": job_ids, "value": now})
    print(f"[{home.name}] (setup) cron_jobs.next_due_at of {job_ids} := {now}", flush=True)


def wait_runs(home: Path, job_ids: list[str], known: dict[str, set[str]], timeout: float = 240) -> dict[str, Any]:
    """Wait until each job has a terminal run that is not in `known` (sqlite polling)."""
    deadline = time.monotonic() + timeout
    result: dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = {}
        for job in job_ids:
            new = [r for r in ev_runs(home, job) if r["run_id"] not in known.get(job, set())]
            done = [r for r in new if r["state"] in TERMINAL]
            result[job] = done[-1] if done else None
        if all(result.values()):
            break
        time.sleep(1)
    transcript.append({"home": home.name, "kind": "evidence", "label": "wait for new terminal runs",
                       "result": result})
    print(f"[{home.name}] (evidence) new terminal runs: "
          f"{ {j: (r or {}).get('state') for j, r in result.items()} }", flush=True)
    return result


def run_ids(home: Path, job: str) -> set[str]:
    return {r["run_id"] for r in ev_runs(home, job)}


def daemon(home: Path, action: str) -> dict[str, Any]:
    row = run(home, f"daemon {action}", "setup", f"openminion daemon {action}")
    if action == "start" and "Started daemon" not in str(row.get("stdout_head") or ""):
        row = run(home, "daemon start (retry)", "setup", "openminion daemon start")
    m = re.search(r"pid=(\d+)", str(row.get("stdout_head") or ""))
    return {"exit": row["exit"], "pid": int(m.group(1)) if m else None, "out": row.get("stdout_head")}


def make_home(home: Path, agents: list[str], default: str, port: int) -> None:
    home.mkdir(parents=True, exist_ok=True)
    code = (
        "import sys\nfrom pathlib import Path\n"
        "from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config\n"
        "root, default, port, agents = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4:]\n"
        "config = OpenMinionConfig()\n"
        "config.agents = {a: AgentProfileConfig(name=a, provider='echo', default_channel='console') for a in agents}\n"
        "config.default_agent = default\nconfig.runtime.daemon_auto_start = False\nconfig.runtime.ipc_port = port\n"
        "(root / '.openminion').mkdir(parents=True, exist_ok=True)\n"
        "save_config(config, str(root / '.openminion' / 'agents.json'))\n"
        "import openminion; print('written with openminion', openminion.__version__)\n")
    proc = subprocess.run([str(PY14), "-I", "-c", code, str(home), default, str(port), *agents], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)
    transcript.append({"home": home.name, "kind": "setup", "label": "agents.json with 0.1.14's config API",
                       "agents": agents, "default_agent": default, "ipc_port": port, "exit": proc.returncode,
                       "stdout": proc.stdout.strip(), "stderr_tail": sanitize_text(proc.stderr[-800:]) or None})
    print(f"[{home.name}] (setup, 0.1.14) agents.json: agents={agents} default={default} ipc_port={port}: "
          f"{proc.stdout.strip()} exit={proc.returncode}", flush=True)


def old_create(home: Path, bindir: Path, settings: str, agent: str | None = None) -> str:
    template = G["create"].replace(EXAMPLE_SETTINGS, settings)
    if agent:
        template = f"OPENMINION_AGENT_ID={agent} " + template
    row = run(home, "pre-upgrade job created by the older package's own CLI", "setup", template, bindir=bindir)
    return created_id(row)


def guarded(name: str, body: Any) -> dict[str, Any]:
    try:
        return body()
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"{name}: case error {type(exc).__name__}: {exc}", flush=True)
        return {"case_error": f"{type(exc).__name__}: {exc}",
                "case_error_frames": [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                      for f in traceback.extract_tb(exc.__traceback__)][-8:]}


# ---------------------------------------------------------------- homes
def home_a() -> dict[str, Any]:
    h = WORK / "A"
    x: dict[str, Any] = {}
    make_home(h, ["openminion"], "openminion", 18911)
    a1 = old_create(h, BIN14, EXAMPLE_SETTINGS)
    a2 = old_create(h, BIN14, '{"interval_hours": 6, "batch_limit": 5}')
    x["ids"] = {"A1": a1, "A2": a2}
    x["kind_before"] = ev_tasks(h)
    # inspect (step 1 notes agent_id, schedule.every_ms, consolidation.batch_limit)
    x["list"] = consolidation_tasks(lst := run(h, "inspect", "guide", G["list"]))
    x["list_raw_kind_hits"] = values_named(lst.get("json"), "memory_consolidation")
    x["show"] = {k: show_task(run(h, "inspect", "guide", G["show"], subs={"TASK_ID": v})) for k, v in x["ids"].items()}
    x["task.list"] = task_list_rows(run(h, "inspect", "guide", G["task.list"]))
    noted = {}
    for k, tid in x["ids"].items():
        tl = next((r for r in x["task.list"] if r["id"] == tid), {})
        noted[k] = {"agent_id": x["show"][k].get("agent_id"), "every_ms": x["show"][k].get("every_ms"),
                    "batch_limit": tl.get("batch_limit")}
    x["noted"] = noted
    # step 2: stop both before any replacement exists
    x["cancel"] = run(h, "step 2", "guide", G["cancel"], subs={"TASK_ID": a1})["json"]
    x["pause"] = run(h, "step 2", "guide", G["pause"], subs={"TASK_ID": a2})["json"]
    x["after_stop"] = consolidation_tasks(run(h, "after step 2", "guide", G["list"]))
    # step 3: one replacement per old job, with the noted settings
    reps = {}
    for k in ("A1", "A2"):
        s = settings_json(noted[k]["every_ms"], noted[k]["batch_limit"])
        row = run(h, "step 3", "guide", G["create"], subs={EXAMPLE_SETTINGS: s})
        reps[k] = {"id": created_id(row), "settings": s, "runtime_source": (row.get("json") or {}).get("runtime_source"),
                   "target_scope": tool_data(row).get("target_scope")}
    x["replacements"] = reps
    x["kind_after"] = ev_tasks(h)
    # step 4
    x["step4"] = consolidation_tasks(run(h, "step 4", "guide", G["list"]))
    x["step4_task.list"] = task_list_rows(run(h, "step 4 settings", "guide", G["task.list"]))
    x["jobs_after"] = ev_jobs(h)
    # the real daemon: the replacements run, the stopped jobs never do
    r1, r2 = reps["A1"]["id"], reps["A2"]["id"]
    known = {j: run_ids(h, j) for j in (a1, a2, r1, r2)}
    due_now(h, [a1, a2, r2])
    x["daemon_start"] = daemon(h, "start")
    run(h, "make a run of the replacement", "setup", "openminion tasks run-now TASK_ID --json", subs={"TASK_ID": r1})
    x["runs"] = wait_runs(h, [r1, r2], known)
    time.sleep(8)
    x["stopped_job_runs"] = {k: ev_runs(h, v) for k, v in (("A1", a1), ("A2", a2))}
    x["daemon_pids"] = daemon_pids_for(h, [str((r or {}).get("run_id")) for r in x["runs"].values()])
    x["daemon_stop"] = daemon(h, "stop")
    x["outcomes"] = ev_outcomes(h)
    x["final"] = consolidation_tasks(run(h, "after the daemon's runs", "guide", G["list"]))
    return x


def home_b() -> dict[str, Any]:
    h = WORK / "B"
    x: dict[str, Any] = {}
    make_home(h, ["openminion"], "openminion", 18912)
    b1 = old_create(h, BIN14, '{"interval_hours": 6, "batch_limit": 5}')
    x["ids"] = {"B1": b1}
    # it ran on 0.1.14 (before the upgrade), under 0.1.14's daemon
    run(h, "0.1.14 daemon", "setup", "openminion daemon start", bindir=BIN14)
    run(h, "a run on 0.1.14", "setup", "openminion tasks run-now TASK_ID --json", bindir=BIN14, subs={"TASK_ID": b1})
    x["run_on_0114"] = wait_runs(h, [b1], {b1: set()})
    run(h, "0.1.14 daemon", "setup", "openminion daemon stop", bindir=BIN14)
    x["outcomes_0114"] = ev_outcomes(h)
    x["kind_before"] = ev_tasks(h)
    x["list_before"] = consolidation_tasks(run(h, "inspect before any run on this package", "guide", G["list"]))
    # the daemon of this package runs it
    known = {b1: run_ids(h, b1)}
    due_now(h, [b1])
    x["daemon_start"] = daemon(h, "start")
    x["legacy_run"] = wait_runs(h, [b1], known)
    x["outcomes_after_legacy_run"] = ev_outcomes(h)
    x["list_after_run"] = consolidation_tasks(run(h, "inspect", "guide", G["list"]))
    x["show_after_run"] = show_task(run(h, "inspect", "guide", G["show"], subs={"TASK_ID": b1}))
    x["task.list"] = task_list_rows(run(h, "inspect", "guide", G["task.list"]))
    x["jobs_after_run"] = ev_jobs(h)
    # resuming it does not help
    x["resume"] = run(h, "guide: 'Resuming it does not help'", "composed", "openminion tasks resume TASK_ID --json",
                      subs={"TASK_ID": b1})["json"]
    x["after_resume"] = show_task(run(h, "after resume", "guide", G["show"], subs={"TASK_ID": b1}))
    known = {b1: run_ids(h, b1)}
    due_now(h, [b1])
    x["resumed_run"] = wait_runs(h, [b1], known)
    x["after_resumed_run"] = show_task(run(h, "after the resumed run", "guide", G["show"], subs={"TASK_ID": b1}))
    # step 2 on the paused job, step 3 with the daemon running (default path, runtime_source auto)
    x["cancel"] = run(h, "step 2", "guide", G["cancel"], subs={"TASK_ID": b1})["json"]
    s = settings_json(x["show_after_run"].get("every_ms"),
                      next((r["batch_limit"] for r in x["task.list"] if r["id"] == b1), None))
    row = run(h, "step 3 (daemon running)", "guide", G["create"], subs={EXAMPLE_SETTINGS: s})
    rb = created_id(row)
    x["replacement"] = {"id": rb, "settings": s, "runtime_source": (row.get("json") or {}).get("runtime_source")}
    x["resume_cancelled"] = run(h, "guide: 'A cancelled job cannot be resumed next to its replacement'", "composed",
                                "openminion tasks resume TASK_ID --json", subs={"TASK_ID": b1})
    x["resume_cancelled"] = {"exit": x["resume_cancelled"]["exit"], "json": x["resume_cancelled"]["json"],
                             "stdout_head": x["resume_cancelled"]["stdout_head"],
                             "stderr_tail": x["resume_cancelled"]["stderr_tail"]}
    x["step4"] = consolidation_tasks(run(h, "step 4", "guide", G["list"]))
    x["step4_task.list"] = task_list_rows(run(h, "step 4 settings", "guide", G["task.list"]))
    x["kind_after"] = ev_tasks(h)
    known = {rb: run_ids(h, rb)}
    due_now(h, [rb])
    x["replacement_run"] = wait_runs(h, [rb], known)
    x["daemon_pids"] = daemon_pids_for(h, [str((r or {}).get("run_id")) for r in
                                           (x["legacy_run"][b1], x["resumed_run"][b1], x["replacement_run"][rb])])
    x["daemon_stop"] = daemon(h, "stop")
    x["outcomes"] = ev_outcomes(h)
    x["runs_b1"] = ev_runs(h, b1)
    # raw cron API
    x["api"] = raw_api(h, 18932)
    x["list_after_api"] = consolidation_tasks(run(h, "after the API call", "guide", G["list"]))
    return x


def raw_api(h: Path, port: int) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_")}
    env.update({"OPENMINION_HOME": str(h), "OPENMINION_NO_UPDATE_CHECK": "1", "PYTHONUTF8": "1",
                "PATH": str(BIN) + os.pathsep + os.environ.get("PATH", "")})
    cmd = f"openminion api run --host 127.0.0.1 --port {port}"
    log = WORK / f"api-{h.name}.log"
    out: dict[str, Any] = {"command": cmd}
    jobs_before = ev_jobs(h)
    with open(log, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen([BASH, "-c", "exec " + cmd], stdout=fh, stderr=subprocess.STDOUT, env=env, cwd=str(h))
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(180):
            try:
                with urllib.request.urlopen(base + "/v1/cron/jobs", timeout=5) as resp:
                    out["get_before"] = {"status": resp.status, "jobs": [
                        (j.get("job_id"), j.get("enabled")) for j in json.loads(resp.read()).get("jobs", [])]}
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                if proc.poll() is not None:
                    break
                time.sleep(1)
        body = {"name": "raw consolidation", "agent_id": "openminion",
                "schedule": {"kind": "every", "every_ms": 43200000},
                "payload": {"kind": "agentTurn", "message": "review memory candidates",
                            "_openminion_memory_consolidation": {"target_scope": "agent:openminion"}}}
        out["post_body"] = body
        req = urllib.request.Request(base + "/v1/cron/jobs", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out["post"] = {"status": resp.status, "body": json.loads(resp.read() or b"{}")}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed: Any = json.loads(raw)
            except ValueError:
                parsed = raw.decode("utf-8", "replace")[:1000]
            out["post"] = {"status": exc.code, "body": parsed}
        except Exception as exc:  # noqa: BLE001
            out["post"] = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            with urllib.request.urlopen(base + "/v1/cron/jobs", timeout=30) as resp:
                out["get_after"] = {"status": resp.status, "jobs": [
                    (j.get("job_id"), j.get("enabled")) for j in json.loads(resp.read()).get("jobs", [])]}
        except Exception as exc:  # noqa: BLE001
            out["get_after"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    out["jobs_before"] = sorted(jobs_before)
    out["jobs_after"] = sorted(ev_jobs(h))
    out["server_log_tail"] = sanitize_text("\n".join(
        ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
        if " INFO " not in ln)[-1500:])
    transcript.append({"home": h.name, "kind": "setup", "label": "raw cron API", **out})
    print(f"[{h.name}] (setup) $ {cmd} &  POST /v1/cron/jobs -> {out.get('post')}", flush=True)
    return out


def home_c() -> dict[str, Any]:
    h = WORK / "C"
    x: dict[str, Any] = {}
    make_home(h, ["openminion", "helper"], "openminion", 18913)
    c1 = old_create(h, BIN14, EXAMPLE_SETTINGS)
    c2 = old_create(h, BIN14, '{"interval_hours": 24, "batch_limit": 10}', agent="helper")
    x["ids"] = {"C1": c1, "C2": c2}
    x["kind_before"] = ev_tasks(h)
    sub = {"AGENT": "helper"}
    x["list_default"] = consolidation_tasks(run(h, "inspect, default agent", "guide", G["list"]))
    x["list_helper"] = consolidation_tasks(run(h, "inspect, agent helper", "composed",
                                               composed_agent_id(G["list"]), subs=sub))
    x["show_c1"] = show_task(run(h, "inspect, default agent", "guide", G["show"], subs={"TASK_ID": c1}))
    x["show_c2"] = show_task(run(h, "inspect, agent helper", "composed", composed_agent_id(G["show"]),
                                 subs={"TASK_ID": c2, **sub}))
    x["task.list_default"] = task_list_rows(run(h, "inspect, as openminion", "guide", G["task.list"]))
    x["task.list_helper"] = task_list_rows(run(h, "inspect, agent helper", "guide", G["task.list other agent"],
                                               subs=sub))
    x["cancel_c1"] = run(h, "step 2, default agent", "guide", G["cancel"], subs={"TASK_ID": c1})["json"]
    x["cancel_c2"] = run(h, "step 2, agent helper", "composed", composed_agent_id(G["cancel"]),
                         subs={"TASK_ID": c2, **sub})["json"]
    b1 = next((r["batch_limit"] for r in x["task.list_default"] if r["id"] == c1), None)
    b2 = next((r["batch_limit"] for r in x["task.list_helper"] if r["id"] == c2), None)
    row1 = run(h, "step 3, default agent", "guide", G["create"],
               subs={EXAMPLE_SETTINGS: settings_json(x["show_c1"].get("every_ms"), b1)})
    row2 = run(h, "step 3, agent helper", "composed", composed_other_create(),
               subs={EXAMPLE_SETTINGS: settings_json(x["show_c2"].get("every_ms"), b2), **sub})
    rc1, rc2 = created_id(row1), created_id(row2)
    x["replacements"] = {"C1": {"id": rc1, "target_scope": tool_data(row1).get("target_scope"),
                                "runtime_source": (row1.get("json") or {}).get("runtime_source")},
                         "C2": {"id": rc2, "target_scope": tool_data(row2).get("target_scope"),
                                "runtime_source": (row2.get("json") or {}).get("runtime_source")}}
    x["step4_default"] = consolidation_tasks(run(h, "step 4, default agent", "guide", G["list"]))
    x["step4_helper"] = consolidation_tasks(run(h, "step 4, agent helper", "composed", composed_agent_id(G["list"]),
                                                subs=sub))
    x["step4_task.list_helper"] = task_list_rows(run(h, "step 4 settings, agent helper", "guide",
                                                     G["task.list other agent"], subs=sub))
    x["kind_after"] = ev_tasks(h)
    x["jobs_after"] = ev_jobs(h)
    known = {j: run_ids(h, j) for j in (rc1, rc2)}
    due_now(h, [rc1, rc2])
    x["daemon_start"] = daemon(h, "start")
    x["runs"] = wait_runs(h, [rc1, rc2], known)
    x["daemon_pids"] = daemon_pids_for(h, [str((r or {}).get("run_id")) for r in x["runs"].values()])
    x["daemon_stop"] = daemon(h, "stop")
    x["outcomes"] = ev_outcomes(h)
    return x


def home_d() -> dict[str, Any]:
    h = WORK / "D"
    x: dict[str, Any] = {}
    make_home(h, ["main"], "main", 18914)
    d1 = old_create(h, BIN14, EXAMPLE_SETTINGS, agent="main")
    x["ids"] = {"D1": d1}
    x["kind_before"] = ev_tasks(h)
    sub = {"AGENT": "main"}
    x["list"] = consolidation_tasks(run(h, "inspect (tasks: the default agent main)", "guide", G["list"]))
    x["show"] = show_task(run(h, "inspect", "guide", G["show"], subs={"TASK_ID": d1}))
    x["task.list_plain"] = task_list_rows(run(h, "inspect (tools run acts as openminion)", "guide", G["task.list"]))
    x["task.list_main"] = task_list_rows(run(h, "inspect, agent main", "guide", G["task.list other agent"], subs=sub))
    x["cancel"] = run(h, "step 2", "guide", G["cancel"], subs={"TASK_ID": d1})["json"]
    b = next((r["batch_limit"] for r in x["task.list_main"] if r["id"] == d1), None)
    s = settings_json(x["show"].get("every_ms"), b)
    row = run(h, "step 3, agent main", "composed", composed_other_create(), subs={EXAMPLE_SETTINGS: s, **sub})
    rd = created_id(row)
    x["replacement"] = {"id": rd, "target_scope": tool_data(row).get("target_scope"),
                        "runtime_source": (row.get("json") or {}).get("runtime_source")}
    x["step4"] = consolidation_tasks(run(h, "step 4", "guide", G["list"]))
    x["step4_task.list_main"] = task_list_rows(run(h, "step 4 settings, agent main", "guide",
                                                   G["task.list other agent"], subs=sub))
    # what the guide warns about: `openminion tools run` acts as openminion, whatever the default agent is
    row = run(h, "observation: step 3 without OPENMINION_AGENT_ID", "guide", G["create"])
    stray = created_id(row)
    x["stray"] = {"id": stray, "target_scope": tool_data(row).get("target_scope")}
    x["stray_list_openminion"] = consolidation_tasks(run(h, "observation", "composed", composed_agent_id(G["list"]),
                                                         subs={"AGENT": "openminion"}))
    x["stray_list_main"] = consolidation_tasks(run(h, "observation", "guide", G["list"]))
    x["stray_cancel"] = run(h, "observation: undo", "composed", composed_agent_id(G["cancel"]),
                            subs={"TASK_ID": stray, "AGENT": "openminion"})["json"]
    x["kind_after"] = ev_tasks(h)
    known = {rd: run_ids(h, rd)}
    due_now(h, [rd])
    x["daemon_start"] = daemon(h, "start")
    x["runs"] = wait_runs(h, [rd], known)
    # with the daemon running, OPENMINION_AGENT_ID without --runtime-source inproc (why the guide says inproc)
    no_inproc = "OPENMINION_AGENT_ID=AGENT " + G["task.list"]
    row = run(h, "observation: OPENMINION_AGENT_ID without --runtime-source inproc, daemon running", "composed",
              no_inproc, subs=sub)
    x["task.list_main_via_daemon"] = {"runtime_source": (row.get("json") or {}).get("runtime_source"),
                                      "rows": task_list_rows(row)}
    x["daemon_pids"] = daemon_pids_for(h, [str((r or {}).get("run_id")) for r in x["runs"].values()])
    x["daemon_stop"] = daemon(h, "stop")
    x["outcomes"] = ev_outcomes(h)
    return x


def home_e() -> dict[str, Any]:
    h = WORK / "E"
    x: dict[str, Any] = {}
    make_home(h, ["openminion"], "openminion", 18915)
    xx = old_create(h, BIN14, EXAMPLE_SETTINGS)
    yy = old_create(h, BIN16, '{"interval_hours": 6, "batch_limit": 5}')
    x["ids"] = {"X": xx, "Y": yy}
    x["kind_before"] = ev_tasks(h)
    x["list_before"] = consolidation_tasks(run(h, "inspect", "guide", G["list"]))
    x["task.list_before"] = task_list_rows(run(h, "inspect", "guide", G["task.list"]))
    known = {j: run_ids(h, j) for j in (xx, yy)}
    due_now(h, [xx, yy])
    x["daemon_start"] = daemon(h, "start")
    x["runs_1"] = wait_runs(h, [xx, yy], known)
    known = {yy: run_ids(h, yy)}
    due_now(h, [yy])
    x["runs_2"] = wait_runs(h, [yy], known)
    x["daemon_pids"] = daemon_pids_for(h, [str((r or {}).get("run_id")) for r in
                                           (x["runs_1"][xx], x["runs_1"][yy], x["runs_2"][yy])])
    x["daemon_stop"] = daemon(h, "stop")
    x["list_after_runs"] = consolidation_tasks(run(h, "inspect after the runs", "guide", G["list"]))
    x["show_x"] = show_task(run(h, "inspect", "guide", G["show"], subs={"TASK_ID": xx}))
    x["cancel"] = run(h, "step 2 (X only)", "guide", G["cancel"], subs={"TASK_ID": xx})["json"]
    b = next((r["batch_limit"] for r in x["task.list_before"] if r["id"] == xx), None)
    row = run(h, "step 3 (X only)", "guide", G["create"], subs={EXAMPLE_SETTINGS: settings_json(x["show_x"].get(
        "every_ms"), b)})
    x["replacement"] = {"id": created_id(row)}
    x["step4"] = consolidation_tasks(run(h, "step 4", "guide", G["list"]))
    x["kind_after"] = ev_tasks(h)
    x["outcomes"] = ev_outcomes(h)
    return x


def home_f() -> dict[str, Any]:
    h = WORK / "F"
    x: dict[str, Any] = {}
    make_home(h, ["openminion"], "openminion", 18916)
    f1 = old_create(h, BIN14, EXAMPLE_SETTINGS)
    row = run(h, "replacement while the old job is enabled (what the guide warns about)", "guide", G["create"])
    rf = created_id(row)
    x["ids"] = {"F1": f1, "RF": rf}
    x["list_both"] = consolidation_tasks(run(h, "inspect", "guide", G["list"]))
    known = {f1: run_ids(h, f1)}
    due_now(h, [f1])
    x["daemon_start"] = daemon(h, "start")
    x["legacy_run"] = wait_runs(h, [f1], known)
    x["daemon_stop"] = daemon(h, "stop")
    x["list_after"] = consolidation_tasks(run(h, "after the old job's run", "guide", G["list"]))
    return x


ONLY = os.environ.get("PROBE_456_ONLY", "")  # debugging only: the recorded runs leave it unset
for name, fn in (("A", home_a), ("B", home_b), ("C", home_c), ("D", home_d), ("E", home_e), ("F", home_f)):
    if ONLY and name not in ONLY:
        observed[name] = {"skipped": "PROBE_456_ONLY"}
        continue
    print(f"\n======== home {name}", flush=True)
    observed[name] = guarded(name, fn)


# ---------------------------------------------------------------- checks
def g(d: Any, *path: Any) -> Any:
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and -len(d) <= p < len(d):
            d = d[p]
        else:
            return None
    return d


def excluded_turns(outcomes: list[dict[str, Any]], session: str) -> list[str]:
    return [o["capture_state"] for o in outcomes if o.get("session") == session]


ok_guide = (observed["guide"]["git_blob"] == GUIDE_BLOB and all(observed["guide"]["prose_found"].values())
            and len(blocks) == 3 and G["list"] == "openminion tasks list --json"
            and G["show"] == "openminion tasks show TASK_ID --json"
            and G["task.list"] == "openminion tools run task.list --json '{\"limit\": 50}'"
            and G["task.list other agent"] == ("OPENMINION_AGENT_ID=AGENT openminion tools run task.list \\\n"
                                               "  --runtime-source inproc --json '{\"limit\": 50}'")
            and G["create"] == ("openminion tools run task.consolidate_memory \\\n"
                                "  --json '{\"interval_hours\": 12, \"batch_limit\": 8}'"))
c.check("guide. the published guide is the v0.1.17 revision (git blob 0f6b9c63...) and its section 'Memory "
        "consolidation jobs from earlier packages' gives the commands this probe runs (three bash blocks and the "
        "inline cancel/pause commands, taken from the file, not retyped)",
        ok_guide, {"git_blob": observed["guide"]["git_blob"], "sha256": observed["guide"]["sha256"],
                   "commands": G, "prose_found": observed["guide"]["prose_found"]})

# ---- item 1
A = observed["A"]
ids = A.get("ids") or {}
a1, a2 = ids.get("A1"), ids.get("A2")
lst = by_id(A.get("list") or [])
tl = by_id(A.get("task.list") or [])
c.check("1. A (upgrade from 0.1.14, default agent): the published inspect commands show both never-replaced 0.1.14 "
        "jobs: `tasks list` lists them as consolidation tasks (the instruction), active and enabled, for agent "
        "openminion; `tasks show` gives schedule.every_ms (43200000, 21600000); `task.list` gives "
        "consolidation.batch_limit (8, 5); the task kind is not shown anywhere (the task database has none for them)",
        bool(a1 and a2) and all(g(lst, t, "lifecycle_state") == "active" and g(lst, t, "enabled") is True
                                and g(lst, t, "agent_id") == "openminion" for t in (a1, a2))
        and g(A, "show", "A1", "every_ms") == 43200000 and g(A, "show", "A2", "every_ms") == 21600000
        and g(tl, a1, "batch_limit") == 8 and g(tl, a2, "batch_limit") == 5
        and A.get("list_raw_kind_hits") == []
        and all(g(A, "kind_before", t, "kind") is None for t in (a1, a2)),
        {"list": A.get("list"), "show": A.get("show"), "task.list": A.get("task.list"), "noted": A.get("noted"),
         "kind_in_task_db": A.get("kind_before"), "cli_values_equal_memory_consolidation": A.get("list_raw_kind_hits")})
after = by_id(A.get("after_stop") or [])
c.check("1. A, step 2 before any replacement: `tasks cancel` leaves A1 cancelled and not enabled; `tasks pause` "
        "leaves A2 paused with enabled false and resumable (valid action resume); no consolidation task is enabled "
        "before step 3",
        g(after, a1, "lifecycle_state") == "cancelled" and g(after, a1, "enabled") is False
        and g(after, a2, "lifecycle_state") == "paused" and g(after, a2, "enabled") is False
        and "resume" in (g(after, a2, "valid_actions") or []) and enabled_ids(A.get("after_stop") or []) == [],
        {"after_step2": A.get("after_stop"), "cancel": g(A, "cancel", "task") or A.get("cancel"),
         "pause": g(A, "pause", "task") or A.get("pause")})
reps = A.get("replacements") or {}
r1, r2 = g(reps, "A1", "id"), g(reps, "A2", "id")
s4 = by_id(A.get("step4") or [])
s4tl = by_id(A.get("step4_task.list") or [])
c.check("1. A, step 3: each replacement is created by `task.consolidate_memory` with the settings noted in step 1 "
        "(interval_hours 12 / batch_limit 8 and 6 / 5), for the same agent openminion, and the task database gives "
        "it the kind memory_consolidation",
        bool(r1 and r2) and g(reps, "A1", "settings") == EXAMPLE_SETTINGS
        and g(reps, "A2", "settings") == '{"interval_hours": 6, "batch_limit": 5}'
        and g(s4, r1, "every_ms") == 43200000 and g(s4tl, r1, "batch_limit") == 8
        and g(s4, r2, "every_ms") == 21600000 and g(s4tl, r2, "batch_limit") == 5
        and all(g(s4, r, "agent_id") == "openminion" for r in (r1, r2))
        and all(g(A, "kind_after", r, "kind") == "memory_consolidation" for r in (r1, r2)),
        {"replacements": reps, "step4_task.list": A.get("step4_task.list"), "kinds": A.get("kind_after")})
B = observed["B"]
b1 = g(B, "ids", "B1")
sb = B.get("show_after_run") or {}
c.check("1. B (a 0.1.14 job that already ran on 0.1.14): after its first run on this package, `tasks show` gives it "
        "paused, enabled false, last_run failed with the guide's message; `tasks cancel` works on the paused job "
        "(cancelled); its replacement has its settings (6 h / 5)",
        g(B, "run_on_0114", b1, "state") == "finished" and sb.get("lifecycle_state") == "paused"
        and sb.get("enabled") is False and g(sb, "last_run", "state") == "failed"
        and g(sb, "last_run", "error") == MESSAGE
        and g(by_id(B.get("step4") or []), b1, "lifecycle_state") == "cancelled"
        and g(B, "replacement", "settings") == '{"interval_hours": 6, "batch_limit": 5}'
        and g(by_id(B.get("step4_task.list") or []), g(B, "replacement", "id"), "batch_limit") == 5,
        {"run_on_0.1.14": B.get("run_on_0114"), "show_after_first_run": sb, "cancel": B.get("cancel"),
         "replacement": B.get("replacement"), "step4": B.get("step4")})

# ---- item 2
c.check("2. A, step 4: the old jobs are not enabled (A1 cancelled, A2 paused with enabled false) and both "
        "replacements are enabled with the old jobs' agent_id; after this upgrade from 0.1.14 the replacements are "
        "the only enabled consolidation tasks (also in the cron table)",
        g(s4, a1, "lifecycle_state") == "cancelled" and g(s4, a1, "enabled") is False
        and g(s4, a2, "lifecycle_state") == "paused" and g(s4, a2, "enabled") is False
        and enabled_ids(A.get("step4") or []) == sorted([str(r1), str(r2)])
        and sorted(j for j, v in (A.get("jobs_after") or {}).items() if v["enabled"] and v["reserved_payload"])
        == sorted([str(r1), str(r2)]),
        {"step4": A.get("step4"), "cron_jobs": A.get("jobs_after")})
c.check("2. B, step 4: the old job is cancelled and not enabled, the replacement is enabled for openminion and is the "
        "only enabled consolidation task",
        enabled_ids(B.get("step4") or []) == [str(g(B, "replacement", "id"))]
        and g(by_id(B.get("step4") or []), b1, "enabled") is False
        and g(by_id(B.get("step4") or []), g(B, "replacement", "id"), "agent_id") == "openminion",
        {"step4": B.get("step4")})
E = observed["E"]
xx, yy = g(E, "ids", "X"), g(E, "ids", "Y")
e4 = by_id(E.get("step4") or [])
rx = g(E, "replacement", "id")
c.check("2. E (mixed age: X from 0.1.14, Y from 0.1.16): before any run both are active and enabled and the CLI "
        "cannot tell them apart; the task database has no kind for X and memory_consolidation for Y",
        all(g(by_id(E.get("list_before") or []), t, "enabled") is True for t in (xx, yy))
        and g(E, "kind_before", xx, "kind") is None and g(E, "kind_before", yy, "kind") == "memory_consolidation"
        and g(by_id(E.get("list_before") or []), xx, "task_kind") == g(by_id(E.get("list_before") or []), yy,
                                                                        "task_kind"),
        {"list_before": E.get("list_before"), "kind_in_task_db": E.get("kind_before")})
c.check("2. E: the daemon runs both: Y's runs finish (twice) and Y stays active and enabled; only X's run fails with "
        "the guide's message, which marks X as the job to replace; after cancel + replacement of X only, the enabled "
        "consolidation tasks are the replacement and Y; Y keeps its id and its kind (not replaced)",
        g(E, "runs_1", yy, "state") == "finished" and g(E, "runs_2", yy, "state") == "finished"
        and g(E, "runs_1", xx, "state") == "failed" and g(E, "runs_1", xx, "summary") == MESSAGE
        and g(by_id(E.get("list_after_runs") or []), xx, "enabled") is False
        and g(by_id(E.get("list_after_runs") or []), yy, "enabled") is True
        and enabled_ids(E.get("step4") or []) == sorted([str(rx), str(yy)])
        and g(e4, xx, "lifecycle_state") == "cancelled" and g(e4, yy, "lifecycle_state") == "active"
        and g(E, "kind_after", yy, "kind") == "memory_consolidation"
        and g(E, "kind_after", rx, "kind") == "memory_consolidation",
        {"runs_1": E.get("runs_1"), "runs_2": E.get("runs_2"), "list_after_runs": E.get("list_after_runs"),
         "replacement": E.get("replacement"), "step4": E.get("step4"), "kinds": E.get("kind_after")})
F = observed["F"]
f1, rf = g(F, "ids", "F1"), g(F, "ids", "RF")
c.check("2. F (the guide's warning): a replacement created while the old job is still enabled is not refused and both "
        "are enabled; the old job's next run fails with the message and pauses it; then only the replacement is "
        "enabled",
        enabled_ids(F.get("list_both") or []) == sorted([str(f1), str(rf)])
        and g(F, "legacy_run", f1, "state") == "failed" and g(F, "legacy_run", f1, "summary") == MESSAGE
        and enabled_ids(F.get("list_after") or []) == [str(rf)]
        and g(by_id(F.get("list_after") or []), f1, "lifecycle_state") == "paused",
        {"list_both": F.get("list_both"), "legacy_run": F.get("legacy_run"), "list_after": F.get("list_after")})

# ---- item 3
C = observed["C"]
c1, c2 = g(C, "ids", "C1"), g(C, "ids", "C2")
rc1, rc2 = g(C, "replacements", "C1", "id"), g(C, "replacements", "C2", "id")
c.check("3. C, inspect: `tasks list` (default agent openminion) lists only C1; with `--agent-id helper` only C2 "
        "(agent_id helper); `tasks show C2 --agent-id helper` gives every_ms 86400000; `tools run task.list` (acts as "
        "openminion) gives only C1 (batch 8); `OPENMINION_AGENT_ID=helper ... --runtime-source inproc` gives only C2 "
        "(batch 10)",
        [r["id"] for r in C.get("list_default") or []] == [c1]
        and [(r["id"], r["agent_id"]) for r in C.get("list_helper") or []] == [(c2, "helper")]
        and g(C, "show_c2", "every_ms") == 86400000
        and [(r["id"], r["batch_limit"]) for r in C.get("task.list_default") or []] == [(c1, 8)]
        and [(r["id"], r["batch_limit"], r["agent_id"]) for r in C.get("task.list_helper") or []]
        == [(c2, 10, "helper")],
        {k: C.get(k) for k in ("list_default", "list_helper", "show_c1", "show_c2", "task.list_default",
                               "task.list_helper")})
c.check("3. C, steps 2 to 4: C1 cancelled by `tasks cancel` and C2 by `tasks cancel --agent-id helper`; the "
        "default-agent replacement belongs to openminion (target agent:openminion) and the helper replacement made "
        "with OPENMINION_AGENT_ID=helper and --runtime-source inproc belongs to helper (target agent:helper, "
        "24 h / 10); each agent's only enabled consolidation task is its replacement; the task database and the "
        "cron table give the same agents",
        enabled_ids(C.get("step4_default") or []) == [str(rc1)]
        and enabled_ids(C.get("step4_helper") or []) == [str(rc2)]
        and g(by_id(C.get("step4_default") or []), c1, "lifecycle_state") == "cancelled"
        and g(by_id(C.get("step4_helper") or []), c2, "lifecycle_state") == "cancelled"
        and g(C, "replacements", "C1", "target_scope") == "agent:openminion"
        and g(C, "replacements", "C2", "target_scope") == "agent:helper"
        and g(C, "replacements", "C2", "runtime_source") == "inproc"
        and g(by_id(C.get("step4_helper") or []), rc2, "every_ms") == 86400000
        and [(r["id"], r["batch_limit"]) for r in C.get("step4_task.list_helper") or [] if r["enabled"]]
        == [(rc2, 10)]
        and g(C, "kind_after", rc1, "agent_id") == "openminion" and g(C, "kind_after", rc2, "agent_id") == "helper"
        and g(C, "jobs_after", rc2, "agent_id") == "helper"
        and all(g(C, "kind_after", r, "kind") == "memory_consolidation" for r in (rc1, rc2)),
        {"replacements": C.get("replacements"), "step4_default": C.get("step4_default"),
         "step4_helper": C.get("step4_helper"), "task_db": C.get("kind_after"), "cron_jobs": C.get("jobs_after")})
D = observed["D"]
d1, rd, stray = g(D, "ids", "D1"), g(D, "replacement", "id"), g(D, "stray", "id")
c.check("3. D (default agent main): `tasks list`/`show` act on main and show D1; `tools run task.list` acts as "
        "openminion and does not show it; `OPENMINION_AGENT_ID=main ... --runtime-source inproc` shows its settings; "
        "after `tasks cancel` the replacement made with OPENMINION_AGENT_ID=main and inproc belongs to main (target "
        "agent:main) and is main's only enabled consolidation task",
        [(r["id"], r["agent_id"]) for r in D.get("list") or []] == [(d1, "main")]
        and g(D, "show", "id") == d1 and (D.get("task.list_plain") or []) == []
        and [(r["id"], r["batch_limit"]) for r in D.get("task.list_main") or []] == [(d1, 8)]
        and g(D, "replacement", "target_scope") == "agent:main"
        and enabled_ids(D.get("step4") or []) == [str(rd)]
        and g(by_id(D.get("step4") or []), rd, "agent_id") == "main"
        and g(D, "kind_after", rd, "agent_id") == "main" and g(D, "kind_after", rd, "kind") == "memory_consolidation",
        {k: D.get(k) for k in ("list", "show", "task.list_plain", "task.list_main", "replacement", "step4",
                               "step4_task.list_main")})
c.check("3. D, the guide's statement that `openminion tools run` acts as openminion whatever the default agent is: "
        "step 3 without OPENMINION_AGENT_ID creates the job for openminion (target agent:openminion; listed only with "
        "--agent-id openminion), not for main; and with the daemon running, OPENMINION_AGENT_ID=main without "
        "--runtime-source inproc goes to the daemon and does not list main's tasks (why the guide says inproc)",
        g(D, "stray", "target_scope") == "agent:openminion"
        and [r["id"] for r in D.get("stray_list_openminion") or []] == [stray]
        and stray not in [r["id"] for r in D.get("stray_list_main") or []]
        and g(D, "task.list_main_via_daemon", "runtime_source") == "daemon"
        and rd not in [r["id"] for r in g(D, "task.list_main_via_daemon", "rows") or []],
        {k: D.get(k) for k in ("stray", "stray_list_openminion", "stray_cancel", "task.list_main_via_daemon")})

# ---- item 5
def daemon_ran(x: dict[str, Any], runs: list[Any]) -> bool:
    pid = g(x, "daemon_start", "pid")
    return bool(pid) and all(r and x.get("daemon_pids", {}).get(r.get("run_id")) == pid for r in runs)


a_runs = [g(A, "runs", r1), g(A, "runs", r2)]
c.check("5. the real daemon reads the persisted kind: replacements created by a separate CLI process before "
        "`openminion daemon start` run as consolidation turns under that daemon (its pid ran them, per its log): A's "
        "two (one from run-now, one due by schedule), B's, C's two (openminion and helper), D's (main) and E's Y "
        "(0.1.16) all finish and each scheduled turn's capture intent is 'excluded'",
        all(r and r.get("state") == "finished" for r in a_runs)
        and daemon_ran(A, a_runs)
        and all(set(excluded_turns(A.get("outcomes") or [], f"consolidate:{r}")) == {"excluded"} for r in (r1, r2))
        and g(B, "replacement_run", g(B, "replacement", "id"), "state") == "finished"
        and set(excluded_turns(B.get("outcomes") or [], f"consolidate:{g(B, 'replacement', 'id')}")) == {"excluded"}
        and all(g(C, "runs", r, "state") == "finished" for r in (rc1, rc2))
        and daemon_ran(C, [g(C, "runs", rc1), g(C, "runs", rc2)])
        and [(o["agent_id"], o["capture_state"]) for o in C.get("outcomes") or []
             if o["session"] == f"consolidate:{rc2}"] == [("helper", "excluded")]
        and g(D, "runs", rd, "state") == "finished"
        and [(o["agent_id"], o["capture_state"]) for o in D.get("outcomes") or []
             if o["session"] == f"consolidate:{rd}"] == [("main", "excluded")]
        and set(excluded_turns(E.get("outcomes") or [], f"consolidate:{yy}")) == {"excluded"}
        and len(excluded_turns(E.get("outcomes") or [], f"consolidate:{yy}")) == 2,
        {"A": {"runs": A.get("runs"), "daemon": A.get("daemon_start"), "daemon_pids": A.get("daemon_pids")},
         "B": B.get("replacement_run"), "C": {"runs": C.get("runs"), "daemon_pids": C.get("daemon_pids")},
         "D": D.get("runs"), "E": {"runs_1": E.get("runs_1"), "runs_2": E.get("runs_2")},
         "turn_outcomes": {k: observed[k].get("outcomes") for k in "ABCDE" if isinstance(observed.get(k), dict)}})
c.check("5. A: the stopped pre-upgrade jobs (cancelled A1, paused A2), due by schedule while the daemon ran, have no "
        "run at all",
        g(A, "stopped_job_runs") == {"A1": [], "A2": []}, {"stopped_job_runs": A.get("stopped_job_runs")})
lr, rr = g(B, "legacy_run", b1) or {}, g(B, "resumed_run", b1) or {}
b_out = B.get("outcomes") or []
c.check("5. B, the legacy refusal as the guide explains it: the first run of the 0.1.14 job under this package's "
        "daemon fails with exactly 'memory consolidation job is missing trusted task provenance; it was paused and "
        "must be recreated with task.consolidate_memory', runs no turn (no turn.outcome for its run), and the task is "
        "paused with its schedule disabled (task and cron table); `tasks resume` makes it active again but its next "
        "run fails the same way and pauses it again",
        lr.get("state") == "failed" and lr.get("summary") == MESSAGE and not excluded_turns(b_out, lr.get("session"))
        and g(B, "jobs_after_run", b1, "enabled") is False and g(B, "kind_before", b1, "kind") is None
        and g(B, "after_resume", "lifecycle_state") == "active" and g(B, "after_resume", "enabled") is True
        and rr.get("state") == "failed" and rr.get("summary") == MESSAGE and not excluded_turns(b_out, rr.get("session"))
        and g(B, "after_resumed_run", "lifecycle_state") == "paused" and g(B, "after_resumed_run", "enabled") is False
        and daemon_ran(B, [lr, rr]),
        {"run_on_0.1.14": B.get("run_on_0114"), "turn_outcome_on_0.1.14": B.get("outcomes_0114"),
         "legacy_run": lr, "cron_job_after": g(B, "jobs_after_run", b1), "after_resume": B.get("after_resume"),
         "resumed_run": rr, "after_resumed_run": B.get("after_resumed_run"), "daemon_pids": B.get("daemon_pids")})
rcx = B.get("resume_cancelled") or {}
c.check("5. B: 'A cancelled job cannot be resumed next to its replacement': `tasks resume` on the cancelled job is "
        "refused and it stays cancelled",
        (rcx.get("exit") not in (0, None) or g(rcx, "json", "ok") is False)
        and g(by_id(B.get("step4") or []), b1, "lifecycle_state") == "cancelled",
        {"resume_cancelled": rcx})
api = B.get("api") or {}
post = api.get("post") or {}
c.check("5. B, the raw cron API: `POST /v1/cron/jobs` with the reserved _openminion_memory_consolidation payload "
        "answers 400 invalid_request 'memory consolidation jobs must be created through task.consolidate_memory' "
        "and adds no job (GET before/after and the cron table unchanged)",
        post.get("status") == 400 and g(post, "body", "error", "code") == "invalid_request"
        and "memory consolidation jobs must be created through task.consolidate_memory"
        in str(g(post, "body", "error", "message"))
        and g(api, "get_before", "jobs") == g(api, "get_after", "jobs") and api.get("jobs_before") == api.get(
            "jobs_after") and bool(api.get("jobs_before")),
        {k: api.get(k) for k in ("command", "post", "get_before", "get_after", "jobs_before", "jobs_after")})

ran = [t for t in transcript if t.get("command")]
c.check("every guide and composed command ran with exit 0 (setup commands excluded, except where a refusal is the "
        "expected result: resume of a cancelled job)",
        all(t["exit"] == 0 for t in ran if t["kind"] in ("guide", "composed")
            and "cannot be resumed" not in t["label"]),
        [{"home": t["home"], "label": t["label"], "exit": t["exit"], "command": t["command"]} for t in ran
         if t["kind"] in ("guide", "composed") and t["exit"] != 0])

observed["counts"] = {"commands": len(ran), "guide": sum(t["kind"] == "guide" for t in ran),
                      "composed": sum(t["kind"] == "composed" for t in ran),
                      "setup": sum(t["kind"] == "setup" for t in ran)}
try:
    observed["shell_version"] = subprocess.run([BASH, "--version"], capture_output=True, text=True,
                                               timeout=60).stdout.splitlines()[0]
except Exception as exc:  # noqa: BLE001
    observed["shell_version"] = f"{type(exc).__name__}: {exc}"
write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": sanitize(observed), "transcript": sanitize(transcript)})
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
