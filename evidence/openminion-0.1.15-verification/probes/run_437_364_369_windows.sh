#!/usr/bin/env bash
# Native Windows runs (SQLite; no PostgreSQL server and no `postgres`/`acp` extras
# here) for #437, #364 and #369 on the installed openminion==0.1.15, one Python
# process at a time:
#   probe_437.py  the SQLite controls, on 0.1.15 and on the published 0.1.14
#                 (control-0.1.14/: the same identity and missing-handle results);
#   probe_364.py  --backend sqlite (the real room CLI and `openminion run`, parity);
#   probe_369.py  --backend sqlite (Focus over a real APIRuntime, parity; ACP and
#                 the pseudo-terminal Focus launch are not run here).
#
#   bash run_437_364_369_windows.sh <work_dir> <evidence_dir>
#
# <work_dir> holds venv (openminion==0.1.15 from PyPI) and venv0114
# (openminion==0.1.14 from PyPI), both CPython 3.12. Each probe runs with
# `python -I -X utf8` from an empty directory outside any checkout.
set -u
W="$1"; E="$2"; P="$E/probes"
export VERIFY_WORK="$(cygpath -w "$W")"
unset OPENMINION_HOME OPENMINION_DATA_ROOT OPENMINION_GENERATED_ROOT OPENMINION_CONFIG_PATH \
      OPENMINION_STORAGE_BACKEND OPENMINION_STORAGE_POSTGRES_URL OPENMINION_TEST_POSTGRES_URL PGURL
run() {  # run <issue> <venv> <out_dir> <probe> args...
  local n=$1 venv=$2 out=$3 probe=$4; shift 4
  local name="${probe%.py}"
  mkdir -p "$out" "$W/empty-$n"
  rm -rf "$W/work/$n-$venv"
  (cd "$W/empty-$n" && "$W/$venv/Scripts/python.exe" -I -X utf8 "$P/$probe" "$(cygpath -w "$W/work/$n-$venv")" \
    "$(cygpath -w "$out/$name.json")" "$@" > "$out/$name.log" 2>&1; echo "exit=$?" >> "$out/$name.log")
  echo "$venv $probe $* | $(tail -1 "$out/$name.log") | $(grep -E '^SUMMARY' "$out/$name.log")"
  echo "empty-$n after: $(ls -A "$W/empty-$n" | wc -l) entries"
}
O437="$E/windows/437"; O364="$E/windows/364"; O369="$E/windows/369"
mkdir -p "$O437" "$O364" "$O369"
(cd "$P" && sha256sum _common.py probe_437_lib.py probe_437.py run_437_364_369_windows.sh) > "$O437/probe-sha256.txt"
(cd "$P" && sha256sum _common.py probe_437_lib.py probe_364.py probe_364_cli.py run_437_364_369_windows.sh) > "$O364/probe-sha256.txt"
(cd "$P" && sha256sum _common.py probe_437_lib.py probe_369.py run_437_364_369_windows.sh) > "$O369/probe-sha256.txt"
run 437 venv "$O437" probe_437.py
run 437 venv0114 "$O437/control-0.1.14" probe_437.py
run 364 venv "$O364" probe_364.py --backend sqlite
run 369 venv "$O369" probe_369.py --backend sqlite
