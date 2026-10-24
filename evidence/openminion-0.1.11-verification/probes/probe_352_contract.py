"""#352: reader contract cases on a fresh 0.1.11 database (SQLite or PostgreSQL).

Adapted from the PR's shared scenario file (tests/_run_status_scenarios.py at
v0.1.11), rewritten without pytest and compared with probe_352_lib's oracle (a
plain event reduction with 0.1.10's per-event normalization), not with the
package's own reader helpers. Each case runs in its own session:

 1. interleaved runs (special/padded/nested/upper-case ids, payload state over
    event type), stepping clock: list_session_runs for 8 scan limits x 3 limits
    and list_session_run_events for every run, exact and padded requests;
 2. interleaved threads: resolve_thread_lifecycle for 5 requests;
 3. payload values the readers normalize (padding, null/bool/number/container
    ids, JSON-token text, zero-width space, RUN. prefix) and thread payload
    variants; the four #372 review cases; ids that look like JSON tokens;
    decoded-ID order of runs that start at the same timestamp;
 4. event facts: 486 events (9 keys x 27 values x run/tool event) vs 0.1.10's
    expressions; exact decoded-ID identity, ASCII keys without NUL;
 5. NaN/Infinity/NUL in run fields and in unrelated tool/attach fields, and the
    five cases of the third #372 review; NUL in a *request* id: on PostgreSQL
    outside the request contract (psycopg takes no NUL in a text parameter;
    recorded, same as 0.1.10 per the PR);
 6. a run id whose key is over 20 kB, with a NUL;
 7. bounded rows per reader at the backend boundary, history 181 vs 1,201 events;
 8. canonical replay/conflict, terminal outcome / capture result transactions,
    rollback of a failed facts write, session deletion: events and facts stay
    consistent;
 9. backfill of 2,345 events stored at migration 12 (batches 1000/1000/345/0):
    rows unchanged, facts = write-path facts, readers = reduction;
10. backfill failure: an unreadable payload at event 1400 of 1,500; the
    migration fails naming it, ledger stays at 12, no event_run_facts table,
    events unchanged; after a deliberate repair a rerun applies 13 whole.

Usage: python -I probe_352_contract.py <work_dir> <out.json> <sqlite:PATH|postgres:SCHEMA>
"""

from __future__ import annotations

import itertools
import json
import sys
import traceback
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

import probe_352_lib as lib  # noqa: E402
from probe_352_lib import INF, NAN, NUL, PADDING  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
TARGET = sys.argv[3]
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
kind, value = lib.parse_target(TARGET)
info = banner("probe_352_contract", target=f"{kind}:{Path(value).name if kind == 'sqlite' else value}")

from openminion.modules.storage.runtime.migrations import MigrationError  # noqa: E402
from openminion.modules.storage.runtime.session_store.backend import RuntimeSessionStoreBackend  # noqa: E402
from openminion.modules.storage.runtime.session_store.models import RuntimeSessionStoreIntegrityError  # noqa: E402
from openminion.modules.storage.runtime.session_store.rows import row_to_event  # noqa: E402
from openminion.modules.storage.runtime.session_store.run_facts import event_run_facts  # noqa: E402
from openminion.modules.task.run import (  # noqa: E402
    append_run_state_event,
    list_session_run_events,
    list_session_runs,
    resolve_thread_lifecycle,
)

c = Checks()
result: dict = {"info": info, "cases": {}}


def subtarget(suffix: str) -> str:
    if kind == "sqlite":
        return f"sqlite:{Path(value).with_name(Path(value).stem + '-' + suffix + '.db')}"
    return f"postgres:{value}_{suffix}"


def case(name: str):
    def wrap(fn):
        try:
            observed = fn()
            c.check(name, True, observed)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed check
            c.check(name, False, {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-1500:]})
        return fn
    return wrap


def summary_dict(run) -> dict:
    return {k: getattr(run, k) for k in ("run_id", "started_at", "ended_at", "current_step", "error", "event_count", "state")}


