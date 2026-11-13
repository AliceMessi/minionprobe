#!/usr/bin/env bash
# Four installed-package probes against openminion==0.1.21 from PyPI, each run in
# isolated mode from an empty directory. Offline: every provider is a scripted
# OpenAI-compatible server on 127.0.0.1.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL
sha256sum "$B"/*.py "$B"/*.sh | sed "s#$B/##" | tee "$OUT/probes.sha256"
python -m pip install -q uv >/dev/null 2>&1
V="$RUNNER_TEMP/venv"
uv venv -q --python 3.12 "$V"
uv pip install -q --python "$V/bin/python" "openminion==0.1.21" || echo "install failed"
uv pip list --python "$V/bin/python" > "$OUT/pip-list.txt" 2>/dev/null
for probe in probe_tool_scope probe_fact_extraction probe_retry_after probe_output_type; do
  EMPTY="$RUNNER_TEMP/empty-$probe"; mkdir -p "$EMPTY"
  echo "=== $probe"
  (cd "$EMPTY" && timeout 900 "$V/bin/python" -B -I -X utf8 "$B/$probe.py" "$RUNNER_TEMP/work-$probe" "$OUT/$probe.json" > "$OUT/$probe.log" 2>&1)
  echo "exit=$?"
  grep -E "^SUMMARY|^CLAIMS|^PASS|^FAIL|^EXPECT|^CLAIM" "$OUT/$probe.log" | cut -c1-400
  tail -5 "$OUT/$probe.log" | cut -c1-400
done
exit 0
