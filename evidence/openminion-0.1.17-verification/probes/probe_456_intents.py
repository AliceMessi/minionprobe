"""#456 on the installed package: pending capture intents through the published replacement procedure.

Installed packages only (no repo test fixtures); the harness of probe_456.py
and of the 0.1.15 round's probe_456_upgrade.py (probe_449_lib.py,
probe_449_ext.py, probe_456_lib.py, byte copies). Two invocations on the same
work directory:

  create   (run with the published 0.1.14) builds three runtime homes. Each
           job is created by 0.1.14's own task.consolidate_memory handler, as
           in the 0.1.15 round.
    P  built-in SQLite store: an ordinary SDK turn; job L; one run of L on
       0.1.14 (its scheduled turn captures itself there); then an SDK turn
       whose own capture write is refused by the database (a BEFORE INSERT
       trigger on memory_capture_bundle_receipts, dropped right after), so its
       intent ("earlier") stays pending into the upgrade.
    R  built-in SQLite store: the same refused turn ("earlier"), then job LR,
       which never runs (the guide's "pause" path for a job that has not run).
    Q  the bundled reference-sqlite backend (cannot commit a bundle): one SDK
       turn (pending, unsupported); job L2; one run of L2 on 0.1.14.
  upgrade  (run with the package under test) opens the same homes and follows
           the published guide (docs/runtime-database-upgrades.md at v0.1.17,
           section "Memory consolidation jobs from earlier packages"):
    P  the legacy job's first run (queued with TaskManager.run_task_now and
       executed by the daemon's own attach_cron_scheduler) fails; then, with
       the runtime closed, the guide's commands as real CLI subprocesses:
       `openminion tasks cancel TASK_ID --json`, `openminion tools run
       task.consolidate_memory --json '{"interval_hours": 12, "batch_limit": 8}'`
       and `openminion tasks list --json`; the runtime is reopened and the
       replacement runs twice; then an ordinary SDK turn and a forged-metadata
       turn on the upgraded home.
    R  `openminion tasks pause TASK_ID --json` on the never-run job, then the
       replacement, then its runs.
    Q  the legacy run fails, `tasks cancel`, the replacement, its run.
  The pending intents are read before and after each of these steps.

The CLI runs through a POSIX shell (`bash -c`, the guide's syntax) with the
package's console script first on PATH and the home selected by
OPENMINION_HOME, so each command is the guide's text as written.

One harness change, in this file only: the homes use the CLI's default layout.
probe_449_lib.build_runtime writes the config to <home>/agents.json with
`storage.path = <home>/.openminion/state.db`; with that path the daemon's cron
store (resolved from storage.path) is <home>/.openminion/brain/sessions.db,
while the CLI's `tasks` and `tools run` use <home>/.openminion/state/brain/
sessions.db, so a job created by the CLI there is not seen by the in-process
scheduler (observed while writing this probe; see the draft's notes). This
probe therefore replaces build_runtime (same body) with one that writes the
config to the default <home>/.openminion/agents.json and leaves storage.path at
its default, so the CLI, the task database and the scheduler share one home.

Usage: python -I -X utf8 probe_456_intents.py create <work_dir> <out.json>
       python -I -X utf8 probe_456_intents.py upgrade <work_dir> <out.json> <guide.md> <bash>
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, sanitize_text, write_result  # noqa: E402

PHASE = sys.argv[1]
WORK = Path(sys.argv[2]).resolve()
OUT = Path(sys.argv[3]).resolve()
GUIDE = Path(sys.argv[4]).resolve() if len(sys.argv) > 4 else None
BASH = sys.argv[5] if len(sys.argv) > 5 else None
STATE = WORK / "state.json"
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
os.environ.pop("OPENMINION_AGENT_ID", None)


def _dist(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


info = banner("probe_456_intents", phase=PHASE, guide=None if GUIDE is None else str(GUIDE), shell=BASH,
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic", "httpx", "openai")})

from openminion.modules.memory.backends.external import register_reference_sqlite_backend  # noqa: E402
from probe_449_ext import create_job, intents, pending, sqlite_count, sqlite_trigger  # noqa: E402
from probe_456_lib import Home, consolidation_jobs, forged_keys, task_view  # noqa: E402

register_reference_sqlite_backend()


def build_runtime_default_layout(root: Path, provider: Any, *, external_adapter: str | None = None,
                                 identity_constraint: str | None = None, memory_config: Any = None) -> Any:
    """probe_449_lib.build_runtime with the config at <home>/.openminion/agents.json and the default storage path."""
    from openminion import APIRuntime
    from openminion.base.config import AgentProfileConfig, ConfigManager, OpenMinionConfig, save_config
    from probe_449_lib import AGENT_ID

    assert identity_constraint is None
    data_root = root / ".openminion"
    data_root.mkdir(parents=True, exist_ok=True)
    config = OpenMinionConfig()
    config.agents = {AGENT_ID: AgentProfileConfig(name=AGENT_ID, provider="openai", default_channel="console")}
    config.default_agent = AGENT_ID
    config.providers.openai.api_key_env = "PROBE_420_PROVIDER_KEY"
    config.providers.openai.base_url = provider.base_url
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    save_config(config, str(data_root / "agents.json"))
    manager = ConfigManager.load(str(data_root / "agents.json"), home_root=root, data_root=data_root)
    if external_adapter is not None:
        external_memory = {
            "store": {"backend": "sqlite", "sqlite_path": str(root / "memory.db")},
            "backend": {"provider": "external", "external_adapter": external_adapter,
                        "options": {"db_path": str(root / "external.sqlite3")}},
        }
        manager.register("memory", lambda **_kwargs: external_memory)
    elif memory_config is not None:
        manager.register("memory", memory_config)
    return APIRuntime.from_manager(manager)


import probe_449_lib  # noqa: E402

probe_449_lib.build_runtime = build_runtime_default_layout
c = Checks()
observed: dict[str, Any] = {"phase": PHASE}
cli_log: list[dict[str, Any]] = []
EXPECTED_CONTEXT = {"memory_consolidation_job": "true", "memory_consolidation_target_scope": "agent:openminion",
                    "memory_consolidation_batch_limit": "8", "memory_consolidation_max_iterations": "2",
                    "memory_consolidation_timeout_seconds": "30"}
MESSAGE = ("memory consolidation job is missing trusted task provenance; it was paused and must be recreated "
           "with task.consolidate_memory")
RECOVERED = ["identity", "authorize=True", "capability=True", "source", "extract", "bundle.store", "bundle", "result"]
GUIDE_BLOB = "0f6b9c63248a823abd79cf2187268d195d5cb97b"


def guarded(name: str, body: Any) -> dict[str, Any]:
    try:
        return body()
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"{name}: case error {type(exc).__name__}: {exc}", flush=True)
        return {"case_error": f"{type(exc).__name__}: {exc}",
                "case_error_frames": [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                      for f in traceback.extract_tb(exc.__traceback__)][-8:]}


def show(row: dict[str, Any]) -> None:
    print(f"{row.get('label')}: state={row.get('state')} raised={row.get('raised')} summary={row.get('summary')!r} "
          f"recovery={row.get('recovery')} steps={row.get('recovery_steps_by_intent')} new={row.get('new_intents')} "
          f"turn_extractions={row.get('turn_extraction_requests')} submits={len(row.get('submits', []))} "
          f"excl={[e.get('returned') for e in row.get('exclusions', [])]} contexts={len(row.get('contexts', []))}",
          flush=True)


def snapshot(h: Home) -> list[dict[str, Any]]:
    return [{"label": h.labels.get(str(i["root_turn_id"])), "capture_state": i["capture_state"],
             "results": i["results"], "payload": i["payload"]} for i in pending(intents(h.runtime))]


def labels_of(items: list[dict[str, Any]] | None) -> list[Any]:
    return [p.get("label") for p in items or []]


# ---------------------------------------------------------------- the guide's commands
def guide_commands() -> dict[str, Any]:
    data = GUIDE.read_bytes()
    text = data.decode("utf-8")
    section = text.split("## Memory consolidation jobs from earlier packages", 1)[1].split("\n## ", 1)[0]
    blocks = [textwrap.dedent(b) for b in re.findall(r"^[ ]*```bash\n(.*?)^[ ]*```", section, re.M | re.S)]
    return {"git_blob": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(),
            "list": blocks[0].strip("\n").split("\n")[0], "create": blocks[2].strip("\n"),
            "cancel": "openminion tasks cancel TASK_ID --json", "pause": "openminion tasks pause TASK_ID --json",
            "inline_found": all(f"`openminion tasks {a} TASK_ID --json`" in section for a in ("cancel", "pause"))}


def cli(root: Path, label: str, template: str, task_id: str | None = None) -> dict[str, Any]:
    text = template if task_id is None else re.sub(r"(?<![A-Za-z0-9_])TASK_ID(?![A-Za-z0-9_])", task_id, template)
    shown = text
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_")}
    env.update({"OPENMINION_HOME": str(root), "OPENMINION_NO_UPDATE_CHECK": "1",
                "PROBE_420_PROVIDER_KEY": "local-dummy-key", "PYTHONUTF8": "1",
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")})
    t0 = time.monotonic()
    proc = subprocess.run([str(BASH), "-c", text], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=str(root), timeout=420)
    try:
        data: Any = json.loads(proc.stdout)
    except ValueError:
        data = None
    noise = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (INFO|DEBUG) ")
    row = {"home": root.name, "label": label, "command": shown, "exit": proc.returncode,
           "seconds": round(time.monotonic() - t0, 1), "json": data,
           "stdout_head": None if data is not None else sanitize_text(proc.stdout[:1500]),
           "stderr_tail": sanitize_text("\n".join([ln for ln in proc.stderr.splitlines()
                                                   if ln.strip() and not noise.match(ln)][-10:])) or None}
    cli_log.append(row)
    print(f"[{root.name}] $ {shown}\n    exit={proc.returncode} {row['seconds']}s", flush=True)
    return row


def cli_tasks(row: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for t in (row.get("json") or {}).get("tasks", []) if isinstance(row.get("json"), dict) else []:
        if not str(t.get("instruction", "")).startswith("Review the provided memory candidates"):
            continue
        out.append({"id": t.get("id"), "agent_id": t.get("agent_id"), "lifecycle_state": t.get("lifecycle_state"),
                    "enabled": t.get("enabled")})
    return out


def created_id(row: dict[str, Any]) -> str:
    d = row.get("json") or {}
    return str((((d.get("tool") or {}).get("data")) or {}).get("task_id") or "") if isinstance(d, dict) else ""


def procedure(root: Path, legacy: str, stop: str, g: dict[str, Any]) -> dict[str, Any]:
    """Steps 2 to 4 of the guide on a closed runtime home: stop the old job, create the replacement, list."""
    out: dict[str, Any] = {}
    row = cli(root, f"step 2 ({stop})", g[stop], legacy)
    out["stop"] = {"exit": row["exit"], "state": ((row.get("json") or {}).get("task") or {}).get("lifecycle_state")
                   if isinstance(row.get("json"), dict) else None}
    row = cli(root, "step 3", g["create"])
    out["replacement"] = created_id(row)
    out["replacement_exit"] = row["exit"]
    out["replacement_runtime_source"] = (row.get("json") or {}).get("runtime_source") if isinstance(
        row.get("json"), dict) else None
    row = cli(root, "step 4", g["list"])
    out["list_exit"] = row["exit"]
    out["list"] = cli_tasks(row)
    return out


# ---------------------------------------------------------------- create (0.1.14)
def create() -> dict[str, Any]:
    state: dict[str, Any] = {}
    out: dict[str, Any] = {}
    labels: dict[str, str] = {}
    h = Home(WORK / "P", labels)
    try:
        out["P_A"] = h.observe("P ordinary turn", lambda: h.sdk_turn("ordinary-before-upgrade"))
        created = create_job(h.cron, {"interval_hours": 12, "batch_limit": 8})
        legacy = str(created.get("task_id"))
        out["P_L_task"] = task_view(h.cron.task_manager, legacy)
        h.cron.start()
        out["P_L_run_on_create_package"] = h.observe("P legacy run on the creating package",
                                                     lambda: h.run(legacy, "scheduled L run 1"))
        db = h.memory_db()
        out["P_trigger_on"] = sqlite_trigger(db, True)
        out["P_B"] = h.observe("P earlier turn (write refused)", lambda: h.sdk_turn("earlier"))
        out["P_trigger_off"] = sqlite_trigger(db, False)
        out["P_pending_at_end"] = snapshot(h)
        state["P"] = {"legacy_job_id": legacy}
        for k in ("P_A", "P_L_run_on_create_package", "P_B"):
            show(out[k])
    finally:
        out["P_closed"] = h.close()
    state["P"]["labels"] = labels
    labels = {}
    h = Home(WORK / "R", labels)
    try:
        db = h.memory_db()
        out["R_trigger_on"] = sqlite_trigger(db, True)
        out["R_B"] = h.observe("R earlier turn (write refused)", lambda: h.sdk_turn("earlier"))
        out["R_trigger_off"] = sqlite_trigger(db, False)
        created = create_job(h.cron, {"interval_hours": 12, "batch_limit": 8})
        legacy = str(created.get("task_id"))
        out["R_L_task"] = task_view(h.cron.task_manager, legacy)
        out["R_pending_at_end"] = snapshot(h)
        state["R"] = {"legacy_job_id": legacy}
        show(out["R_B"])
    finally:
        out["R_closed"] = h.close()
    state["R"]["labels"] = labels
    labels = {}
    h = Home(WORK / "Q", labels, external="reference-sqlite")
    try:
        out["Q_U"] = h.observe("Q user turn", lambda: h.sdk_turn("capture"))
        created = create_job(h.cron, {"interval_hours": 12, "batch_limit": 8})
        legacy = str(created.get("task_id"))
        out["Q_L_task"] = task_view(h.cron.task_manager, legacy)
        h.cron.start()
        out["Q_L_run_on_create_package"] = h.observe("Q legacy run on the creating package",
                                                     lambda: h.run(legacy, "scheduled L2 run 1"))
        out["Q_pending_at_end"] = snapshot(h)
        state["Q"] = {"legacy_job_id": legacy}
        for k in ("Q_U", "Q_L_run_on_create_package"):
            show(out[k])
    finally:
        out["Q_closed"] = h.close()
    state["Q"]["labels"] = labels
    state["created_with"] = info.get("openminion.__version__")
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return out


# ---------------------------------------------------------------- upgrade (the package under test)
def upgrade() -> dict[str, Any]:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    g = guide_commands()
    out: dict[str, Any] = {"created_with": state.get("created_with"), "guide": g}

    # P: failed legacy run, cancel, replacement, recovery once, controls
    labels = dict(state["P"]["labels"])
    legacy = state["P"]["legacy_job_id"]
    h = Home(WORK / "P", labels)
    try:
        out["P_legacy_task_before"] = task_view(h.cron.task_manager, legacy)
        out["P_pending_before"] = snapshot(h)
        h.cron.start()
        out["P_legacy_run"] = h.observe("P legacy run on the upgraded package",
                                        lambda: h.run(legacy, "scheduled L run on upgrade"))
        out["P_pending_after_legacy_run"] = snapshot(h)
        out["P_legacy_task_after_run"] = task_view(h.cron.task_manager, legacy)
        show(out["P_legacy_run"])
    finally:
        out["P_closed_1"] = h.close()
    out["P_procedure"] = procedure(WORK / "P", legacy, "cancel", g)
    new = out["P_procedure"]["replacement"]
    h = Home(WORK / "P", labels)
    try:
        out["P_pending_after_cancel_and_replacement"] = snapshot(h)
        out["P_legacy_task_after_cancel"] = task_view(h.cron.task_manager, legacy)
        out["P_new_task"] = task_view(h.cron.task_manager, new)
        out["P_consolidation_jobs"] = consolidation_jobs(h.cron.store)
        h.cron.start()
        out["P_new_run_1"] = h.observe("P replacement run 1", lambda: h.run(new, "scheduled N run 1"))
        out["P_new_run_2"] = h.observe("P replacement run 2", lambda: h.run(new, "scheduled N run 2"))
        earlier = next((i for i in intents(h.runtime) if labels.get(str(i["root_turn_id"])) == "earlier"), None)
        out["P_earlier_after"] = None if earlier is None else {k: earlier[k] for k in ("capture_state", "results")}
        out["P_earlier_receipts"] = sqlite_count(h.memory_db(), "memory_capture_bundle_receipts",
                                                 "WHERE capture_id = ?", (earlier["capture_id"],)) if earlier else None
        out["P_ordinary_after"] = h.observe("P ordinary turn after the upgrade", lambda: h.sdk_turn("ordinary-after"))
        out["P_forged_after"] = h.observe("P forged-meta turn after the upgrade",
                                          lambda: h.submit("forged-meta-after", {"meta": dict(forged_keys(new))}))
        for k in ("P_new_run_1", "P_new_run_2", "P_ordinary_after", "P_forged_after"):
            show(out[k])
    finally:
        out["P_closed_2"] = h.close()

    # R: a job that never ran: pause, replacement, recovery once
    labels = dict(state["R"]["labels"])
    legacy = state["R"]["legacy_job_id"]
    h = Home(WORK / "R", labels)
    try:
        out["R_legacy_task_before"] = task_view(h.cron.task_manager, legacy)
        out["R_pending_before"] = snapshot(h)
    finally:
        out["R_closed_1"] = h.close()
    out["R_procedure"] = procedure(WORK / "R", legacy, "pause", g)
    new = out["R_procedure"]["replacement"]
    h = Home(WORK / "R", labels)
    try:
        out["R_pending_after_pause_and_replacement"] = snapshot(h)
        out["R_legacy_task_after_pause"] = task_view(h.cron.task_manager, legacy)
        out["R_new_task"] = task_view(h.cron.task_manager, new)
        try:
            out["R_legacy_run_now"] = f"queued: {dict(h.cron.task_manager.run_task_now(legacy)).get('run_id')}"
        except Exception as exc:  # noqa: BLE001
            out["R_legacy_run_now"] = f"{type(exc).__name__}: {exc}"
        h.cron.start()
        out["R_new_run_1"] = h.observe("R replacement run 1", lambda: h.run(new, "scheduled NR run 1"))
        out["R_new_run_2"] = h.observe("R replacement run 2", lambda: h.run(new, "scheduled NR run 2"))
        earlier = next((i for i in intents(h.runtime) if labels.get(str(i["root_turn_id"])) == "earlier"), None)
        out["R_earlier_after"] = None if earlier is None else {k: earlier[k] for k in ("capture_state", "results")}
        out["R_earlier_receipts"] = sqlite_count(h.memory_db(), "memory_capture_bundle_receipts",
                                                 "WHERE capture_id = ?", (earlier["capture_id"],)) if earlier else None
        for k in ("R_new_run_1", "R_new_run_2"):
            show(out[k])
    finally:
        out["R_closed_2"] = h.close()

    # Q: incapable store
    labels = dict(state["Q"]["labels"])
    legacy = state["Q"]["legacy_job_id"]
    h = Home(WORK / "Q", labels, external="reference-sqlite")
    try:
        out["Q_pending_before"] = snapshot(h)
        h.cron.start()
        out["Q_legacy_run"] = h.observe("Q legacy run on the upgraded package",
                                        lambda: h.run(legacy, "scheduled L2 run on upgrade"))
        out["Q_pending_after_legacy_run"] = snapshot(h)
        show(out["Q_legacy_run"])
    finally:
        out["Q_closed_1"] = h.close()
    out["Q_procedure"] = procedure(WORK / "Q", legacy, "cancel", g)
    new = out["Q_procedure"]["replacement"]
    h = Home(WORK / "Q", labels, external="reference-sqlite")
    try:
        out["Q_pending_after_cancel_and_replacement"] = snapshot(h)
        out["Q_new_task"] = task_view(h.cron.task_manager, new)
        h.cron.start()
        out["Q_new_run_1"] = h.observe("Q replacement run 1", lambda: h.run(new, "scheduled N2 run 1"))
        out["Q_pending_after_new_run"] = snapshot(h)
        show(out["Q_new_run_1"])
    finally:
        out["Q_closed_2"] = h.close()
    return out


def captured_normally(row: dict[str, Any]) -> bool:
    exc = row.get("exclusions", [])
    return (len(exc) == 1 and exc[0].get("consolidation_keys") == {} and exc[0].get("returned") is False
            and row.get("contexts") == [] and row.get("turn_extraction_requests") == 1
            and len(row.get("new_intents", [])) == 1 and row["new_intents"][0]["capture_state"] == "pending"
            and row["new_intents"][0]["results"] == 1)


if PHASE == "create":
    observed["create"] = guarded("create", create)
    x = observed["create"]
    c.check("create. the creating package's jobs and the pending intents for the upgrade are in place: P's job ran "
            "once and P has the 'earlier' intent pending; R has the 'earlier' intent pending and a job that never "
            "ran; Q's job ran once",
            (x.get("P_L_run_on_create_package") or {}).get("state") == "finished"
            and labels_of(x.get("P_pending_at_end")) == ["earlier"]
            and labels_of(x.get("R_pending_at_end")) == ["earlier"]
            and (x.get("R_L_task") or {}).get("state") == "active"
            and (x.get("Q_L_run_on_create_package") or {}).get("state") == "finished",
            {"P_L_task": x.get("P_L_task"), "P_pending": labels_of(x.get("P_pending_at_end")),
             "R_L_task": x.get("R_L_task"), "R_pending": labels_of(x.get("R_pending_at_end")),
             "Q_pending": labels_of(x.get("Q_pending_at_end"))})
else:
    observed["upgrade"] = guarded("upgrade", upgrade)
    x = observed["upgrade"]
    gd = x.get("guide") or {}
    c.check("guide. the commands come from the published guide (git blob 0f6b9c63...): `tasks cancel|pause TASK_ID "
            "--json`, the step 3 block and `tasks list --json`",
            gd.get("git_blob") == GUIDE_BLOB and gd.get("inline_found") is True
            and gd.get("list") == "openminion tasks list --json"
            and gd.get("create") == ("openminion tools run task.consolidate_memory \\\n"
                                     "  --json '{\"interval_hours\": 12, \"batch_limit\": 8}'"),
            gd)
    lr = x.get("P_legacy_run") or {}
    c.check("P. the 0.1.14 job's first run on this package fails with the guide's message: no turn is submitted, no "
            "capture recovery runs, no intent is added, and the 'earlier' pending intent is unchanged",
            (x.get("P_legacy_task_before") or {}).get("kind") is None and lr.get("state") == "failed"
            and lr.get("summary") == MESSAGE and lr.get("submits") == [] and lr.get("recovery") == []
            and lr.get("new_intents") == [] and lr.get("exclusions") == []
            and labels_of(x.get("P_pending_before")) == ["earlier"]
            and x.get("P_pending_after_legacy_run") == x.get("P_pending_before")
            and (x.get("P_legacy_task_after_run") or {}).get("state") == "paused",
            {k: lr.get(k) for k in ("state", "summary", "submits", "recovery", "new_intents")}
            | {"task_before": x.get("P_legacy_task_before"), "task_after": x.get("P_legacy_task_after_run"),
               "pending_before": labels_of(x.get("P_pending_before")),
               "pending_after": labels_of(x.get("P_pending_after_legacy_run"))})
    pp = x.get("P_procedure") or {}
    new_p = pp.get("replacement")
    c.check("P. the guide's `tasks cancel` (on the job its failed run paused) and the step 3 replacement, as real CLI "
            "subprocesses, leave the pending intent unchanged (same payload); the old task is cancelled, the "
            "replacement has kind memory_consolidation, and `tasks list` shows only the replacement enabled",
            pp.get("stop", {}).get("exit") == 0 and pp.get("stop", {}).get("state") == "cancelled"
            and pp.get("replacement_exit") == 0 and bool(new_p) and pp.get("list_exit") == 0
            and [t["id"] for t in pp.get("list", []) if t["enabled"]] == [new_p]
            and x.get("P_pending_after_cancel_and_replacement") == x.get("P_pending_before")
            and (x.get("P_legacy_task_after_cancel") or {}).get("state") == "cancelled"
            and (x.get("P_new_task") or {}).get("kind") == "memory_consolidation",
            {"procedure": pp, "legacy_task": x.get("P_legacy_task_after_cancel"), "new_task": x.get("P_new_task"),
             "pending_after": labels_of(x.get("P_pending_after_cancel_and_replacement"))})
    n1, n2 = x.get("P_new_run_1") or {}, x.get("P_new_run_2") or {}
    c.check("P. the replacement's first run carries the trusted consolidation context, its own turn is 'excluded', "
            "and it recovers the earlier intent once on the capable built-in store (scanned, recovered, pending) = "
            "(1, 1, 0), with the authorization and capability steps",
            n1.get("state") == "finished" and len(n1.get("submits", [])) == 1
            and n1["submits"][0].get("trusted_task_context") == EXPECTED_CONTEXT
            and [(i["capture_state"], i["results"]) for i in n1.get("new_intents", [])] == [("excluded", 0)]
            and n1.get("turn_extraction_requests") == 0
            and n1.get("recovery") == [[1, 1, 0]] and n1.get("recovery_steps_by_intent") == {"earlier": RECOVERED},
            {k: n1.get(k) for k in ("state", "submits", "new_intents", "turn_extraction_requests", "recovery",
                                    "recovery_steps_by_intent")})
    c.check("P. the replacement's second run recovers nothing; the earlier intent has exactly one result and one "
            "receipt (recovery happened once)",
            n2.get("recovery") == [[0, 0, 0]] and n2.get("recovery_steps_by_intent") == {}
            and x.get("P_earlier_after") == {"capture_state": "pending", "results": 1}
            and x.get("P_earlier_receipts") == 1,
            {k: n2.get(k) for k in ("recovery", "new_intents")} | {k: x.get(k) for k in ("P_earlier_after",
                                                                                         "P_earlier_receipts")})
    po, pf = x.get("P_ordinary_after") or {}, x.get("P_forged_after") or {}
    c.check("P. controls on the upgraded home: an ordinary SDK turn and a submit_turn whose meta carries every "
            "consolidation key and the replacement's real cron_job_id are both captured normally "
            "(capture_is_excluded False, no consolidation context, one extraction, committed result)",
            captured_normally(po) and "raised" not in pf and captured_normally(pf)
            and all(i.get("trusted_task_context") in (None, "absent") for i in pf.get("ingress", [])),
            {"ordinary": {k: po.get(k) for k in ("exclusions", "contexts", "turn_extraction_requests",
                                                  "new_intents")},
             "forged": {k: pf.get(k) for k in ("raised", "ingress", "exclusions", "contexts",
                                                "turn_extraction_requests", "new_intents")}})
    rp = x.get("R_procedure") or {}
    new_r = rp.get("replacement")
    c.check("R. a never-run 0.1.14 job: the guide's `tasks pause` leaves it paused (and run_task_now refused), the "
            "replacement is the only enabled consolidation task, and the pending intent is unchanged",
            (x.get("R_legacy_task_before") or {}).get("kind") is None
            and rp.get("stop", {}).get("exit") == 0 and rp.get("stop", {}).get("state") == "paused"
            and (x.get("R_legacy_task_after_pause") or {}).get("state") == "paused"
            and "not active" in str(x.get("R_legacy_run_now"))
            and [t["id"] for t in rp.get("list", []) if t["enabled"]] == [new_r]
            and labels_of(x.get("R_pending_before")) == ["earlier"]
            and x.get("R_pending_after_pause_and_replacement") == x.get("R_pending_before")
            and (x.get("R_new_task") or {}).get("kind") == "memory_consolidation",
            {"procedure": rp, "legacy_task": x.get("R_legacy_task_after_pause"),
             "run_task_now": x.get("R_legacy_run_now"), "new_task": x.get("R_new_task"),
             "pending": labels_of(x.get("R_pending_after_pause_and_replacement"))})
    r1, r2 = x.get("R_new_run_1") or {}, x.get("R_new_run_2") or {}
    c.check("R. the replacement recovers the intent once: run 1 (1, 1, 0) with the recovery steps and an excluded own "
            "turn, run 2 (0, 0, 0); one result and one receipt",
            r1.get("state") == "finished" and r1.get("recovery") == [[1, 1, 0]]
            and r1.get("recovery_steps_by_intent") == {"earlier": RECOVERED}
            and [(i["capture_state"], i["results"]) for i in r1.get("new_intents", [])] == [("excluded", 0)]
            and r2.get("recovery") == [[0, 0, 0]]
            and x.get("R_earlier_after") == {"capture_state": "pending", "results": 1}
            and x.get("R_earlier_receipts") == 1,
            {"run_1": {k: r1.get(k) for k in ("state", "recovery", "recovery_steps_by_intent", "new_intents")},
             "run_2": {k: r2.get(k) for k in ("recovery",)},
             "earlier": x.get("R_earlier_after"), "receipts": x.get("R_earlier_receipts")})
    ql, qp, qn = x.get("Q_legacy_run") or {}, x.get("Q_procedure") or {}, x.get("Q_new_run_1") or {}
    n_pending = len(x.get("Q_pending_before") or [])
    c.check("Q. incapable store (reference-sqlite): the legacy run fails with the message, with no submit and no "
            "recovery; then `tasks cancel` and the replacement; the pending intents are unchanged through all of it",
            n_pending >= 1 and ql.get("state") == "failed" and ql.get("summary") == MESSAGE
            and ql.get("submits") == [] and ql.get("recovery") == []
            and x.get("Q_pending_after_legacy_run") == x.get("Q_pending_before")
            and qp.get("stop", {}).get("state") == "cancelled"
            and [t["id"] for t in qp.get("list", []) if t["enabled"]] == [qp.get("replacement")]
            and x.get("Q_pending_after_cancel_and_replacement") == x.get("Q_pending_before"),
            {"legacy_run": {k: ql.get(k) for k in ("state", "summary", "submits", "recovery")}, "procedure": qp,
             "pending_before": labels_of(x.get("Q_pending_before")),
             "pending_after": labels_of(x.get("Q_pending_after_cancel_and_replacement"))})
    c.check("Q. the replacement on the incapable store leaves unsupported work pending: it asks the store "
            "(capability False), reports every intent pending, extracts nothing, its own turn is excluded, and the "
            "pending intents are unchanged afterwards",
            qn.get("state") == "finished" and qn.get("recovery") == [[n_pending, 0, n_pending]]
            and all(v == ["identity", "authorize=True", "capability=False"]
                    for v in (qn.get("recovery_steps_by_intent") or {}).values())
            and qn.get("recovery_extraction_requests") == 0
            and [(i["capture_state"], i["results"]) for i in qn.get("new_intents", [])] == [("excluded", 0)]
            and x.get("Q_pending_after_new_run") == x.get("Q_pending_before"),
            {k: qn.get(k) for k in ("state", "recovery", "recovery_steps_by_intent", "recovery_extraction_requests",
                                    "new_intents")})

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": sanitize(observed), "cli": sanitize(cli_log)})
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
