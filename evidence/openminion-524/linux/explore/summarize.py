"""Summarize the OBS / OBS4 lines of an exploration run log.

    python -I summarize.py <run log>

Lines before "DEVSRC" ran on the branch sources, lines after it on dev's
sources (git checkout aec2df9e -- src). Per scenario and turn: the reply,
the provider requests, the memory.write decisions, the new candidates
(type, title) and the new memory_records rows.
"""

import json
import sys

tree = "branch"
for line in open(sys.argv[1], encoding="utf-8"):
    if line.startswith("DEVSRC"):
        tree = "dev"
    prefix = "OBS4 " if line.startswith("OBS4 ") else "OBS " if line.startswith("OBS ") else ""
    if not prefix:
        continue
    o = json.loads(line[len(prefix):])
    name = o.get("scenario") or o.get("variant")
    handled = o.get("handler_calls", o.get("handled", []))
    print(f"\n[{tree}] {name}: approval callback calls {len(o.get('approvals', []))}, handler runs {len(handled)}")
    for t in o["turns"]:
        text = (t.get("text") or t.get("error") or "").splitlines()[0][:80]
        print(f"  turn {t.get('input', '')[:24]!r}: {t.get('run_state')} {text!r}")
        if "meta" in t:
            m = t["meta"]
            print(f"    thread {m.get('thread_decision_action')} ({m.get('thread_decision_reason')}), respond_kind {m.get('respond_kind')}")
        print(f"    memory.write decisions {[tuple(d[1:2] + d[4:5]) for d in t['decisions']]}")
        print(f"    new candidates {[tuple(c[:2]) for c in t['new_candidates']]}")
        print(f"    new records {[tuple(r) for r in t['new_records']]}")
