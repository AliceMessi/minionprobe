"""#541 release verification: `Agent.run`/`run_stream` accept `thread_id` and return
`AgentRunResult.thread_id`.

Installed package only (no repo test fixtures); run with `python -I -X utf8`
from an empty directory outside any checkout. Harness: `probe_420_lib.py` and
`probe_496_lib.py` (byte copies of the 0.1.16 round's #448/#496 helpers) and
`probe_524_lib.py` (`build_runtime_ext`, `ArmedProvider`, `HandlerRecorder`).
Each case is a real `APIRuntime` whose agents' `openai` provider is a scripted
OpenAI-compatible server on 127.0.0.1 (a local stub, not a live provider), and
one `Agent(runtime=runtime, forced_tools=["memory.write"])` per agent. Once
armed, the scripted model calls `memory_write` with fixed arguments; without an
approval callback the call then waits for the user's confirmation. A
call-through recorder lists each run of memory.write's handler (the original
handler then writes the record), and a call-through wrapper on
`APIRuntime.run_turn` keeps each payload the SDK sends. Every SDK call here is
in-process (the checklist is about the SDK); nothing is changed in the package.

The published SDK example is the code block that #546 added to
docs/runtime-surfaces.md (v0.1.17 blob 358abd59, byte copy passed with --doc);
the probe extracts it from that file and executes it as written, with `agent`
bound to the case's Agent.

Usage: python -I -X utf8 probe_541.py <work_dir> <out.json> --doc <docs_runtime-surfaces.md>
           [--baseline <control.json>] [--expect-version 0.1.17]
The 0.1.16 control runs without --baseline: the calls with `thread_id` raise
TypeError there and are recorded as such; the ordinary calls are compared with it.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import re
import sys
import time
import traceback
from importlib import metadata
from pathlib import Path
from threading import Event
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("work")
ap.add_argument("out")
ap.add_argument("--doc", required=True)
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


DOC_BYTES = Path(ARGS.doc).read_bytes()
DOC_BLOB = hashlib.sha1(b"blob %d\0" % len(DOC_BYTES) + DOC_BYTES).hexdigest()
info = banner("probe_541",
              provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1 (local stub)",
              dist_versions={n: _dist(n) for n in ("openminion", "pydantic")},
              doc={"name": Path(ARGS.doc).name, "git_blob": DOC_BLOB,
                   "sha256": hashlib.sha256(DOC_BYTES).hexdigest()},
              baseline=Path(ARGS.baseline).name if ARGS.baseline else None)

from openminion import Agent, AgentRunResult  # noqa: E402
from openminion.api import RuntimeTurnHandle, TurnChunk, TurnResponse  # noqa: E402
from openminion.modules.controlplane.constants import CALLER_HANDLES_DELIVERY_METADATA_KEY  # noqa: E402

import probe_420_lib as lib  # noqa: E402
import probe_524_lib as X  # noqa: E402

c = Checks()
AGENT_ID = lib.AGENT_ID
HELPER = "helper"
ARGS1 = X.MEMORY_ARGS
MSG1 = X.MESSAGE  # "Remember that I write code in Iosevka 14." (the doc example's message)
ARGS2 = {"record_type": "fact", "key": "fact:editor_theme", "title": "Editor theme",
         "content": "The user prefers a light editor theme."}
MSG2 = "Remember that I prefer a light editor theme."
PENDING = "Policy confirmation required."
DENIED = "Confirmation denied. The action was not run."
VIEW_KEYS = ("thread_id", "thread_decision_action", "thread_decision_reason", "respond_kind",
             "pending_confirmation_command_id")


def short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc).splitlines()[0][:240] if str(exc) else ''}"


class Ctx:
    def __init__(self, label: str, *, extra_agents: tuple[str, ...] = ()) -> None:
        self.label = label
        self.root = WORK / label
        self.provider = X.ArmedProvider()
        self.old_env = lib.chdir_env(self.root)
        self.runtime = X.build_runtime_ext(self.root, self.provider, extra_agents=extra_agents)
        self.handler = X.HandlerRecorder(self.runtime, "memory.write", "pass")
        self.payloads: list[dict[str, Any]] = []
        original = self.runtime.run_turn
        ctx = self

        def recording_run_turn(*, payload: dict[str, Any], **kwargs: Any) -> Any:
            ctx.payloads.append(json.loads(json.dumps(payload, default=str)))
            return original(payload=payload, **kwargs)

        self.runtime.run_turn = recording_run_turn
        self.calls: list[dict[str, Any]] = []

    def call(self, label: str, fn: Any, *, arm: dict[str, Any] | None = None) -> dict[str, Any]:
        if arm is not None:
            self.provider.arm("memory_write", arm)
        mark_h, mark_p = len(self.handler.calls), len(self.payloads)
        row: dict[str, Any] = {"label": label}
        try:
            result = fn()
            if isinstance(result, AgentRunResult):
                md = dict(result.metadata or {})
                row.update(run_state=result.run_state, text=str(result.text or "")[:200],
                           session_id=result.session_id, run_id=result.run_id,
                           result_thread_id=getattr(result, "thread_id", "<no attribute>"),
                           metadata_keys=sorted(md), **{k: md.get(k) for k in VIEW_KEYS})
            else:
                row["returned"] = repr(result)[:200]
        except Exception as exc:  # noqa: BLE001 - recorded; the checks then fail
            row["error"] = short(exc)
            row["error_type"] = type(exc).__name__
        row["handler_runs"] = [{k: c.get(k) for k in ARGS1} for c in self.handler.calls[mark_h:]]
        row["handler_runs_total"] = len(self.handler.calls)
        row["payloads"] = self.payloads[mark_p:]
        self.calls.append(row)
        return row

    def records(self) -> list[Any]:
        store = self.runtime.resolve_memory_assembly(AGENT_ID).service._store
        db = Path(str(getattr(store, "_store", store).db_path))
        return lib.sqlite_rows(db, "SELECT key FROM memory_records WHERE type = 'fact' AND is_deleted = 0 "
                                   "ORDER BY created_at")

    def close(self) -> dict[str, Any]:
        out: dict[str, Any] = {"calls": self.calls,
                               "provider_hosts": sorted({self.provider.base_url.split("/")[2].split(":")[0]})}
        try:
            out["fact_records"] = self.records()
        except Exception as exc:  # noqa: BLE001
            out["fact_records"] = short(exc)
        self.handler.restore()
        try:
            self.runtime.close()
        except Exception as exc:  # noqa: BLE001
            out["runtime_close"] = short(exc)
        lib.restore_env(self.old_env)
        self.provider.close()
        return out


def agent_for(ctx: Ctx, **kwargs: Any) -> Agent:
    return Agent(runtime=ctx.runtime, forced_tools=["memory.write"], **kwargs)


CASES: dict[str, Any] = {}


def case(name: str, **ctx_kwargs: Any) -> Any:
    def wrap(fn: Any) -> Any:
        t0 = time.monotonic()
        ctx = None
        row: dict[str, Any] = {}
        try:
            ctx = Ctx(name, **ctx_kwargs)
            row["extra"] = fn(ctx) or {}
        except Exception as exc:  # noqa: BLE001
            row["case_error"] = short(exc)
            row["case_error_frames"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}"
                                        for f in traceback.extract_tb(exc.__traceback__)][-8:]
        finally:
            if ctx is not None:
                row.update(ctx.close())
        row["seconds"] = round(time.monotonic() - t0, 1)
        CASES[name] = row
        print(f"case {name}: {row['seconds']} s, error={row.get('case_error')}", flush=True)
        return fn
    return wrap


# ---------------------------------------------------------------- item 1
@case("resume")
def _resume(ctx: Ctx) -> None:
    agent = agent_for(ctx)
    first = ctx.call("first: agent.run(MSG1), no callback", lambda: agent.run(MSG1), arm=ARGS1)
    tid = first.get("result_thread_id")
    ctx.call("answer: agent.run('yes', thread_id=first.thread_id)", lambda: agent.run("yes", thread_id=tid))
    ctx.call("stale: agent.run('yes', thread_id=first.thread_id) again", lambda: agent.run("yes", thread_id=tid))


# ---------------------------------------------------------------- item 2
for label, value in (("omitted", None), ("empty", ""), ("blank", "  ")):
    def _default(ctx: Ctx, value: Any = value) -> None:
        agent = agent_for(ctx)
        ctx.call("first: agent.run(MSG1)", lambda: agent.run(MSG1), arm=ARGS1)
        kwargs = {} if value is None else {"thread_id": value}
        ctx.call(f"reply: agent.run('yes'{'' if value is None else ', thread_id=' + repr(value)})",
                 lambda: agent.run("yes", **kwargs))
    case(f"default-{label}")(_default)


# ---------------------------------------------------------------- item 3
@case("two-threads")
def _two(ctx: Ctx) -> None:
    agent = agent_for(ctx)
    a = ctx.call("first wait (ARGS1)", lambda: agent.run(MSG1), arm=ARGS1)
    b = ctx.call("second wait (ARGS2), same session", lambda: agent.run(MSG2), arm=ARGS2)
    ctx.call("answer the second thread", lambda: agent.run("yes", thread_id=b.get("result_thread_id")))
    ctx.call("answer the first thread", lambda: agent.run("yes", thread_id=a.get("result_thread_id")))


@case("other-session")
def _other_session(ctx: Ctx) -> None:
    agent = agent_for(ctx)
    a = ctx.call("owner session wait (ARGS1)", lambda: agent.run(MSG1), arm=ARGS1)
    o = ctx.call("other-session wait (ARGS2)", lambda: agent.run(MSG2, session_id="other-session"), arm=ARGS2)
    ta, to = a.get("result_thread_id"), o.get("result_thread_id")
    ctx.call("crossed: other-session + owner's thread", lambda: agent.run("yes", session_id="other-session", thread_id=ta))
    ctx.call("crossed: new-session + owner's thread", lambda: agent.run("yes", session_id="new-session", thread_id=ta))
    ctx.call("crossed: owner's session + other-session's thread", lambda: agent.run("yes", thread_id=to))
    ctx.call("own: owner's session + owner's thread", lambda: agent.run("yes", thread_id=ta))
    ctx.call("own: other-session + its thread", lambda: agent.run("yes", session_id="other-session", thread_id=to))
    return {"owner_session": agent.session_id}


@case("other-agent", extra_agents=(HELPER,))
def _other_agent(ctx: Ctx) -> None:
    owner = agent_for(ctx, agent_id=AGENT_ID)
    helper = agent_for(ctx, agent_id=HELPER)
    a = ctx.call("owner wait (ARGS1)", lambda: owner.run(MSG1), arm=ARGS1)
    ta = a.get("result_thread_id")
    ctx.call("helper in the owner's session with the owner's thread",
             lambda: helper.run("yes", session_id=owner.session_id, thread_id=ta))
    ctx.call("helper in its own session with the owner's thread", lambda: helper.run("yes", thread_id=ta))
    ctx.call("owner answers its thread", lambda: owner.run("yes", thread_id=ta))
    return {"owner_session": owner.session_id, "helper_session": helper.session_id}


# ---------------------------------------------------------------- item 4
for label, kind, approve in (("cb-waiting-approve", "waiting", True), ("cb-waiting-deny", "waiting", False),
                             ("cb-new-approve", "new", True), ("cb-new-deny", "new", False)):
    def _cb(ctx: Ctx, kind: str = kind, approve: bool = approve) -> dict[str, Any]:
        agent = agent_for(ctx)
        approvals: list[Any] = []

        def callback(tool: str, args: dict[str, Any], _approval_id: str) -> bool:
            approvals.append({"tool": tool, "args": {k: args.get(k) for k in ARGS1}})
            return approve

        if kind == "waiting":
            first = ctx.call("first wait (ARGS1)", lambda: agent.run(MSG1), arm=ARGS1)
            ctx.call(f"'yes' on its thread with a callback that returns {approve}",
                     lambda: agent.run("yes", thread_id=first.get("result_thread_id"), approval_callback=callback))
        else:
            first = ctx.call("settled turn: agent.run('Hello.')", lambda: agent.run("Hello."))
            ctx.call(f"new call (ARGS2) on that thread with a callback that returns {approve}",
                     lambda: agent.run(MSG2, thread_id=first.get("result_thread_id"), approval_callback=callback),
                     arm=ARGS2)
        return {"approvals": approvals}
    case(label)(_cb)


@case("deny-reply")
def _deny_reply(ctx: Ctx) -> None:
    agent = agent_for(ctx)
    first = ctx.call("first wait (ARGS1)", lambda: agent.run(MSG1), arm=ARGS1)
    tid = first.get("result_thread_id")
    ctx.call("'no' on its thread", lambda: agent.run("no", thread_id=tid))
    ctx.call("later 'yes' on its thread", lambda: agent.run("yes", thread_id=tid))


@case("pre-cancelled")
def _pre_cancelled(ctx: Ctx) -> dict[str, Any]:
    agent = agent_for(ctx)
    first = ctx.call("first wait (ARGS1)", lambda: agent.run(MSG1), arm=ARGS1)
    cancelled = Event()
    cancelled.set()
    ctx.call("'yes' on its thread with cancel_event already set",
             lambda: agent.run("yes", thread_id=first.get("result_thread_id"), cancel_event=cancelled))
    return {}


# ---------------------------------------------------------------- item 5
@case("stream")
def _stream(ctx: Ctx) -> dict[str, Any]:
    agent = agent_for(ctx)
    first = ctx.call("first wait (ARGS1)", lambda: agent.run(MSG1), arm=ARGS1)
    tid = first.get("result_thread_id")
    events: list[tuple[int, Any]] = []
    stale: list[Any] = []
    ctx.call("run_stream('yes', thread_id=...)",
             lambda: agent.run_stream("yes", thread_id=tid,
                                      on_delta=lambda e: events.append((len(ctx.handler.calls), e))))
    ctx.call("stale run_stream('yes', thread_id=...)",
             lambda: agent.run_stream("yes", thread_id=tid, on_delta=stale.append))
    tool_events = [[n, e.get("kind"), e.get("tool_name")] for n, e in events if isinstance(e, dict)]
    status_runs = sorted({n for n, e in events if getattr(e, "status_key", None)})
    return {"tool_events": tool_events, "status_event_handler_runs": status_runs, "events": len(events),
            "event_types": sorted({type(e).__name__ for _n, e in events}),
            "stale_events": len(stale), "stale_tool_events": [e.get("kind") for e in stale if isinstance(e, dict)]}


# ---------------------------------------------------------------- item 6
DOC_TEXT = DOC_BYTES.decode("utf-8")
m = re.search(r"In that flow the turn ends.*?```python\n(.*?)```", DOC_TEXT, flags=re.S)
DOC_EXAMPLE = m.group(1) if m else ""


@case("doc-example")
def _doc(ctx: Ctx) -> dict[str, Any]:
    agent = agent_for(ctx)
    ns: dict[str, Any] = {"agent": agent}
    ctx.provider.arm("memory_write", ARGS1)
    mark = len(ctx.payloads)
    exec(compile(DOC_EXAMPLE, "docs/runtime-surfaces.md example", "exec"), ns)  # noqa: S102 - the published example
    out: dict[str, Any] = {"payloads": ctx.payloads[mark:], "handler_runs": [{k: c.get(k) for k in ARGS1}
                                                                            for c in ctx.handler.calls]}
    for name in ("first", "answer"):
        r = ns.get(name)
        if isinstance(r, AgentRunResult):
            md = dict(r.metadata or {})
            out[name] = {"run_state": r.run_state, "text": str(r.text or "")[:200], "session_id": r.session_id,
                         "result_thread_id": r.thread_id, **{k: md.get(k) for k in VIEW_KEYS}}
        else:
            out[name] = repr(r)
    return out


@case("ordinary")
def _ordinary(ctx: Ctx) -> dict[str, Any]:
    agent = agent_for(ctx)
    plain = Agent(runtime=ctx.runtime)
    ctx.call("plain Agent().run('Hello.')", lambda: plain.run("Hello."))
    ctx.call("plain Agent().run_stream('Hello.')", lambda: plain.run_stream("Hello.", on_delta=lambda e: None))
    ctx.call("forced-tool agent.run('Hello.') with session_id and timeout_seconds",
             lambda: agent.run("Hello.", session_id="ordinary-session", timeout_seconds=60))
    out: dict[str, Any] = {}
    handle = ctx.runtime.submit_turn(payload={"message": "installed wheel", "session_id": "sdk-wheel"})
    chunks = list(handle.stream(timeout_s=60))
    result = handle.result(timeout_s=60)
    out["submit_turn"] = {"handle_is_RuntimeTurnHandle": isinstance(handle, RuntimeTurnHandle),
                          "chunks": len(chunks), "all_TurnChunk": all(isinstance(x, TurnChunk) for x in chunks),
                          "result_is_TurnResponse": isinstance(result, TurnResponse)}
    return out


# ---------------------------------------------------------------- AgentRunResult (no runtime)
fields = [f.name for f in dataclasses.fields(AgentRunResult)]
positional: dict[str, Any] = {}
try:
    r8 = AgentRunResult("out", "text", {"raw": 1}, "run-1", "completed", "sess-1", "id-1", {"thread_id": "t-1"},
                        {"llm_calls": 1})
    positional["9 positional (pre-0.1.17 fields)"] = {f: getattr(r8, f, "<absent>") for f in fields}
except Exception as exc:  # noqa: BLE001
    positional["9 positional (pre-0.1.17 fields)"] = short(exc)
try:
    r9 = AgentRunResult("out", "text", {}, None, None, None, None, {}, {}, "t-2")
    positional["10 positional"] = {"thread_id": getattr(r9, "thread_id", "<absent>")}
except Exception as exc:  # noqa: BLE001
    positional["10 positional"] = short(exc)

baseline = json.loads(Path(ARGS.baseline).read_text(encoding="utf-8")) if ARGS.baseline else None
bobs = (baseline or {}).get("observed") or {}
observed = {"cases": CASES, "agent_run_result_fields": fields, "positional": positional,
            "doc_example": DOC_EXAMPLE}


# ---------------------------------------------------------------- views
def calls(name: str) -> list[dict[str, Any]]:
    return (CASES.get(name) or {}).get("calls") or []


def at(name: str, i: int) -> dict[str, Any]:
    cs = calls(name)
    return cs[i] if len(cs) > i else {}


def waiting(row: dict[str, Any]) -> bool:
    return (row.get("run_state") == "completed" and PENDING in str(row.get("text"))
            and row.get("respond_kind") == "policy_confirmation_prompt" and bool(row.get("pending_confirmation_command_id"))
            and row.get("handler_runs") == [] and bool(row.get("result_thread_id"))
            and row.get("result_thread_id") == row.get("thread_id"))


def resumed(row: dict[str, Any], thread_id: Any) -> bool:
    return (row.get("run_state") == "completed" and bool(thread_id) and row.get("result_thread_id") == thread_id
            and row.get("thread_id") == thread_id and row.get("thread_decision_action") == "resume_thread"
            and row.get("thread_decision_reason") == "explicit_thread_requested")


def runs(row: dict[str, Any]) -> list[dict[str, Any]]:
    return row.get("handler_runs") or []


def no_error(*names: str) -> bool:
    return all(not (CASES.get(n) or {}).get("case_error") and not any(x.get("error") for x in calls(n)) for n in names)


def view(name: str) -> dict[str, Any]:
    cs = CASES.get(name) or {}
    return {"case_error": cs.get("case_error"), "fact_records": cs.get("fact_records"), "extra": cs.get("extra"),
            "calls": [{k: x.get(k) for k in ("label", "error", "run_state", "text", "session_id", "result_thread_id",
                                             *VIEW_KEYS, "handler_runs", "handler_runs_total")}
                      | {"inbound_metadata": [p.get("inbound_metadata") for p in x.get("payloads") or []]}
                      for x in cs.get("calls") or []]}


hosts = sorted({h for cs in CASES.values() for h in cs.get("provider_hosts", [])})
c.check(f"environment: openminion {ARGS.expect_version} imported from site-packages in isolated mode; the provider is "
        "the scripted local stub on 127.0.0.1; the example file is the published docs/runtime-surfaces.md (git blob "
        "358abd59...)",
        info.get("openminion.__version__") == ARGS.expect_version and info.get("imported_from_site_packages") is True
        and info.get("isolated_mode") is True and hosts == ["127.0.0.1"] and DOC_BLOB.startswith("358abd59"),
        {"version": info.get("openminion.__version__"), "file": info.get("openminion.__file__"),
         "python": info.get("python"), "platform": info.get("platform"), "sqlite": info.get("sqlite3.sqlite_version"),
         "dist_versions": info.get("dist_versions"), "doc": info.get("doc"), "hosts": hosts})

# item 1
f, a, s = at("resume", 0), at("resume", 1), at("resume", 2)
tid = f.get("result_thread_id")
c.check("item 1: Agent.run returns the thread id (AgentRunResult.thread_id = metadata['thread_id'], non-blank) for a "
        "turn whose memory.write call waits; agent.run('yes', thread_id=first.thread_id) resumes that thread in the "
        "same session (resume_thread / explicit_thread_requested) and the waiting call runs exactly once, with the "
        "model's arguments, writing fact:editor_font; a stale second 'yes' on the thread is resumed but runs nothing",
        no_error("resume") and waiting(f) and resumed(a, tid) and a.get("session_id") == f.get("session_id")
        and runs(a) == [ARGS1] and resumed(s, tid) and runs(s) == [] and s.get("handler_runs_total") == 1
        and (CASES.get("resume") or {}).get("fact_records") == [("fact:editor_font",)],
        view("resume"))

# item 2
flag = {CALLER_HANDLES_DELIVERY_METADATA_KEY: "true"}
dv = {}
for label in ("omitted", "empty", "blank"):
    name = f"default-{label}"
    f, r = at(name, 0), at(name, 1)
    pf = (f.get("payloads") or [{}])[0]
    pr = (r.get("payloads") or [{}])[0]
    dv[label] = (no_error(name) and waiting(f) and pf.get("inbound_metadata") == flag
                 and pr == {**pf, "message": "yes"} and r.get("thread_decision_action") == "fork_thread"
                 and r.get("thread_decision_reason") == "settled_without_resume"
                 and bool(r.get("thread_id")) and r.get("thread_id") != f.get("thread_id") and runs(r) == []
                 and r.get("result_thread_id") == r.get("thread_id"))
    c.check(f"item 2: thread_id {label}: the payload the SDK sends is unchanged (inbound_metadata holds only the "
            "delivery flag; the reply's payload equals the first one but for the message), the reply keeps the "
            "default routing (fork_thread / settled_without_resume, a new thread) and the waiting call does not run",
            dv[label], view(name))
exp_fields = ["output", "text", "raw", "run_id", "run_state", "session_id", "id", "metadata", "stats", "thread_id"]
p9 = positional.get("9 positional (pre-0.1.17 fields)")
c.check("item 2: AgentRunResult positional construction stays compatible: the fields are the 0.1.16 fields in their "
        "order with thread_id appended last; nine positional values keep their meaning (thread_id None), a tenth sets "
        "thread_id; a returned result's metadata is the turn's metadata dict, with thread_id equal to "
        "result.thread_id",
        fields == exp_fields and isinstance(p9, dict) and p9.get("thread_id") is None and p9.get("stats") == {"llm_calls": 1}
        and p9.get("metadata") == {"thread_id": "t-1"} and p9.get("raw") == {"raw": 1}
        and (positional.get("10 positional") or {}).get("thread_id") == "t-2"
        and "thread_id" in (at("resume", 0).get("metadata_keys") or [])
        and at("resume", 0).get("result_thread_id") == at("resume", 0).get("thread_id"),
        {"fields": fields, "positional": positional, "first_metadata_keys": at("resume", 0).get("metadata_keys")})
if baseline is not None:
    bfields = bobs.get("agent_run_result_fields") or []
    bpos = (bobs.get("positional") or {}).get("9 positional (pre-0.1.17 fields)")
    bcases = bobs.get("cases") or {}

    def keyset(cases_: dict[str, Any], name: str, i: int) -> list[str]:
        cs = (cases_.get(name) or {}).get("calls") or []
        return list(cs[i].get("metadata_keys") or []) if len(cs) > i else []

    same_keys = {n: (keyset(CASES, n, 0), keyset(bcases, n, 0)) for n in ("default-omitted", "ordinary")}
    c.check("item 2: compared with the control run (published "
            f"{(baseline.get('info') or {}).get('openminion.__version__')}): its AgentRunResult fields are this "
            "version's fields without thread_id, in the same order; the same nine positional values give the same "
            "fields; the returned metadata of the same turns has the same keys",
            bfields + ["thread_id"] == fields and isinstance(bpos, dict) and isinstance(p9, dict)
            and {k: v for k, v in p9.items() if k != "thread_id"} == bpos
            and all(a_ == b_ and a_ for a_, b_ in same_keys.values()),
            {"control_fields": bfields, "control_positional": bpos, "metadata_keys (this, control)": same_keys})

# item 3
a1, b1, a2, a3 = (at("two-threads", i) for i in range(4))
c.check("item 3: two calls wait in one session, each on its own thread; answering the second thread runs only its "
        "call (ARGS2), then answering the first runs only ARGS1; each reply resumes the thread it names",
        no_error("two-threads") and waiting(a1) and waiting(b1) and a1.get("session_id") == b1.get("session_id")
        and a1.get("result_thread_id") != b1.get("result_thread_id")
        and resumed(a2, b1.get("result_thread_id")) and runs(a2) == [ARGS2]
        and resumed(a3, a1.get("result_thread_id")) and runs(a3) == [ARGS1],
        view("two-threads"))
os_ = [at("other-session", i) for i in range(7)]
c.check("item 3: a different session cannot execute the owner's pending call: the owner's thread sent with "
        "session 'other-session' (which has its own waiting call) or with 'new-session', and the other session's "
        "thread sent with the owner's session, each run nothing (the routing resumes the named thread inside the "
        "caller's session, where no call waits on it); each session's own reply then runs its own call once",
        no_error("other-session") and waiting(os_[0]) and waiting(os_[1]) and os_[1].get("session_id") == "other-session"
        and all(runs(x) == [] for x in os_[2:5])
        and [x.get("session_id") for x in os_[2:5]] == ["other-session", "new-session", os_[0].get("session_id")]
        and runs(os_[5]) == [ARGS1] and runs(os_[6]) == [ARGS2]
        and resumed(os_[5], os_[0].get("result_thread_id")) and resumed(os_[6], os_[1].get("result_thread_id")),
        view("other-session"))
oa = [at("other-agent", i) for i in range(4)]
c.check("item 3: a different agent cannot execute the owner's pending call: 'helper' in the owner's session is "
        "refused (TurnRequestError: the session belongs to agent 'openminion'); in its own session the owner's thread "
        "runs nothing; the owner's own reply then runs the call once",
        not (CASES.get("other-agent") or {}).get("case_error") and waiting(oa[0])
        and oa[1].get("error_type") == "TurnRequestError" and f"belongs to agent '{AGENT_ID}'" in str(oa[1].get("error"))
        and runs(oa[1]) == [] and not oa[2].get("error") and runs(oa[2]) == []
        and resumed(oa[2], oa[0].get("result_thread_id")) and str(oa[2].get("text")).startswith(f"{HELPER}:")
        and runs(oa[3]) == [ARGS1] and resumed(oa[3], oa[0].get("result_thread_id")),
        view("other-agent"))

# item 4
for label, kind, approve in (("cb-waiting-approve", "waiting", True), ("cb-waiting-deny", "waiting", False),
                             ("cb-new-approve", "new", True), ("cb-new-deny", "new", False)):
    first_, reply = at(label, 0), at(label, 1)
    expected = ARGS1 if kind == "waiting" else ARGS2
    approvals = ((CASES.get(label) or {}).get("extra") or {}).get("approvals")
    c.check(f"item 4: {label}: on an explicit thread the approval callback decides"
            + (" for the call waiting there ('yes' is sent)" if kind == "waiting" else " for a new call made on the "
               "thread of an earlier settled turn")
            + f": it is asked once with the call's arguments and returns {approve}; the call "
            + ("runs once" if approve else "does not run (reply 'Confirmation denied. The action was not run.')"),
            no_error(label) and (waiting(first_) if kind == "waiting" else first_.get("run_state") == "completed")
            and resumed(reply, first_.get("result_thread_id")) and approvals == [{"tool": "memory.write", "args": expected}]
            and runs(reply) == ([expected] if approve else [])
            and (approve or DENIED in str(reply.get("text"))),
            view(label))
d0, d1, d2 = (at("deny-reply", i) for i in range(3))
c.check("item 4: a 'no' reply on the thread denies the waiting call (reply 'Confirmation denied. The action was not "
        "run.'); a later 'yes' on the thread is stale and runs nothing",
        no_error("deny-reply") and waiting(d0) and resumed(d1, d0.get("result_thread_id")) and DENIED in str(d1.get("text"))
        and runs(d1) == [] and resumed(d2, d0.get("result_thread_id")) and runs(d2) == []
        and d2.get("handler_runs_total") == 0,
        view("deny-reply"))
pc0, pc1 = at("pre-cancelled", 0), at("pre-cancelled", 1)
c.check("item 4: an already-cancelled reply ('yes' on the thread with cancel_event set before the call) does not "
        "execute the waiting call (only whether the call runs is asserted, not how the cancelled turn ends; the "
        "approval/cancellation race #527 is not covered)",
        not (CASES.get("pre-cancelled") or {}).get("case_error") and waiting(pc0) and runs(pc1) == []
        and pc1.get("handler_runs_total") == 0 and (CASES.get("pre-cancelled") or {}).get("fact_records") == [],
        view("pre-cancelled"))

# item 5
st0, st1, st2 = (at("stream", i) for i in range(3))
sx = (CASES.get("stream") or {}).get("extra") or {}
c.check("item 5: run_stream('yes', thread_id=...) sends the selected thread (inbound_metadata.thread_id), resumes it "
        "and returns it; on_delta gets the progress while the turn runs: tool_started before the handler ran and "
        "tool_completed after it, and status events on both sides; the call runs once; a stale run_stream on the "
        "thread streams progress but runs nothing and has no tool event",
        no_error("stream") and waiting(st0)
        and [p.get("inbound_metadata") for p in st1.get("payloads") or []]
        == [{**flag, "thread_id": st0.get("result_thread_id")}]
        and resumed(st1, st0.get("result_thread_id")) and runs(st1) == [ARGS1]
        and sx.get("tool_events") == [[0, "tool_started", "memory.write"], [1, "tool_completed", "memory.write"]]
        and sx.get("status_event_handler_runs") == [0, 1]
        and resumed(st2, st0.get("result_thread_id")) and runs(st2) == [] and (sx.get("stale_events") or 0) > 0
        and sx.get("stale_tool_events") == [],
        {"calls": view("stream")["calls"], "stream": sx})

# item 6
dx = (CASES.get("doc-example") or {}).get("extra") or {}
df, da = dx.get("first") or {}, dx.get("answer") or {}
c.check("item 6: the published SDK example (docs/runtime-surfaces.md at v0.1.17, the block added by #546), executed "
        "as written from the installed package: `first` waits (respond_kind policy_confirmation_prompt), so the "
        "example sends agent.run('yes', thread_id=first.thread_id); `answer` resumes first's thread and the waiting "
        "call runs once with the model's arguments",
        not (CASES.get("doc-example") or {}).get("case_error") and bool(DOC_EXAMPLE.strip())
        and "agent.run(\"yes\", thread_id=first.thread_id)" in DOC_EXAMPLE
        and isinstance(df, dict) and df.get("respond_kind") == "policy_confirmation_prompt"
        and isinstance(da, dict) and resumed(da, df.get("result_thread_id")) and dx.get("handler_runs") == [ARGS1]
        and [p.get("inbound_metadata") for p in dx.get("payloads") or []]
        == [flag, {**flag, "thread_id": df.get("result_thread_id")}],
        {"example": DOC_EXAMPLE, "result": dx, "case_error": (CASES.get("doc-example") or {}).get("case_error")})
oc = [at("ordinary", i) for i in range(3)]
ox = (CASES.get("ordinary") or {}).get("extra") or {}
c.check("item 6: ordinary SDK calls without thread_id are unchanged: plain Agent().run and run_stream and a forced-tool "
        "run with session_id and timeout_seconds complete, send only the delivery flag in inbound_metadata, start a "
        "thread of their own (fork_thread) and return it as result.thread_id; APIRuntime.submit_turn streams "
        "TurnChunk items and returns a TurnResponse (the ci.yml installed-wheel check)",
        no_error("ordinary") and all(x.get("run_state") == "completed" and x.get("thread_decision_action") == "fork_thread"
                                     and bool(x.get("result_thread_id")) and x.get("result_thread_id") == x.get("thread_id")
                                     and [p.get("inbound_metadata") for p in x.get("payloads") or []] == [flag]
                                     for x in oc)
        and (oc[2].get("payloads") or [{}])[0].get("session_id") == "ordinary-session"
        and (oc[2].get("payloads") or [{}])[0].get("timeout_seconds") == 60
        and ox.get("submit_turn") == {"handle_is_RuntimeTurnHandle": True, "chunks": ox.get("submit_turn", {}).get("chunks"),
                                      "all_TurnChunk": True, "result_is_TurnResponse": True}
        and (ox.get("submit_turn") or {}).get("chunks", 0) > 0,
        {"calls": view("ordinary")["calls"], "extra": ox})
if baseline is not None:
    bcases = bobs.get("cases") or {}

    def payload_view(cases_: dict[str, Any], name: str) -> list[Any]:
        out = []
        for x in (cases_.get(name) or {}).get("calls") or []:
            # The Agent's own session id is a new uuid in each run.
            out.append([{k: (v if k != "session_id" or v == "ordinary-session" else "<agent session id>")
                         for k, v in p.items()} for p in x.get("payloads") or []])
        return out

    pv = {n: (payload_view(CASES, n), payload_view(bcases, n)) for n in ("ordinary", "default-omitted")}
    c.check("item 6: the payloads that the ordinary calls and the default-routing reply send are equal to the control "
            "run's, key by key", all(a_ == b_ and a_ for a_, b_ in pv.values()),
            {k: {"this": v[0], "control": v[1]} for k, v in pv.items()})

summary = c.summary()
write_result(OUT, {"info": info, "summary": summary, "all_pass": c.all_pass, "checks": c.items, "observed": observed})
for name, row in CASES.items():
    for x in row.get("calls") or []:
        print(f"[{name}] {x.get('label')}: error={x.get('error')} state={x.get('run_state')} "
              f"session={x.get('session_id')} thread={x.get('result_thread_id')} "
              f"decision={x.get('thread_decision_action')}/{x.get('thread_decision_reason')} "
              f"respond_kind={x.get('respond_kind')} handler_runs={len(x.get('handler_runs') or [])} "
              f"text={str(x.get('text'))[:60]!r}")
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if c.all_pass else 1)
