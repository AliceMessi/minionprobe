# #369 on the runner (sourced by linux_setup.sh from an empty directory).
# `openminion acp` needs the `acp` extra, so this installs openminion[postgres,acp]
# ==0.1.15 and ==0.1.14 from PyPI into two more fresh venvs, records their
# provenance, and runs probe_369.py: PostgreSQL 16 and, for parity, SQLite on
# 0.1.15; PostgreSQL on 0.1.14 as the negative control.
set +e
for v in 0.1.15 0.1.14; do
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-acp-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-acp-$v/bin/python" --index-url https://pypi.org/simple "openminion[postgres,acp]==$v"
  uv pip show --python "$RUNNER_TEMP/venv-acp-$v/bin/python" openminion agent-client-protocol > "$OUT/pip-show-acp-$v.txt"
  "$RUNNER_TEMP/venv-acp-$v/bin/python" -I "$B/provenance.py" "$RUNNER_TEMP/dl-acp-$v" "$OUT/provenance-linux-acp-$v.json" "$v" > "$OUT/provenance-linux-acp-$v.log" 2>&1
  echo "provenance acp $v: $(grep -c '"wheel_sha256_matches_pypi": true' "$OUT/provenance-linux-acp-$v.json") match"
done
PYA="$RUNNER_TEMP/venv-acp-0.1.15/bin/python"
PYA0114="$RUNNER_TEMP/venv-acp-0.1.14/bin/python"
run369() {  # run369 <name> <python> <probe> args...
  local name=$1 py=$2 probe=$3; shift 3
  echo "=== $name"
  "$py" -I -X utf8 "$B/$probe" "$RUNNER_TEMP/work/$name" "$OUT/$name.json" "$@" > "$OUT/$name.log" 2>&1
  echo "exit=$?" | tee -a "$OUT/$name.log"
  grep -E "^(SUMMARY|NOT RUN)" "$OUT/$name.log"
}
run369 probe_369_postgres "$PYA" probe_369.py --backend postgres
run369 probe_369_sqlite "$PYA" probe_369.py --backend sqlite
run369 probe_369_postgres_0114 "$PYA0114" probe_369.py --backend postgres
