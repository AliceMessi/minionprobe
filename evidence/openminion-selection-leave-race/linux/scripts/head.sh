# selection-leave-race: common header. BASE is the commit the checkout's probe commit sits on.
P=.venv/bin/python
PROBE=probe_tmp/probe_selection_leave_race.py
sec() { echo; echo "== $*"; }
echo "checkout: $(git log --oneline -1)"
echo "commits on top of $BASE:"; git log --oneline $BASE..HEAD
echo "files changed against $BASE:"; git diff --stat $BASE HEAD | sed 's/^/  /'
echo "$($P -c 'import sys,sqlite3;print("CPython", sys.version.split()[0], "SQLite", sqlite3.sqlite_version)'); $(grep PRETTY_NAME /etc/os-release)"
echo "packages: $($P -m pip list 2>/dev/null | grep -iE '^(sqlalchemy|psycopg|psycopg-binary) ' | tr -s ' ' | tr '\n' ';')"
echo "OPENMINION_TEST_POSTGRES_URL set: $([ -n "${OPENMINION_TEST_POSTGRES_URL:-}" ] && echo yes || echo no)"
mkdir -p /tmp/ev
probe_runs() {  # $1 = tree label; the probe from the checkout, three runs
  sec "[$1] probe sha256: $(sha256sum $PROBE)"
  sec "[$1] openminion imported from: $(PYTHONPATH=src $P -c 'import openminion; print(openminion.__file__)')"
  for run in 1 2 3; do
    sec "[$1] probe run $run"
    PYTHONPATH=src $P $PROBE 2> /tmp/ev/stderr-$1-$run.txt; echo "probe exit=$?"
    echo "(stderr: $(wc -l < /tmp/ev/stderr-$1-$run.txt) lines; $(grep -c 'has pending migrations' /tmp/ev/stderr-$1-$run.txt) of them are the PostgreSQL storage's 'has pending migrations' log warning; the others follow)"
    grep -v 'has pending migrations' /tmp/ev/stderr-$1-$run.txt | head -20
  done
}
pyt() { $P -m pytest -q -p no:cacheprovider "$@" 2>&1 | tail -25; echo "pytest exit=${PIPESTATUS[0]}"; }
