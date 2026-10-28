#!/bin/bash
# #448 PR: the full make test-ci on the branch head.
set -u
DEV="63ac9a17c3f1d926072a1d474c052ef1f01de1b4"
P=.venv/bin/python
sec() { echo; echo "== $*"; }
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|sophiagraph|typer|pydantic) ' | tr -s ' ' | tr '\n' ';')"
sec "make test-ci"; make test-ci 2>&1 | tail -60; echo "test-ci exit=${PIPESTATUS[0]}"
