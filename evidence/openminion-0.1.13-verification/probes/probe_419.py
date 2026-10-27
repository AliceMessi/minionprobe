"""#419 on the installed package: an entry step's text answer is checked against
the turn's typed freshness obligations, and the turn evidence that the working
state keeps for a same-turn continuation (turn_evidence, turn_evidence_trace_id).

Proof boundary: the real installed SDK and runtime (`APIRuntime` + `Agent` with
an `@tool lookup_order`, min_scope READ_ONLY) talk to a scripted
OpenAI-compatible provider served on 127.0.0.1 (the `openai` provider with
base_url http://127.0.0.1:<port>/v1 and a dummy key). The script plays the
freshness classifier, entry steps, the act loop and the judge; everything else
(routing, the closure guard, `final_close_message`, the verifier, persistence)
is the installed package. A socket guard refuses non-loopback connections.
Read-only observers wrap (do not replace) `_apply_entry_freshness`,
`verify_freshness_answer` as imported by the closure owner and by the closure
evaluator, and `CanonicalEventLogger.emit`.

Scenarios (fresh runtime home each; "obligations" = [live, sources, exact date]):
  F1  first entry answers in text; classifier: live + sources + exact date
  F2  lookup (undated) -> judge closes -> closure guard continues -> text
  F3  lookup (undated) -> judge continues -> text (exact date required)
  P1  lookup -> judge continues -> text (live + sources, no exact date)
  P2  dated lookup -> judge continues -> text (exact date required)
  P3  undated lookup -> guard continues -> dated lookup -> judge continues -> text
  P4  first entry text, sources-only obligations (control: supported first entry)
  C1  tool path: lookup -> judge closes, guard accepts (control)
  C2  tool continuation: lookups -> guard continues twice -> text (control)
  C3  first entry text, not time sensitive (no-obligation control)
  N1  two turns in one conversation (conversation_id, as the HTTP turns route and
      the interactive CLI send it): turn 1 = P1, turn 2 answers in text
  N2  two SDK runs on one SDK session id, no conversation id
Stored working state: every row of the brain's SQLite session store (raw,
read-only SQL) and loads through `load_or_init_state` with `SessctlAdapter`;
the local session store through the real `brainctl` console script (local
adapter mode) and through `save_state`/`load_or_init_state` with
`LocalSessionStore`. States written by 0.1.12 are in probe_419_upgrade.py.

Usage: python -I -X utf8 probe_419.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, sanitize_text, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
quiet()
clear_override_env()
info = banner("probe_419", provider="openai (OpenAI-compatible HTTP) -> scripted ThreadingHTTPServer on 127.0.0.1",
              fixture_response_model="fixture-model")

import probe_419_lib as L  # noqa: E402
from openminion.modules.brain.runtime.verification.policy import build_freshness_failure_message  # noqa: E402
from openminion.modules.brain.schemas.freshness import FreshnessContract  # noqa: E402

c = Checks()
observed: dict[str, Any] = {}
SHOPPING = FreshnessContract(domain="shopping", time_sensitive=True)
FAIL_BOTH = build_freshness_failure_message(contract=SHOPPING, reasons=[L.MISSING_LIVE, L.MISSING_DATE])
FAIL_DATE = build_freshness_failure_message(contract=SHOPPING, reasons=[L.MISSING_DATE])
FAIL_LIVE = build_freshness_failure_message(contract=SHOPPING, reasons=[L.MISSING_LIVE])
ALL3, LIVE_SRC, SRC_ONLY, NONE3 = [True, True, True], [True, True, False], [False, True, False], [False, False, False]
TP = L.TurnPlan


def first(items: list[Any] | None, i: int = 0) -> Any:
    return items[i] if items and len(items) > i else None


def text_of(out: dict[str, Any], i: int = 0) -> str:
    res = first(out.get("results"), i) or {}
    return str(res.get("text") or res.get("error") or "")


def stored(name: str, root: Path) -> dict[str, Any]:
    """Every stored working-state row (raw SQL) and the latest state through the SQLite owner."""
    db = L.brain_sessions_db(root)
    info_: dict[str, Any] = {"files_with_working_state": L.tables_with_working_state(root),
                             "brain_sessions_db": str(db.relative_to(root)).replace("\\", "/")}
    rows = L.raw_sqlite_states(db) if db.exists() else []
    info_["rows"] = rows
    info_["row_count"] = len(rows)
    info_["rows_with_both_keys"] = sum(1 for r in rows if r["state"] and r["state"]["has_turn_evidence_key"]
                                       and r["state"]["has_turn_evidence_trace_id_key"])
    last = rows[-1] if rows else None
    if last is not None:
        loaded = L.load_through_owner("sqlite", db, last["session_id"])
        info_["owner_latest"] = L.public(loaded)
        info_["_state"] = loaded.get("_state")
    return info_


def run(name: str, plans: list[L.TurnPlan], *, inputs: list[str] | None = None, forced: list[str] | None = None,
        dated: tuple[bool, ...] = (False,), conversation_id: str | None = None) -> dict[str, Any]:
    root = WORK / name
    out = L.run_scenario(root, L.Script(plans), inputs=inputs or ["Check order A-104"],
                         forced_tools=forced if forced is not None else ["lookup_order"], dated_lookups=dated,
                         conversation_id=conversation_id)
    out["stored"] = stored(name, root)
    observed[name] = {k: v for k, v in out.items() if k != "stored"}
    observed[name]["stored"] = {k: v for k, v in out["stored"].items() if not k.startswith("_")}
    return out


def latest(out: dict[str, Any]) -> dict[str, Any]:
    rows = out["stored"]["rows"]
    return (rows[-1]["state"] or {}) if rows else {}


def owner_latest(out: dict[str, Any]) -> dict[str, Any]:
    return (out["stored"].get("owner_latest") or {}).get("state") or {}


def respond(out: dict[str, Any], i: int = 0) -> dict[str, Any] | None:
    return first(out.get("respond_verify"), i)


def evidence_matches_stored(out: dict[str, Any], i: int = 0) -> bool:
    rv = respond(out, i) or {}
    ev = rv.get("evidence") or {}
    st = rv.get("state_at_respond") or {}
    te = latest(out).get("turn_evidence") or {}
    return (bool(ev.get("command_id")) and ev.get("command_id") == st.get("turn_evidence_command_id")
            and st.get("trace_ids_match") is True and "lookup_order" in (ev.get("tool_result_names") or [])
            and isinstance(te, dict) and te.get("command_id") == ev.get("command_id"))


def paired(out: dict[str, Any]) -> bool:
    st = latest(out)
    ol = owner_latest(out)
    te = st.get("turn_evidence")
    return (isinstance(te, dict) and te.get("status") == "success" and te.get("has_outputs") is True
            and st.get("turn_evidence_trace_id") == st.get("trace_id") and bool(st.get("trace_id"))
            and ol.get("turn_evidence_trace_id") == st.get("trace_id")
            and (ol.get("turn_evidence") or {}).get("command_id") == te.get("command_id")
            and ol.get("same_turn_evidence") == te.get("command_id"))


def v(out: dict[str, Any], i: int = 0) -> dict[str, Any] | None:
    rv = respond(out, i)
    if rv is None:
        return None
    return {k: rv.get(k) for k in ("called_from", "answer", "obligations", "result")} | {
        "evidence_command_id": (rv.get("evidence") or {}).get("command_id"),
        "evidence_dated": (rv.get("evidence") or {}).get("dated"),
        "state_at_respond": rv.get("state_at_respond")}


# ======================================================== 1. missing evidence: no unverified answer
f1 = run("F1", [TP(classifier=L.LIVE_EXACT, first_entry="text")], forced=[])
e0 = first(f1.get("entry"))
c.check("F1 first-entry text: the entry step leaves the classifier's typed obligations on the turn "
        "(live, sources, exact date = true, true, true) and takes the respond route (entry_text_response)",
        e0 is not None and e0["obligations"] == ALL3 and e0["response"] == "text"
        and first(f1.get("brain_entry")) == {"route": "respond", "reason_code": "entry_text_response"},
        {"entry": e0, "brain_entry": f1.get("brain_entry")})
c.check("F1: the respond route runs the existing verifier (verify_freshness_answer via final_close_message, called "
        "from _respond_execute) once, on the entry text, with no evidence: missing live-data and exact-date evidence",
        len(f1.get("respond_verify") or []) == 1 and v(f1) is not None and v(f1)["called_from"] == "_respond_execute"
        and v(f1)["answer"] == L.ENTRY_TEXT and v(f1)["obligations"] == ALL3 and v(f1)["evidence_command_id"] is None
        and v(f1)["result"] == [L.MISSING_LIVE, L.MISSING_DATE], v(f1))
c.check("F1: the turn ends with the existing freshness-failure message for those reasons, not with the unverified "
        "entry text",
        text_of(f1).endswith(FAIL_BOTH) and L.ENTRY_TEXT not in text_of(f1) and f1["lookup_order_calls"] == [],
        {"results": f1.get("results"), "lookup_calls": f1["lookup_order_calls"]})
c.check("F1: the stored freshness diagnostics carry the verifier's reasons (verifier_notes, runtime SQLite store)",
        latest(f1).get("verifier_notes") == [L.MISSING_LIVE, L.MISSING_DATE], latest(f1).get("verifier_notes"))

f2 = run("F2", [TP(classifier=L.LIVE_EXACT, first_entry="tool", judges=("close",), continuations=("text",))])
c.check("F2 guard continuation: after the undated lookup the closure freshness guard blocks the judge's close "
        "(missing exact date) and the gate continues the turn (freshness_verifier_blocked)",
        [g["result"] for g in f2.get("guard") or []][:1] == [[L.MISSING_DATE]]
        and first(f2.get("gate")) == {"next_action": "continue", "reason": "freshness_verifier_blocked"},
        {"guard": [g["result"] for g in f2.get("guard") or []], "gate": f2.get("gate")})
e1 = first(f2.get("entry"), 1)
c.check("F2: the continuation entry step (no user input) keeps the turn's obligations [true, true, true] and answers "
        "in text (respond route)",
        e1 is not None and e1["user_input"] is None and e1["obligations"] == ALL3 and e1["response"] == "text"
        and first(f2.get("brain_entry"), 1) == {"route": "respond", "reason_code": "entry_text_response"},
        {"entry": e1, "brain_entry": f2.get("brain_entry")})
c.check("F2: the respond route verifies the continuation text with the turn's own lookup result as evidence (same "
        "command_id as the stored turn_evidence, trace ids equal): only the exact date is missing, so the live-data "
        "evidence was retained across the continuation",
        len(f2.get("respond_verify") or []) == 1 and (v(f2) or {}).get("answer") == L.CONTINUATION_TEXT
        and (v(f2) or {}).get("result") == [L.MISSING_DATE] and evidence_matches_stored(f2), v(f2))
c.check("F2: the turn ends with the freshness-failure message (exact date); neither the continuation text, the "
        "judge's blocked answer nor the tool loop's summary is returned",
        text_of(f2).endswith(FAIL_DATE) and not any(s in text_of(f2) for s in (L.CONTINUATION_TEXT, L.JUDGE_TEXT,
                                                                                 L.LOOP_MARK))
        and [x["order_id"] for x in f2["lookup_order_calls"]] == ["A-104"], f2.get("results"))

f3 = run("F3", [TP(classifier=L.LIVE_EXACT, first_entry="tool", judges=("continue",))])
c.check("F3 judge continuation: the judge asks to continue after the undated lookup and the continuation answers in "
        "text; the respond route verifies it with the turn's lookup: missing exact date",
        first(f3.get("gate"), 0) is not None and f3["gate"][0]["next_action"] == "continue"
        and len(f3.get("respond_verify") or []) == 1 and (v(f3) or {}).get("answer") == L.CONTINUATION_TEXT
        and (v(f3) or {}).get("result") == [L.MISSING_DATE] and evidence_matches_stored(f3),
        {"gate": f3.get("gate"), "verify": v(f3)})
c.check("F3: the turn ends with the freshness-failure message (exact date); the unverified continuation text is not "
        "returned",
        text_of(f3).endswith(FAIL_DATE) and L.CONTINUATION_TEXT not in text_of(f3) and L.JUDGE_TEXT not in text_of(f3),
        f3.get("results"))

# ======================================================== 2. real same-turn lookup then text: supported answer kept
p1 = run("P1", [TP(classifier=L.LIVE_NO_DATE, first_entry="tool", judges=("continue",))])
c.check("P1 judge continuation (live + sources, no exact date): the respond route verifies the continuation text "
        "with the turn's successful lookup_order result (trace ids equal): no reasons",
        len(p1.get("respond_verify") or []) == 1 and (v(p1) or {}).get("answer") == L.CONTINUATION_TEXT
        and (v(p1) or {}).get("obligations") == LIVE_SRC and (v(p1) or {}).get("result") == []
        and evidence_matches_stored(p1), v(p1))
c.check("P1: the supported continuation text is the answer, not the judge's answer or the tool loop's summary",
        text_of(p1).endswith(L.CONTINUATION_TEXT) and L.JUDGE_TEXT not in text_of(p1) and L.LOOP_MARK not in text_of(p1),
        p1.get("results"))
c.check("P1: the stored working state keeps the lookup as turn_evidence with this turn's trace id; the SQLite owner "
        "(SessctlAdapter + load_or_init_state) loads the same pair, and _same_turn_evidence returns it",
        paired(p1), {"stored": {k: latest(p1).get(k) for k in ("trace_id", "turn_evidence_trace_id", "turn_evidence")},
                     "owner": owner_latest(p1)})

p2 = run("P2", [TP(classifier=L.LIVE_EXACT, first_entry="tool", judges=("continue",))], dated=(True,))
c.check("P2 judge continuation, exact date required, the lookup returns a dated field (observed_at): the continuation "
        "text is verified with that dated lookup: no reasons, and it is the answer",
        len(p2.get("respond_verify") or []) == 1 and (v(p2) or {}).get("obligations") == ALL3
        and (v(p2) or {}).get("result") == [] and (v(p2) or {}).get("evidence_dated") is True
        and evidence_matches_stored(p2) and text_of(p2).endswith(L.CONTINUATION_TEXT), {"verify": v(p2),
                                                                                         "results": p2.get("results")})

p3 = run("P3", [TP(classifier=L.LIVE_EXACT, first_entry="tool", judges=("close", "continue"),
                   continuations=("tool", "text"))], dated=(False, True))
c.check("P3 guard continuation then a new same-turn lookup: the guard blocks the close on the undated lookup, the "
        "continuation's dated lookup passes the guard, the judge continues",
        [g["result"] for g in p3.get("guard") or []] == [[L.MISSING_DATE], []]
        and [g["next_action"] for g in p3.get("gate") or []] == ["continue", "continue"]
        and [x["dated"] for x in p3["lookup_order_calls"]] == [False, True],
        {"guard": [g["result"] for g in p3.get("guard") or []], "gate": p3.get("gate"),
         "lookups": p3["lookup_order_calls"]})
c.check("P3: the final continuation text is verified with the latest same-turn lookup (dated, the stored "
        "turn_evidence): no reasons, and it is the answer",
        len(p3.get("respond_verify") or []) == 1 and (v(p3) or {}).get("result") == []
        and (v(p3) or {}).get("evidence_dated") is True and evidence_matches_stored(p3) and paired(p3)
        and text_of(p3).endswith(L.CONTINUATION_TEXT), {"verify": v(p3), "results": p3.get("results")})

p4 = run("P4", [TP(classifier=L.SOURCES_ONLY, first_entry="text")], forced=[])
c.check("P4 (control) first-entry text whose obligations it meets (sources only): verified (no reasons) and kept",
        len(p4.get("respond_verify") or []) == 1 and (v(p4) or {}).get("obligations") == SRC_ONLY
        and (v(p4) or {}).get("result") == [] and text_of(p4).endswith(L.ENTRY_TEXT), {"verify": v(p4),
                                                                                        "results": p4.get("results")})

c1 = run("C1", [TP(classifier=L.LIVE_NO_DATE, first_entry="tool", judges=("close",))])
c.check("C1 (tool-path control): the closure guard accepts the lookup for a live answer without exact date, the "
        "judge's close ends the turn and the respond route is not taken",
        [g["result"] for g in c1.get("guard") or []] == [[]] and (c1.get("respond_verify") or []) == []
        and text_of(c1).endswith(L.JUDGE_TEXT), {"guard": [g["result"] for g in c1.get("guard") or []],
                                                 "respond_verify": c1.get("respond_verify"),
                                                 "results": c1.get("results")})

c2 = run("C2", [TP(classifier=L.LIVE_EXACT, first_entry="tool", judges=("close",), continuations=("tool", "text"))])
c.check("C2 (tool-continuation control): the continuation entry calls lookup_order again (act route) and the closure "
        "guard checks each close (missing exact date twice); lookup ran twice",
        [g["result"] for g in c2.get("guard") or []] == [[L.MISSING_DATE], [L.MISSING_DATE]]
        and [b["route"] for b in c2.get("brain_entry") or []][:2] == ["act", "act"]
        and len(c2["lookup_order_calls"]) == 2,
        {"guard": [g["result"] for g in c2.get("guard") or []], "brain_entry": c2.get("brain_entry"),
         "lookups": len(c2["lookup_order_calls"])})
c.check("C2: the text after the tool continuation is verified with the latest lookup and ends as the freshness "
        "failure (exact date)",
        (v(c2) or {}).get("result") == [L.MISSING_DATE] and evidence_matches_stored(c2)
        and text_of(c2).endswith(FAIL_DATE), {"verify": v(c2), "results": c2.get("results")})

c3 = run("C3", [TP(classifier=L.NOT_TIME_SENSITIVE, first_entry="text")], forced=[])
c.check("C3 (no-obligation control): not time sensitive, the verifier runs with no obligations and the entry text "
        "is kept unchanged",
        len(c3.get("respond_verify") or []) == 1 and (v(c3) or {}).get("obligations") == NONE3
        and (c3["respond_verify"][0].get("time_sensitive") is False) and (v(c3) or {}).get("result") == []
        and text_of(c3).endswith(L.ENTRY_TEXT), {"verify": v(c3), "results": c3.get("results")})

# ======================================================== 3. a new user turn does not reuse the previous turn's evidence
turn2 = TP(classifier=L.LIVE_NO_DATE, first_entry="text", entry_text=L.SECOND_TURN_TEXT,
           continuation_text=L.SECOND_TURN_TEXT)
n1 = run("N1", [TP(classifier=L.LIVE_NO_DATE, first_entry="tool", judges=("continue",)), turn2],
         inputs=["Check order A-104", "Is order A-104 still ready?"], conversation_id="order-chat")
t1_trace = ((respond(n1, 0) or {}).get("state_at_respond") or {}).get("trace_id")
st2 = (respond(n1, 1) or {}).get("state_at_respond") or {}
c.check("N1 two turns in one conversation: turn 1 (lookup, judge continues, text) keeps its supported answer",
        text_of(n1, 0).endswith(L.CONTINUATION_TEXT) and (v(n1, 0) or {}).get("result") == [], v(n1, 0))
c.check("N1: turn 2 starts from turn 1's working state (turn 1's evidence and trace id are on it) but its text is "
        "verified without evidence: the trace ids differ, so turn 1's lookup is not used; missing live data",
        (v(n1, 1) or {}).get("answer") == L.SECOND_TURN_TEXT and (v(n1, 1) or {}).get("evidence_command_id") is None
        and (v(n1, 1) or {}).get("result") == [L.MISSING_LIVE] and bool(t1_trace)
        and st2.get("turn_evidence_trace_id") == t1_trace and st2.get("trace_id") not in (None, t1_trace),
        v(n1, 1))
c.check("N1: turn 2 ends with the freshness-failure message (live data); one lookup in total",
        text_of(n1, 1).endswith(FAIL_LIVE) and L.SECOND_TURN_TEXT not in text_of(n1, 1)
        and len(n1["lookup_order_calls"]) == 1, {"results": n1.get("results"), "lookups": n1["lookup_order_calls"]})
n1_last, n1_owner = latest(n1), owner_latest(n1)
c.check("N1: stored after turn 2 (runtime SQLite store, raw and through the owner): trace_id is turn 2's, the "
        "evidence keeps its pairing with turn 1's trace id, and _same_turn_evidence returns nothing for turn 2",
        n1_last.get("trace_id") == st2.get("trace_id") and n1_last.get("turn_evidence_trace_id") == t1_trace
        and isinstance(n1_last.get("turn_evidence"), dict)
        and n1_owner.get("turn_evidence_trace_id") == t1_trace and n1_owner.get("same_turn_evidence") is None,
        {"stored": {k: n1_last.get(k) for k in ("trace_id", "turn_evidence_trace_id")},
         "owner": n1_owner, "turn1_trace": t1_trace})
n1_state = n1["stored"].get("_state")
fn = L.same_turn_evidence_fn()
if n1_state is not None and fn is not None:
    probe_state = n1_state.model_copy(update={"trace_id": t1_trace})
    back = fn(probe_state)
    c.check("N1: the same loaded state with turn 1's trace id gives back turn 1's evidence (the pair is intact; only "
            "the trace id decides)", back is not None and back.command_id == (n1_last.get("turn_evidence") or {}).get(
                "command_id"), None if back is None else back.command_id)
else:
    c.check("N1: the same loaded state with turn 1's trace id gives back turn 1's evidence (the pair is intact; only "
            "the trace id decides)", False, {"loaded_state": n1_state is not None,
                                             "_same_turn_evidence": "absent" if fn is None else "present"})

n2 = run("N2", [TP(classifier=L.LIVE_NO_DATE, first_entry="tool", judges=("continue",)), turn2],
         inputs=["Check order A-104", "Is order A-104 still ready?"])
n2_sessions = sorted({r["session_id"] for r in n2["stored"]["rows"]})
c.check("N2 two SDK runs with the same session id and no conversation id: each run is its own brain conversation "
        "(two working-state session ids); turn 2's text has no evidence and ends as the freshness failure",
        len(n2_sessions) == 2 and (v(n2, 1) or {}).get("evidence_command_id") is None
        and (v(n2, 1) or {}).get("result") == [L.MISSING_LIVE] and text_of(n2, 1).endswith(FAIL_LIVE)
        and text_of(n2, 0).endswith(L.CONTINUATION_TEXT),
        {"working_state_sessions": n2_sessions, "verify_turn2": v(n2, 1), "results": n2.get("results")})

# ======================================================== stored working state: both owners
scen = {"F1": f1, "F2": f2, "F3": f3, "P1": p1, "P2": p2, "P3": p3, "P4": p4, "C1": c1, "C2": c2, "C3": c3,
        "N1": n1, "N2": n2}
counts = {k: (o["stored"]["row_count"], o["stored"]["rows_with_both_keys"]) for k, o in scen.items()}
files = {k: o["stored"]["files_with_working_state"] for k, o in scen.items()}
c.check("Runtime SQLite store: the brain's sessions.db is the only file with a working_state table, and every "
        "working-state row the runtime wrote in the 12 scenarios contains both turn_evidence and "
        "turn_evidence_trace_id",
        all(f == ["brain/sessions.db"] for f in files.values()) and all(n > 0 and n == b for n, b in counts.values()),
        {"rows (total, with both keys)": counts, "files": sorted({x for f in files.values() for x in f})})
no_cont = {k: [(r["state"] or {}).get("turn_evidence_trace_id") for r in scen[k]["stored"]["rows"]]
           + [(r["state"] or {}).get("turn_evidence") for r in scen[k]["stored"]["rows"]] for k in ("F1", "P4", "C1", "C3")}
c.check("Runtime SQLite store: where no same-turn continuation ran (F1, P4, C1, C3) both fields are stored as null "
        "in every row",
        all(vals and all(x is None for x in vals) for vals in no_cont.values()), no_cont)
pair_rows = {k: [((r["state"] or {}).get("trace_id"), (r["state"] or {}).get("turn_evidence_trace_id"))
                 for r in scen[k]["stored"]["rows"] if (r["state"] or {}).get("turn_evidence")]
             for k in ("F2", "F3", "P1", "P2", "P3", "C2")}
c.check("Runtime SQLite store: in every stored row that holds a turn_evidence (F2, F3, P1, P2, P3, C2) its "
        "turn_evidence_trace_id is set and equals that turn's trace id",
        all(rows and all(a and a == b for a, b in rows) for rows in pair_rows.values()),
        {k: len(rows) for k, rows in pair_rows.items()})

# local session store, real writer: brainctl in local adapter mode (LocalLLMAdapter, no provider, no network)
bin_dir = Path(sys.executable).parent
brainctl = bin_dir / ("brainctl.exe" if os.name == "nt" else "brainctl")
local_root = WORK / "local-brainctl"
local_root.mkdir(parents=True, exist_ok=True)
cfg = local_root / "brain.yaml"
cfg.write_text("brain:\n  agent_id: openminion\n  budgets:\n    max_ticks_per_user_turn: 20\n    max_tool_calls: 5\n"
               "    max_a2a_calls: 5\n    max_total_llm_tokens: 5000\n    max_elapsed_ms: 120000\n"
               "  adapters:\n    mode: local\n", encoding="utf-8")
env = {k: v_ for k, v_ in os.environ.items() if k not in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT")}
cli_runs = []
LOCAL_FILE = "sessions/local-419/working_state.json"
for args in (["run", "--input", "Check order A-104", "--session-id", "local-419"],
             ["step", "--input", "Is order A-104 still ready?", "--session-id", "local-419"]):
    proc = subprocess.run([str(brainctl), *args, "--config", str(cfg), "--root", str(local_root / "brain")],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(local_root),
                          env=env)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        payload = {}
    after = L.local_state_files(local_root / "brain").get(LOCAL_FILE) or {}
    cli_runs.append({"argv": ["brainctl", *args], "rc": proc.returncode, "status": payload.get("status"),
                     "stdout_working_state_has_both_keys": all(f in (payload.get("working_state") or {})
                                                               for f in L.NEW_FIELDS),
                     "file_version_after": after.get("version"), "file_state_after": after.get("state"),
                     "stderr_tail": sanitize_text(proc.stderr.strip())[-400:]})
loc_state = cli_runs[-1]["file_state_after"] or {}
observed["local_brainctl"] = {"runs": cli_runs}
v_run, v_step = cli_runs[0]["file_version_after"], cli_runs[1]["file_version_after"]
c.check("Local session store, real writer: `brainctl run` then `brainctl step` (local adapter mode, the installed "
        "console script) write one session's <root>/sessions/local-419/working_state.json; after each command it "
        "contains both fields, null (no continuation ran), and the step continued the run's state (higher version)",
        [r["rc"] for r in cli_runs] == [0, 0]
        and all((r["file_state_after"] or {}).get("has_turn_evidence_key") is True
                and (r["file_state_after"] or {}).get("has_turn_evidence_trace_id_key") is True
                and (r["file_state_after"] or {}).get("turn_evidence") is None
                and (r["file_state_after"] or {}).get("turn_evidence_trace_id") is None for r in cli_runs)
        and isinstance(v_run, int) and isinstance(v_step, int) and v_step > v_run,
        [{k: r[k] for k in ("argv", "rc", "status", "file_version_after", "file_state_after")} for r in cli_runs])
loc_load = L.load_through_owner("local", local_root / "brain" / "sessions", "local-419")
c.check("Local session store: load_or_init_state through LocalSessionStore reads that state with both fields unset",
        loc_load.get("ok") is True and (loc_load.get("state") or {}).get("turn_evidence") is None
        and (loc_load.get("state") or {}).get("turn_evidence_trace_id") is None, L.public(loc_load))

# local session store with the SDK turns' real evidence: save_state / load_or_init_state with LocalSessionStore
from openminion.modules.brain.adapters.session import LocalSessionStore  # noqa: E402
from openminion.modules.brain.state import save_state  # noqa: E402

store_root = WORK / "local-store" / "sessions"
local_checks: dict[str, Any] = {}
for label, src in (("P1", p1), ("N1", n1)):
    state = src["stored"].get("_state")
    if state is None:
        local_checks[label] = {"error": "no state loaded from the SQLite owner"}
        continue
    # The SDK's brain session ids contain ':' ("<sid>::conv:<id>"), which Windows does not
    # allow in the local store's directory names, so the copy uses a plain session id.
    original_sid = state.session_id
    state = state.model_copy(update={"session_id": f"{label.lower()}-from-sdk"})
    api = LocalSessionStore(store_root)
    save_state(L.owner_runner(api), state)
    raw = L.local_state_files(store_root.parent).get(f"sessions/{state.session_id}/working_state.json", {})
    back = L.load_through_owner("local", store_root, state.session_id)
    local_checks[label] = {"sdk_session_id": original_sid, "session_id": state.session_id, "saved": raw.get("state"),
                           "loaded": (back.get("state") if back.get("ok") else L.public(back)),
                           "equal_evidence": back.get("ok") is True
                           and getattr(back["_state"], "turn_evidence", "<absent>") == getattr(state, "turn_evidence",
                                                                                               "<absent>")
                           and getattr(back["_state"], "turn_evidence_trace_id", "<a>") == getattr(
                               state, "turn_evidence_trace_id", "<b>")}
observed["local_store_from_sdk_states"] = local_checks
lp1, ln1 = local_checks.get("P1") or {}, local_checks.get("N1") or {}
c.check("Local session store with real evidence: P1's state (loaded from the runtime SQLite owner) saved with "
        "save_state into a LocalSessionStore keeps turn_evidence and its trace id in working_state.json; "
        "load_or_init_state returns an equal pair and _same_turn_evidence returns the evidence",
        bool(lp1.get("equal_evidence")) and ((lp1.get("saved") or {}).get("turn_evidence_trace_id")
                                             == (lp1.get("saved") or {}).get("trace_id") is not None)
        and isinstance((lp1.get("saved") or {}).get("turn_evidence"), dict)
        and (lp1.get("loaded") or {}).get("same_turn_evidence") == ((lp1.get("saved") or {}).get("turn_evidence")
                                                                    or {}).get("command_id"), lp1)
c.check("Local session store: N1's state after turn 2 keeps turn 1's pair through save/load, and _same_turn_evidence "
        "returns nothing (trace ids differ)",
        bool(ln1.get("equal_evidence")) and (ln1.get("saved") or {}).get("turn_evidence_trace_id") == t1_trace
        and (ln1.get("loaded") or {}).get("same_turn_evidence") is None, ln1)

c.check("Network: only loopback connections; no external provider call", L.BLOCKED == [], L.BLOCKED)
models = sorted({m for o in scen.values() for m in o.get("provider_request_models") or []})
observed["provider_request_models"] = models
s = c.summary()
write_result(OUT, {"info": info, "observed": observed, "checks": c.items, "summary": s})
print(f"SUMMARY passed={s['passed']} failed={s['failed']} total={s['total']} models={models}")
sys.exit(0 if c.all_pass else 1)
