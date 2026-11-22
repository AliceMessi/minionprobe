#!/usr/bin/env bash
# #717 on Linux: probe_717.py against openminion==0.1.24 (the release under test)
# and openminion==0.1.23 (negative control). Each wheel is downloaded from PyPI,
# its sha256 recorded and compared with the PyPI JSON digest, then installed
# from that file. Offline probe: every provider is a stub on 127.0.0.1.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
sha256sum "$B"/*.py "$B"/*.sh | sed "s#$B/##" | tee "$OUT/probes.sha256"
uname -a > "$OUT/os.txt"; cat /etc/os-release >> "$OUT/os.txt"
python -m pip install -q uv >/dev/null 2>&1
for v in 0.1.24 0.1.23; do
  V="$RUNNER_TEMP/venv-$v"; DL="$RUNNER_TEMP/dl-$v"; mkdir -p "$DL"
  uv venv -q --python 3.12 "$V"
  python -m pip download -q --no-deps --only-binary=:all: -d "$DL" --index-url https://pypi.org/simple "openminion==$v" >/dev/null 2>&1 || echo "download $v failed"
  W="$(ls "$DL"/openminion-$v-*.whl)"
  python -I -c "import hashlib,json,sys,urllib.request; f=sys.argv[1]; d=json.loads(urllib.request.urlopen('https://pypi.org/pypi/openminion/$v/json').read().decode('utf-8')); want=[u['digests']['sha256'] for u in d['urls'] if u['filename']==f.rsplit('/',1)[1]][0]; got=hashlib.sha256(open(f,'rb').read()).hexdigest(); print('$v wheel', f.rsplit('/',1)[1], 'sha256', got, 'matches PyPI' if got==want else 'DOES NOT MATCH PyPI '+want)" "$W" | tee -a "$OUT/wheel-sha256.txt"
  uv pip install -q --python "$V/bin/python" --index-url https://pypi.org/simple "$W" || echo "install $v failed"
  uv pip list --python "$V/bin/python" > "$OUT/pip-list-$v.txt" 2>/dev/null
  EMPTY="$RUNNER_TEMP/empty-$v"; mkdir -p "$EMPTY"
  echo "=== probe_717 on $v"
  (cd "$EMPTY" && VERIFY_WORK="$RUNNER_TEMP" timeout 900 "$V/bin/python" -B -I -X utf8 "$B/probe_717.py" "$RUNNER_TEMP/work-$v" "$OUT/probe_717_$v.json" > "$OUT/probe_717_$v.log" 2>&1)
  echo "exit=$?" | tee "$OUT/probe_717_$v.exit"
  grep -E "^SUMMARY|^PASS|^FAIL|^ERROR" "$OUT/probe_717_$v.log" | cut -c1-300
done
exit 0
