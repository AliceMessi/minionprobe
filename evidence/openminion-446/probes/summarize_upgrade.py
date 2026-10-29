"""Summarize probe_446_upgrade.py results: one row per release and case, and the verdict per
listed history (the pre-upgrade layout), over every release that built it.

    python -I summarize_upgrade.py upgrade-0.0.1.json ... > upgrade-summary.txt
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def version_key(v: str) -> tuple:
    return tuple(int(p) for p in v.split("."))


def main() -> int:
    reports = [json.loads(Path(p).read_text(encoding="utf-8")) for p in sys.argv[1:]]
    reports.sort(key=lambda r: version_key(r["release"]))
    by_history: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    totals = defaultdict(int)
    print("release | cases built | PASS | REFUSED | FAIL | refused cases | failed cases")
    for r in reports:
        built = {c: e for c, e in r["cases"].items() if e.get("verdict") in ("PASS", "FAIL", "REFUSED")}
        fails = sorted(c for c, e in built.items() if e["verdict"] == "FAIL")
        refused = sorted(c for c, e in built.items() if e["verdict"] == "REFUSED")
        passes = len(built) - len(fails) - len(refused)
        totals["PASS"] += passes
        totals["FAIL"] += len(fails)
        totals["REFUSED"] += len(refused)
        print(f"{r['release']} | {len(built)} | {passes} | {len(refused)} | {len(fails)} | "
              f"{', '.join(refused) or '-'} | {', '.join(fails) or '-'}")
        for case, e in built.items():
            key = f"{case}: {e.get('before_listed') or 'NOT LISTED'}"
            by_history[key][e["verdict"]].append(r["release"])
    print(f"\ntotal: {totals['PASS']} PASS, {totals['REFUSED']} REFUSED, {totals['FAIL']} FAIL")
    print("\ncase: pre-upgrade listed history -> releases (PASS / FAIL)")
    for key in sorted(by_history):
        v = by_history[key]
        line = f"{key} -> PASS {v['PASS'][0]}..{v['PASS'][-1]} ({len(v['PASS'])})" if v["PASS"] else f"{key} -> PASS none"
        if v["REFUSED"]:
            line += f"; REFUSED {v['REFUSED'][0]}..{v['REFUSED'][-1]} ({len(v['REFUSED'])})"
        if v["FAIL"]:
            line += f"; FAIL {', '.join(v['FAIL'])}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
