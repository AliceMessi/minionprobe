"""#524 release verification: no `tool_outcome` candidate for a `policy_denied` outcome.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. Harness: `probe_420_lib.py` and
`probe_496_lib.py` (the #448/#496 probes' helpers, byte copies of the 0.1.16
round's files) and `probe_524_lib.py`. Each runtime case is a real `APIRuntime`
for one agent, `openminion`, whose `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1 (a local stub, not a live provider). The
scripted model makes one tool call per armed turn:
  memory.write  {"record_type": "fact", "key": "fact:editor_font", "title": "Editor font",
                 "content": "The user writes code in Iosevka 14."}
  weather       {"location": "Probe524ville"}  (its handler is replaced by a stub: no network)
The memory store is the built-in store, on SQLite (`--backend sqlite`) or in
its own PostgreSQL schema per case (`--backend postgres`; runtime, policy and
brain session storage stay SQLite).

Runtime cases (each a fresh runtime home; N = identity profile `NEVER reveal
secrets`, X = no identity profile, W = `NEVER write memory records`):
  <P>-denied      memory.write; the approval callback records the call and denies it
  <P>-pending     memory.write; no callback, the call waits for the user's confirmation
  <P>-blocked     memory.write; an exact-tool `action_policy` rule (mode block) denies it
  N-blocked-weather  weather; the same rule on weather
  N-scope-weather    weather; its `min_scope` set to POWER_USER (observation only, see notes)
  N-takeover      weather; the stub returns {"ok": true, "requires_user_takeover": true, ...}
  N-weather-ok    weather; the stub returns {"ok": true, ...} (allowlisted success)
  N-weather-fail  weather; the stub raises
  N-approve-ok / N-approve-fail   memory.write; the callback approves; the handler runs / raises
  N-later-ok / N-later-fail       memory.write waits (no callback); "yes" is then sent twice on the
                  first turn's thread with `APIRuntime.run_turn` (inbound_metadata.thread_id)
  W-denied, W-pending, W-approve-ok, W-approve-fail   as above under W
Standalone cases (SQLite only): a `BrainRunner` built from the installed package's
local adapters, no agent service; `runner.step(user_input='tool <name> {"location": "Probe524ville"}')`
with an allowing policy (tools weather, echo, t_failed, t_timeout, t_needs_user,
t_blocked; the t_* tools return that status), a REQUIRE_CONFIRMATION policy and
a DENY policy (weather).

Usage: python -I -X utf8 probe_524.py <work_dir> <out.json> [--backend sqlite|postgres]
           [--baseline <control.json>] [--expect-version 0.1.17]
The 0.1.16 control runs without --baseline; the 0.1.17 run compares the policy
and approval history of the calls that did not run with it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from importlib import metadata
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("work")
ap.add_argument("out")
ap.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
ap.add_argument("--baseline")
ap.add_argument("--expect-version", default="0.1.17")
ARGS = ap.parse_args()
WORK = Path(ARGS.work).resolve()
OUT = Path(ARGS.out).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()


def _dist(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


info = banner("probe_524", backend=ARGS.backend,
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1 (local stub)",
              dist_versions={n: _dist(n) for n in ("openminion", "sophiagraph", "sqlalchemy", "psycopg")},
              baseline=Path(ARGS.baseline).name if ARGS.baseline else None)

from openminion import Agent  # noqa: E402
from openminion.modules.brain.loop.tools.contracts import canonical_tool_arguments  # noqa: E402
from openminion.modules.controlplane.constants import CALLER_HANDLES_DELIVERY_METADATA_KEY  # noqa: E402

import probe_420_lib as lib  # noqa: E402
import probe_496_lib as L  # noqa: E402
import probe_524_lib as X  # noqa: E402

c = Checks()
N_C, W_C = L.NEUTRAL_CONSTRAINT, L.WRITE_CONSTRAINT
CANON_MEMORY = canonical_tool_arguments(X.MEMORY_ARGS)
TOOLS = {"memory.write": ("memory_write", X.MEMORY_ARGS, X.MESSAGE, X.MEMORY_NEEDLES),
         "weather": ("weather", X.WEATHER_ARGS, X.WEATHER_MESSAGE, X.WEATHER_NEEDLES)}


# ---------------------------------------------------------------- runtime cases
def turn_view(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        md = dict(result.get("metadata") or {})
        view = {"via": "APIRuntime.run_turn", "run_state": result.get("run_state"),
                "text": str(result.get("body") or "")[:240], "session_id": result.get("session_id")}
    else:
        md = dict(result.metadata or {})
        view = {"via": "Agent.run", "run_state": result.run_state, "text": str(result.text or "")[:240],
                "session_id": result.session_id}
    for k in ("respond_kind", "pending_confirmation_command_id", "thread_id", "thread_decision_action",
              "thread_decision_reason"):
        view[k] = md.get(k)
    return view


def store_view(db: L.MemDB, needles: tuple[str, ...]) -> dict[str, Any]:
    cands = db.candidates()
    return {
        "candidates": [{k: r.get(k) for k in ("type", "title", "proposed_scope", "status", "content_json",
                                              "meta_json")} for r in cands],
        "tool_outcome_titles": [r.get("title") for r in cands if r.get("type") == "tool_outcome"],
        "candidates_with_argument_text": [r.get("title") for r in cands
                                          if any(n in X.candidate_texts(r) for n in needles)],
        "records": [{k: r.get(k) for k in ("type", "key", "title")} for r in db.records()],
        "argument_text_in_store": db.search(needles),
    }


def run_case(label: str, *, constraint: str | None, tool: str, callback: bool | None = None, mode: str = "pass",
             stub: Any = None, block: tuple[str, ...] = (), scope: str | None = None,
             replies: tuple[str, ...] = (), reply_forced: bool = True) -> dict[str, Any]:
    model_tool, args, message, needles = TOOLS[tool]
    root = WORK / label
    provider = X.ArmedProvider()
    old_env = lib.chdir_env(root)
    row: dict[str, Any] = {"case": label, "constraint": constraint, "tool": tool, "callback": callback,
                           "handler_mode": mode, "block_rule": list(block), "min_scope": scope,
                           "replies": list(replies), "reply_forced_tools": reply_forced, "backend": ARGS.backend}
    runtime = obs = rec = handler = pg = None
    try:
        if ARGS.backend == "postgres":
            pg = X.PgMemorySchema(label.replace("-", "_"))
        runtime = X.build_runtime_ext(root, provider, identity_constraint=constraint, block_tools=block,
                                      memory_config=pg.memory_config if pg is not None else None)
        obs = lib.Observers(runtime).install()
        runner = obs.service._get_runner()
        rec = L.Recorder(runner.memory_api, obs.security_policy)
        handler = X.HandlerRecorder(runtime, tool, mode, stub)
        if scope is not None:
            runtime.tools.get(tool).min_scope = scope
        db = L.MemDB(L.memory_db_path(obs), pg)
        row["store_chain"] = L.store_chain(obs)
        row["memory_db"] = db.sqlite_path.name if db.sqlite_path is not None else "postgres: a new schema per case"
        approvals: list[dict[str, Any]] = []

        def approval(*a: Any) -> bool:
            approvals.append({"tool": str(a[0]) if a else None, "args": dict(a[1]) if len(a) > 1 else None})
            return bool(callback)

        turns: list[dict[str, Any]] = []
        provider.arm(model_tool, args)
        kwargs = {"approval_callback": approval} if callback is not None else {}
        mark = len(provider.requests)
        with Agent(runtime=runtime, forced_tools=[tool]) as agent:
            first = agent.run(message, timeout_seconds=120, **kwargs)
        view = turn_view(first)
        view.update(model_requests=provider.kinds(mark), handler_calls_after=len(handler.calls),
                    store_after=store_view(db, needles))
        turns.append(view)
        for n, text in enumerate(replies):
            mark = len(provider.requests)
            payload = {"message": text, "session_id": first.session_id, "deliver": False, "timeout_seconds": 120,
                       "inbound_metadata": {CALLER_HANDLES_DELIVERY_METADATA_KEY: "true",
                                            "thread_id": first.metadata["thread_id"]}}
            if reply_forced:
                payload["forced_tools"] = [tool]
            raw = runtime.run_turn(payload=payload)
            view = turn_view(raw)
            view.update(model_requests=provider.kinds(mark), handler_calls_after=len(handler.calls),
                        store_after=store_view(db, needles), reply_number=n + 1, reply=text)
            turns.append(view)
        row["turns"] = turns
        row["approval_callback_calls"] = approvals
        row["handler_calls"] = X.model_args(handler.calls, args)
        row["memory_write_decisions"] = [[d.get("target"), d.get("decision"), d.get("reason_code")]
                                         for d in rec.decisions]
        row["stage_candidate_calls"] = [" <- ".join(w.get("callers", [])[:2]) for w in rec.writes
                                        if w.get("method") == "stage_candidate"]
        row["store"] = store_view(db, needles)
        row["policy_history"] = X.policy_history(root)
        row["runtime_event_counts"] = X.runtime_event_counts(runtime)
        row["brain_event_counts"] = X.brain_event_counts(root)
        row["brain_tool_events"] = X.brain_tool_events(root)
        row["memory_audit"] = X.memory_audit(root)
        row["provider_hosts"] = sorted({provider.base_url.split("/")[2].split(":")[0]})
        if pg is not None:
            row["postgres_server_side"] = {
                "tool_outcome_candidates": db.rows("SELECT COUNT(*) FROM memory_candidates WHERE type = 'tool_outcome'"),
                "all_candidates": db.rows("SELECT COUNT(*) FROM memory_candidates"),
                "all_records": db.rows("SELECT COUNT(*) FROM memory_records"),
                "server_version": db.rows("SHOW server_version"),
            }
    except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
        row["case_error"] = L.short_error(exc)
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-10:]
    finally:
        for r in (handler,):
            if r is not None:
                r.restore()
        for r in (rec, obs):
            if r is not None:
                try:
                    r.uninstall()
                except Exception as exc:  # noqa: BLE001
                    row.setdefault("uninstall_errors", []).append(L.short_error(exc))
        if runtime is not None:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                row["runtime_close"] = L.short_error(exc)
        if pg is not None:
            try:
                row["postgres_layout"] = pg.layout()
            except Exception as exc:  # noqa: BLE001
                row["postgres_layout"] = L.short_error(exc)
            try:
                pg.drop()
            except Exception as exc:  # noqa: BLE001
                row["pg_drop"] = L.short_error(exc)
        lib.restore_env(old_env)
        provider.close()
    return row


# ---------------------------------------------------------------- standalone cases
def run_standalone(label: str, policy: str, tools: tuple[str, ...]) -> dict[str, Any]:
    root = WORK / f"standalone-{label}"
    row: dict[str, Any] = {"case": f"standalone-{label}", "policy": policy, "steps": {}}
    try:
        runner, session, memory, adapter = X.standalone_runner(root, policy)
        row["memory_write_allowed_for_turn"] = getattr(runner, "_memory_write_allowed_for_turn", "<absent>")
        for tool in tools:
            before = len(X.standalone_entries(memory))
            mark = len(adapter.calls)
            sid = f"s-{label}-{tool}"
            out = runner.step(session_id=sid, user_input=f"tool {tool} {json.dumps(X.WEATHER_ARGS)}",
                              trace_id=f"t-{label}-{tool}")
            new = X.standalone_entries(memory)[before:]
            types = [e.get("type") for e in session.list_events(sid)]
            row["steps"][tool] = {
                "status": out.status, "message": str(out.message or "")[:120],
                "tool_adapter_calls": adapter.calls[mark:],
                "new_entries": [{"kind": e.get("kind"), "type": e.get("type") or e.get("record_type"),
                                 "title": e.get("title"),
                                 "has_argument_text": X.WEATHER_NEEDLES[0] in json.dumps(e, default=str)}
                                for e in new],
                "policy_events": {k: types.count(k) for k in ("policy.applied", "tool.call.blocked", "tool.request",
                                                              "tool.completed")},
            }
    except Exception as exc:  # noqa: BLE001
        row["case_error"] = L.short_error(exc)
        row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                    for f in traceback.extract_tb(exc.__traceback__)][-10:]
    return row


TAKEOVER = {"ok": True, "requires_user_takeover": True, "content": "Finish this step yourself in the browser."}
SUNNY = {"ok": True, "content": "Sunny in Probe524ville."}
PLAN: dict[str, dict[str, Any]] = {}
for p, constraint in (("N", N_C), ("X", None)):
    PLAN[f"{p}-denied"] = dict(constraint=constraint, tool="memory.write", callback=False)
    PLAN[f"{p}-pending"] = dict(constraint=constraint, tool="memory.write")
    PLAN[f"{p}-blocked"] = dict(constraint=constraint, tool="memory.write", callback=False, block=("memory.write",))
PLAN.update({
    "N-blocked-weather": dict(constraint=N_C, tool="weather", callback=False, mode="stub", stub=SUNNY,
                              block=("weather",)),
    "N-scope-weather": dict(constraint=N_C, tool="weather", callback=False, mode="stub", stub=SUNNY,
                            scope="POWER_USER"),
    "N-takeover": dict(constraint=N_C, tool="weather", callback=True, mode="stub", stub=TAKEOVER),
    "N-weather-ok": dict(constraint=N_C, tool="weather", callback=True, mode="stub", stub=SUNNY),
    "N-weather-fail": dict(constraint=N_C, tool="weather", callback=True, mode="raise"),
    "N-approve-ok": dict(constraint=N_C, tool="memory.write", callback=True),
    "N-approve-fail": dict(constraint=N_C, tool="memory.write", callback=True, mode="raise"),
    "N-later-ok": dict(constraint=N_C, tool="memory.write", replies=("yes", "yes")),
    "N-later-fail": dict(constraint=N_C, tool="memory.write", mode="raise", replies=("yes", "no", "yes")),
    "N-later-fail-unforced": dict(constraint=N_C, tool="memory.write", mode="raise", replies=("yes", "yes"),
                                  reply_forced=False),
    "W-denied": dict(constraint=W_C, tool="memory.write", callback=False),
    "W-pending": dict(constraint=W_C, tool="memory.write"),
    "W-approve-ok": dict(constraint=W_C, tool="memory.write", callback=True),
    "W-approve-fail": dict(constraint=W_C, tool="memory.write", callback=True, mode="raise"),
})
cases: dict[str, Any] = {}
for name, kwargs in PLAN.items():
    t0 = time.monotonic()
    cases[name] = run_case(name, **kwargs)
    cases[name]["seconds"] = round(time.monotonic() - t0, 1)
    print(f"case {name}: {cases[name]['seconds']} s, error={cases[name].get('case_error')}", flush=True)
standalone: dict[str, Any] = {}
if ARGS.backend == "sqlite":
    standalone = {
        "allow": run_standalone("allow", "allow", ("weather", "echo", "t_failed", "t_timeout", "t_needs_user",
                                                   "t_blocked")),
        "confirm": run_standalone("confirm", "confirm", ("weather",)),
        "deny": run_standalone("deny", "deny", ("weather",)),
    }
    for k, v in standalone.items():
        print(f"standalone {k}: error={v.get('case_error')}", flush=True)
baseline = json.loads(Path(ARGS.baseline).read_text(encoding="utf-8")) if ARGS.baseline else None
bcases = ((baseline or {}).get("observed") or {}).get("cases") or {}
observed: dict[str, Any] = {"cases": cases, "standalone": standalone}


# ---------------------------------------------------------------- views
def case(name: str) -> dict[str, Any]:
    return cases.get(name) or {}


def first(name: str) -> dict[str, Any]:
    return (case(name).get("turns") or [{}])[0]


def tool_outcomes(name: str) -> list[str]:
    return list((case(name).get("store") or {}).get("tool_outcome_titles") or [])


def arg_candidates(name: str) -> list[str]:
    return list((case(name).get("store") or {}).get("candidates_with_argument_text") or [])


def store_clean(name: str) -> bool:
    hits = (case(name).get("store") or {}).get("argument_text_in_store") or {}
    return bool(hits) and all(v == {} for v in hits.values())


def ev(name: str, key: str) -> int:
    return int((case(name).get("runtime_event_counts") or {}).get(key, 0))


def bev(name: str, key: str) -> int:
    counts = case(name).get("brain_event_counts")
    return int(counts.get(key, 0)) if isinstance(counts, dict) else -1


def decisions(name: str) -> list[list[Any]]:
    return [list(d[:3]) for d in (case(name).get("policy_history") or {}).get("policy_decisions") or []
            if isinstance(d, (list, tuple))]


def capture(name: str) -> list[list[Any]]:
    return [d for d in case(name).get("memory_write_decisions") or [] if d[0] == "memory-capture"]


def facts(name: str) -> dict[str, Any]:
    cs = case(name)
    return {"case_error": cs.get("case_error"), "turns": [{k: t.get(k) for k in (
                "run_state", "text", "respond_kind", "pending_confirmation_command_id", "thread_decision_action",
                "thread_decision_reason", "handler_calls_after", "model_requests")} | {
                "tool_outcome_titles_after": (t.get("store_after") or {}).get("tool_outcome_titles")}
                for t in cs.get("turns") or []],
            "approval_callback_calls": cs.get("approval_callback_calls"), "handler_calls": cs.get("handler_calls"),
            "turn_memory_write_answer": capture(name), "stage_candidate_calls": cs.get("stage_candidate_calls"),
            "tool_outcome_candidates": tool_outcomes(name), "candidates_with_argument_text": arg_candidates(name),
            "argument_text_in_store": (cs.get("store") or {}).get("argument_text_in_store"),
            "records": (cs.get("store") or {}).get("records"),
            "policy_decisions": decisions(name),
            "policy_pending_confirmations": (cs.get("policy_history") or {}).get("policy_pending_confirmations"),
            "history_events": {"runtime policy_confirmation_prompt": ev(name, "policy_confirmation_prompt"),
                               "runtime policy_denied": ev(name, "policy_denied"),
                               "runtime run.waiting_tool": ev(name, "run.waiting_tool"),
                               "brain policy.applied": bev(name, "policy.applied"),
                               "brain tool.call.blocked": bev(name, "tool.call.blocked"),
                               "brain tool.completed": bev(name, "tool.completed")},
            "brain_tool_events": cs.get("brain_tool_events"), "memory_audit": cs.get("memory_audit"),
            "postgres_server_side": cs.get("postgres_server_side")}


ALLOW = [["memory-capture", "allow", "allowed"]]
RA = [["memory-capture", "require_approval", "identity_hard_constraint_restricted"]]
DENIED_TEXT = "openminion: Confirmation denied. The action was not run."
PENDING_PREFIX = "openminion: Policy confirmation required."
BLOCK_TEXT = "openminion: Denied by configured exact-tool rule"


def not_run_ok(name: str, *, kind: str, tool: str = "memory.write") -> bool:
    cs, t = case(name), first(name)
    if cs.get("case_error") or cs.get("handler_calls") != []:
        return False
    if capture(name) != ALLOW or tool_outcomes(name) or arg_candidates(name) or not store_clean(name):
        return False
    if cs.get("stage_candidate_calls"):
        return False
    if t.get("run_state") != "completed" or ev(name, "policy_confirmation_prompt") != 1:
        return False
    if bev(name, "policy.applied") < 1 or bev(name, "tool.call.blocked") != 1 or bev(name, "tool.completed") != 0:
        return False
    method = "write" if tool == "memory.write" else "default"
    tname = "memory" if tool == "memory.write" else "weather"
    if kind == "denied":
        return (t.get("text") == DENIED_TEXT and cs.get("approval_callback_calls")
                == [{"tool": "memory.write", "args": X.MEMORY_ARGS}]
                and decisions(name) == [[tname, method, "require_confirm"]])
    if kind == "pending":
        return (str(t.get("text") or "").startswith(PENDING_PREFIX) and bool(t.get("pending_confirmation_command_id"))
                and t.get("respond_kind") == "policy_confirmation_prompt"
                and cs.get("approval_callback_calls") == [] and decisions(name) == [[tname, method, "require_confirm"]])
    if kind == "blocked":
        return (t.get("text") == BLOCK_TEXT and cs.get("approval_callback_calls") == []
                and decisions(name) == [[tname, method, "deny"]]
                and [d[3] for d in (cs.get("policy_history") or {}).get("policy_decisions") or []]
                == ["CONFIG_RULE_BLOCK"])
    return False


# ---------------------------------------------------------------- environment
hosts = sorted({h for cs in cases.values() for h in cs.get("provider_hosts", [])})
errors = {k: v.get("case_error") for k, v in {**cases, **standalone}.items() if v.get("case_error")}
chains = {k: v.get("store_chain") for k, v in cases.items()}
expected_store = "PostgresMemoryStore" if ARGS.backend == "postgres" else "SQLiteMemoryStore"
c.check(f"environment: openminion {ARGS.expect_version} imported from site-packages in isolated mode; every runtime "
        f"case's memory store is the built-in {expected_store} ({ARGS.backend}); the provider is the scripted local "
        "stub on 127.0.0.1; no case error",
        info.get("openminion.__version__") == ARGS.expect_version and info.get("imported_from_site_packages") is True
        and info.get("isolated_mode") is True and hosts == ["127.0.0.1"] and not errors
        and all(expected_store in (ch or []) for ch in chains.values()),
        {"version": info.get("openminion.__version__"), "file": info.get("openminion.__file__"),
         "python": info.get("python"), "platform": info.get("platform"), "sqlite": info.get("sqlite3.sqlite_version"),
         "dist_versions": info.get("dist_versions"), "store_chains": sorted({" > ".join(v or []) for v in chains.values()}),
         "postgres_server_version": sorted({str((v.get("postgres_server_side") or {}).get("server_version"))
                                           for v in cases.values() if v.get("postgres_server_side")}),
         "hosts": hosts, "errors": errors})

# ---------------------------------------------------------------- item 1
for p, desc in (("N", "identity profile `NEVER reveal secrets`"), ("X", "no identity profile")):
    for kind, what in (("denied", "the approval callback denies the model's memory.write call (asked once, with its "
                                  "arguments); reply 'Confirmation denied. The action was not run.'"),
                       ("pending", "no callback: the call waits (respond_kind policy_confirmation_prompt, a "
                                   "pending_confirmation_command_id, reply 'Policy confirmation required. ...')"),
                       ("blocked", "an exact-tool action_policy rule (block) denies memory.write before any "
                                   "confirmation (reply 'Denied by configured exact-tool rule'; the callback is not "
                                   "asked)")):
        name = f"{p}-{kind}"
        c.check(f"item 1: {name} ({desc}; the turn's memory.write answer is allow / allowed): {what}; the handler does "
                "not run; no stage_candidate call, no tool_outcome candidate, no candidate of any type and no row of "
                "any memory-store table holding the call's argument text ('fact:editor_font', 'Iosevka 14'); the "
                "policy/approval history remains: one policy_confirmation_prompt session event, brain policy.applied "
                "and one tool.call.blocked event, and the policy store's decision row for memory.write",
                not_run_ok(name, kind=kind), facts(name))
c.check("item 1: N-blocked-weather (another tool, weather, denied by the same exact-tool rule, under the allowing "
        "memory.write answer): the stub does not run; no tool_outcome candidate and no candidate or store row holding "
        "'Probe524ville'; the history keeps policy_confirmation_prompt, policy.applied, tool.call.blocked and the "
        "policy store's weather deny row (CONFIG_RULE_BLOCK)",
        not_run_ok("N-blocked-weather", kind="blocked", tool="weather"), facts("N-blocked-weather"))
if case("N-denied") and case("N-pending") and ARGS.backend == "postgres":
    def srv(name: str) -> Any:
        side = case(name).get("postgres_server_side") or {}
        v = side.get("tool_outcome_candidates")
        return v[0][0] if isinstance(v, list) and v else v
    c.check("item 1: PostgreSQL, counted server-side in each case's own schema: 0 tool_outcome candidates in N-denied, "
            "N-pending, N-blocked, X-denied, X-pending and X-blocked, while N-approve-fail on the same server has 1",
            all(srv(n) == 0 for n in ("N-denied", "N-pending", "N-blocked", "X-denied", "X-pending", "X-blocked"))
            and srv("N-approve-fail") == 1,
            {n: case(n).get("postgres_server_side") for n in ("N-denied", "N-pending", "N-blocked", "X-denied",
                                                               "X-pending", "X-blocked", "N-approve-fail")})


def history_view(cs: dict[str, Any]) -> dict[str, Any]:
    t = (cs.get("turns") or [{}])[0]
    audit = [a for a in cs.get("memory_audit") or [] if a != ["memory.candidate.put", "candidate", "tool_outcome"]]
    return json.loads(json.dumps({"reply": [t.get("run_state"), t.get("text"), t.get("respond_kind"),
                      bool(t.get("pending_confirmation_command_id"))],
            "approval_callback_calls": cs.get("approval_callback_calls"), "handler_calls": cs.get("handler_calls"),
            "turn_memory_write_answer": [d for d in cs.get("memory_write_decisions") or [] if d[0] == "memory-capture"],
            "policy_history": cs.get("policy_history"), "runtime_event_counts": cs.get("runtime_event_counts"),
            "brain_event_counts": cs.get("brain_event_counts"),
            "memory_audit_without_tool_outcome_candidate_puts": audit}, default=str))


NOT_RUN = ("N-denied", "N-pending", "N-blocked", "X-denied", "X-pending", "X-blocked", "N-blocked-weather")
if baseline is not None:
    same, diff, removed = [], {}, {}
    for name in NOT_RUN:
        if name not in bcases:
            diff[name] = "missing in the control"
            continue
        a, b = history_view(case(name)), history_view(bcases[name])
        if a == b:
            same.append(name)
        else:
            diff[name] = {k: {"this": a[k], "control": b[k]} for k in a if a[k] != b[k]}
        removed[name] = {"this": (case(name).get("store") or {}).get("tool_outcome_titles"),
                         "control": (bcases[name].get("store") or {}).get("tool_outcome_titles")}
    c.check("item 1: canonical policy/approval history unchanged from the control run (published "
            f"{(baseline.get('info') or {}).get('openminion.__version__')}): for every call that did not run, the reply, "
            "the approval callback calls, the handler calls, the turn's memory.write answer, the policy store "
            "(decisions, pending confirmations, grants), every runtime session event type count, every brain session "
            "event type count and the memory audit trail are equal; the only difference is the memory audit's "
            "`memory.candidate.put` of the control's tool_outcome candidate",
            len(same) == len(NOT_RUN) and not diff, {"equal": same, "different": diff,
                                                      "tool_outcome_candidates": removed})

# ---------------------------------------------------------------- item 2
tk = case("N-takeover")
tk_events = [e for e in tk.get("brain_tool_events") or [] if e.get("event_type") == "tool.completed"]
c.check("item 2: executed requires_user_takeover (N-takeover): weather's stub runs once with the model's arguments "
        "and returns requires_user_takeover; its tool.completed status is needs_user; no tool_outcome candidate (no "
        "policy_denied candidate) and no candidate holding 'Probe524ville'",
        not tk.get("case_error") and tk.get("handler_calls") == [X.WEATHER_ARGS]
        and [e.get("status") for e in tk_events] == ["needs_user"] and tool_outcomes("N-takeover") == []
        and arg_candidates("N-takeover") == [] and capture("N-takeover") == ALLOW,
        facts("N-takeover"))
wo = case("N-weather-ok")
c.check("item 2: the same call without takeover (N-weather-ok) stages its allowlisted success candidate, once: "
        "tool_outcome:weather:success (agent:openminion, proposed) from _stage_tool_outcome_candidate",
        not wo.get("case_error") and wo.get("handler_calls") == [X.WEATHER_ARGS]
        and tool_outcomes("N-weather-ok") == ["tool_outcome:weather:success"]
        and [(r.get("proposed_scope"), r.get("status")) for r in (wo.get("store") or {}).get("candidates") or []
             if r.get("type") == "tool_outcome"] == [("agent:openminion", "proposed")]
        and len(wo.get("stage_candidate_calls") or []) == 1
        and "_stage_tool_outcome_candidate" in (wo.get("stage_candidate_calls") or [""])[0],
        facts("N-weather-ok"))
sa = standalone.get("allow") or {}
sst = sa.get("steps") or {}


def sa_titles(st: dict[str, Any], kind: str = "tool_outcome") -> list[str]:
    return [e.get("title") for e in st.get("new_entries") or [] if e.get("type") == kind]


def sa_view(row: dict[str, Any]) -> dict[str, Any]:
    return {k: {"status": v.get("status"), "message": v.get("message"),
                "tool_adapter_calls": len(v.get("tool_adapter_calls") or []),
                "new_entries": v.get("new_entries"), "policy_events": v.get("policy_events")}
            for k, v in (row.get("steps") or {}).items()} | {"case_error": row.get("case_error"),
                                                              "memory_write_allowed_for_turn":
                                                                  row.get("memory_write_allowed_for_turn")}


if ARGS.backend == "sqlite":
    c.check("item 2: needs_user/blocked category on a standalone BrainRunner (no agent service): t_needs_user and "
            "t_blocked run once each and return that status; neither stages a tool_outcome candidate, and no new entry "
            "holds the arguments",
            not sa.get("case_error") and all(len(sst.get(t, {}).get("tool_adapter_calls") or []) == 1
                                             and sa_titles(sst.get(t, {})) == []
                                             and not any(e.get("has_argument_text")
                                                         for e in sst.get(t, {}).get("new_entries") or [])
                                             for t in ("t_needs_user", "t_blocked")),
            sa_view(sa))

# ---------------------------------------------------------------- item 3
ao, af = case("N-approve-ok"), case("N-approve-fail")
c.check("item 3: an approved call (N-approve-ok, the callback approves) runs once with the model's arguments and writes "
        "its record (fact:editor_font); memory.write is not in the success allowlist, so no tool_outcome candidate",
        not ao.get("case_error") and ao.get("approval_callback_calls") == [{"tool": "memory.write", "args": X.MEMORY_ARGS}]
        and ao.get("handler_calls") == [X.MEMORY_ARGS]
        and [r.get("key") for r in (ao.get("store") or {}).get("records") or []].count("fact:editor_font") == 1
        and tool_outcomes("N-approve-ok") == [],
        facts("N-approve-ok"))
af_c = [r for r in (af.get("store") or {}).get("candidates") or [] if r.get("type") == "tool_outcome"]
c.check("item 3: an approved call that fails (N-approve-fail, the handler raises) runs once and keeps its candidate: "
        "one tool_outcome:memory.write:failure:EXEC_ERROR, outcome failure, args_signature and source_args_signature "
        "= the call's canonical arguments",
        not af.get("case_error") and af.get("handler_calls") == [X.MEMORY_ARGS]
        and [r.get("title") for r in af_c] == ["tool_outcome:memory.write:failure:EXEC_ERROR"]
        and (af_c[0].get("content_json") or {}).get("outcome") == "failure"
        and (af_c[0].get("content_json") or {}).get("args_signature") == CANON_MEMORY
        and (af_c[0].get("meta_json") or {}).get("source_args_signature") == CANON_MEMORY,
        facts("N-approve-fail"))


FAILURE = "tool_outcome:memory.write:failure:EXEC_ERROR"


def later_ok(name: str, *, failed: bool) -> bool:
    cs = case(name)
    turns = cs.get("turns") or []
    if cs.get("case_error") or len(turns) != (4 if failed else 3):
        return False
    t0, t1 = turns[0], turns[1]
    keys = [r.get("key") for r in (cs.get("store") or {}).get("records") or []]
    same_thread = all(t.get("thread_id") == t0.get("thread_id") and t.get("thread_decision_action") == "resume_thread"
                      for t in turns[1:])
    ok = (str(t0.get("text") or "").startswith(PENDING_PREFIX) and t0.get("handler_calls_after") == 0
          and (t0.get("store_after") or {}).get("tool_outcome_titles") == []
          and (t0.get("store_after") or {}).get("candidates_with_argument_text") == []
          and same_thread and t1.get("handler_calls_after") == 1
          and X.model_args(cs.get("handler_calls") or [], X.MEMORY_ARGS) == [X.MEMORY_ARGS])
    if not failed:
        t2 = turns[2]
        return (ok and (t1.get("store_after") or {}).get("tool_outcome_titles") == []
                and t2.get("handler_calls_after") == 1 and (t2.get("store_after") or {}).get("tool_outcome_titles") == []
                and keys.count("fact:editor_font") == 1)
    t2, t3 = turns[2], turns[3]
    return (ok and (t1.get("store_after") or {}).get("tool_outcome_titles") == [FAILURE]
            and str(t1.get("text") or "").startswith(PENDING_PREFIX)
            and t2.get("reply") == "no" and t2.get("text") == DENIED_TEXT and t2.get("handler_calls_after") == 1
            and (t2.get("store_after") or {}).get("tool_outcome_titles") == [FAILURE]
            and t3.get("reply") == "yes" and t3.get("handler_calls_after") == 1
            and (t3.get("store_after") or {}).get("tool_outcome_titles") == [FAILURE]
            and not (t3.get("pending_confirmation_command_id")) and keys.count("fact:editor_font") == 0)


for name, failed in (("N-later-ok", False), ("N-later-fail", True)):
    lf = [r for r in (case(name).get("store") or {}).get("candidates") or [] if r.get("type") == "tool_outcome"]
    sig_ok = True
    if failed:
        sig = json.loads((lf[0].get("content_json") or {}).get("args_signature") or "{}") if lf else {}
        sig_ok = (len(lf) == 1 and {k: sig.get(k) for k in X.MEMORY_ARGS} == X.MEMORY_ARGS
                  and (lf[0].get("meta_json") or {}).get("source_args_signature")
                  == (lf[0].get("content_json") or {}).get("args_signature"))
    what = (" and it fails: exactly one tool_outcome:memory.write:failure:EXEC_ERROR, whose signature holds the "
            "model's arguments (the replayed call carries its validated arguments, defaults included); the reply asks "
            "to confirm the failed call again, 'no' denies that (reply 'Confirmation denied. The action was not run.'; "
            "no run, nothing staged), and a stale 'yes' after it runs nothing and stages nothing more"
            if failed else " and writes its record; no tool_outcome candidate; a second, stale 'yes' on the thread "
            "runs nothing and stages nothing")
    c.check(f"item 3: {name}: the waiting call stages nothing while it waits; 'yes' on its thread (resume_thread, "
            "APIRuntime.run_turn with inbound_metadata.thread_id, as the #534 test sends it) runs it once" + what,
            later_ok(name, failed=failed) and sig_ok, facts(name))
wf = case("N-weather-fail")
c.check("item 3: ordinary allowlisted calls keep their candidates: N-weather-ok has tool_outcome:weather:success "
        "(above); a failing weather call (N-weather-fail, the stub raises) stages tool_outcome:weather:failure:EXEC_ERROR",
        not wf.get("case_error") and wf.get("handler_calls") == [X.WEATHER_ARGS]
        and tool_outcomes("N-weather-fail") == ["tool_outcome:weather:failure:EXEC_ERROR"]
        and tool_outcomes("N-weather-ok") == ["tool_outcome:weather:success"],
        {"N-weather-fail": facts("N-weather-fail"), "N-weather-ok": tool_outcomes("N-weather-ok")})
if ARGS.backend == "sqlite":
    c.check("item 3: standalone BrainRunner (allowing policy): weather (allowlisted) stages tool_outcome:weather:success; "
            "echo (not allowlisted) stages none; t_failed stages tool_outcome:t_failed:failure:T_FAILED; t_timeout "
            "stages tool_outcome:t_timeout:timeout:T_TIMEOUT; each tool runs once and each step keeps its decision "
            "record",
            not sa.get("case_error")
            and sa_titles(sst.get("weather", {})) == ["tool_outcome:weather:success"]
            and sa_titles(sst.get("echo", {})) == []
            and sa_titles(sst.get("t_failed", {})) == ["tool_outcome:t_failed:failure:T_FAILED"]
            and sa_titles(sst.get("t_timeout", {})) == ["tool_outcome:t_timeout:timeout:T_TIMEOUT"]
            and all(len(v.get("tool_adapter_calls") or []) == 1 and sa_titles(v, "decision") for v in sst.values()),
            sa_view(sa))

# ---------------------------------------------------------------- item 4
for name, kind in (("W-denied", "denied"), ("W-pending", "pending"), ("W-approve-fail", "fail")):
    cs = case(name)
    ok = (not cs.get("case_error") and capture(name) == RA and tool_outcomes(name) == [] and arg_candidates(name) == []
          and not [r for r in (cs.get("store") or {}).get("records") or [] if r.get("type") == "decision"]
          and not cs.get("stage_candidate_calls"))
    if kind == "fail":
        ok = ok and cs.get("handler_calls") == [X.MEMORY_ARGS] and cs.get("approval_callback_calls") == [
            {"tool": "memory.write", "args": X.MEMORY_ARGS}]
    else:
        ok = ok and cs.get("handler_calls") == [] and store_clean(name)
    c.check(f"item 4: {name} (`NEVER write memory records`: the turn's memory.write answer is require_approval / "
            "identity_hard_constraint_restricted): the turn's optional writes stay suppressed: no decision record, no "
            "stage_candidate call, no tool_outcome candidate"
            + (" even for the approved call that ran and failed" if kind == "fail" else
               ", and no row of the memory store holds the argument text"),
            ok, facts(name))
wa = case("W-approve-ok")
c.check("item 4: ordinary authorization under W (W-approve-ok): the callback is asked once with the arguments and the "
        "approved record is written (fact:editor_font); no decision record and no tool_outcome candidate",
        not wa.get("case_error") and wa.get("approval_callback_calls") == [{"tool": "memory.write", "args": X.MEMORY_ARGS}]
        and wa.get("handler_calls") == [X.MEMORY_ARGS]
        and [r.get("key") for r in (wa.get("store") or {}).get("records") or []] == ["fact:editor_font"]
        and tool_outcomes("W-approve-ok") == [] and capture("W-approve-ok") == RA,
        facts("W-approve-ok"))
c.check("item 4: allowed-policy controls write their optional decision records (N-denied, N-pending, X-denied, "
        "X-pending: one `decision` record each) while staging no tool_outcome candidate for the call that did not run",
        all([r.get("type") for r in (case(n).get("store") or {}).get("records") or []].count("decision") == 1
            and tool_outcomes(n) == [] for n in ("N-denied", "N-pending", "X-denied", "X-pending")),
        {n: (case(n).get("store") or {}).get("records") for n in ("N-denied", "N-pending", "X-denied", "X-pending")})
if ARGS.backend == "sqlite":
    sc, sd = standalone.get("confirm") or {}, standalone.get("deny") or {}
    c.check("item 4: standalone BrainRunner (no agent service, no per-turn memory.write answer) keeps both writes: with "
            "the allowing policy each step writes its decision record and the eligible candidate (above); with a "
            "REQUIRE_CONFIRMATION policy and a DENY policy, weather does not run, the step waits for the user, writes "
            "its decision record and stages no tool_outcome candidate; policy.applied and tool.call.blocked are recorded",
            all(not r.get("case_error") for r in (sc, sd))
            and all((r.get("steps") or {}).get("weather", {}).get("status") == "waiting_user"
                    and not (r.get("steps") or {}).get("weather", {}).get("tool_adapter_calls")
                    and sa_titles((r.get("steps") or {}).get("weather", {})) == []
                    and sa_titles((r.get("steps") or {}).get("weather", {}), "decision")
                    and ((r.get("steps") or {}).get("weather", {}).get("policy_events") or {}).get("tool.call.blocked") == 1
                    for r in (sc, sd)),
            {"confirm": sa_view(sc), "deny": sa_view(sd)})

# ---------------------------------------------------------------- notes (not judged)
sw = case("N-scope-weather")
observed["notes"] = {
    "N-scope-weather (weather min_scope POWER_USER: refused by the runtime tool adapter's scope check at execution, "
    "not by the brain's policy decision; classified failure, not policy_denied)": facts("N-scope-weather"),
    "N-later-fail-unforced (as N-later-fail, replies 'yes', 'yes' without forced_tools)": facts("N-later-fail-unforced"),
}
print("NOTE N-scope-weather: tool_outcome=" + json.dumps(tool_outcomes("N-scope-weather"))
      + " handler_calls=" + json.dumps(sw.get("handler_calls")) + " reply=" + json.dumps(first("N-scope-weather").get("text"))
      + " candidates_with_argument_text=" + json.dumps(arg_candidates("N-scope-weather")))
lu = case("N-later-fail-unforced")
print("NOTE N-later-fail-unforced: per turn (reply, handler runs so far, tool_outcome titles, pending command id): "
      + json.dumps([[t.get("reply"), t.get("handler_calls_after"), (t.get("store_after") or {}).get("tool_outcome_titles"),
                     t.get("pending_confirmation_command_id")] for t in lu.get("turns") or []])
      + " source_command_ids=" + json.dumps([(r.get("meta_json") or {}).get("source_command_id")
                                             for r in (lu.get("store") or {}).get("candidates") or []
                                             if r.get("type") == "tool_outcome"]))
summary = c.summary()
write_result(OUT, {"info": info, "summary": summary, "all_pass": c.all_pass, "checks": c.items, "observed": observed})
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
