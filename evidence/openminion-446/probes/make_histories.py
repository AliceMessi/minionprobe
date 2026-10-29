"""#446: turn the measured layouts (probe_446_histories.py output) into postgres_histories.json.

    python -I make_histories.py OUT.json histories-0.1.0.json ... histories-checkout.json

Every successful step of every owner becomes one listed history: its owner and ledger (runtime
storage: "v<k>" from the migrations rows; a module: "alembic <revision>" when it has an
alembic_version row, else "store <om_meta.schema_head>"), and its exact tables. The storage engine's
tables and the ledger tables are not part of a history. The same history measured from several
packages is listed once, with every package that produced it ("current" is the checkout).
Only the modules that `openminion storage migrate --backend postgres` runs are listed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ENGINE_TABLES = ["core_events", "core_sidecar_rows", "sidecar_ingest_log"]
LEDGER_TABLES = {"alembic_version", "om_meta"}
MODULES = {"secret", "session", "telemetry", "identity", "registry", "task", "skill", "controlplane",
           "policy", "compress", "retrieve", "artifact", "a2a", "memory"}


def version_key(label: str) -> tuple:
    if label == "current":
        return (10**6,)
    return tuple(int(p) for p in label.split("."))


def sig(table: dict) -> str:
    cols = ", ".join(f"{n} {t}{' NOT NULL' if nn else ''}" for n, t, nn in sorted(map(tuple, table["columns"])))
    return f"{cols}; pk({', '.join(sorted(table['pk']))})"


def main() -> int:
    out = Path(sys.argv[1])
    found: dict[tuple, set] = {}  # (owner, ledger, frozenset((table, sig))) -> labels
    runtime_versions: dict[str, set] = {}
    problems: list[str] = []
    for path in sys.argv[2:]:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        label = "current" if data["label"] == "checkout" else data["label"]
        for v, name in (data.get("runtime_versions") or {}).items():
            runtime_versions.setdefault(v, set()).add(name)
        for owner_key, steps in data["owners"].items():
            for step in steps:
                if not step["result"].startswith("ok") and "store: ok" not in step["result"]:
                    continue
                if "FAILED" in step["result"]:
                    continue
                layout = step["layout"] or {}
                schemas = [s for s, b in layout.items() if b["tables"]]
                if len(schemas) != 1:
                    problems.append(f"{label} {owner_key} {step['step']}: schemas {schemas}")
                    continue
                block = layout[schemas[0]]
                ledgers = block["ledgers"]
                if owner_key == "runtime":
                    owner = "runtime"
                    rows = ledgers.get("migrations") or []
                    ledger = f"v{len(rows)}"
                    if [r[0] for r in rows] != list(range(1, len(rows) + 1)):
                        problems.append(f"{label} runtime {step['step']}: ledger {rows}")
                        continue
                elif owner_key.startswith(("alembic:", "store:")):
                    meta = ledgers.get("om_meta") or {}
                    owner = meta.get("module_id")
                    if owner not in MODULES:
                        continue
                    alembic = ledgers.get("alembic_version")
                    if alembic is not None:
                        if len(alembic) != 1:
                            problems.append(f"{label} {owner_key} {step['step']}: alembic {alembic}")
                            continue
                        ledger = f"alembic {alembic[0]}"
                    else:
                        ledger = f"store {meta.get('schema_head', '')}"
                else:
                    continue
                tables = frozenset(
                    (t, sig(spec)) for t, spec in block["tables"].items()
                    if t not in ENGINE_TABLES and t not in LEDGER_TABLES
                )
                if owner != "runtime" and ledgers.get("migrations"):
                    problems.append(f"{label} {owner_key} {step['step']}: migrations rows {ledgers['migrations']}")
                found.setdefault((owner, ledger, tables), set()).add(label)
    names = {v: sorted(n) for v, n in runtime_versions.items()}
    for v, n in names.items():
        if len(n) != 1:
            problems.append(f"runtime v{v} has names {n}")
    all_sigs = sorted({(t, s) for (_, _, tables) in found for t, s in tables})
    ids: dict[tuple, str] = {}
    counter: dict[str, int] = {}
    for t, s in all_sigs:
        counter[t] = counter.get(t, 0) + 1
        ids[(t, s)] = f"{t}/{counter[t]}"

    def order(item):
        (owner, ledger, tables), labels = item
        o = 0 if owner == "runtime" else 1
        num = int(ledger[1:]) if owner == "runtime" else 0
        return (o, owner, num, ledger, sorted(ids[x] for x in tables))

    histories = []
    for (owner, ledger, tables), labels in sorted(found.items(), key=order):
        histories.append({
            "owner": owner,
            "ledger": ledger,
            "tables": sorted(ids[x] for x in tables),
            "measured": sorted(labels, key=version_key),
        })
    result = {
        "about": [
            "Supported PostgreSQL histories per owner (#446): measured, never inferred.",
            "Each history is one owner's ledger and its exact tables (columns, format_type, NOT NULL, primary key).",
            "measured: the published packages (installed from PyPI) and 'current' (this code) that created it.",
            "See docs/runtime-database-upgrades.md, 'PostgreSQL schemas per owner'.",
        ],
        "engine_tables": ENGINE_TABLES,
        "runtime_versions": {v: n[0] for v, n in sorted(names.items(), key=lambda kv: int(kv[0]))},
        "signatures": {ids[x]: x[1] for x in all_sigs},
        "histories": histories,
    }
    text = json.dumps(result, indent=1, ensure_ascii=True)
    # One history per line keeps the file reviewable.
    lines = []
    for h in histories:
        lines.append("  " + json.dumps(h, ensure_ascii=True))
    head = json.dumps({k: v for k, v in result.items() if k != "histories"}, indent=1, ensure_ascii=True)
    text = head[:-2] + ',\n "histories": [\n' + ",\n".join(lines) + "\n ]\n}\n"
    json.loads(text)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"histories {len(histories)}, signatures {len(all_sigs)}, owners "
          f"{sorted({h['owner'] for h in histories})}")
    for p in problems:
        print("PROBLEM", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
