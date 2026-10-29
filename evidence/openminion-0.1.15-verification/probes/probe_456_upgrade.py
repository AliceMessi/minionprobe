"""#456 on the installed package: a consolidation job created by 0.1.14, then the same runtime home on the new package.

Installed packages only (no repo test fixtures), the harness of probe_456.py.
Two invocations on the same work directory:

  create   (run with the published 0.1.14) builds two runtime homes:
    P  built-in SQLite memory store. An ordinary SDK turn; the consolidation
       job L created by 0.1.14's task.consolidate_memory handler; one run of L
       on 0.1.14 (the scheduled turn captures itself there); then an SDK turn
       whose own capture write is refused by the database (a BEFORE INSERT
       trigger on memory_capture_bundle_receipts, dropped right after), so its
       intent stays pending into the upgrade.
    Q  the bundled reference-sqlite backend (cannot commit a bundle). One SDK
       turn (pending, unsupported); job L2 by 0.1.14's handler; one run of L2
       on 0.1.14.
  upgrade  (run with the package under test, 0.1.15; or 0.1.14 again as the
           control) opens the same homes:
    P  records the legacy task record and job; queues a run of L
       (TaskManager.run_task_now) under the daemon's own scheduler; records
       what the run did, the task and job afterwards and a second run_task_now;
       then recreates the job with this package's task.consolidate_memory and
       runs the new job twice; lists the enabled consolidation schedules; then
       runs the package's own `openminion tasks list --json` on the home.
    Q  recreates the job first, while L2 is still enabled, and lists the enabled
       consolidation schedules; then queues a run of L2; then runs the new job.

No upgrade or recreate procedure is documented in the package, its tag's docs/
or CHANGELOG (see the draft): this probe records what the package does, it does
not follow a documented procedure.

Usage: python -I -X utf8 probe_456_upgrade.py create|upgrade <work_dir> <out.json>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, sanitize_text, write_result  # noqa: E402

PHASE = sys.argv[1]
WORK = Path(sys.argv[2]).resolve()
OUT = Path(sys.argv[3]).resolve()
STATE = WORK / "state.json"
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()


def _dist(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


info = banner("probe_456_upgrade", phase=PHASE,
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic", "httpx", "openai")})

from openminion.modules.memory.backends.external import register_reference_sqlite_backend  # noqa: E402
from probe_449_ext import create_job, intents, pending, sqlite_count, sqlite_trigger  # noqa: E402
from probe_456_lib import Home, consolidation_jobs, task_view  # noqa: E402

register_reference_sqlite_backend()
c = Checks()
observed: dict[str, Any] = {"phase": PHASE}
EXPECTED_CONTEXT = {"memory_consolidation_job": "true", "memory_consolidation_target_scope": "agent:openminion",
                    "memory_consolidation_batch_limit": "8", "memory_consolidation_max_iterations": "2",
                    "memory_consolidation_timeout_seconds": "30"}


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


def run_now_outcome(h: Home, task_id: str) -> str:
    try:
        return f"queued: {dict(h.cron.task_manager.run_task_now(task_id)).get('run_id')}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def cli_tasks(root: Path) -> dict[str, Any]:
    """The package's own `openminion tasks list --json` on this home (a real subprocess of the console script,
    run in the home directory: the CLI creates a .openminion folder in its working directory)."""
    exe = Path(sys.executable).parent / ("openminion.exe" if os.name == "nt" else "openminion")
    cmd = [str(exe), "--home-root", str(root), "--data-root", str(root / ".openminion"), "--config",
           str(root / "agents.json"), "--no-update-check", "tasks", "list", "--json"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_")}
    env["PROBE_420_PROVIDER_KEY"] = "local-dummy-key"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                              cwd=str(root), timeout=300)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}
    out: dict[str, Any] = {"argv": ["openminion", "--home-root", "<home>", "--data-root", "<home>/.openminion",
                                    "--config", "<home>/agents.json", "--no-update-check", "tasks", "list",
                                    "--json"], "exit": proc.returncode}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        out["stdout_head"] = sanitize_text(proc.stdout[:2000])
        out["stderr_tail"] = sanitize_text(proc.stderr[-2000:])
        return out
    tasks = []
    for t in data.get("tasks", []):
        last = t.get("last_run") or {}
        err = last.get("error")
        tasks.append({"id": t.get("id"), "lifecycle_state": t.get("lifecycle_state"), "status": t.get("status"),
                      "enabled": t.get("enabled"), "task_kind": t.get("task_kind"),
                      "operator_state": t.get("operator_state"), "valid_actions": t.get("valid_actions"),
                      "instruction_head": str(t.get("instruction") or "")[:60],
                      "last_run": {"state": last.get("state"), "summary": last.get("summary"),
                                   "error": err.get("message") if isinstance(err, dict) else err}})
    out["tasks"] = tasks
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
        out["P_L_created"] = {k: created.get(k) for k in ("ok", "task_id", "batch_limit", "target_scope")}
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


# ---------------------------------------------------------------- upgrade (0.1.15, or 0.1.14 as the control)
def upgrade() -> dict[str, Any]:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    out: dict[str, Any] = {"created_with": state.get("created_with")}
    labels = dict(state["P"]["labels"])
    legacy = state["P"]["legacy_job_id"]
    h = Home(WORK / "P", labels)
    try:
        out["P_legacy_task_before"] = task_view(h.cron.task_manager, legacy)
        out["P_legacy_job_enabled_before"] = bool((h.cron.store.get_cron_job(legacy) or {}).get("enabled"))
        out["P_consolidation_jobs_before"] = consolidation_jobs(h.cron.store)
        out["P_pending_before"] = snapshot(h)
        h.cron.start()
        out["P_legacy_run"] = h.observe("P legacy run on the upgraded package",
                                        lambda: h.run(legacy, "scheduled L run on upgrade"))
        out["P_pending_after_legacy_run"] = snapshot(h)
        out["P_legacy_task_after"] = task_view(h.cron.task_manager, legacy)
        out["P_legacy_job_enabled_after"] = bool((h.cron.store.get_cron_job(legacy) or {}).get("enabled"))
        out["P_consolidation_jobs_after_legacy_run"] = consolidation_jobs(h.cron.store)
        out["P_legacy_run_now_again"] = run_now_outcome(h, legacy)
        if out["P_legacy_run_now_again"].startswith("queued: "):  # the control: let that run finish too
            from probe_449_ext import wait_run

            out["P_legacy_second_run"] = h.observe(
                "P legacy second run", lambda: dict(wait_run(h.cron, h.obs, legacy,
                                                             out["P_legacy_run_now_again"].split(": ", 1)[1]),
                                                    _label="scheduled L second run on upgrade"))
        created = create_job(h.cron, {"interval_hours": 12, "batch_limit": 8})
        new = str(created.get("task_id"))
        out["P_new_task"] = task_view(h.cron.task_manager, new)
        out["P_consolidation_jobs_after_recreate"] = consolidation_jobs(h.cron.store)
        out["P_new_run_1"] = h.observe("P new job run 1", lambda: h.run(new, "scheduled N run 1"))
        out["P_new_run_2"] = h.observe("P new job run 2", lambda: h.run(new, "scheduled N run 2"))
        earlier = next((i for i in intents(h.runtime) if labels.get(str(i["root_turn_id"])) == "earlier"), None)
        out["P_earlier_after"] = None if earlier is None else {k: earlier[k] for k in ("capture_state", "results")}
        out["P_earlier_receipts"] = sqlite_count(h.memory_db(), "memory_capture_bundle_receipts",
                                                 "WHERE capture_id = ?", (earlier["capture_id"],)) if earlier else None
        out["P_consolidation_jobs_at_end"] = consolidation_jobs(h.cron.store)
        out["P_legacy_task_at_end"] = task_view(h.cron.task_manager, legacy)
        out["P_ids"] = {"legacy": legacy, "new": new}
        for k in ("P_legacy_run", "P_legacy_second_run", "P_new_run_1", "P_new_run_2"):
            if k in out:
                show(out[k])
    finally:
        out["P_closed"] = h.close()
    out["P_cli_tasks_list"] = cli_tasks(WORK / "P")

    labels = dict(state["Q"]["labels"])
    legacy = state["Q"]["legacy_job_id"]
    h = Home(WORK / "Q", labels, external="reference-sqlite")
    try:
        out["Q_legacy_task_before"] = task_view(h.cron.task_manager, legacy)
        out["Q_pending_before"] = snapshot(h)
        created = create_job(h.cron, {"interval_hours": 12, "batch_limit": 8})
        new = str(created.get("task_id"))
        out["Q_consolidation_jobs_after_recreate_before_legacy_run"] = consolidation_jobs(h.cron.store)
        h.cron.start()
        out["Q_legacy_run"] = h.observe("Q legacy run on the upgraded package",
                                        lambda: h.run(legacy, "scheduled L2 run on upgrade"))
        out["Q_legacy_task_after"] = task_view(h.cron.task_manager, legacy)
        out["Q_consolidation_jobs_after_legacy_run"] = consolidation_jobs(h.cron.store)
        out["Q_pending_after_legacy_run"] = snapshot(h)
        out["Q_new_run_1"] = h.observe("Q new job run 1", lambda: h.run(new, "scheduled N2 run 1"))
        out["Q_pending_after_new_run"] = snapshot(h)
        # Observation: the paused legacy task offers 'resume'; resume it and run it once more.
        try:
            h.cron.task_manager.resume_task(legacy)
            out["Q_legacy_resume"] = "resumed"
        except Exception as exc:  # noqa: BLE001
            out["Q_legacy_resume"] = f"{type(exc).__name__}: {exc}"
        out["Q_consolidation_jobs_after_resume"] = consolidation_jobs(h.cron.store)
        if out["Q_legacy_resume"] == "resumed":
            out["Q_legacy_run_after_resume"] = h.observe("Q legacy run after resume",
                                                         lambda: h.run(legacy, "scheduled L2 run after resume"))
            out["Q_legacy_task_after_resumed_run"] = task_view(h.cron.task_manager, legacy)
            out["Q_consolidation_jobs_after_resumed_run"] = consolidation_jobs(h.cron.store)
            out["Q_pending_after_resumed_run"] = snapshot(h)
        out["Q_ids"] = {"legacy": legacy, "new": new}
        for k in ("Q_legacy_run", "Q_new_run_1", "Q_legacy_run_after_resume"):
            if k in out:
                show(out[k])
    finally:
        out["Q_closed"] = h.close()
    return out


if PHASE == "create":
    observed["create"] = guarded("create", create)
    x = observed["create"]
    lr = x.get("P_L_run_on_create_package") or {}
    c.check("create. the creating package's task.consolidate_memory job runs; its record and the pending intent "
            "for the upgrade are in place",
            lr.get("state") == "finished" and (x.get("P_L_task") or {}).get("state") == "active"
            and [p.get("label") for p in x.get("P_pending_at_end", [])] == ["earlier"]
            and (x.get("Q_L_run_on_create_package") or {}).get("state") == "finished",
            {"P_L_task": x.get("P_L_task"), "P_L_run": {k: lr.get(k) for k in ("state", "new_intents")},
             "P_pending": [p.get("label") for p in x.get("P_pending_at_end", [])],
             "Q_pending": [p.get("label") for p in x.get("Q_pending_at_end", [])]})
else:
    observed["upgrade"] = guarded("upgrade", upgrade)
    x = observed["upgrade"]
    lr = x.get("P_legacy_run") or {}
    c.check("P. the home created by 0.1.14 opens with this package; the legacy job's task record has no provenance "
            "kind and its cron job is enabled; the earlier intent is pending",
            (x.get("P_legacy_task_before") or {}).get("state") == "active"
            and (x.get("P_legacy_task_before") or {}).get("kind") is None
            and x.get("P_legacy_job_enabled_before") is True
            and [p.get("label") for p in x.get("P_pending_before", [])] == ["earlier"],
            {k: x.get(k) for k in ("created_with", "P_legacy_task_before", "P_legacy_job_enabled_before",
                                   "P_consolidation_jobs_before")}
            | {"pending": [p.get("label") for p in x.get("P_pending_before", [])]})
    c.check("P. pre-upgrade job: its first run on this package is not executed as a consolidation turn, so it cannot "
            "capture itself or carry consolidation context: the run fails with 'memory consolidation job is missing "
            "trusted task provenance; it was paused and must be recreated with task.consolidate_memory', no turn is "
            "submitted, no recovery runs and no intent is added",
            lr.get("state") == "failed"
            and "must be recreated with task.consolidate_memory" in json.dumps([lr.get("summary"), lr.get("error")])
            and lr.get("submits") == [] and lr.get("recovery") == [] and lr.get("new_intents") == []
            and lr.get("exclusions") == [] and lr.get("contexts") == [],
            {k: lr.get(k) for k in ("state", "summary", "error", "submits", "recovery", "new_intents",
                                    "task_lookups")})
    c.check("P. pre-upgrade job: pending work is preserved through that run (the earlier intent is unchanged and "
            "still pending)",
            x.get("P_pending_after_legacy_run") == x.get("P_pending_before")
            and [p.get("label") for p in x.get("P_pending_after_legacy_run", [])] == ["earlier"],
            {"before": [p.get("label") for p in x.get("P_pending_before", [])],
             "after": [p.get("label") for p in x.get("P_pending_after_legacy_run", [])]})
    c.check("P. the legacy task is paused and its cron job disabled; another run_task_now is refused (task not active); "
            "no consolidation schedule is enabled until one is recreated",
            (x.get("P_legacy_task_after") or {}).get("state") == "paused" and x.get("P_legacy_job_enabled_after") is False
            and "not active" in str(x.get("P_legacy_run_now_again"))
            and not any(j.get("enabled") for j in x.get("P_consolidation_jobs_after_legacy_run", [])),
            {k: x.get(k) for k in ("P_legacy_task_after", "P_legacy_job_enabled_after", "P_legacy_run_now_again",
                                   "P_consolidation_jobs_after_legacy_run")})
    n1, n2 = x.get("P_new_run_1") or {}, x.get("P_new_run_2") or {}
    RECOVERED = ["identity", "authorize=True", "capability=True", "source", "extract", "bundle.store", "bundle",
                 "result"]
    c.check("P. recreated with task.consolidate_memory: the new task has kind memory_consolidation; its first run "
            "carries the trusted context to the brain (capture_is_excluded receives it and returns True; the "
            "consolidation context is attached with scope agent:openminion and batch limit 8), the scheduled turn "
            "is 'excluded' with no extraction of its own, and the earlier pending capture is recovered once",
            (x.get("P_new_task") or {}).get("kind") == "memory_consolidation" and n1.get("state") == "finished"
            and len(n1.get("submits", [])) == 1 and n1["submits"][0].get("trusted_task_context") == EXPECTED_CONTEXT
            and len(n1.get("exclusions", [])) == 1 and n1["exclusions"][0].get("returned") is True
            and n1["exclusions"][0].get("consolidation_keys") == EXPECTED_CONTEXT
            and len(n1.get("contexts", [])) == 1
            and (n1["contexts"][0].get("state") or {}).get("target_scope") == "agent:openminion"
            and (n1["contexts"][0].get("state") or {}).get("batch_limit") == 8
            and [(i["capture_state"], i["results"]) for i in n1.get("new_intents", [])] == [("excluded", 0)]
            and n1.get("turn_extraction_requests") == 0
            and n1.get("recovery") == [[1, 1, 0]] and n1.get("recovery_steps_by_intent") == {"earlier": RECOVERED},
            {"task": x.get("P_new_task")} | {k: n1.get(k) for k in ("state", "submits", "exclusions", "contexts",
                                                                     "new_intents", "turn_extraction_requests",
                                                                     "recovery", "recovery_steps_by_intent")})
    c.check("P. the recreated job's second run recovers nothing and is excluded again; the earlier intent has exactly "
            "one result and one receipt",
            n2.get("recovery") == [[0, 0, 0]] and n2.get("recovery_steps_by_intent") == {}
            and [(i["capture_state"], i["results"]) for i in n2.get("new_intents", [])] == [("excluded", 0)]
            and x.get("P_earlier_after") == {"capture_state": "pending", "results": 1}
            and x.get("P_earlier_receipts") == 1,
            {k: n2.get(k) for k in ("recovery", "new_intents")} | {k: x.get(k) for k in ("P_earlier_after",
                                                                                         "P_earlier_receipts")})
    ids = x.get("P_ids") or {}
    c.check("P. after recreation there is exactly one enabled consolidation schedule (the new job); the legacy task "
            "stays paused with its job disabled",
            [j.get("job_id") for j in x.get("P_consolidation_jobs_at_end", []) if j.get("enabled")] == [ids.get("new")]
            and (x.get("P_legacy_task_at_end") or {}).get("state") == "paused",
            {k: x.get(k) for k in ("P_consolidation_jobs_at_end", "P_legacy_task_at_end", "P_ids")})
    ql, qn = x.get("Q_legacy_run") or {}, x.get("Q_new_run_1") or {}
    qids = x.get("Q_ids") or {}
    c.check("Q. incapable store, job recreated before the legacy job's next run: the legacy run is not executed as a "
            "consolidation turn (no submit, no recovery), the legacy task is paused, and the pending intents are "
            "unchanged",
            ql.get("state") == "failed" and ql.get("submits") == [] and ql.get("recovery") == []
            and (x.get("Q_legacy_task_after") or {}).get("state") == "paused"
            and x.get("Q_pending_after_legacy_run") == x.get("Q_pending_before") and len(x.get("Q_pending_before",
                                                                                               [])) >= 1,
            {k: ql.get(k) for k in ("state", "summary", "submits", "recovery")}
            | {"task_after": x.get("Q_legacy_task_after"),
               "pending_before": [p.get("label") for p in x.get("Q_pending_before", [])],
               "pending_after": [p.get("label") for p in x.get("Q_pending_after_legacy_run", [])]})
    n_pending = len(x.get("Q_pending_before", []))
    c.check("Q. the recreated job on the incapable store preserves pending work: its run asks the store and reports "
            "every pending intent pending without extracting; its scheduled turn is excluded; the pending intents "
            "are unchanged afterwards",
            qn.get("state") == "finished" and qn.get("recovery") == [[n_pending, 0, n_pending]]
            and all(v == ["identity", "authorize=True", "capability=False"]
                    for v in (qn.get("recovery_steps_by_intent") or {}).values())
            and qn.get("recovery_extraction_requests") == 0
            and [(i["capture_state"], i["results"]) for i in qn.get("new_intents", [])] == [("excluded", 0)]
            and x.get("Q_pending_after_new_run") == x.get("Q_pending_before"),
            {k: qn.get(k) for k in ("state", "recovery", "recovery_steps_by_intent", "recovery_extraction_requests",
                                    "new_intents")})
    qr = x.get("Q_legacy_run_after_resume") or {}
    c.check("Q. a resumed legacy task still cannot run a consolidation turn: after 'resume' its next run again submits "
            "no turn and runs no recovery, and it is paused again; the pending intents are unchanged",
            x.get("Q_legacy_resume") == "resumed" and qr.get("state") == "failed" and qr.get("submits") == []
            and qr.get("recovery") == [] and (x.get("Q_legacy_task_after_resumed_run") or {}).get("state") == "paused"
            and x.get("Q_pending_after_resumed_run") == x.get("Q_pending_before"),
            {"resume": x.get("Q_legacy_resume"), "run": {k: qr.get(k) for k in ("state", "summary", "submits",
                                                                                 "recovery")},
             "task_after": x.get("Q_legacy_task_after_resumed_run")})
    # Observation only (no documented procedure to check it against): enabled schedules while both jobs exist.
    observed["observation_enabled_schedules"] = {
        "after_recreate_before_legacy_run": x.get("Q_consolidation_jobs_after_recreate_before_legacy_run"),
        "after_legacy_run": x.get("Q_consolidation_jobs_after_legacy_run"),
        "after_resume_of_legacy_task": x.get("Q_consolidation_jobs_after_resume"),
        "after_resumed_legacy_run": x.get("Q_consolidation_jobs_after_resumed_run"), "ids": qids}

write_result(OUT, {"info": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items,
                   "observed": sanitize(observed)})
summary = c.summary()
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