def readers_vs_oracle(sessions, session_id, *, scans=None, limits=(1, 3, 20), target=None):
    oracle = lib.Oracle(target or TARGET, session_id)
    n = len(oracle.run_rows)
    bad, combos, calls = [], 0, 0
    for scan in scans or (1, 2, 5, 13, 40, n - 1, n, 2000):
        for limit in limits:
            combos += 1
            got = [summary_dict(r) for r in list_session_runs(sessions, session_id=session_id, limit=limit, scan_limit=scan)]
            if got != oracle.runs(limit=limit, scan_limit=scan):
                bad.append((scan, limit, got[:2], oracle.runs(limit=limit, scan_limit=scan)[:2]))
    for rid in oracle.run_ids():
        for limit in (1, 2, 1000):
            for req in (rid, f" {rid}\t"):
                calls += 1
                got = [e.id for e in list_session_run_events(sessions, session_id=session_id, run_id=req, limit=limit)]
                if got != oracle.run_event_ids(rid, limit):
                    bad.append((rid, limit, got[-3:], oracle.run_event_ids(rid, limit)[-3:]))
    assert not bad, bad[:3]
    return {"run_events": n, "runs": len(oracle.run_ids()), "listing_combinations": combos, "run_event_calls": calls}


def threads_vs_oracle(sessions, session_id, requests, target=None):
    oracle = lib.Oracle(target or TARGET, session_id)
    out, bad = {}, []
    for conv, thread in requests:
        conv_v = session_id if conv == "<session>" else conv
        p = resolve_thread_lifecycle(sessions, session_id=session_id, conversation_id=conv_v, thread_id=thread)
        exp = oracle.thread(conv_v, thread)
        got = {k: getattr(p, k) for k in exp}
        if got != exp:
            bad.append({"request": [conv, thread], "got": got, "expected": exp})
        out[f"{conv!r}|{thread!r}"] = {k: got[k] for k in ("latest_run_id", "latest_run_state", "invocation_id", "latest_event_id")}
    assert not bad, bad[:2]
    return out


if kind == "postgres":
    lib.pg_create_schema(value)
