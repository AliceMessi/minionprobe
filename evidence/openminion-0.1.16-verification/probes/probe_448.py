"""#448 release verification: a turn's own memory capture asks the memory.write authorization first.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout:

    python -I -X utf8 probe_448.py <work_dir> <out.json> [--backend sqlite|postgres]

Every case builds a real `APIRuntime` for one agent, `openminion`, whose
`openai` provider is a scripted OpenAI-compatible server on 127.0.0.1 (the
harness of the #448 report and PR probes, `probe_420_lib.py`, unchanged). SDK
turns send "Please remember that I prefer tabs over spaces for indentation.";
the scripted auto fact extraction answers with one `user_preference` item.
Recorders call the original and never change a result (probe_448_lib.py):
every `memory.write` and `tool-runtime` decision of the agent's security
policy with its context, the runner and capture flags of each executed turn,
the steps of pending-capture recovery in order, the provider requests by kind,
the memctl bundle writes and the extractor calls. Consolidation runs go
through the daemon's TaskManager and cron scheduler (`task.consolidate_memory`
handler, `run_task_now`, `attach_cron_scheduler`). Diagnostic turns are the
installed `openminion agent-check` and `openminion doctor --check-turn` console
scripts as subprocesses.

--backend sqlite: runtime storage and the built-in memory store on SQLite.
--backend postgres: runtime storage on PostgreSQL (a new database per case) and
the built-in memory store on PostgreSQL (its own schema in that database);
needs PGURL or OPENMINION_TEST_POSTGRES_URL. The reference-sqlite cases (U1,
U2) use the bundled external backend on both.

Cases (check names start with the checklist item they cover):
  T   identity `NEVER write memory records` (require_approval): SDK turn with an
      approval callback that would approve; recovery gate; one consolidation
      run; then the home reopened with `NEVER reveal secrets`: two runs and a
      replay of the recovered bundle write.
  D   no identity, memory.write registered as POWER_USER (deny,
      missing_tool_scope; set in-process as the PR's test does).
  Ma/Md  `NEVER write memory records`, the model calls the `memory.write` tool
      (forced tool); the approval callback approves (Ma) or refuses (Md).
  R   one runtime and one runner: allowed turn, scheduled consolidation turn
      (capture excluded), denied turn, allowed turn, in-process diagnostic turn
      (capture disabled, agent-check's metadata), with the identity profile
      rewritten and `identity_reload()` between turns.
  X   `openminion agent-check` and `openminion doctor --check-turn` subprocesses.
  N1/N2  no identity profile; security policy disabled
      (OPENMINION_DISABLE_SECURITY_POLICY=true); the installed predicates.
  U1/U2  the bundled `reference-sqlite` backend (#468's unsupported store):
      allowed turn and recovery; denied turn, recovery, reopened allowed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize, write_result  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("work")
ap.add_argument("out")
ap.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
ARGS = ap.parse_args()
WORK = Path(ARGS.work).resolve()
OUT = Path(ARGS.out).resolve()
BACKEND = ARGS.backend
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
os.environ.pop("OPENMINION_DISABLE_SECURITY_POLICY", None)


def _dist(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


from probe_448_lib import (  # noqa: E402
    NEUTRAL_CONSTRAINT,
    TOOL_ARGS,
    WRITE_CONSTRAINT,
    DecisionLog,
    PgCase,
    StepRecorder,
    ToolCallProvider,
    TurnLog,
    build448,
    create_job,
    event_types,
    first_json,
    intents,
    parse,
    pg_base,
    pg_server_info,
    run_cli,
    run_job,
    save448,
    steps_by_root,
    write_profile,
)

if BACKEND == "postgres" and not pg_base():
    print("NOT RUN: --backend postgres needs PGURL or OPENMINION_TEST_POSTGRES_URL")
    sys.exit(2)

info = banner("probe_448", backend=BACKEND,
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "pydantic", "httpx", "SQLAlchemy",
                                                   "psycopg", "psycopg-binary", "alembic")},
              **(pg_server_info() if BACKEND == "postgres" else {}))

from openminion import Agent  # noqa: E402
from openminion.base.types import Message  # noqa: E402
from openminion.modules.memory.backends.external import register_reference_sqlite_backend  # noqa: E402
from openminion.modules.policy.adapters import memory as memory_adapter  # noqa: E402
from openminion.modules.session.capture import build_capture_identity  # noqa: E402
from openminion.services.agent.execution.composition import AgentServiceTurnFlowAdapter  # noqa: E402
from probe_420_lib import AGENT_ID, CAPTURE_MESSAGE, Cron, Observers, chdir_env, restore_env  # noqa: E402

register_reference_sqlite_backend()
c = Checks()
UNAUTHORIZED = "CAPTURE_RECOVERY_UNAUTHORIZED"
UNSUPPORTED = "CAPTURE_RECOVERY_UNSUPPORTED"
REPLY = f"{AGENT_ID}: Noted: tabs over spaces."
RESTRICTED = ("require_approval", "identity_hard_constraint_restricted")
PG_CASES: dict[str, PgCase] = {}


def pg_for(name: str) -> PgCase | None:
    if BACKEND != "postgres":
        return None
    if name not in PG_CASES:
        PG_CASES[name] = PgCase(name)
    return PG_CASES[name]


# ---------------------------------------------------------------- one runtime over one home
class Home:
    def __init__(self, name: str, root: Path, *, constraint: str | None = None, external: str | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.name, self.root = name, root
        self.pg = pg_for(name)
        self.provider = ToolCallProvider()
        self.old_env = chdir_env(root)
        self.env_keys = dict(env or {})
        for k, v in self.env_keys.items():
            os.environ[k] = v
        self.labels: dict[str, str] = {}
        self.runtime = self.obs = self.decisions = self.turns = self.rec = self.cron = None
        try:
            self.runtime = build448(root, self.provider, constraint=constraint, external=external,
                                    memory=self.pg.memory_config if self.pg is not None and external is None else None,
                                    storage_url=self.pg.url if self.pg is not None else None)
            self.obs = Observers(self.runtime).install()
            self.decisions = DecisionLog(self.obs.security_policy).install()
            self.turns = TurnLog(self.obs.service).install()
            self.rec = StepRecorder(self.runtime, self.obs).install()
            self.cron = Cron(self.runtime, root)
        except Exception:
            self.close()
            raise
        store = self.obs.assembly.service._store
        backend = getattr(store, "_backend", None)
        self.inner = getattr(store, "_store", None)
        session_backend = getattr(self.runtime.sessions, "_backend", None)
        self.backend = {
            "runtime_storage": self.runtime.config.storage.backend,
            "runtime_sessions": type(self.runtime.sessions).__name__,
            "runtime_sessions_record_store": type(getattr(session_backend, "_record_store", None)).__name__,
            "runtime_sessions_dialect": getattr(session_backend, "_dialect", "absent"),
            "memory_store": type(store).__name__,
            "memory_store_inner": None if self.inner is None else type(self.inner).__name__,
            "memory_backend": None if backend is None else f"{type(backend).__module__}.{type(backend).__name__}",
            "external_adapter": external,
            "memctl_supports_capture_bundles": self.capability(),
            "security_policy": None if self.obs.security_policy is None else type(self.obs.security_policy).__name__,
            "identity_constraints": list(getattr(self.obs.security_policy, "_identity_constraints", []) or []),
        }

    def capability(self) -> Any:
        predicate = getattr(self.obs.memctl, "supports_capture_bundles", None)
        if not callable(predicate):
            return "absent"
        try:
            return bool(predicate())
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    # -- memory store rows (SQLite file or PostgreSQL schema, read outside the package)
    def mem_rows(self, capture_id: str | None) -> dict[str, Any]:
        cid = str(capture_id or "")
        db = getattr(self.inner, "db_path", None)
        if db:
            from probe_420_lib import sqlite_rows

            def one(sql: str, params: tuple = ()) -> Any:
                r = sqlite_rows(Path(str(db)), sql, params)
                return r[0][0] if isinstance(r, list) and r else r

            where = "json_extract(meta_json, '$.capture_id')"
            return {"where": "sqlite memory store",
                    "capture_candidates_for_capture": one(f"SELECT COUNT(*) FROM memory_candidates WHERE {where} = ?",
                                                          (cid,)),
                    "receipts_for_capture": one("SELECT COUNT(*) FROM memory_capture_bundle_receipts WHERE "
                                                "capture_id = ?", (cid,)),
                    "capture_candidates_total": one(f"SELECT COUNT(*) FROM memory_candidates WHERE {where} IS NOT NULL"),
                    "receipts_total": one("SELECT COUNT(*) FROM memory_capture_bundle_receipts"),
                    "tool_records": one("SELECT COUNT(*) FROM memory_records WHERE key = ?", (TOOL_ARGS["key"],))}
        if self.pg is not None and self.inner is not None and "Postgres" in type(self.inner).__name__:
            s = self.pg.schema
            where = "CAST(meta_json AS jsonb)->>'capture_id'"

            def one(sql: str, params: dict[str, Any] | None = None) -> Any:
                r = self.pg.rows(sql, params)
                return r[0][0] if isinstance(r, list) and r else r

            return {"where": "postgres memory schema (server-side)",
                    "capture_candidates_for_capture": one(f'SELECT COUNT(*) FROM "{s}".memory_candidates WHERE '
                                                          f"{where} = :c", {"c": cid}),
                    "receipts_for_capture": one(f'SELECT COUNT(*) FROM "{s}".memory_capture_bundle_receipts WHERE '
                                                "capture_id = :c", {"c": cid}),
                    "capture_candidates_total": one(f'SELECT COUNT(*) FROM "{s}".memory_candidates WHERE '
                                                    f"{where} IS NOT NULL"),
                    "receipts_total": one(f'SELECT COUNT(*) FROM "{s}".memory_capture_bundle_receipts'),
                    "tool_records": one(f'SELECT COUNT(*) FROM "{s}".memory_records WHERE key = :k',
                                        {"k": TOOL_ARGS["key"]})}
        return {"where": f"not a built-in store ({self.backend.get('memory_backend')})"}

    def results(self, capture_id: str | None) -> list[dict[str, Any]]:
        from probe_448_lib import all_events

        return [dict(e.payload) for e in all_events(self.runtime.sessions, "memory.capture.result")
                if e.payload.get("capture_id") == capture_id]

    def server_side(self) -> dict[str, Any] | None:
        if self.pg is None:
            return None
        counts = self.pg.table_counts()
        return {"event_counts": self.pg.event_counts(),
                "nonzero_tables": {k: v for k, v in counts.items() if v} if isinstance(counts, dict) else counts,
                "connections_other_than_reader": self.pg.connections()}

    # -- one SDK turn
    def sdk_turn(self, session: str, *, approve: bool | None = None, forced_tools: list[str] | None = None,
                 label: str = "user") -> dict[str, Any]:
        before = {i["capture_id"] for i in intents(self.runtime)}
        dmark, tmark, pmark = self.decisions.mark(), len(self.turns.rows), len(self.provider.requests)
        bmark, emark = len(self.obs.bundle_writes), len(self.obs.extractor_calls)
        approvals: list[list[Any]] = []
        callback = None
        if approve is not None:
            def callback(*args: Any) -> bool:
                approvals.append([str(args[0]) if args else None, args[1] if len(args) > 1 else None])
                return bool(approve)
        out: dict[str, Any] = {"session": session}
        try:
            with Agent(runtime=self.runtime, forced_tools=forced_tools) as agent:
                res = agent.run(CAPTURE_MESSAGE, session_id=session, timeout_seconds=120, approval_callback=callback)
            md = dict(res.metadata or {})
            out.update({"run_state": res.run_state, "text": res.text,
                        "memory_capture_state": md.get("memory_capture_state"),
                        "memory_capture_bundle_result": parse(md.get("memory_capture_bundle_result"))})
        except Exception as exc:  # noqa: BLE001
            out["raised"] = f"{type(exc).__name__}: {exc}"
        new = [i for i in intents(self.runtime) if i["capture_id"] not in before]
        for i in new:
            self.labels.setdefault(str(i["root_turn_id"]), label)
        intent = new[0] if len(new) == 1 else {}
        kinds = [r["kind"] for r in self.provider.requests[pmark:]]
        out.update({
            "provider_requests": kinds,
            "extraction_requests": kinds.count("extraction"),
            "memory_capture_decisions": self.decisions.since(dmark, target="memory-capture"),
            "memory_capture_decision_actor_scopes": [d["actor_scopes"] for d in self.decisions.rows[dmark:]
                                                     if d["target"] == "memory-capture"],
            "tool_runtime_decisions": self.decisions.since(dmark, target="tool-runtime"),
            "approval_callback_calls": approvals,
            "bundle_writes": [{k: w.get(k) for k in ("capture_id", "raised")} for w in self.obs.bundle_writes[bmark:]],
            "extractor_calls": len(self.obs.extractor_calls) - emark,
            "turn_flags": self.turns.rows[tmark:],
            "new_intents": [{k: i[k] for k in ("capture_id", "capture_state", "session", "root_turn_id", "results")}
                            for i in new],
            "event_types": event_types(self.runtime, session),
        })
        out["intent"] = {k: intent.get(k) for k in ("capture_id", "capture_state", "session", "root_turn_id")}
        out["results_for_capture"] = self.results(intent.get("capture_id"))
        out["store"] = self.mem_rows(intent.get("capture_id"))
        print(f"[{self.name}] turn {session}: state={out.get('run_state')} capture={out.get('memory_capture_state')} "
              f"bundle={out.get('memory_capture_bundle_result')} requests={kinds} "
              f"decisions={[(d['target'], d['decision']) for d in out['memory_capture_decisions']]} "
              f"approvals={len(approvals)}", flush=True)
        return out

    def recovery_gate(self, intent: dict[str, Any]) -> dict[str, Any]:
        identity = build_capture_identity(runtime_session_id=intent["session"], root_turn_id=intent["root_turn_id"])
        port = AgentServiceTurnFlowAdapter(self.obs.service)
        dmark = self.decisions.mark()
        allowed = memory_adapter.memory_capture_recovery_allowed(
            identity.runtime_session_id, identity.root_turn_id, identity.event_id, identity.capture_id,
            policy=port.security_policy, tools=port.tools, agent_id=port.identity_agent_id)
        return {"returned": allowed, "decisions": self.decisions.since(dmark)}

    def consolidation(self, job_id: str, index: int) -> dict[str, Any]:
        rt = self.runtime
        before = intents(rt)
        self.rec.capture_roots = {i["capture_id"]: i["root_turn_id"] for i in before}
        smark, pmark, rmark = self.rec.mark(), len(self.provider.requests), len(self.obs.recoveries)
        dmark, tmark = self.decisions.mark(), len(self.turns.rows)
        done = run_job(self.cron, self.obs, job_id)
        steps = self.rec.since(smark)
        recs = self.obs.recoveries[rmark:]
        windows = [(r["t_start"], r["t_end"]) for r in recs]
        rec_kinds = [k for s, e in windows for k in self.provider.kinds_between(s, e)]
        kinds = [r["kind"] for r in self.provider.requests[pmark:]]
        after = intents(rt)
        known = {i["capture_id"] for i in before}
        for i in after:
            if i["capture_id"] not in known:
                self.labels.setdefault(str(i["root_turn_id"]), f"scheduled turn {index}")
        row = {"run": index, **done,
               "recovery": [r.get("result") for r in recs], "recovery_raised": [r.get("raised") for r in recs
                                                                                if r.get("raised")],
               "steps_by_intent": steps_by_root(steps, self.labels),
               "recovery_extraction_requests": rec_kinds.count("extraction"),
               "extraction_requests_in_run": kinds.count("extraction"),
               "provider_requests_by_kind": {k: kinds.count(k) for k in sorted(set(kinds))},
               "recovery_decisions": self.decisions.since(dmark, target="memory-capture-recovery"),
               "scheduled_turn_decisions": self.decisions.since(dmark, target="memory-capture"),
               "scheduled_turn_flags": self.turns.rows[tmark:],
               "scheduled_turn_intents": [{"capture_state": i["capture_state"], "results": i["results"]}
                                          for i in after if i["capture_id"] not in known]}
        print(f"[{self.name}] consolidation run {index}: state={row['state']} recovery={row['recovery']} "
              f"steps={row['steps_by_intent']} recovery_extractions={row['recovery_extraction_requests']}", flush=True)
        return row

    def close(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.cron is not None:
            try:
                self.cron.stop()
            except Exception as exc:  # noqa: BLE001
                out["scheduler_stop"] = f"{type(exc).__name__}: {exc}"
        for part in (self.rec, self.turns, self.decisions, self.obs):
            if part is not None:
                try:
                    part.uninstall()
                except Exception as exc:  # noqa: BLE001
                    out.setdefault("uninstall", []).append(f"{type(exc).__name__}: {exc}")
        if self.runtime is not None:
            try:
                self.runtime.close()
            except Exception as exc:  # noqa: BLE001
                out["runtime_close"] = f"{type(exc).__name__}: {exc}"
        for k in self.env_keys:
            os.environ.pop(k, None)
        restore_env(self.old_env)
        self.provider.close()
        self.runtime = None
        return out


def guarded(name: str, fn: Any) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        print(f"[{name}] case error: {type(exc).__name__}: {exc}", flush=True)
        return {"case_error": f"{type(exc).__name__}: {exc}",
                "case_error_frames": [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                      for f in traceback.extract_tb(exc.__traceback__)][-8:]}


def offline(*homes: Home) -> list[str]:
    return sorted({h.provider.base_url.split("/")[2].split(":")[0] for h in homes})


HOSTS: set[str] = set()


# ---------------------------------------------------------------- T: require_approval, then recovery
def case_t() -> dict[str, Any]:
    root = WORK / "T"
    out: dict[str, Any] = {}
    h = Home("T", root, constraint=WRITE_CONSTRAINT)
    try:
        HOSTS.update(offline(h))
        out["backend"] = h.backend
        out["turn"] = t = h.sdk_turn("capture", approve=True)
        out["recovery_gate"] = h.recovery_gate(t["intent"]) if t["intent"].get("root_turn_id") else "no intent"
        out["job_id"] = job = str(create_job(h.cron).get("task_id"))
        h.cron.start()
        out["run1_denied"] = h.consolidation(job, 1)
        out["store_after_run1"] = h.mem_rows(t["intent"].get("capture_id"))
        out["results_after_run1"] = h.results(t["intent"].get("capture_id"))
    finally:
        out["close1"] = h.close()
    labels = h.labels
    h = Home("T", root, constraint=NEUTRAL_CONSTRAINT)
    h.labels.update(labels)
    try:
        out["backend_reopened"] = h.backend
        h.cron.start()
        out["run2_allowed"] = h.consolidation(job, 2)
        out["run3"] = h.consolidation(job, 3)
        cid = out["turn"]["intent"].get("capture_id")
        writes = [w for w in h.obs.bundle_writes if w.get("capture_id") == cid and "raised" not in w]
        out["recovery_bundle_writes"] = len(writes)
        out["store_after_run3"] = h.mem_rows(cid)
        out["results_after_run3"] = h.results(cid)
        if writes:
            kwargs = {k: writes[0][k] for k in ("capture_id", "root_turn_id", "session_id", "agent_id", "candidates")}
            replay = h.obs.memctl.apply_capture_bundle(**kwargs)
            out["replay_bundle_write"] = {k: replay.get(k) for k in ("capture_id", "disposition", "output_ids")} \
                if isinstance(replay, dict) else repr(replay)
            out["original_bundle_write"] = {k: (writes[0].get("result") or {}).get(k)
                                            for k in ("capture_id", "disposition", "output_ids")}
        out["run4_after_replay"] = h.consolidation(job, 4)
        out["store_after_replay"] = h.mem_rows(cid)
        out["results_after_replay"] = h.results(cid)
        out["intent_after"] = next(({k: i[k] for k in ("capture_state", "results", "result_disposition")}
                                    for i in intents(h.runtime) if i["capture_id"] == cid), None)
        out["server_side"] = h.server_side()
    finally:
        out["close2"] = h.close()
    out["labels"] = labels
    return out


# ---------------------------------------------------------------- D: deny (missing_tool_scope)
def case_d() -> dict[str, Any]:
    out: dict[str, Any] = {}
    h = Home("D", WORK / "D")
    try:
        HOSTS.update(offline(h))
        out["backend"] = h.backend
        tools = AgentServiceTurnFlowAdapter(h.obs.service).tools
        spec = h.runtime.tools.get("memory.write")
        out["same_registry"] = tools is h.runtime.tools
        out["profile_before"] = sorted(tools.policy_for("memory.write").required_scopes_all)
        spec.min_scope = "POWER_USER"
        out["profile_after"] = sorted(tools.policy_for("memory.write").required_scopes_all)
        out["turn"] = t = h.sdk_turn("capture", approve=True)
        out["recovery_gate"] = h.recovery_gate(t["intent"]) if t["intent"].get("root_turn_id") else "no intent"
        out["server_side"] = h.server_side()
    finally:
        out["close"] = h.close()
    return out


# ---------------------------------------------------------------- Ma/Md: model memory.write tool call
def case_m(name: str, approve: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    h = Home(name, WORK / name, constraint=WRITE_CONSTRAINT)
    try:
        HOSTS.update(offline(h))
        out["backend"] = h.backend
        h.provider.write_tool_call = True
        out["turn"] = h.sdk_turn("capture", approve=approve, forced_tools=["memory.write"])
        out["server_side"] = h.server_side()
    finally:
        out["close"] = h.close()
    return out


# ---------------------------------------------------------------- R: one runner, allow/deny transitions
def case_r() -> dict[str, Any]:
    root = WORK / "R"
    out: dict[str, Any] = {}
    h = Home("R", root, constraint=NEUTRAL_CONSTRAINT)
    try:
        HOSTS.update(offline(h))
        out["backend"] = h.backend
        service = h.obs.service
        runner = service._get_runner()
        out["runner_flag_before_first_turn"] = getattr(runner, "_capture_authorized_for_turn", "absent")

        def constrain(constraint: str) -> Any:
            write_profile(root, constraint)
            reloaded = service.identity_reload()
            return {"constraint": constraint,
                    "policy_constraints": list(getattr(h.obs.security_policy, "_identity_constraints", []) or []),
                    "reload_ok": bool(reloaded.get("ok", True)) if isinstance(reloaded, dict) else repr(reloaded)}

        seq: list[dict[str, Any]] = []
        seq.append({"step": "allow-1", "turn": h.sdk_turn("r1-allow")})
        job = str(create_job(h.cron).get("task_id"))
        h.cron.start()
        seq.append({"step": "consolidation (excluded)", "run": h.consolidation(job, 1)})
        seq.append({"step": "deny-2", "reload": constrain(WRITE_CONSTRAINT), "turn": h.sdk_turn("r2-deny")})
        seq.append({"step": "allow-3", "reload": constrain(NEUTRAL_CONSTRAINT), "turn": h.sdk_turn("r3-allow")})
        seq.append({"step": "deny-4", "reload": constrain(WRITE_CONSTRAINT), "turn": h.sdk_turn("r4-deny")})
        seq.append({"step": "allow-5", "reload": constrain(NEUTRAL_CONSTRAINT), "turn": h.sdk_turn("r5-allow")})
        # The in-process diagnostic turn, with agent-check's metadata (terminal capture disabled).
        dmark, tmark, pmark = h.decisions.mark(), len(h.turns.rows), len(h.provider.requests)
        request_id = "probe-check-1"
        try:
            reply = asyncio.run(service.run_turn(Message(
                channel="console", target="doctor", body=CAPTURE_MESSAGE,
                metadata={"session_id": f"runtime:agent-check:{request_id}", "request_id": request_id,
                          "turn_id": request_id, "invocation_scope": "runtime", "diagnostic_scope": "runtime"})))
            diag = {"text": getattr(reply, "text", None)}
        except Exception as exc:  # noqa: BLE001
            diag = {"raised": f"{type(exc).__name__}: {exc}"}
        kinds = [r["kind"] for r in h.provider.requests[pmark:]]
        diag.update({"provider_requests": kinds, "extraction_requests": kinds.count("extraction"),
                     "memory_capture_decisions": h.decisions.since(dmark, target="memory-capture"),
                     "turn_flags": h.turns.rows[tmark:]})
        seq.append({"step": "diagnostic (disabled, in-process)", "diagnostic": diag})
        out["sequence"] = seq
        out["runner_ids"] = sorted(set(h.turns.runner_ids.values()))
        out["all_turn_flags"] = h.turns.rows
        out["service_runner_is_same_object"] = service._get_runner() is runner
        out["server_side"] = h.server_side()
    finally:
        out["close"] = h.close()
    return out


# ---------------------------------------------------------------- X: diagnostic CLI turns
def case_x() -> dict[str, Any]:
    from probe_420_lib import ScriptedProvider

    root = WORK / "X"
    out: dict[str, Any] = {}
    pg = pg_for("X")
    provider = ScriptedProvider()
    try:
        HOSTS.add(provider.base_url.split("/")[2].split(":")[0])
        cfg = save448(root, provider.base_url, constraint=NEUTRAL_CONSTRAINT,
                      storage_url=pg.url if pg is not None else None)
        for key, argv in (("agent_check", ["agent-check", "--message", CAPTURE_MESSAGE, "--json"]),
                          ("doctor", ["doctor", "--check-turn", "--message", CAPTURE_MESSAGE, "--json"])):
            mark = len(provider.requests)
            result = run_cli(root, cfg, argv)
            kinds = [r["kind"] for r in provider.requests[mark:]]
            payload = first_json(result["stdout"])
            row = {k: v for k, v in result.items() if k != "stdout"}
            row["provider_requests"] = kinds
            row["extraction_requests"] = kinds.count("extraction")
            if key == "agent_check":
                row["json"] = {k: (payload or {}).get(k) for k in ("ok", "status", "scope", "channel")}
                md = (payload or {}).get("metadata") or {}
                row["response_metadata"] = {k: md.get(k) for k in ("memory_capture_state",
                                                                   "memory_capture_bundle_result")}
            else:
                checks = (payload or {}).get("checks") or []
                smoke = [ch for ch in checks if isinstance(ch, dict) and ch.get("id") == "agent.turn_smoke"]
                row["turn_smoke"] = {k: (smoke[0] if smoke else {}).get(k) for k in ("status", "message")}
                row["doctor_status"] = (payload or {}).get("status") if isinstance(payload, dict) else None
            out[key] = row
            print(f"[X] {key}: exit={result['exit_code']} requests={kinds}", flush=True)
        if pg is not None:
            counts = pg.table_counts()
            out["server_side_after_cli"] = {
                "nonzero_tables": {k: v for k, v in counts.items() if v} if isinstance(counts, dict) else counts,
                "event_counts": pg.event_counts(), "connections_other_than_reader": pg.connections()}
    finally:
        provider.close()
    # The same home, opened in-process afterwards: nothing captured.
    h = Home("X", root, constraint=NEUTRAL_CONSTRAINT)
    try:
        out["backend"] = h.backend
        out["intents"] = [{k: i[k] for k in ("capture_state", "session", "results")} for i in intents(h.runtime)]
        out["capture_results_total"] = len([1 for i in intents(h.runtime) if i["results"]])
        out["store"] = h.mem_rows(None)
        out["server_side"] = h.server_side()
    finally:
        out["close"] = h.close()
    return out


# ---------------------------------------------------------------- N1/N2: no identity, no policy, predicates
def case_n1() -> dict[str, Any]:
    out: dict[str, Any] = {}
    h = Home("N1", WORK / "N1")
    try:
        HOSTS.update(offline(h))
        out["backend"] = h.backend
        out["runner_flag_before_first_turn"] = getattr(h.obs.service._get_runner(), "_capture_authorized_for_turn",
                                                       "absent")
        out["turn"] = h.sdk_turn("capture")
        # The installed predicates, with this runtime's real policy and tool registry.
        port = AgentServiceTurnFlowAdapter(h.obs.service)
        pred: dict[str, Any] = {}
        for name in ("memory_capture_allowed", "memory_capture_recovery_allowed"):
            fn = getattr(memory_adapter, name, None)
            if fn is None:
                pred[name] = "absent"
                continue
            args = ("s-1", "root-1") if name == "memory_capture_allowed" else ("s-1", "root-1", "e-1", "c-1")
            extra = {"channel": "console"} if name == "memory_capture_allowed" else {}
            row: dict[str, Any] = {}
            for label, policy, tools in (("no policy", None, port.tools), ("policy, no tools", port.security_policy,
                                                                             None),
                                         ("policy and tools", port.security_policy, port.tools)):
                mark = h.decisions.mark()
                try:
                    row[label] = {"returned": fn(*args, policy=policy, tools=tools, agent_id=port.identity_agent_id,
                                                 **extra),
                                  "policy_evaluations": len(h.decisions.rows) - mark}
                except Exception as exc:  # noqa: BLE001
                    row[label] = f"{type(exc).__name__}: {exc}"
            pred[name] = row
        out["predicates"] = pred
        out["server_side"] = h.server_side()
    finally:
        out["close"] = h.close()
    return out


def case_n2() -> dict[str, Any]:
    out: dict[str, Any] = {}
    h = Home("N2", WORK / "N2", constraint=WRITE_CONSTRAINT, env={"OPENMINION_DISABLE_SECURITY_POLICY": "true"})
    try:
        HOSTS.update(offline(h))
        out["backend"] = h.backend
        out["service_security_policy_is_none"] = getattr(h.obs.service, "_security_policy", "absent") is None
        out["turn"] = h.sdk_turn("capture")
        out["server_side"] = h.server_side()
    finally:
        out["close"] = h.close()
    return out


# ---------------------------------------------------------------- U1/U2: reference-sqlite (#468's controls)
def case_u1() -> dict[str, Any]:
    out: dict[str, Any] = {}
    h = Home("U1", WORK / "U1", constraint=NEUTRAL_CONSTRAINT, external="reference-sqlite")
    try:
        HOSTS.update(offline(h))
        out["backend"] = h.backend
        out["turn"] = h.sdk_turn("capture")
        job = str(create_job(h.cron).get("task_id"))
        h.cron.start()
        out["runs"] = [h.consolidation(job, i) for i in (1, 2)]
        out["intent_after"] = [{k: i[k] for k in ("capture_state", "results")} for i in intents(h.runtime)
                               if h.labels.get(str(i["root_turn_id"])) == "user"]
        out["server_side"] = h.server_side()
    finally:
        out["close"] = h.close()
    return out


def case_u2() -> dict[str, Any]:
    root = WORK / "U2"
    out: dict[str, Any] = {}
    h = Home("U2", root, constraint=WRITE_CONSTRAINT, external="reference-sqlite")
    try:
        HOSTS.update(offline(h))
        out["backend"] = h.backend
        out["turn"] = h.sdk_turn("capture")
        out["job_id"] = job = str(create_job(h.cron).get("task_id"))
        h.cron.start()
        out["run1_denied"] = h.consolidation(job, 1)
    finally:
        out["close1"] = h.close()
    labels = h.labels
    h = Home("U2", root, constraint=NEUTRAL_CONSTRAINT, external="reference-sqlite")
    h.labels.update(labels)
    try:
        out["backend_reopened"] = h.backend
        h.cron.start()
        out["run2_allowed"] = h.consolidation(job, 2)
        out["intent_after"] = [{k: i[k] for k in ("capture_state", "results")} for i in intents(h.runtime)
                               if h.labels.get(str(i["root_turn_id"])) == "user"]
        out["server_side"] = h.server_side()
    finally:
        out["close2"] = h.close()
    return out


# ---------------------------------------------------------------- run the cases
observed: dict[str, Any] = {}
try:
    for key, fn in (("T", case_t), ("D", case_d), ("Ma", lambda: case_m("Ma", True)),
                    ("Md", lambda: case_m("Md", False)), ("R", case_r), ("X", case_x), ("N1", case_n1),
                    ("N2", case_n2), ("U1", case_u1), ("U2", case_u2)):
        t0 = time.monotonic()
        observed[key] = guarded(key, fn)
        observed[key]["seconds"] = round(time.monotonic() - t0, 1)
finally:
    for pg in PG_CASES.values():
        try:
            pg.drop()
        except Exception as exc:  # noqa: BLE001
            print(f"drop {pg.dbname}: {type(exc).__name__}: {exc}")


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


def decision_rows(rows: list[dict[str, Any]] | None) -> list[tuple]:
    return [(d.get("channel"), d.get("target"), d.get("session_id"), d.get("run_id"), d.get("decision"),
             d.get("reason_code")) for d in rows or []]


def pending_unauthorized(turn: dict[str, Any]) -> bool:
    return (turn.get("memory_capture_state") == "pending" and g(turn, "intent", "capture_state") == "pending"
            and turn.get("memory_capture_bundle_result") == {"capture_id": g(turn, "intent", "capture_id"),
                                                             "disposition": "pending", "error_code": UNAUTHORIZED})


def nothing_captured(turn: dict[str, Any]) -> bool:
    st = turn.get("store") or {}
    return (turn.get("extraction_requests") == 0 and turn.get("extractor_calls") == 0
            and turn.get("bundle_writes") == [] and turn.get("results_for_capture") == []
            and st.get("capture_candidates_for_capture") == 0 and st.get("receipts_for_capture") == 0)


def captured(turn: dict[str, Any]) -> bool:
    st = turn.get("store") or {}
    bundle = turn.get("memory_capture_bundle_result") or {}
    res = turn.get("results_for_capture") or []
    return (turn.get("extraction_requests") == 1 and turn.get("memory_capture_state") == "succeeded"
            and bundle.get("disposition") == "succeeded" and len(bundle.get("output_ids") or []) == 1
            and len(res) == 1 and res[0].get("output_ids") == bundle.get("output_ids")
            and st.get("capture_candidates_for_capture") == 1 and st.get("receipts_for_capture") == 1)


def turn_view(turn: dict[str, Any] | None) -> dict[str, Any]:
    turn = turn or {}
    return {k: turn.get(k) for k in ("run_state", "text", "raised", "memory_capture_state",
                                     "memory_capture_bundle_result", "provider_requests", "bundle_writes",
                                     "results_for_capture", "store", "memory_capture_decisions",
                                     "approval_callback_calls", "tool_runtime_decisions", "intent")}


def no_approval_events(turn: dict[str, Any]) -> bool:
    return not [e for e in turn.get("event_types") or [] if "approv" in e.lower() or "confirm" in e.lower()]


T, D, MA, MD, R, X = (observed.get(k, {}) for k in ("T", "D", "Ma", "Md", "R", "X"))
N1, N2, U1, U2 = (observed.get(k, {}) for k in ("N1", "N2", "U1", "U2"))
tt, dt = T.get("turn") or {}, D.get("turn") or {}

c.check("setup: the identity profile is loaded where a case has one (the agent's SecurityPolicyEngine holds the "
        "constraint)",
        g(T, "backend", "identity_constraints") == [WRITE_CONSTRAINT]
        and g(T, "backend_reopened", "identity_constraints") == [NEUTRAL_CONSTRAINT]
        and g(D, "backend", "identity_constraints") == [] and g(MA, "backend", "identity_constraints") == [WRITE_CONSTRAINT]
        and g(R, "backend", "identity_constraints") == [NEUTRAL_CONSTRAINT],
        {k: [g(v, "backend", "identity_constraints"), g(v, "backend_reopened", "identity_constraints")]
         for k, v in observed.items()})
c.check(f"setup: backend {BACKEND}: runtime storage and the built-in memory store as requested "
        "(classes recorded per case)",
        all(g(v, "backend", "runtime_storage") == ("postgres" if BACKEND == "postgres" else "sqlite")
            for k, v in observed.items() if k in ("T", "D", "R", "N1", "U1"))
        and (g(T, "backend", "memory_store_inner") or "").startswith(
            "Postgres" if BACKEND == "postgres" else "SQLite")
        and all(g(v, "backend", "runtime_sessions_dialect") == BACKEND
                for k, v in observed.items() if k in ("T", "D", "R", "N1", "U1")),
        {k: v.get("backend") for k, v in observed.items()})

# item 1
c.check("item 1: require_approval (identity `NEVER write memory records`, case T): the SDK turn asks memory.write "
        "once with the turn's context (console, memory-capture, its runtime session, its root turn) and gets "
        "require_approval / identity_hard_constraint_restricted",
        decision_rows(tt.get("memory_capture_decisions"))
        == [("console", "memory-capture", "capture", g(tt, "intent", "root_turn_id"), *RESTRICTED)],
        tt.get("memory_capture_decisions"))
c.check("item 1: require_approval (T): no automatic extraction request, no extractor call, no bundle write, no "
        "capture candidate, no receipt and no memory.capture.result",
        nothing_captured(tt), turn_view(tt))
c.check("item 1: require_approval (T): no ordinary tool approval borrowed or requested for the capture: the SDK "
        "approval callback (which would approve) is never called, no tool-runtime decision, no approval event",
        tt.get("approval_callback_calls") == [] and tt.get("tool_runtime_decisions") == [] and no_approval_events(tt),
        {"approvals": tt.get("approval_callback_calls"), "tool_runtime": tt.get("tool_runtime_decisions"),
         "event_types": tt.get("event_types")})
c.check("item 1: require_approval (T): the reply remains usable (completed, the same text as the allowed turns)",
        tt.get("run_state") == "completed" and tt.get("text") == REPLY == g(N1, "turn", "text"),
        {"T": [tt.get("run_state"), tt.get("text"), tt.get("raised")], "N1": g(N1, "turn", "text")})
c.check("item 1: deny (case D, memory.write registered as POWER_USER; the internal actor lacks "
        "tool.execute.elevated): the turn asks memory.write once with its context and gets deny / "
        "missing_tool_scope",
        g(D, "profile_after") == ["tool.execute", "tool.execute.elevated"]
        and decision_rows(dt.get("memory_capture_decisions"))
        == [("console", "memory-capture", "capture", g(dt, "intent", "root_turn_id"), "deny", "missing_tool_scope")],
        {"profile_before": D.get("profile_before"), "profile_after": D.get("profile_after"),
         "decisions": dt.get("memory_capture_decisions")})
c.check("item 1: deny (D): no extraction request, no bundle write, candidate, receipt or result; no approval "
        "callback call or approval event; the reply is the same",
        nothing_captured(dt) and dt.get("approval_callback_calls") == [] and no_approval_events(dt)
        and dt.get("run_state") == "completed" and dt.get("text") == REPLY, turn_view(dt))
for key, m in (("Ma", MA), ("Md", MD)):
    mt = m.get("turn") or {}
    c.check(f"item 1: the user's approval of the model's own memory.write tool call is not borrowed ({key}, "
            f"callback {'approves' if key == 'Ma' else 'refuses'}): the turn's capture still asks its own "
            "question (require_approval), makes no extraction request and writes no capture candidate, receipt or "
            "result; the intent stays pending with CAPTURE_RECOVERY_UNAUTHORIZED",
            decision_rows(mt.get("memory_capture_decisions"))
            == [("console", "memory-capture", "capture", g(mt, "intent", "root_turn_id"), *RESTRICTED)]
            and nothing_captured(mt) and pending_unauthorized(mt), turn_view(mt))

# item 2
c.check("item 2: the capture intent stays pending; the turn's memory_capture_state is pending and its "
        "memory_capture_bundle_result is {capture_id, disposition pending, error_code CAPTURE_RECOVERY_UNAUTHORIZED} "
        "(T require_approval and D deny)",
        pending_unauthorized(tt) and pending_unauthorized(dt),
        {"T": [tt.get("intent"), tt.get("memory_capture_state"), tt.get("memory_capture_bundle_result")],
         "D": [dt.get("intent"), dt.get("memory_capture_state"), dt.get("memory_capture_bundle_result")]})
gate = T.get("recovery_gate") or {}
c.check("item 2: recovery's own gate for the same capture (installed memory_capture_recovery_allowed, as the cron "
        "executor binds it) returns False with the same decision; it differs only by channel cron and target "
        "memory-capture-recovery (T; D gives deny / missing_tool_scope)",
        gate.get("returned") is False and decision_rows(gate.get("decisions"))
        == [("cron", "memory-capture-recovery", "capture", g(tt, "intent", "root_turn_id"), *RESTRICTED)]
        and g(D, "recovery_gate", "returned") is False
        and [r[4:] for r in decision_rows(g(D, "recovery_gate", "decisions"))] == [("deny", "missing_tool_scope")],
        {"T": gate, "D": D.get("recovery_gate")})
r1 = T.get("run1_denied") or {}
c.check("item 2: a consolidation run while the write is still denied checks policy first and stops: recovery "
        "(1, 0, 1), the user intent's steps are exactly [identity, authorize=False] (no capability query, source "
        "read, extraction, bundle write or result write), no extraction request in recovery, no result",
        r1.get("state") == "finished" and r1.get("recovery") == [[1, 0, 1]]
        and g(r1, "steps_by_intent", "user") == ["identity", "authorize=False"]
        and r1.get("recovery_extraction_requests") == 0 and T.get("results_after_run1") == []
        and g(T, "store_after_run1", "capture_candidates_for_capture") == 0,
        {k: r1.get(k) for k in ("state", "error", "recovery", "steps_by_intent", "recovery_extraction_requests",
                                "recovery_decisions")})
r2, r3, r4 = (T.get(k) or {} for k in ("run2_allowed", "run3", "run4_after_replay"))
c.check("item 2: reopened with the write allowed, the next run checks policy and capability before extraction and "
        "recovers the intent once: recovery (1, 1, 0); the user intent's steps in order are identity, "
        "authorize=True, capability=True, source, extract, the store and memctl bundle writes, result; one "
        "extraction request in recovery",
        r2.get("state") == "finished" and r2.get("recovery") == [[1, 1, 0]]
        and g(r2, "steps_by_intent", "user") == ["identity", "authorize=True", "capability=True", "source", "extract",
                                                 "bundle.store", "bundle", "result"]
        and r2.get("recovery_extraction_requests") == 1
        and decision_rows(r2.get("recovery_decisions"))
        == [("cron", "memory-capture-recovery", "capture", g(tt, "intent", "root_turn_id"), "allow", "allowed")],
        {k: r2.get(k) for k in ("state", "error", "recovery", "steps_by_intent", "recovery_extraction_requests",
                                "recovery_decisions")})
res3 = T.get("results_after_run3") or []
c.check("item 2: the intent is committed once: one memory.capture.result (succeeded, one output id), one capture "
        "candidate and one receipt for its capture id; the next run scans nothing (0, 0, 0) with no recovery step",
        len(res3) == 1 and res3[0].get("disposition") == "succeeded" and len(res3[0].get("output_ids") or []) == 1
        and g(T, "store_after_run3", "capture_candidates_for_capture") == 1
        and g(T, "store_after_run3", "receipts_for_capture") == 1 and T.get("recovery_bundle_writes") == 1
        and r3.get("recovery") == [[0, 0, 0]] and r3.get("steps_by_intent") == {},
        {"results": res3, "store": T.get("store_after_run3"), "recovery_bundle_writes": T.get("recovery_bundle_writes"),
         "run3": {k: r3.get(k) for k in ("recovery", "steps_by_intent")}, "intent_after": T.get("intent_after")})
c.check("item 2: no duplicates on replay: replaying the recovered bundle write returns the original receipt "
        "(same disposition and output ids); another run scans nothing; still one candidate, one receipt and one "
        "memory.capture.result",
        T.get("replay_bundle_write") == T.get("original_bundle_write") and bool(T.get("original_bundle_write"))
        and r4.get("recovery") == [[0, 0, 0]] and len(T.get("results_after_replay") or []) == 1
        and g(T, "store_after_replay", "capture_candidates_for_capture") == 1
        and g(T, "store_after_replay", "receipts_for_capture") == 1
        and g(T, "store_after_replay", "capture_candidates_total") == 1 and g(T, "store_after_replay", "receipts_total") == 1,
        {"replay": T.get("replay_bundle_write"), "original": T.get("original_bundle_write"),
         "run4": r4.get("recovery"), "store": T.get("store_after_replay"),
         "results": len(T.get("results_after_replay") or []), "server_side": T.get("server_side")})

# item 3
seq = R.get("sequence") or []
by = {s["step"]: s for s in seq}
rt = {k: (by.get(k) or {}).get("turn") or {} for k in ("allow-1", "deny-2", "allow-3", "deny-4", "allow-5")}
c.check("item 3: an allowed turn still captures normally (R allow-1, the neutral profile): one extraction "
        "request, memory_capture_state succeeded, one candidate, one receipt and one memory.capture.result with "
        "the turn's output id",
        captured(rt["allow-1"]), turn_view(rt["allow-1"]))
c.check("item 3: the allowed turn asked memory.write once, before its capture, with its context, and got allow",
        decision_rows(rt["allow-1"].get("memory_capture_decisions"))
        == [("console", "memory-capture", "r1-allow", g(rt["allow-1"], "intent", "root_turn_id"), "allow", "allowed")],
        rt["allow-1"].get("memory_capture_decisions"))
expected_flags = [True, False, False, True, False, True, False]
flags = [row.get("capture_authorized_for_turn") for row in R.get("all_turn_flags") or []]
c.check("item 3: one runner is reused for every turn of case R (the service's runner object is the same one before "
        "the first turn and after the last; every executed turn ran on it)",
        R.get("runner_ids") == [1] and R.get("service_runner_is_same_object") is True and len(flags) == 7,
        {"runner_ids": R.get("runner_ids"), "same_object": R.get("service_runner_is_same_object"),
         "turns_executed": len(flags)})
c.check("item 3: the decision is refreshed per turn on that runner: _capture_authorized_for_turn at each executed "
        "turn is allow-1 True, consolidation False (excluded), deny-2 False, allow-3 True, deny-4 False, allow-5 True, "
        "diagnostic False (disabled)",
        flags == expected_flags, R.get("all_turn_flags"))
ctx_ok = True
ctx_rows = {}
for k, want in (("allow-1", "allow"), ("deny-2", "require_approval"), ("allow-3", "allow"),
                ("deny-4", "require_approval"), ("allow-5", "allow")):
    t = rt[k]
    rows = decision_rows(t.get("memory_capture_decisions"))
    ctx_rows[k] = rows
    ctx_ok = ctx_ok and len(rows) == 1 and rows[0][:4] == ("console", "memory-capture", t.get("session"),
                                                            g(t, "intent", "root_turn_id")) and rows[0][4] == want \
        and bool(g(t, "intent", "root_turn_id"))
roots = [g(rt[k], "intent", "root_turn_id") for k in rt]
c.check("item 3: allow->deny and deny->allow transitions (twice each) use the actual context: each SDK turn asks "
        "memory.write once with channel console, target memory-capture, its own runtime session and its own root "
        "turn (all different), and gets allow / require_approval / allow / require_approval / allow",
        ctx_ok and len(set(roots)) == 5, ctx_rows)
c.check("item 3: the outcomes follow the current answer: allowed turns captured (1 extraction, succeeded), "
        "denied turns made no extraction request and stay pending with CAPTURE_RECOVERY_UNAUTHORIZED",
        all(captured(rt[k]) for k in ("allow-1", "allow-3", "allow-5"))
        and all(nothing_captured(rt[k]) and pending_unauthorized(rt[k]) for k in ("deny-2", "deny-4")),
        {k: [t.get("memory_capture_state"), t.get("extraction_requests"), t.get("memory_capture_bundle_result")]
         for k, t in rt.items()})

# item 4
cons = (by.get("consolidation (excluded)") or {}).get("run") or {}
diag = (by.get("diagnostic (disabled, in-process)") or {}).get("diagnostic") or {}
c.check("item 4: a capture-excluded turn (R: the scheduled memory consolidation turn) makes no extraction request "
        "and commits no capture result",
        cons.get("state") == "finished" and cons.get("extraction_requests_in_run") == 0
        and [i.get("results") for i in cons.get("scheduled_turn_intents") or []] in ([0], [])
        and [f.get("capture_excluded_for_turn") for f in cons.get("scheduled_turn_flags") or []] == [True],
        {k: cons.get(k) for k in ("state", "error", "extraction_requests_in_run", "provider_requests_by_kind",
                                  "scheduled_turn_intents", "scheduled_turn_flags", "scheduled_turn_decisions")})
xa, xd = X.get("agent_check") or {}, X.get("doctor") or {}
c.check("item 4: capture-disabled diagnostic turns make no extraction request: the installed `openminion "
        "agent-check --json` (exit 0, ok, healthy) and `openminion doctor --check-turn --json` (agent.turn_smoke ok) "
        "each ran a real turn against the scripted provider with 0 extraction requests; the in-process diagnostic "
        "turn on R's runner made 0 too",
        xa.get("exit_code") == 0 and g(xa, "json", "ok") is True and xa.get("extraction_requests") == 0
        and len(xa.get("provider_requests") or []) > 0 and g(xd, "turn_smoke", "status") == "ok"
        and xd.get("extraction_requests") == 0 and len(xd.get("provider_requests") or []) > 0
        and diag.get("extraction_requests") == 0 and len(diag.get("provider_requests") or []) > 0,
        {"agent_check": {k: xa.get(k) for k in ("exit_code", "json", "provider_requests", "response_metadata")},
         "doctor": {k: xd.get(k) for k in ("exit_code", "turn_smoke", "doctor_status", "provider_requests")},
         "in_process": {k: diag.get(k) for k in ("provider_requests", "memory_capture_decisions", "turn_flags")}})
c.check("item 4: the diagnostic turns wrote nothing for a capture: the X home has no capture candidate, receipt or "
        "memory.capture.result afterwards",
        g(X, "store", "capture_candidates_total") == 0 and g(X, "store", "receipts_total") == 0
        and X.get("capture_results_total") == 0,
        {"store": X.get("store"), "intents": X.get("intents"), "server_side": X.get("server_side")})
n1t, n2t = N1.get("turn") or {}, N2.get("turn") or {}
c.check("item 4: no identity profile (N1): the turn captures as before (one extraction, succeeded, one candidate, "
        "receipt and result)",
        captured(n1t), turn_view(n1t))
c.check("item 4: no identity profile (N1): the turn's memory.write question is answered allow",
        decision_rows(n1t.get("memory_capture_decisions"))
        == [("console", "memory-capture", "capture", g(n1t, "intent", "root_turn_id"), "allow", "allowed")],
        n1t.get("memory_capture_decisions"))
c.check("item 4: no security policy (N2, OPENMINION_DISABLE_SECURITY_POLICY=true, even with `NEVER write memory "
        "records` in the profile): the service has no policy, and the turn captures as before",
        N2.get("service_security_policy_is_none") is True and captured(n2t)
        and n2t.get("memory_capture_decisions") == [],
        {"policy_is_none": N2.get("service_security_policy_is_none"), "turn": turn_view(n2t)})
pred = N1.get("predicates") or {}
def predicate_rule(name: str) -> bool:
    return (g(pred, name, "no policy") == {"returned": True, "policy_evaluations": 0}
            and g(pred, name, "policy, no tools") == {"returned": False, "policy_evaluations": 0}
            and g(pred, name, "policy and tools") == {"returned": True, "policy_evaluations": 1})


c.check("item 4: recovery's installed predicate keeps its standalone rule: no policy -> True; a policy without a "
        "tool registry -> False with no policy evaluation; policy and tools -> the policy's answer (allow)",
        predicate_rule("memory_capture_recovery_allowed"), pred.get("memory_capture_recovery_allowed"))
c.check("item 4: the turn's installed predicate (memory_capture_allowed) has the same standalone rule, and a runner "
        "before its first turn has _capture_authorized_for_turn True (a BrainRunner used without the service "
        "keeps capturing)",
        predicate_rule("memory_capture_allowed") and N1.get("runner_flag_before_first_turn") is True
        and R.get("runner_flag_before_first_turn") is True,
        {"memory_capture_allowed": pred.get("memory_capture_allowed"),
         "N1 runner flag": N1.get("runner_flag_before_first_turn"),
         "R runner flag": R.get("runner_flag_before_first_turn")})
mat, mdt = MA.get("turn") or {}, MD.get("turn") or {}
c.check("item 4: ordinary tool approval is unchanged (Ma, approved): the model's memory.write call asks the approval "
        "callback once with the tool's arguments, then the tool policy's identity restriction (require_approval), "
        "and the tool's record is written; the turn completes",
        mat.get("approval_callback_calls") == [["memory.write", TOOL_ARGS]]
        and [d[4:] for d in decision_rows(mat.get("tool_runtime_decisions"))] == [RESTRICTED]
        and g(mat, "store", "tool_records") == 1 and mat.get("run_state") == "completed"
        and mat.get("provider_requests", []).count("tool_call") == 1,
        {k: mat.get(k) for k in ("approval_callback_calls", "tool_runtime_decisions", "store", "run_state", "text",
                                 "provider_requests")})
c.check("item 4: ordinary tool approval is unchanged (Md, refused): the callback is asked once, the tool does not "
        "run (no record, no tool-runtime decision), and the turn completes with the refusal reply",
        mdt.get("approval_callback_calls") == [["memory.write", TOOL_ARGS]] and mdt.get("tool_runtime_decisions") == []
        and g(mdt, "store", "tool_records") == 0 and mdt.get("run_state") == "completed",
        {k: mdt.get(k) for k in ("approval_callback_calls", "tool_runtime_decisions", "store", "run_state", "text",
                                 "provider_requests")})

# item 5
u1t = U1.get("turn") or {}
u1r = U1.get("runs") or []
c.check("item 5: #468 control, allowed turn on the bundled reference-sqlite backend (U1, memctl answers "
        "supports_capture_bundles False): the turn extracts once; the store cannot take the bundle, so the intent "
        "stays pending with CAPTURE_RECOVERY_UNSUPPORTED (as in 0.1.15)",
        g(U1, "backend", "memctl_supports_capture_bundles") is False
        and "SophiaGraphSqliteStore" in str(g(U1, "backend", "memory_backend"))
        and u1t.get("extraction_requests") == 1 and u1t.get("memory_capture_state") == "pending"
        and g(u1t, "memory_capture_bundle_result", "error_code") == UNSUPPORTED,
        {"backend": U1.get("backend"), "turn": turn_view(u1t)})
c.check("item 5: U1: the allowed turn asked memory.write first (allow), so authorization comes before the store's "
        "capability answer",
        [r[4] for r in decision_rows(u1t.get("memory_capture_decisions"))] == ["allow"],
        u1t.get("memory_capture_decisions"))
c.check("item 5: #468 control (U1): two consolidation runs each give (1, 0, 1) with the user intent's steps "
        "[identity, authorize=True, capability=False]: no source read, extraction request, bundle write or result "
        "write for the unsupported store",
        len(u1r) == 2 and all(r.get("recovery") == [[1, 0, 1]]
                              and g(r, "steps_by_intent", "user") == ["identity", "authorize=True", "capability=False"]
                              and r.get("recovery_extraction_requests") == 0 for r in u1r)
        and U1.get("intent_after") == [{"capture_state": "pending", "results": 0}],
        [{k: r.get(k) for k in ("state", "recovery", "steps_by_intent", "recovery_extraction_requests")} for r in u1r])
u2t = U2.get("turn") or {}
u2a, u2b = U2.get("run1_denied") or {}, U2.get("run2_allowed") or {}
c.check("item 5: #468 control with a denial (U2, reference-sqlite, `NEVER write memory records`): the turn makes no "
        "extraction request and no bundle write, and stays pending with CAPTURE_RECOVERY_UNAUTHORIZED (the denial "
        "comes before the store's capability)",
        nothing_captured({**u2t, "store": {"capture_candidates_for_capture": 0, "receipts_for_capture": 0}})
        and pending_unauthorized(u2t),
        turn_view(u2t))
c.check("item 5: #468 control with a denial (U2): the denied run stops at [identity, authorize=False] before the "
        "capability query; reopened with the write allowed, the run asks the store and stops at [identity, "
        "authorize=True, capability=False]; no extraction in recovery, the intent stays pending",
        u2a.get("recovery") == [[1, 0, 1]] and g(u2a, "steps_by_intent", "user") == ["identity", "authorize=False"]
        and u2b.get("recovery") == [[1, 0, 1]]
        and g(u2b, "steps_by_intent", "user") == ["identity", "authorize=True", "capability=False"]
        and u2a.get("recovery_extraction_requests") == 0 and u2b.get("recovery_extraction_requests") == 0
        and U2.get("intent_after") == [{"capture_state": "pending", "results": 0}],
        {"run1": {k: u2a.get(k) for k in ("recovery", "steps_by_intent", "recovery_extraction_requests")},
         "run2": {k: u2b.get(k) for k in ("recovery", "steps_by_intent", "recovery_extraction_requests")},
         "intent_after": U2.get("intent_after")})
if BACKEND == "postgres":
    ss = g(T, "server_side", "event_counts") or {}
    c.check("item 5: PostgreSQL really ran: the runtime's session events are in the case database (server-side "
            "counts of turn.outcome and memory.capture.result in T) and the memory store's rows are read server-side "
            "from its own schema",
            sum(v for k, v in ss.items() if k.endswith(":memory.capture.result") and isinstance(v, int)) == 1
            and sum(v for k, v in ss.items() if k.endswith(":turn.outcome") and isinstance(v, int)) >= 1
            and g(T, "store_after_replay", "where") == "postgres memory schema (server-side)",
            {"T": T.get("server_side"), "store_where": g(T, "store_after_replay", "where")})
    xs = X.get("server_side_after_cli") or {}
    c.check("item 5: PostgreSQL really ran for the diagnostic CLI turns too: right after `agent-check` and `doctor "
            "--check-turn` (before the probe opens the home), the new X case database holds the runtime tables the "
            "CLI created, with rows (server-side), and no memory.capture.result",
            any(k.startswith("public.") and isinstance(v, int) and v > 0
                for k, v in (xs.get("nonzero_tables") or {}).items())
            and sum(v for k, v in (xs.get("event_counts") or {}).items()
                    if k.endswith(":memory.capture.result") and isinstance(v, int)) == 0,
            xs)
errors = {k: v.get("case_error") for k, v in observed.items() if v.get("case_error")}
c.check("setup: every case ran to the end (no case error)", not errors, errors or None)
c.check("setup: offline provider: every case's openai base URL is the scripted server on 127.0.0.1",
        sorted(HOSTS) == ["127.0.0.1"], sorted(HOSTS))

summary = c.summary()
write_result(OUT, {"info": info, "backend": BACKEND, "summary": summary, "all_pass": c.all_pass, "checks": c.items,
                   "observed": sanitize(json.loads(json.dumps(observed, default=str)))})
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
