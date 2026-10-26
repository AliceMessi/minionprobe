"""#408 (test-only): does the v0.1.12 release-tag source contain #414's changes?

    python -I check_408_source.py <git_dir> <out.json>

<git_dir> is a clone of openminion/openminion with the tags v0.1.11 and v0.1.12
and the #414 merge commit a41487ab fetched. Three checks, on the test files
only (the change has no package code):

1. Ancestry: the #414 merge commit and its reviewed head d69a9dc5 are
   ancestors of the v0.1.12 tag commit.
2. Lines: every line that #414's merge added (`git diff a41487ab^1 a41487ab`)
   is present in the same file at v0.1.12, and the file at the tag differs from
   the PR head only where later merges changed it (listed, with the diff).
3. AST of the twelve tests named in #408 and the helpers they use
   (`_init_db`, `_create_test_db`, the nested `fake_upgrade`), at v0.1.11 and
   at v0.1.12: `with` items that open `sqlite3.connect(...)` bare versus
   inside `closing(...)`, `APIRuntime.close()` in the runtime test, and the
   number of assertions in each test (so none was dropped).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

GIT, OUT = sys.argv[1], Path(sys.argv[2])
MERGE = "a41487ab1b51f7fd526ff29cd526f0b98dc01fc7"
HEAD = "d69a9dc55637992a63275eb9559063a48662d750"
TAGS = ("v0.1.11", "v0.1.12")
TESTS = {
    "tests/test_storage_migrations.py": [
        "StorageMigrationTests.test_migrate_database_creates_expected_schema",
        "StorageMigrationTests.test_migrate_database_is_idempotent_on_rerun",
    ],
    "tests/storage/test_migrations_runner.py": [
        "test_runner_rolls_back_on_failed_verify",
        "test_secret_backup_restore_round_trip_after_migrate",
    ],
    "tests/storage/test_migration_telemetry.py": [
        "test_migrate_with_verify_emits_failed_outcome_without_exception",
        "_init_db",
    ],
    "tests/storage/test_module_ops.py": [
        "TestStorageModuleOps.test_restore_from_backup",
        "TestStorageModuleOps.test_restore_envelope",
        "TestRollbackSafety.test_backup_then_restore_preserves_data",
        "TestRollbackSafety.test_snapshot_not_silently_deleted",
        "TestRollbackSafety.test_restore_is_idempotent",
        "TestE2ESmoke.test_full_lifecycle_envelope_mode",
        "_create_test_db",
    ],
    "tests/test_api_runtime.py": [
        "APIRuntimeBootstrapTests.test_runtime_bootstraps_storage_schema",
    ],
}


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", GIT, *args], capture_output=True, text=True, encoding="utf-8", check=False).stdout


def is_ancestor(rev: str, tag: str) -> bool:
    return subprocess.run(["git", "-C", GIT, "merge-base", "--is-ancestor", rev, f"{tag}^{{commit}}"]).returncode == 0


def find(tree: ast.Module, dotted: str):
    node: ast.AST = tree
    for part in dotted.split("."):
        node = next(
            n for n in ast.iter_child_nodes(node)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part
        )
    return node


def call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        return ast.unparse(node.func)
    return ""


def analyse(func: ast.AST) -> dict:
    bare, wrapped, asserts, closes = [], [], 0, []
    for node in ast.walk(func):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                expr = item.context_expr
                name = call_name(expr)
                if name in ("sqlite3.connect", "connect"):
                    bare.append(node.lineno)
                elif name == "closing" and expr.args and call_name(expr.args[0]) in ("sqlite3.connect", "connect"):
                    wrapped.append(node.lineno)
        if isinstance(node, ast.Assert):
            asserts += 1
        if isinstance(node, ast.Call):
            name = call_name(node)
            if name.startswith("self.assert") or name.startswith("pytest.raises"):
                asserts += 1
            if name.endswith(".close"):
                closes.append(name)
    return {
        "with_bare_sqlite3_connect_lines": bare,
        "with_closing_sqlite3_connect_lines": wrapped,
        "assertions": asserts,
        "close_calls": sorted(set(closes)),
    }


result: dict = {
    "merge_commit": MERGE,
    "pr_head": HEAD,
    "tag_commits": {t: git("rev-parse", f"{t}^{{commit}}").strip() for t in TAGS},
    "ancestry": {
        "merge_commit_in_v0.1.12": is_ancestor(MERGE, "v0.1.12"),
        "pr_head_in_v0.1.12": is_ancestor(HEAD, "v0.1.12"),
        "merge_commit_in_v0.1.11": is_ancestor(MERGE, "v0.1.11"),
    },
    "files": {},
}
all_ok = result["ancestry"]["merge_commit_in_v0.1.12"] and result["ancestry"]["pr_head_in_v0.1.12"]
for path, names in TESTS.items():
    added = [
        line[1:].strip()
        for line in git("diff", "--unified=0", f"{MERGE}^1", MERGE, "--", path).splitlines()
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
    ]
    tag_lines = {line.strip() for line in git("show", f"v0.1.12:{path}").splitlines()}
    missing = [line for line in added if line not in tag_lines]
    head_vs_tag = git("diff", "--unified=0", HEAD, "v0.1.12", "--", path)
    entry = {
        "lines_added_by_414": len(added),
        "added_lines_missing_at_v0.1.12": missing,
        "diff_pr_head_to_v0.1.12": [
            line for line in head_vs_tag.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ],
        "tests": {},
    }
    all_ok = all_ok and not missing
    trees = {t: ast.parse(git("show", f"{t}:{path}")) for t in TAGS}
    for name in names:
        per = {t: analyse(find(trees[t], name)) for t in TAGS}
        new, old = per["v0.1.12"], per["v0.1.11"]
        ok = (not new["with_bare_sqlite3_connect_lines"]) and new["assertions"] >= old["assertions"]
        if name.endswith("test_runtime_bootstraps_storage_schema"):
            ok = ok and "app.close" in new["close_calls"]
        per["closed_at_v0.1.12_and_assertions_kept"] = ok
        entry["tests"][name] = per
        all_ok = all_ok and ok
    result["files"][path] = entry
result["all_checks_pass"] = all_ok
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
for path, entry in result["files"].items():
    print(path, "| #414 lines:", entry["lines_added_by_414"], "missing at tag:", len(entry["added_lines_missing_at_v0.1.12"]),
          "| head->tag diff lines:", len(entry["diff_pr_head_to_v0.1.12"]))
    for name, per in entry["tests"].items():
        print("   ", name, "| v0.1.11 bare/closing/asserts:",
              len(per["v0.1.11"]["with_bare_sqlite3_connect_lines"]), len(per["v0.1.11"]["with_closing_sqlite3_connect_lines"]),
              per["v0.1.11"]["assertions"], "| v0.1.12:", len(per["v0.1.12"]["with_bare_sqlite3_connect_lines"]),
              len(per["v0.1.12"]["with_closing_sqlite3_connect_lines"]), per["v0.1.12"]["assertions"],
              "| ok" if per["closed_at_v0.1.12_and_assertions_kept"] else "| NOT OK")
print("ancestry:", result["ancestry"])
print("SUMMARY all_checks_pass=", all_ok)
sys.exit(0 if all_ok else 1)
