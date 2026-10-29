"""#446: keep the listed histories that are this code's own or verified by an installed upgrade.

    python -I filter_histories.py ALL.json OUT.json upgrade-0.0.1.json ... upgrade-0.1.15.json

ALL.json is make_histories.py's output (every layout every published release and this code built,
one revision at a time and through the module stores). A history is kept when:
- this code builds it ("current" in measured); or
- it is the layout a published release leaves (its Alembic head, a store alone, or a store after
  the Alembic head), and probe_446_upgrade.py opened that layout with the branch build, in place,
  for every release that left it, ended in one of this code's histories, and kept every row.
Kept historical histories get "upgraded_from": the releases whose installed upgrade was verified.
Everything else is dropped, so the branch refuses it as an unknown history.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    sigs = data["signatures"]
    verdicts: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    unmatched = []
    for path in sys.argv[3:]:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        release = report["release"]
        for case, entry in report["cases"].items():
            verdict = entry.get("verdict")
            if verdict not in ("PASS", "FAIL", "REFUSED"):
                continue
            label = entry.get("before_listed") or ""
            owner, _, ledger = label.replace(" (historical)", "").partition(" ")
            names = set(entry["before_tables"].get("public", [])) - {
                "alembic_version", "om_meta", "core_events", "core_sidecar_rows", "sidecar_ingest_log"}
            layout = entry.get("before_layout")
            matches = [
                i for i, h in enumerate(data["histories"])
                if h["owner"] == owner and h["ledger"] == ledger and release in h["measured"]
                and ({ref.split("/")[0]: sigs[ref] for ref in h["tables"]} == layout if layout is not None
                     else {ref.split("/")[0] for ref in h["tables"]} == names)
            ]
            if len(matches) != 1:
                unmatched.append(f"{release} {case}: {label} {sorted(names)} -> {len(matches)} matches")
                continue
            verdicts[matches[0]][verdict].append(release)
    kept, dropped = [], []
    for i, history in enumerate(data["histories"]):
        if "current" in history["measured"]:
            kept.append(history)
            continue
        v = verdicts.get(i, {})
        if v.get("PASS") and not v.get("FAIL"):
            releases = sorted(set(v["PASS"]), key=lambda r: tuple(int(p) for p in r.split(".")))
            kept.append({**history, "upgraded_from": releases})
        else:
            dropped.append((history, dict(v)))
    used = {ref for h in kept for ref in h["tables"]}
    result = {**data, "signatures": {k: s for k, s in sigs.items() if k in used}, "histories": kept}
    head = json.dumps({k: v for k, v in result.items() if k != "histories"}, indent=1, ensure_ascii=True)
    lines = ["  " + json.dumps(h, ensure_ascii=True) for h in kept]
    text = head[:-2] + ',\n "histories": [\n' + ",\n".join(lines) + "\n ]\n}\n"
    json.loads(text)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"kept {len(kept)} ({sum(1 for h in kept if 'upgraded_from' in h)} historical), "
          f"dropped {len(dropped)}, signatures {len(result['signatures'])}")
    for history, v in dropped:
        m = history["measured"]
        print(f"DROPPED {history['owner']} {history['ledger']} measured {m[0]}..{m[-1]} ({len(m)}) "
              f"tables {len(history['tables'])} upgrade {v or 'not a release end state'}")
    for line in unmatched:
        print("UNMATCHED", line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
