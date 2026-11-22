"""One-off edit of probe_446_0124.py for the second runner round (kept for the record)."""
import sys
from pathlib import Path

p = Path(sys.argv[1])
s = p.read_text(encoding="utf-8")

old = '''    c.check("collision: a module store's first start whose CREATE SCHEMA waits on a competitor's uncommitted "
            "CREATE SCHEMA of the same name (memory store, registry store), the competitor then committing "
            "(PostgreSQL raises 23505 on pg_namespace_nspname_index): the start raises SchemaOwnershipError "
            "\\"... is being initialized by another concurrent process ... Nothing was written\\"; the database is "
            "the starting one plus only the competitor's empty schema; no connection left",
            all(v["waited_on_competitor"] and v["result"][0] is False
                and v["result"][1].startswith("SchemaOwnershipError:") and COMPETING in v["result"][1]
                and L.NOTHING in v["result"][1] and v["only_competitor_schema"] and v["connections"] == 0
                for v in per.values()),'''
assert old in s, "store check"
new = '''    types = {"registry_store": ("SchemaOwnershipError:",),
             "memory_store": ("SchemaOwnershipError:", "MigrationRequiredError:")}
    c.check("collision: a module store's first start whose CREATE SCHEMA waits on a competitor's uncommitted "
            "CREATE SCHEMA of the same name (memory store, registry store), the competitor then committing "
            "(PostgreSQL raises 23505 on pg_namespace_nspname_index): the start fails with \\"... is being "
            "initialized by another concurrent process ... Nothing was written\\" (the registry store raises "
            "SchemaOwnershipError; the memory store raises its own MigrationRequiredError with that text); the "
            "database is the starting one plus only the competitor's empty schema; no connection left",
            all(v["waited_on_competitor"] and v["result"][0] is False
                and v["result"][1].startswith(types[k]) and COMPETING in v["result"][1]
                and L.NOTHING in v["result"][1] and v["only_competitor_schema"] and v["connections"] == 0
                for k, v in per.items()),'''
s = s.replace(old, new)

old = '''def collision_3(c: Checks, work: Path) -> None:
    """Free-running concurrent first starts, as in the 0.1.17 verification (6 rounds)."""
    outcomes: dict = {"registry_store": [], "memory_store": [], "storage migrate --module secret": []}
    later_ok, layouts_ok, others = [], [], []
    for rnd in range(6):'''
assert old in s, "rounds"
new = '''def raw_kind(message: str) -> str | None:
    """The raw database error a failure carries when it is not a refusal, by constraint."""
    if L.NOTHING in message:
        return None
    if "DuplicateSchema" in message:
        return "DuplicateSchema (namespace)"
    if "UniqueViolation" in message:
        if "pg_namespace_nspname_index" in message:
            return "UniqueViolation on pg_namespace_nspname_index (namespace)"
        if "pg_class_relname_nsp_index" in message:
            return "UniqueViolation on pg_class_relname_nsp_index (table or index name in the schema)"
        if "pg_type_typname_nsp_index" in message:
            return "UniqueViolation on pg_type_typname_nsp_index (table type in the schema)"
        return "UniqueViolation (other)"
    return "other: " + L.short(message, 160)


def collision_3(c: Checks, work: Path) -> None:
    """Free-running concurrent first starts, as in the 0.1.17 verification (6 rounds)."""
    outcomes: dict = {"registry_store": [], "memory_store": [], "storage migrate --module secret": []}
    later_ok, layouts_ok, others = [], [], []
    for rnd in range(int(os.environ.get("PROBE_446_ROUNDS", "6"))):'''
s = s.replace(old, new)

old = '''    observe("collision.free_running", {"outcomes": summary, "kinds": kinds})
    c.check("collision: free-running concurrent first starts (6 rounds, each a new database: 3 registry stores, 3 "
            "memory stores and 3 `storage migrate --module secret` started together): every failure is a refusal "
            "with \\"Nothing was written\\" (no raw UniqueViolation or DuplicateSchema)",
            all(L.NOTHING in m for m in failures), {"kinds": kinds, "outcomes": summary})'''
assert old in s, "free-running check"
new = '''    raw = [k for k in (raw_kind(m) for m in failures) if k]
    kinds["raw errors by constraint"] = {k: raw.count(k) for k in sorted(set(raw))}
    observe("collision.free_running", {"outcomes": summary, "kinds": kinds})
    c.check("collision: free-running concurrent first starts (6 rounds, each a new database: 3 registry stores, 3 "
            "memory stores and 3 `storage migrate --module secret` started together): no failure is a raw namespace "
            "collision (no UniqueViolation on pg_namespace_nspname_index, no DuplicateSchema)",
            not any("(namespace)" in k for k in raw), {"raw": kinds["raw errors by constraint"],
                                                       "attempts": kinds["attempts"]})
    c.check("collision: free-running concurrent first starts, the same rounds: every failure is a refusal with "
            "\\"Nothing was written\\" (no raw database error of any kind)",
            all(L.NOTHING in m for m in failures), {"kinds": kinds, "outcomes": summary})'''
s = s.replace(old, new)

old = '''# ---------------------------------------------------------------------------- report
'''
assert old in s, "report marker"
new = '''def collision_4(c: Checks, work: Path) -> None:
    """Registry stores only, more rounds (PROBE_446_REG_ROUNDS, 0 = skip): how often a first start fails
    with a raw error after the schema exists (the storage engine's tables and indexes)."""
    rounds = int(os.environ.get("PROBE_446_REG_ROUNDS", "0"))
    if not rounds:
        return
    results, later_ok, layouts_ok = [], [], []
    for rnd in range(rounds):
        u = L.new_db(f"c_reg_{rnd}")
        start_at = time.time() + 2.5
        procs = [spawn(sys.executable, "concurrent_store", "registry_store", u,
                       work / f"reg{rnd}-{i}", start_at, root=work / f"reg{rnd}-cwd") for i in range(4)]
        results.extend(child_result(p) for p in procs)
        later_ok.append(L.attempt(lambda u=u, rnd=rnd: L.open_close("registry_store", u, work / f"reg{rnd}-later"))[0])
        layouts_ok.append(bool(H.match("registry", u, "public__registry")))
        L.drop_db(L.dbname(u))
        L.CREATED.remove(L.dbname(u))
    failures = [r["message"] for r in results if not r["ok"]]
    raw = [k for k in (raw_kind(m) for m in failures) if k]
    summary = {"rounds": rounds, "attempts": len(results), "ok": len(results) - len(failures),
               "refused (Nothing was written)": sum(1 for m in failures if L.NOTHING in m),
               "raw errors by constraint": {k: raw.count(k) for k in sorted(set(raw))},
               "raw examples": [L.short(m, 400) for m in failures if L.NOTHING not in m][:3],
               "later start ok": sum(later_ok), "listed layouts": sum(layouts_ok)}
    observe("collision.registry_only", summary)
    c.check(f"collision: registry stores only ({rounds} rounds of 4 first starts together, each a new database): "
            "every failure is a refusal with \\"Nothing was written\\"; a later start succeeds and the schema holds "
            "a listed current history in every round",
            not raw and all(later_ok) and all(layouts_ok), summary)


# ---------------------------------------------------------------------------- report
'''
s = s.replace(old, new, 1)
s = s.replace("import json\nimport subprocess", "import json\nimport os\nimport subprocess", 1)
p.write_bytes(s.encode("utf-8"))
print("patched")