storage = lib.open_storage(TARGET, WORK)
try:
    sessions = storage.sessions
    counter = itertools.count()

    def new_session(target: str) -> str:
        return sessions.resolve_session(agent_id="main", channel="console", target=f"{target}-{next(counter)}").id

    @case("1. interleaved runs (special, padded, nested, upper-case ids; stepping clock): run listing and run events equal the reduction")
    def _runs():
        sid = new_session("runs")
        with lib.stepping_clock():
            lib.append_runs(sessions, sid)
        return readers_vs_oracle(sessions, sid)

    @case("2. interleaved threads: thread lifecycle equals the reduction (latest run/state, invocation, high water, delivery, writer)")
    def _threads():
        sid = new_session("threads")
        with lib.stepping_clock():
            lib.append_threads(sessions, sid)
        return threads_vs_oracle(sessions, sid, [("conv", "quiet"), ("conv", "busy"), ("conv", "missing"), ("<session>", "busy"), ("", "quiet")])

    @case("3a. padded fields, null/bool/number/container ids, JSON-token text, zero-width space, RUN. prefix: readers equal the reduction")
    def _payloads():
        runs, threads = new_session("payload-runs"), new_session("payload-threads")
        lib.append_payload_variants(sessions, runs, append_run_state_event)
        lib.append_thread_payload_variants(sessions, threads)
        a = readers_vs_oracle(sessions, runs)
        b = threads_vs_oracle(sessions, threads, [("conv", "t"), ("<session>", "t"), ("conv", "​t"), ("", "t"),
                                                  (f" conv{PADDING}", f"{PADDING}t"), ("trace: NaN", "[NaN]"), ("trace: 1e999", "[1e999]")])
        return {"runs": a, "threads": b}

    @case("3b. the four #372 review cases: padded run id is one run; blank error keeps the earlier error; padded state; padded thread ids")
    def _review():
        def append(sid, state, **kw):
            return append_run_state_event(sessions, session_id=sid, run_id="r", state=state, current_step="", **kw)

        out = {}
        sid = new_session("padded-run-id")
        append(sid, "queued")
        append(sid, "running", payload={"run_id": " r "})
        runs = list_session_runs(sessions, session_id=sid)
        assert [(r.run_id, r.event_count) for r in runs] == [("r", 2)]
        assert len(list_session_run_events(sessions, session_id=sid, run_id="r")) == 2
        out["padded run id"] = [(r.run_id, r.event_count) for r in runs]
        sid = new_session("blank-error")
        append(sid, "queued", payload={"error": "earlier error"})
        append(sid, "running", payload={"error": "\n\t"})
        out["blank error"] = list_session_runs(sessions, session_id=sid)[0].error
        assert out["blank error"] == "earlier error"
        sid = new_session("padded-state")
        append(sid, "queued")
        append(sid, "failed", payload={"state": "\trunning\n"})
        r = list_session_runs(sessions, session_id=sid)[0]
        out["padded state"] = (r.state, r.ended_at)
        assert (r.state, r.ended_at) == ("running", "")
        sid = new_session("padded-thread")
        ids = {"conversation_id": "conv", "thread_id": "t"}
        append(sid, "queued", **ids)
        failed = append(sid, "failed", **ids, payload={"conversation_id": "\tconv\n", "thread_id": "\nt\t"})
        p = resolve_thread_lifecycle(sessions, session_id=sid, **ids)
        out["padded thread"] = (p.latest_run_state, p.latest_event_id, failed.id)
        assert (p.latest_run_state, p.latest_event_id) == ("failed", failed.id)
        return out

    @case("3c. ids that look like JSON tokens keep their exact text: 4 distinct runs, each run/thread read exactly")
    def _tokens():
        sid = new_session("token-text-ids")
        ids = ("trace: NaN", "trace: 1e999", "trace, Infinity", "[NaN]")
        events = {v: append_run_state_event(sessions, session_id=sid, run_id=v, state="queued", current_step="",
                                            conversation_id=v, thread_id=v) for v in ids}
        runs = list_session_runs(sessions, session_id=sid)
        assert sorted((r.run_id, r.event_count) for r in runs) == sorted((v, 1) for v in ids)
        for v, ev in events.items():
            assert [i.id for i in list_session_run_events(sessions, session_id=sid, run_id=v)] == [ev.id], v
            p = resolve_thread_lifecycle(sessions, session_id=sid, conversation_id=v, thread_id=v)
            assert (p.latest_run_id, p.latest_event_id) == (v, ev.id), v
        return sorted((r.run_id, r.event_count) for r in runs)

    @case("3d. decoded-ID order: runs starting at the same timestamp are listed by decoded run id (descending), not by their JSON-encoded keys")
    def _decoded_order():
        sid = new_session("decoded-order")
        ids = ["z", "é", 'b"', "b#", "back\\slash", "back]slash", "éa", "Z"]
        with patch(lib.EVENT_CLOCK, return_value="2026-10-23T12:00:00.000001+00:00"):
            for rid in ids:
                append_run_state_event(sessions, session_id=sid, run_id=rid, state="queued", current_step="")
        listed = [r.run_id for r in list_session_runs(sessions, session_id=sid)]
        expected = sorted(ids, reverse=True)
        encoded_order = [json.loads(k) for k in sorted((json.dumps(i) for i in ids), reverse=True)]
        assert expected != encoded_order  # the case tells the two orders apart
        assert listed == expected, listed
        oracle = [r["run_id"] for r in lib.Oracle(TARGET, sid).runs(limit=20, scan_limit=2000)]
        assert listed == oracle
        return {"listed": listed, "json_key_order_would_be": encoded_order}

    @case("4. event facts of 486 events (9 keys x 27 values incl. padding, zero-width space, token text, every JSON type, NaN, +-Infinity, NUL) equal 0.1.10's expressions; exact decoded ids; ASCII keys without NUL")
    def _facts():
        sid = new_session("facts")
        for key in lib.FACT_KEYS:
            for v in lib.FACT_VALUES:
                for event_type in ("run.failed", "tool.call"):
                    sessions.append_event(session_id=sid, event_type=event_type, payload={"run_id": "r", key: v})
        rows = lib.raw_query(TARGET, "SELECT * FROM event_run_facts WHERE session_id = ? ORDER BY event_id", (sid,))
        events = {int(r["id"]): r for r in lib.raw_query(TARGET, "SELECT id, event_type, payload_json FROM events WHERE session_id = ? ORDER BY id", (sid,))}
        assert [int(r["event_id"]) for r in rows] == sorted(events)
        bad = []
        for row in rows:
            ev = events[int(row["event_id"])]
            payload = json.loads(ev["payload_json"])
            if lib.decoded_fact_row(row) != lib.expected_facts(payload, ev["event_type"]):
                bad.append(int(row["event_id"]))
            for col in ("run_key", "conversation_key", "thread_key", "attach_role"):
                if not (row[col].isascii() and NUL not in row[col]):
                    bad.append(int(row["event_id"]))
        assert not bad, bad[:5]
        return {"events": len(events), "facts_rows": len(rows)}

    @case("5a. NaN/Infinity/NUL in run fields and in unrelated tool/attach fields: readers equal the reduction; run r = failed/'inf'/'bad<NUL>byte'/4")
    def _nonstandard():
        sid = new_session("nonstandard")
        lib.append_nonstandard_values(sessions, sid)
        a = readers_vs_oracle(sessions, sid)
        requests = [("conv", "t"), ("<session>", "t"), ("", "t")]
        if kind == "sqlite":
            requests.append((f"c{NUL}", f"t{NUL}"))
        b = threads_vs_oracle(sessions, sid, requests)
        runs = {r.run_id: r for r in list_session_runs(sessions, session_id=sid)}
        assert sorted(runs) == sorted(["r", f"nul{NUL}id", "nan", "-inf", "x"]), sorted(runs)
        r = runs["r"]
        assert (r.state, r.current_step, r.error, r.event_count) == ("failed", "inf", f"bad{NUL}byte", 4)
        return {"runs": a, "threads": b, "run_ids": sorted(runs), "r": (r.state, r.current_step, r.error, r.event_count),
                "nul_thread_request_tested": kind == "sqlite"}

    @case("5b. the five cases of the third #372 review (earlier error, then error NaN / step Infinity / other key -Infinity / error with NUL / other key with NUL)")
    def _probe_cases():
        cases = {
            "error NaN": ({"error": NAN}, "s", "nan"),
            "step Infinity": ({"step": INF}, "inf", "earlier"),
            "other key -Infinity": ({"latency": -INF}, "s", "earlier"),
            "error with NUL": ({"error": f"bad{NUL}byte"}, "s", f"bad{NUL}byte"),
            "other key with NUL": ({"output": f"bin{NUL}ary"}, "s", "earlier"),
        }
        out = {}
        for name, (payload, step, error) in cases.items():
            sid = new_session(name.replace(" ", "-"))
            common = {"session_id": sid, "run_id": "r", "current_step": "s", "conversation_id": "conv", "thread_id": "t"}
            append_run_state_event(sessions, **common, state="queued", payload={"error": "earlier"})
            last = append_run_state_event(sessions, **common, state="running", payload=payload)
            runs = [(r.run_id, r.state, r.current_step, r.error, r.event_count) for r in list_session_runs(sessions, session_id=sid)]
            assert runs == [("r", "running", step, error, 2)], (name, runs)
            assert len(list_session_run_events(sessions, session_id=sid, run_id="r")) == 2, name
            p = resolve_thread_lifecycle(sessions, session_id=sid, conversation_id="conv", thread_id="t")
            assert (p.latest_run_id, p.latest_run_state, p.latest_event_id) == ("r", "running", last.id), name
            out[name] = runs[0]
        return out

    if kind == "postgres":
        @case("5c. outside the request contract on PostgreSQL: a conversation/thread *request* id with NUL is rejected by the message read (stored NUL values are read; see 5a)")
        def _nul_request():
            sid = new_session("nul-request")
            sessions.append_event(session_id=sid, event_type="run.queued", payload={"run_id": "x", "conversation_id": f"c{NUL}", "thread_id": f"t{NUL}"})
            text = lib.expect_raises(Exception, lambda: resolve_thread_lifecycle(sessions, session_id=sid, conversation_id=f"c{NUL}", thread_id=f"t{NUL}"))
            runs = [(r.run_id, r.event_count) for r in list_session_runs(sessions, session_id=sid)]
            assert runs == [("x", 1)]
            return {"request_with_nul": text[:200], "stored_nul_run_listed": runs}

    @case("6. a run id whose JSON key is over 20 kB, with a NUL: listing, run events and thread projection read it exactly")
    def _long_ids():
        sid = new_session("long-ids")
        long_text = "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(4000))
        run_id = f"{long_text}{NUL}run"
        conv, thread = f"{long_text}conv", f"{long_text}thread"
        appended = [append_run_state_event(sessions, session_id=sid, run_id=run_id, state=s, current_step="",
                                           conversation_id=conv, thread_id=thread) for s in ("queued", "failed")]
        assert len(json.dumps(run_id)) > 20_000
        runs = list_session_runs(sessions, session_id=sid)
        assert [(r.run_id, r.state, r.event_count) for r in runs] == [(run_id, "failed", 2)]
        assert [e.id for e in list_session_run_events(sessions, session_id=sid, run_id=run_id)] == [e.id for e in appended]
        p = resolve_thread_lifecycle(sessions, session_id=sid, conversation_id=conv, thread_id=thread)
        assert (p.latest_run_id, p.latest_event_id) == (run_id, appended[-1].id)
        return {"run_key_chars": len(json.dumps(run_id)), "listed": [(len(r.run_id), r.state, r.event_count) for r in runs]}

    @case("7. bounded rows: the rows each reader reads at the backend boundary are the same at 181 and 1,201 events of history")
    def _bounds():
        sid = new_session("bounds")
        reads: list[int] = []
        real = RuntimeSessionStoreBackend.query_dicts

        def counting(self, sql, params=None):
            rows = real(self, sql, params)
            reads.append(len(rows))
            return rows

        def measured(fn):
            reads.clear()
            with patch.object(RuntimeSessionStoreBackend, "query_dicts", counting):
                out = fn()
            return out, list(reads)

        def append(run_id, state, thread, **payload):
            return append_run_state_event(sessions, session_id=sid, run_id=run_id, state=state, current_step=state,
                                          conversation_id="conv", thread_id=thread, payload=payload or None)

        turns = itertools.count()
        measurements = []
        start = append("long", "queued", "thread-a", invocation_id="inv-long")
        for added in (60, 340):
            for _ in range(added):
                turn = next(turns)
                last = append("long", "running", "thread-a")
                append(f"short-{turn}", "queued", "thread-b", invocation_id="i")
                append(f"short-{turn}", "completed", "thread-b")
            runs, r1 = measured(lambda: list_session_runs(sessions, session_id=sid, limit=5, scan_limit=10))
            evs, r2 = measured(lambda: list_session_run_events(sessions, session_id=sid, run_id="long", limit=5))
            p, r3 = measured(lambda: resolve_thread_lifecycle(sessions, session_id=sid, conversation_id="conv", thread_id="thread-a"))
            history = sessions.event_high_water(session_id=sid) - start.id + 1
            long_run = next(r for r in runs if r.run_id == "long")
            assert (long_run.event_count, long_run.started_at) == (1 + turn + 1, start.created_at)
            assert [e.id for e in evs][-1] == last.id
            assert (p.invocation_id, p.latest_event_id) == ("inv-long", last.id)
            assert len(r1) == 2 and r1[0] <= 10 and r1[1] <= 20 and r2 == [5]
            assert len(r3) == 3 and r3[1:] in ([1, 2], [1, 3])
            measurements.append({"history_events": history, "list_session_runs": r1, "list_session_run_events": r2,
                                 "resolve_thread_lifecycle": r3})
        assert [m["history_events"] for m in measurements] == [181, 1201]
        assert {k: v for k, v in measurements[0].items() if k != "history_events"} == \
            {k: v for k, v in measurements[1].items() if k != "history_events"}
        return measurements

    @case("8. canonical replay/conflict, terminal outcome and capture result transactions, rollback of a failed facts write, session deletion: events and facts stay consistent")
    def _fact_writes():
        sid = new_session("fact-writes")

        def facts():
            return [(int(r["event_id"]), r["run_key"]) for r in
                    lib.raw_query(TARGET, "SELECT event_id, run_key FROM event_run_facts WHERE session_id = ? ORDER BY event_id", (sid,))]

        first = sessions.append_event(session_id=sid, event_type="run.queued", canonical_event_id="facts.v1:first", payload={"run_id": "first"})
        replay = sessions.append_event(session_id=sid, event_type="run.queued", canonical_event_id="facts.v1:first", payload={"run_id": "first"})
        assert replay == first
        conflict = lib.expect_raises(RuntimeSessionStoreIntegrityError, lambda: sessions.append_event(
            session_id=sid, event_type="run.queued", canonical_event_id="facts.v1:first", payload={"run_id": "second"}))
        assert facts() == [(first.id, json.dumps("first"))]
        outcome_payload = {"capture_id": "cap", "capture_state": "pending", "run_id": "o"}
        outcome = sessions.commit_terminal_capture_intent(session_id=sid, canonical_event_id="turn.outcome.v1:facts",
                                                          capture_id="cap", payload=outcome_payload, payload_hash="h")
        assert sessions.commit_terminal_capture_intent(session_id=sid, canonical_event_id="turn.outcome.v1:facts",
                                                       capture_id="cap", payload=outcome_payload, payload_hash="h") == outcome
        terminal_conflict = lib.expect_raises(RuntimeSessionStoreIntegrityError, lambda: sessions.commit_terminal_capture_intent(
            session_id=sid, canonical_event_id="turn.outcome.v1:facts", capture_id="cap",
            payload={**outcome_payload, "run_id": "other"}, payload_hash="h2"))
        result_payload = {"capture_id": "cap", "capture_state": "processed", "run_id": "c"}
        res = sessions.commit_capture_result_and_release_hold(session_id=sid, canonical_event_id="memory.capture.result.v1:cap",
                                                              capture_id="cap", payload=result_payload)
        replayed = sessions.commit_capture_result_and_release_hold(session_id=sid, canonical_event_id="memory.capture.result.v1:cap",
                                                                   capture_id="cap", payload=result_payload)
        assert (res.replayed, replayed.replayed, replayed.event) == (False, True, res.event)
        written = [(first.id, json.dumps("first")), (outcome.id, json.dumps("o")), (res.event.id, json.dumps("c"))]
        assert facts() == written
        for row, ev in zip(lib.raw_query(TARGET, "SELECT * FROM event_run_facts WHERE session_id = ? ORDER BY event_id", (sid,)),
                           (first, outcome, res.event)):
            norm = {k: (int(v) if k in ("event_id", "has_step", "has_error", "has_invocation", "has_attach") else v) for k, v in row.items()}
            assert norm == event_run_facts(ev)
        no_hold = lib.expect_raises(RuntimeSessionStoreIntegrityError, lambda: sessions.commit_capture_result_and_release_hold(
            session_id=sid, canonical_event_id="memory.capture.result.v1:no-hold", capture_id="no-hold",
            payload={"capture_id": "no-hold", "run_id": "orphan"}))
        assert sessions.get_event_by_canonical_id("memory.capture.result.v1:no-hold") is None
        high_water = sessions.event_high_water(session_id=sid)
        with patch("openminion.modules.storage.runtime.session_store.lifecycle.event_run_facts", side_effect=RuntimeError("facts failed")):
            rollback = lib.expect_raises(RuntimeError, lambda: sessions.append_event(
                session_id=sid, event_type="run.queued", payload={"run_id": "x"}), match="facts failed")
        assert sessions.event_high_water(session_id=sid) == high_water
        assert facts() == written
        assert [r.run_id for r in list_session_runs(sessions, session_id=sid)] == ["first"]
        other = new_session("fact-writes-other")
        kept = sessions.append_event(session_id=other, event_type="run.queued", payload={"run_id": "first"})
        assert sessions.delete_session(sid)
        assert facts() == []
        assert lib.raw_query(TARGET, "SELECT COUNT(*) AS n FROM events WHERE session_id = ?", (sid,))[0]["n"] == 0
        assert [int(r["event_id"]) for r in lib.raw_query(TARGET, "SELECT event_id FROM event_run_facts WHERE session_id = ?", (other,))] == [kept.id]
        return {"replay_equals_first": True, "canonical_conflict": conflict[:120], "terminal_conflict": terminal_conflict[:120],
                "capture_without_hold": no_hold[:120], "facts_write_failure": rollback[:120],
                "facts_after_writes": len(written), "facts_after_session_delete": 0, "other_session_facts_kept": 1}
