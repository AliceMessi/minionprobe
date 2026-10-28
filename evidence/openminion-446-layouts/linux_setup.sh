#!/usr/bin/env bash
# #446 round 2: probe_446_round2.py on the published 0.1.14 and on the checked-out dev commit.
# The runner checks out openminion/openminion at the dev commit; this script and the probe are unpacked
# next to each other in $RUNNER_TEMP/p446. Each build gets a fresh uv venv and runs the probe with
# `python -I` from an empty directory against the job's PostgreSQL 16 service.
set -uo pipefail
B="$(cd "$(dirname "$0")" && pwd)"
OUT="$RUNNER_TEMP/logs/out"; mkdir -p "$OUT"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL PGOPTIONS
(cd "$B" && sha256sum probe_446_round2.py linux_setup.sh) | tee "$OUT/probe-sha256.txt"
git -C "$GITHUB_WORKSPACE" log -1 --format='%H %s' | tee "$OUT/dev-commit.txt"
docker exec pg psql -U postgres -Atc 'select version()' | tee "$OUT/postgres-version.txt"
(. /etc/os-release && echo "$PRETTY_NAME") | tee "$OUT/os.txt"
python -m pip install -q uv >/dev/null 2>&1
declare -A SPEC=( [v0114]="openminion[postgres]==0.1.14" [dev]="openminion[postgres] @ file://$GITHUB_WORKSPACE" )
for v in v0114 dev; do
  uv venv -q --python 3.12 "$RUNNER_TEMP/venv-$v"
  uv pip install -q --python "$RUNNER_TEMP/venv-$v/bin/python" "${SPEC[$v]}" || echo "install $v failed"
  uv pip freeze --python "$RUNNER_TEMP/venv-$v/bin/python" > "$OUT/pip-freeze-$v.txt" 2>/dev/null
done
for v in v0114 dev; do
  EMPTY="$RUNNER_TEMP/empty-$v"; mkdir -p "$EMPTY"
  echo "=== $v $("$RUNNER_TEMP/venv-$v/bin/python" -c 'import importlib.metadata as m; print(m.version("openminion"))')"
  (cd "$EMPTY" && "$RUNNER_TEMP/venv-$v/bin/python" -I -X utf8 "$B/probe_446_round2.py" "$OUT/probe_446_round2_$v.json" > "$OUT/probe_446_round2_$v.log" 2>&1)
  echo "exit=$?"
  grep -E '^(VERSIONS|MANIFEST|STEP|DECIDE|UNCHANGED|CLAIM|RELOCATE|FACT|ERROR) ' "$OUT/probe_446_round2_$v.log"
done
