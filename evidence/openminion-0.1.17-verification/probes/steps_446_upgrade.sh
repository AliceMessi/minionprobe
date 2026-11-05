# #446 installed upgrade and restore, release by release (sourced by a steps_446_upgrade_<x>.sh that
# sets RELEASES). Each published release is installed from PyPI into its own venv (CPython 3.11, as in
# evidence/openminion-446) and builds every owner it can in `public` of a new database; the package
# under test ($PY, openminion[postgres]==0.1.17) then opens each one through its production entry
# points; the pre-upgrade dump is restored and reopened by the release. probe_446_upgrade.py is a byte
# copy of evidence/openminion-446/probes/probe_446_upgrade.py (its "branch build" is $PY here).
set +e
export OPENMINION_TEST_POSTGRES_URL="$PGURL"
"$PY" -I -c "import openminion, pathlib; p = pathlib.Path(openminion.__file__).parent; print(openminion.__version__, (p / 'modules/storage/migrations/postgres_histories.json').exists())" | tee "$OUT/upgrader.txt"
for v in $RELEASES; do
  V="$RUNNER_TEMP/venv-$v"
  uv venv -q --python 3.11 "$V"
  if ! uv pip install -q --python "$V/bin/python" --index-url https://pypi.org/simple "openminion[postgres]==$v" > "$OUT/install-$v.log" 2>&1; then
    echo "INSTALL FAILED $v"; continue
  fi
  uv pip freeze --python "$V/bin/python" > "$OUT/pip-freeze-$v.txt" 2>/dev/null
  (cd "$EMPTY" && "$PY" -I -X utf8 "$B/probe_446_upgrade.py" drive "$v" "$V/bin/python" "$OUT/upgrade-$v.json" > "$OUT/upgrade-$v.log" 2>&1)
  echo "release $v exit=$? $(grep '^DONE' "$OUT/upgrade-$v.log")"
  grep '^CASE' "$OUT/upgrade-$v.log" | grep -v '| PASS |' | cut -c1-400
  rm -rf "$V"
done
"$PY" -I "$B/probe_446_upgrade_summarize.py" "$OUT"/upgrade-*.json > "$OUT/upgrade-summary.txt" 2>&1
sed -n '/^total/p' "$OUT/upgrade-summary.txt"
echo "empty dir after: $(ls -A "$EMPTY" | wc -l) entries"
