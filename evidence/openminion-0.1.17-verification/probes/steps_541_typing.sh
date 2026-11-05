#!/usr/bin/env bash
# #541 consumer typing check against an installed openminion (sourced by
# run_541_windows.sh and steps_541.sh; not run on its own).
#
#   typing_541 <python of a venv with openminion + mypy> <consumer files dir> <work dir> <out dir> <expect: fixed|control>
#
# As the ci.yml step "Verify installed wheel SDK and typing": the consumer files
# are copied into a new empty directory and checked there with
# `mypy --strict` (PYTHONPATH empty), so only the installed package is seen.
#   probe_541_sdk_consumer_valid.py    = tests/typing/fixtures/sdk_consumer_valid.py at v0.1.17 (blob 1c61dfb8)
#   probe_541_sdk_consumer_invalid.py  = tests/typing/fixtures/sdk_consumer_invalid.py at v0.1.17 (blob a76fc787)
#   probe_541_typing_example.py        = the published docs example with assert_type lines
#   probe_541_typing_invalid_thread.py = thread_id=1, one expected error
# fixed: valid files no issues; each invalid file exactly one error, the expected one.
# control: the result is recorded (0.1.16 has no thread_id), and the invalid fixture still gives its error.
typing_541() {
  local py=$1 src=$2 wk=$3 o=$4 expect=$5 fails=0 out st n
  rm -rf "$wk"; mkdir -p "$wk/consumer" "$o"
  cp "$src"/probe_541_sdk_consumer_valid.py "$src"/probe_541_sdk_consumer_invalid.py \
     "$src"/probe_541_typing_example.py "$src"/probe_541_typing_invalid_thread.py "$wk/consumer/"
  (
    cd "$wk/consumer" || exit 9
    {
      echo "# cwd: <consumer dir> ($(ls -A | wc -l) files, all copied in: $(ls -A | tr '\n' ' '))"
      PYTHONPATH='' "$py" -I -c "import openminion,sys;print('openminion', openminion.__version__, openminion.__file__.replace(sys.prefix,'<VENV>'))"
      PYTHONPATH='' "$py" -I -m mypy --version
    } > "$o/typing-env.txt" 2>&1
    for f in probe_541_sdk_consumer_valid.py probe_541_typing_example.py probe_541_sdk_consumer_invalid.py probe_541_typing_invalid_thread.py; do
      out="$(PYTHONPATH='' "$py" -I -m mypy --strict --show-error-codes --cache-dir "$wk/mypy-cache" "$f" 2>&1)"
      st=$?
      printf '$ mypy --strict --show-error-codes %s\n%s\nexit=%s\n\n' "$f" "$out" "$st" >> "$o/typing.txt"
      n="$(printf '%s\n' "$out" | grep -c ': error:')"
      case "$expect:$f" in
        fixed:probe_541_sdk_consumer_valid.py|fixed:probe_541_typing_example.py)
          if [[ $st -eq 0 && $n -eq 0 ]]; then echo "PASS typing: $f: no issues"; else echo "FAIL typing: $f: exit $st, $n errors"; fails=$((fails+1)); fi ;;
        *:probe_541_sdk_consumer_invalid.py)
          if [[ $st -eq 1 && $n -eq 1 ]] && printf '%s\n' "$out" | grep -qF 'Argument 1 to "run" of "Agent" has incompatible type "int"; expected "str"  [arg-type]'; then
            echo "PASS typing: $f: exit 1, its one expected error"; else echo "FAIL typing: $f: exit $st, $n errors"; fails=$((fails+1)); fi ;;
        fixed:probe_541_typing_invalid_thread.py)
          if [[ $st -eq 1 && $n -eq 1 ]] && printf '%s\n' "$out" | grep -qF 'Argument "thread_id" to "run" of "Agent" has incompatible type "int"; expected "str | None"  [arg-type]'; then
            echo "PASS typing: $f: exit 1, its one expected error"; else echo "FAIL typing: $f: exit $st, $n errors"; fails=$((fails+1)); fi ;;
        control:*)
          echo "CONTROL typing: $f: exit $st, $n errors: $(printf '%s\n' "$out" | grep ': error:' | sed 's/^[^:]*:[0-9]*: error: //' | head -3 | tr '\n' '|')" ;;
      esac
    done
    echo "SUMMARY typing failures=$fails"
    exit $fails
  ) | tee "$o/typing-summary.txt"
  return "${PIPESTATUS[0]}"
}