finally:
    storage.close()


@case("9. backfill: 2,345 events stored at migration 12 (interleaved runs/threads, tool events, NaN/Infinity/NUL); migration 13 reads batches 1000/1000/345/0; rows unchanged; facts = write-path facts; readers = reduction")
def _backfill():
    t = subtarget("backfill")
    if kind == "postgres":
        lib.pg_create_schema(t.split(":", 1)[1])
    res12 = lib.migrate_target(t, lib.migrations_upto(12))
    assert lib.ledger(t) == list(range(1, 13)) and not lib.has_table(t, "event_run_facts")
    sids = lib.insert_legacy_history(t, 2345)
    stored = lib.stored_event_rows(t)
    batches: list[int] = []
    res = lib.migrate_target(t, lib.recorded_backfill_migrations(batches))
    assert list(res.applied_versions) == [13], res.applied_versions
    assert batches == [1000, 1000, 345, 0], batches
    assert lib.stored_event_rows(t) == stored
    facts = lib.check_facts_table(t)
    assert facts["ok"], facts
    st = lib.open_storage(t, WORK / "backfill")
    try:
        s = st.sessions
        fresh = s.resolve_session(agent_id="main", channel="console", target="written-after-upgrade").id
        appended = [s.append_event(session_id=fresh, event_type=r["event_type"], payload=row_to_event(r).payload) for r in stored[:40]]
        by_id = {r["id"]: r for r in lib.stored_event_rows(t)}
        assert [by_id[e.id]["payload_json"] for e in appended] == [r["payload_json"] for r in stored[:40]]
        f_all = lib.raw_query(t, "SELECT * FROM event_run_facts ORDER BY event_id")
        f_fresh = [r for r in f_all if r["session_id"] == fresh]

        def strip(r):
            return {k: v for k, v in r.items() if k not in ("event_id", "session_id")}

        assert [strip(r) for r in f_fresh] == [strip(r) for r in f_all[:40]]
        per = {}
        for sid in sids:
            per[sid] = readers_vs_oracle(s, sid, limits=(1, 3, 20), target=t)
            per[sid]["threads"] = threads_vs_oracle(s, sid, [("conv", "t0"), ("conv", "t2"), ("<session>", "t1")], target=t)
    finally:
        st.close()
    return {"migration_12_applied_first": list(res12.applied_versions), "applied": list(res.applied_versions),
            "batches": batches, "events": len(stored), "facts": facts["facts"], "per_session": per}


