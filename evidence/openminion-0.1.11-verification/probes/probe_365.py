"""#365: `openminion export transcript --include-events` keeps tied events in insertion order.

Every export runs the installed console script (`openminion`, next to sys.executable)
in a subprocess against a disposable runtime database with explicit roots and config.
The store's own list_messages / list_events order is recorded next to each export.

  A. Twelve events at one timestamp (ids 1..12, crossing 9/10): JSONL event ids and
     Markdown payload order.
  B. Mixed exact ties (the presentation rule): an earlier message; an event written
     with a clock 2 s ahead; then one turn inside one clock tick (7 setup events,
     run.running, message "hello", run.responding, message "reply",
     response.persisted, run.completed). The tied messages' ids sort in reverse of
     append order. Expected: created_at first; at the tie, messages in append order,
     then events in insertion order (ids 2..12); the clock-ahead event last.
  C. Four tied messages interleaved with events, plus ten more tied events (14 event
     ids crossing 9/10), for both message id orders: messages in append order, then
     the 14 events in insertion order.

On 0.1.10 the same script is a control: it records the pre-fix export order.

Usage: python -I probe_365.py <work_dir> <out.json>
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
WORK.mkdir(parents=True, exist_ok=True)
clear_override_env()
quiet()
info = banner("probe_365")

import openminion  # noqa: E402
from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config  # noqa: E402
from openminion.modules.storage.runtime.context import build_runtime_storage  # noqa: E402

VERSION = openminion.__version__
FIXED = VERSION != "0.1.10"
MESSAGE_CLOCK = "openminion.modules.storage.runtime.session_store.messages.utc_now_iso"
MESSAGE_UUID = "openminion.modules.storage.runtime.session_store.messages.uuid4"
EVENT_CLOCK = "openminion.modules.storage.runtime.session_store.lifecycle.utc_now_iso"
TIED_AT = "2026-10-22T01:00:00+00:00"
SCRIPT = Path(sys.executable).parent / ("openminion.exe" if os.name == "nt" else "openminion")
c = Checks()
observed: dict[str, object] = {"version": VERSION, "mode": "check" if FIXED else "control (pre-fix release)",
                               "console_script": SCRIPT.name}


@contextmanager
def clock(at: str):
    with patch(MESSAGE_CLOCK, return_value=at), patch(EVENT_CLOCK, return_value=at):
        yield


def make_session(label: str, write) -> tuple[Path, Path, dict]:
    root = WORK / label
    db = root / ".openminion" / "state.db"
    cfg = OpenMinionConfig()
    cfg.agents = {"openminion": AgentProfileConfig(name="openminion", provider="echo", default_channel="console")}
    cfg.default_agent = "openminion"
    cfg.storage.path = str(db)
    root.mkdir(parents=True, exist_ok=True)
    save_config(cfg, str(root / "config.json"))
    st = build_runtime_storage(db)
    try:
        s = st.sessions
        sess = s.resolve_session(agent_id="openminion", channel="console", target="focus", session_id="tied-session")
        write(s, sess.id)
        store = {
            "list_messages": [m.body for m in s.list_messages(session_id=sess.id, limit=1000)],
            "list_events": [f"#{e.id}" for e in s.list_events(session_id=sess.id, newest_first=False, limit=1000)],
        }
    finally:
        st.close()
    return root, root / "config.json", store


def export(root: Path, cfg: Path, fmt: str) -> tuple[str, dict]:
    out_file = root / f"transcript.{fmt}"
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_") and k != "PYTHONPATH"}
    cmd = [str(SCRIPT), "--home-root", str(root), "--data-root", str(root / ".openminion"), "--config", str(cfg),
           "--no-update-check", "export", "transcript", "--session-id", "tied-session", "--format", fmt,
           "--include-events", "--output", str(out_file)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, timeout=300)
    meta = {"returncode": proc.returncode, "stdout": proc.stdout.strip()[-200:], "stderr": proc.stderr.strip()[-600:]}
    return (out_file.read_text(encoding="utf-8") if out_file.exists() else ""), meta


def jsonl_order(text: str) -> list[str]:
    recs = [json.loads(line) for line in text.splitlines() if line.strip()]
    return [r["body"] if r["type"] == "message" else f"#{r['id']}" for r in recs]


def jsonl_timestamps(text: str) -> list[str]:
    return [json.loads(line)["created_at"] for line in text.splitlines() if line.strip()]


observed["command"] = ("openminion --home-root <root> --data-root <root>/.openminion --config <root>/config.json "
                       "--no-update-check export transcript --session-id tied-session --format jsonl|md --include-events --output <file>")

# ---------------------------------------------------------------- A. twelve tied events
def write_a(s, sid):
    with clock(TIED_AT):
        for n in range(12):
            s.append_event(session_id=sid, event_type="test.tied", payload={"append": n})


root, cfg, store = make_session("a-twelve-events", write_a)
jl, jl_meta = export(root, cfg, "jsonl")
md, md_meta = export(root, cfg, "md")
md_payload_order = re.findall(r'"append": (\d+)', md)
A = {"store_list_events": store["list_events"], "jsonl_order": jsonl_order(jl), "markdown_payload_append_order": md_payload_order,
     "cli": {"jsonl": jl_meta, "md": md_meta}}
observed["A_twelve_tied_events"] = A
c.check("A export CLI exit 0 (JSONL, Markdown)", jl_meta["returncode"] == 0 and md_meta["returncode"] == 0,
        {"jsonl": jl_meta["returncode"], "md": md_meta["returncode"]})
c.check("A store returns insertion ids #1..#12", store["list_events"] == [f"#{i}" for i in range(1, 13)], store["list_events"])
if FIXED:
    c.check("A JSONL exports the 12 tied events in insertion order across 9/10", A["jsonl_order"] == store["list_events"], A["jsonl_order"])
    c.check("A Markdown shows the 12 tied events in insertion order (payload append 0..11)",
            md_payload_order == [str(n) for n in range(12)], md_payload_order)
else:
    c.check("control on 0.1.10: JSONL sorts tied event ids as strings (1,10,11,12,2..9)",
            A["jsonl_order"] == ["#1", "#10", "#11", "#12"] + [f"#{i}" for i in range(2, 10)], A["jsonl_order"])


# ---------------------------------------------------------------- B. mixed exact ties
def write_b(s, sid):
    def msg(role, body):
        s.append_message(session_id=sid, role=role, body=body)

    def ev(event_type):
        s.append_event(session_id=sid, event_type=event_type, payload={})

    with patch(MESSAGE_UUID, side_effect=[SimpleNamespace(hex=ch * 32) for ch in "8f0"]):
        with clock("2026-10-22T01:00:00+00:00"):
            msg("inbound", "earlier")
        with clock("2026-10-22T01:00:02+00:00"):  # the clock steps back after this event
            ev("clock.ahead")
        with clock("2026-10-22T01:00:01+00:00"):  # one turn inside one clock tick
            for _ in range(7):
                ev("test.setup")
            ev("run.running")
            msg("inbound", "hello")
            ev("run.responding")
            msg("outbound", "reply")
            ev("response.persisted")
            ev("run.completed")


root, cfg, store = make_session("b-mixed-ties", write_b)
jl, jl_meta = export(root, cfg, "jsonl")
md, md_meta = export(root, cfg, "md")
headings = re.findall(r"^### (.+)$", md, flags=re.MULTILINE)
md_bodies = [line for line in md.splitlines() if line in {"earlier", "hello", "reply"}]
stamps = jsonl_timestamps(jl)
B = {"store_list_messages": store["list_messages"], "store_list_events": store["list_events"], "jsonl_order": jsonl_order(jl),
     "jsonl_created_at": stamps, "markdown_headings": headings, "markdown_message_bodies": md_bodies,
     "cli": {"jsonl": jl_meta, "md": md_meta}}
observed["B_mixed_exact_ties"] = B
expected_b = ["earlier", "hello", "reply", *(f"#{i}" for i in range(2, 13)), "#1"]
expected_headings = ["Inbound", "Inbound", "Outbound", *["Event: test.setup"] * 7, "Event: run.running", "Event: run.responding",
                     "Event: response.persisted", "Event: run.completed", "Event: clock.ahead"]
c.check("B export CLI exit 0 (JSONL, Markdown)", jl_meta["returncode"] == 0 and md_meta["returncode"] == 0,
        {"jsonl": jl_meta["returncode"], "md": md_meta["returncode"]})
if FIXED:
    c.check("B store: tied messages in append order (ids sort the other way)", store["list_messages"] == ["earlier", "hello", "reply"],
            store["list_messages"])
    c.check("B JSONL: created_at first; at the tie messages in append order, then events #2..#12 in insertion order; clock-ahead #1 last",
            B["jsonl_order"] == expected_b, B["jsonl_order"])
    c.check("B JSONL: primary created_at order is non-decreasing", stamps == sorted(stamps))
    c.check("B Markdown: same order (headings and message bodies)", headings == expected_headings and md_bodies == ["earlier", "hello", "reply"],
            {"headings": headings, "bodies": md_bodies})


# ---------------------------------------------------------------- C. tied messages + 14 tied events
for label, chars in (("ids_descending", "fc80"), ("ids_ascending", "08cf")):
    def write_c(s, sid, chars=chars):
        with clock(TIED_AT), patch(MESSAGE_UUID, side_effect=[SimpleNamespace(hex=ch * 32) for ch in chars]):
            for k in range(4):
                s.append_message(session_id=sid, role="inbound", body=f"m{k}")
                s.append_event(session_id=sid, event_type="test.tied", payload={"after": f"m{k}"})
            for k in range(10):
                s.append_event(session_id=sid, event_type="test.tied", payload={"tail": k})

    root, cfg, store = make_session(f"c-{label}", write_c)
    jl, jl_meta = export(root, cfg, "jsonl")
    md, md_meta = export(root, cfg, "md")
    md_msgs = [line for line in md.splitlines() if re.fullmatch(r"m\d", line)]
    C = {"message_ids": [ch * 4 + "..." for ch in chars], "store_list_messages": store["list_messages"],
         "store_list_events": store["list_events"], "jsonl_order": jsonl_order(jl), "markdown_message_bodies": md_msgs,
         "cli": {"jsonl": jl_meta, "md": md_meta}}
    observed[f"C_{label}"] = C
    expected_c = [*(f"m{k}" for k in range(4)), *(f"#{i}" for i in range(1, 15))]
    c.check(f"C ({label}) export CLI exit 0", jl_meta["returncode"] == 0 and md_meta["returncode"] == 0)
    if FIXED:
        c.check(f"C ({label}) JSONL: 4 tied messages in append order, then 14 tied events in insertion order",
                C["jsonl_order"] == expected_c, C["jsonl_order"])
        c.check(f"C ({label}) Markdown: tied messages in append order", md_msgs == ["m0", "m1", "m2", "m3"], md_msgs)

payload = {"banner": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items, "observed": observed}
write_result(OUT, payload)
print(json.dumps({"summary": c.summary(), "all_pass": c.all_pass}))
sys.exit(0 if c.all_pass else 1)
