#!/usr/bin/env bash
# #745 on Linux: probe_745.py against openminion==0.1.23 (the release under test)
# and openminion==0.1.21 (negative control), each from PyPI, each with its own
# published README (the PyPI project description of that version). Offline: the
# provider is a scripted OpenAI-compatible server on 127.0.0.1.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
sha256sum "$B"/*.py "$B"/*.sh | sed "s#$B/##" | tee "$OUT/probes.sha256"
uname -a > "$OUT/os.txt"; cat /etc/os-release >> "$OUT/os.txt"
python -m pip install -q uv >/dev/null 2>&1
for v in 0.1.23 0.1.21; do
  V="$RUNNER_TEMP/venv-$v"
  uv venv -q --python 3.12 "$V"
  uv pip install -q --python "$V/bin/python" --index-url https://pypi.org/simple "openminion==$v" || echo "install $v failed"
  uv pip list --python "$V/bin/python" > "$OUT/pip-list-$v.txt" 2>/dev/null
  "$V/bin/python" -I -c "import json,sys,urllib.request,hashlib; d=json.loads(urllib.request.urlopen('https://pypi.org/pypi/openminion/$v/json').read().decode('utf-8')); t=d['info']['description']; open(sys.argv[1],'w',encoding='utf-8',newline='').write(t); print('$v description sha256', hashlib.sha256(t.encode('utf-8')).hexdigest())" "$RUNNER_TEMP/readme-$v.md" | tee -a "$OUT/readme-sha256.txt"
  EMPTY="$RUNNER_TEMP/empty-$v"; mkdir -p "$EMPTY"
  echo "=== probe_745 on $v"
  (cd "$EMPTY" && timeout 900 "$V/bin/python" -B -I -X utf8 "$B/probe_745.py" "$RUNNER_TEMP/work-$v" "$OUT/probe_745_$v.json" "$RUNNER_TEMP/readme-$v.md" > "$OUT/probe_745_$v.log" 2>&1)
  echo "exit=$?" | tee "$OUT/probe_745_$v.exit"
  grep -E "^SUMMARY|^PASS|^FAIL" "$OUT/probe_745_$v.log" | cut -c1-300
done
exit 0
