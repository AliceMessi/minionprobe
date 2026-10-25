#!/bin/bash
# PostgreSQL plans of the two run readers at a larger size, branch head and dev sources (#396).
set -u
EV=/tmp/ev
PY=.venv/bin/python3.11
DEV=b670bbb6
echo "head: $(git log --oneline -1)"; echo "dev:  $(git log --oneline -1 $DEV)"
echo "$($PY -c 'import sys;print("CPython", sys.version.split()[0])'); $(grep PRETTY_NAME /etc/os-release)"
sha256sum $EV/*.py $EV/run396b.sh
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; echo; echo "== dev sources from here on: git checkout $DEV -- src"; fi
  for shape in "40 100" "8 500"; do
    echo; echo "== [$tree] plans_396_pg.py $shape"
    PYTHONPATH=src:. $PY $EV/plans_396_pg.py $shape 2>&1
  done
done
git checkout -q HEAD -- src
