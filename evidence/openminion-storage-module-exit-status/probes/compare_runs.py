"""Compare two probe_storage_exit.py result files case by case.

Usage: python -I -X utf8 compare_runs.py BEFORE.json AFTER.json

For each case: the module exit status before/after, main()'s result, and
whether the module's stdout and stderr are byte-identical once the package
directory is written as <PKG>. When stderr differs, the differing lines are
printed.
"""

import difflib
import json
import sys


def load(path):
    data = json.load(open(path, encoding="utf-8"))
    pkg = data["header"]["openminion_package"]
    return data["header"], {r["case"]: r for r in data["results"]}, pkg


def norm(text, pkg):
    return text.replace(pkg, "<PKG>")


before_header, before, before_pkg = load(sys.argv[1])
after_header, after, after_pkg = load(sys.argv[2])
print(f"before: {before_header['label']} (__main__.py sha256 {before_header['storage___main___py_sha256'][:12]})")
print(f"after:  {after_header['label']} (__main__.py sha256 {after_header['storage___main___py_sha256'][:12]})")
print(f"storage/cli.py sha256 identical: {before_header['storage_cli_py_sha256'] == after_header['storage_cli_py_sha256']}")
for name, b in before.items():
    a = after[name]
    bm, am = b["runs"]["module"], a["runs"]["module"]
    same_out = norm(bm["stdout"], before_pkg) == norm(am["stdout"], after_pkg)
    same_err = norm(bm["stderr"], before_pkg) == norm(am["stderr"], after_pkg)
    print()
    print(f"- {name}: main() {b['main_result']} / {a['main_result']}; "
          f"module exit {bm['exit']} -> {am['exit']}; stdout identical: {same_out}; stderr identical: {same_err}")
    if not same_err:
        diff = difflib.unified_diff(
            norm(bm["stderr"], before_pkg).splitlines(),
            norm(am["stderr"], after_pkg).splitlines(),
            lineterm="", n=0,
        )
        for line in list(diff)[2:]:
            print("    " + line)
