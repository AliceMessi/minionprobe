"""#352: compare probe outputs.

  diff     <out.json> <a.json> <b.json>           probe_352_diff outputs of 0.1.10 (a) and 0.1.11 (b):
                                                  every reader result of every session must be identical.
  readers  <out.json> <check.json> <check.json>... probe_352_check outputs (fresh and upgraded databases
                                                  holding the same history): reader-output and event-content
                                                  digests must be identical.
  digest   <out.json> <history.json> <history.json> probe_352_history outputs (the 0.1.10 writer's record and
                                                  the pre-upgrade backup read back with 0.1.10): identical
                                                  sha256 of every stored event row, ledger at 11.

Usage: python -I probe_352_compare.py diff|readers|digest <out.json> <file> <file> [...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, write_result  # noqa: E402

MODE, OUT, FILES = sys.argv[1], Path(sys.argv[2]), [Path(p) for p in sys.argv[3:]]
info = banner("probe_352_compare", mode=MODE, files=[p.name for p in FILES])
docs = [json.loads(p.read_text(encoding="utf-8")) for p in FILES]
c = Checks()
result: dict = {"info": info, "versions": [d["info"]["openminion.__version__"] for d in docs]}

if MODE == "diff":
    a, b = docs
    compared = differing = 0
    first_diffs = []
    for ra, rb in zip(a["results"], b["results"]):
        for i, (sa, sb) in enumerate(zip(ra["sessions"], rb["sessions"])):
            for key in sa:
                if isinstance(sa[key], dict):
                    for k in sa[key]:
                        compared += 1
                        if sa[key][k] != sb[key].get(k):
                            differing += 1
                            if len(first_diffs) < 5:
                                first_diffs.append({"seed": ra["seed"], "session": i, "reader": key, "key": k,
                                                    "a": sa[key][k], "b": sb[key].get(k)})
                else:
                    compared += 1
                    if sa[key] != sb[key]:
                        differing += 1
                        if len(first_diffs) < 5:
                            first_diffs.append({"seed": ra["seed"], "session": i, "reader": key, "a": sa[key][:3], "b": sb[key][:3]})
    same_shape = [len(r["sessions"]) for r in a["results"]] == [len(r["sessions"]) for r in b["results"]]
    events = [r["events"] for r in a["results"]], [r["events"] for r in b["results"]]
    nul = [r["postgres_nul_thread_request"] for r in a["results"]], [r["postgres_nul_thread_request"] for r in b["results"]]
    result.update({"compared_values": compared, "differing": differing, "first_differences": first_diffs,
                   "events_per_seed": events, "postgres_nul_thread_request": nul})
    c.check(f"{result['versions'][0]} vs {result['versions'][1]}: identical reader results for every session (runs, run events of 19 run ids, thread projections)",
            same_shape and events[0] == events[1] and compared > 0 and differing == 0,
            {"seeds": [r["seed"] for r in a["results"]], "sessions": sum(len(r["sessions"]) for r in a["results"]),
             "events": sum(events[0]), "compared_values": compared, "differing": differing})
    if any(v is not None for v in nul[0] + nul[1]):
        c.check("PostgreSQL: a thread request with NUL is rejected by both versions alike (outside the request contract)",
                all(x is not None and x != "no error" for x in nul[0] + nul[1])
                and [x.split(":")[0] for x in nul[0]] == [x.split(":")[0] for x in nul[1]],
                {"0.1.10": nul[0], "0.1.11": nul[1]})
elif MODE == "digest":
    digests = {p.name: d.get("events_digest_sha256") for p, d in zip(FILES, docs)}
    ledgers = {p.name: max(d.get("ledger") or [0]) for p, d in zip(FILES, docs)}
    result.update({"events_digest": digests, "ledger_max": ledgers})
    c.check("pre-upgrade backup read back by 0.1.10: same sha256 of every event row as the 0.1.10 writer recorded, ledger 11",
            len(set(digests.values())) == 1 and None not in digests.values() and set(ledgers.values()) == {11},
            {"events_digest": digests, "ledger_max": ledgers})
else:
    readers = {p.name: d.get("readers_digest_sha256") for p, d in zip(FILES, docs)}
    content = {p.name: d.get("events_content_digest_sha256") for p, d in zip(FILES, docs)}
    result.update({"readers_digest": readers, "events_content_digest": content})
    c.check("same history: identical event content and identical reader outputs on the fresh and the upgraded databases",
            len(set(readers.values())) == 1 and None not in readers.values() and len(set(content.values())) == 1,
            {"readers_digest": readers, "events_content_digest": content})

result["checks"] = c.items
result["summary"] = c.summary()
write_result(OUT, result)
print("summary:", result["summary"])