@case("10. backfill failure: unreadable payload at event 1400 of 1,500 -> MigrationError naming it; ledger stays at 12; no event_run_facts; events unchanged; after a deliberate repair the rerun applies 13 whole (1,500 facts)")
def _backfill_failure():
    t = subtarget("backfill_fail")
    if kind == "postgres":
        lib.pg_create_schema(t.split(":", 1)[1])
    lib.migrate_target(t, lib.migrations_upto(12))
    lib.insert_legacy_history(t, 1500)
    lib.raw_execute(t, "UPDATE events SET payload_json = ? WHERE id = 1400", ('{"run_id": ',))
    stored = lib.stored_event_rows(t)
    error = lib.expect_raises(MigrationError, lambda: lib.migrate_target(t, lib.migrations_upto(13)),
                              match="add_event_run_facts.*event 1400 has a payload_json that is not")
    led = lib.ledger(t)
    assert led == list(range(1, 13)), led
    assert not lib.has_table(t, "event_run_facts")
    assert lib.stored_event_rows(t) == stored
    # The normal runtime open refuses the same database the same way.
    open_error = lib.expect_raises(Exception, lambda: lib.open_storage(t, WORK / "fail-open").close(), match="event 1400")
    assert lib.ledger(t) == list(range(1, 13)) and lib.stored_event_rows(t) == stored
    lib.raw_execute(t, "UPDATE events SET payload_json = '{}' WHERE id = 1400")
    res = lib.migrate_target(t, lib.migrations_upto(13))
    assert list(res.applied_versions) == [13]
    n = lib.raw_query(t, "SELECT COUNT(*) AS n FROM event_run_facts")[0]["n"]
    assert int(n) == 1500
    facts = lib.check_facts_table(t)
    assert facts["ok"], facts
    return {"migration_error": error[:220], "normal_open_error": open_error[:220], "ledger_after_failure_max": max(led),
            "event_run_facts_table_after_failure": False, "events_unchanged": True, "rerun_applied": list(res.applied_versions),
            "facts_after_rerun": int(n)}


lib.dispose_engines()
result["checks"] = c.items
result["summary"] = c.summary()
write_result(OUT, result)
print("summary:", result["summary"])
