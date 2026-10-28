# #393 PR gates: the upstream postgres job, then the focused, PostgreSQL and related tests on the
# branch head and on dev sources (git checkout $DEV -- src), then the repo gates on the head.
F="tests/test_session_store.py"
FK="active_agent or selection or leave or legacy or blank or join"
PGX="tests/integration/test_runtime_storage_postgres.py"
R="tests/test_session_store.py tests/test_room_cli.py tests/test_turn_router.py tests/test_storage_migrations.py tests/test_run_status.py tests/session tests/storage tests/services tests/api tests/cli tests/runtime tests/integration"
RMARK="not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow"
pyt() { $P -m pytest -q -p no:cacheprovider -rfE --tb=short "$@" 2>&1 | grep -E "^FAILED|^ERROR|^E   |passed|failed|error" | grep -v "^E   *$" | sed -E 's/^(.{300}).*/\1 .../' | head -60; }
sec "[head] upstream postgres-memory-retrieval job, its command list from .github/workflows/ci.yml"
sed -n '/Run PostgreSQL memory, skill and runtime storage conformance/,/^  [a-z]/p' .github/workflows/ci.yml | grep -E "python -m pytest" | sed -E 's/^ +//' | while read -r line; do echo "\$ $line"; eval "${line/python/$P}" 2>&1 | tail -1; done
for tree in head dev; do
  if [ $tree = dev ]; then git checkout -q $DEV -- src; sec "dev sources from here on: git checkout $DEV -- src ($(git status --short | grep -v '^?? .deps' | wc -l) paths differ)"; fi
  sec "[$tree] openminion imported from: $($P -c 'import openminion; print(openminion.__file__)')"
  sec "[$tree] pytest <focused>: $F -k \"$FK\" -v"; $P -m pytest -q -p no:cacheprovider -rfE --tb=short -v $F -k "$FK" 2>&1 | grep -E "PASSED|FAILED|ERROR|^E   |passed|failed" | grep -v "^E   *$" | sed -E 's/ +\[ *[0-9]+%\]$//' | head -80
  sec "[$tree] pytest -m postgres <postgres> -v (the #393 tests)"; $P -m pytest -q -p no:cacheprovider -rfE --tb=short -v -m postgres $PGX -k "room_active_agent_on_two_connections or room_join_activation_rules" 2>&1 | grep -E "PASSED|FAILED|ERROR|^E   |passed|failed|deselected" | grep -v "^E   *$" | sed -E 's/ +\[ *[0-9]+%\]$//' | head -60
  sec "[$tree] pytest -m postgres <postgres> (the whole file)"; pyt -m postgres $PGX
  sec "[$tree] pytest -m \"<test-ci markers>\" <related>"; pyt -m "$RMARK" $R
done
git checkout -q HEAD -- src; sec "back to the branch head: $(git status --short | grep -v '^?? .deps' | wc -l) paths differ"
sec "[head] the #393 tests 20 times each (SQLite and PostgreSQL)"
for i in $(seq 1 20); do $P -m pytest -q -p no:cacheprovider -rfE --tb=short $F -k "stored_during_it or waits_and_then or two_connections or activate_no_agent or blank_active" 2>&1 | tail -1; done | sed -E 's/ in [0-9.]+s.*//' | sort | uniq -c
for i in $(seq 1 20); do $P -m pytest -q -p no:cacheprovider -rfE --tb=short -m postgres $PGX -k "room_active_agent_on_two_connections or room_join_activation_rules" 2>&1 | tail -1; done | sed -E 's/ in [0-9.]+s.*//' | sort | uniq -c
sec "ruff check ."; .venv/bin/ruff check . 2>&1 | tail -3
sec "make format-check"; make format-check 2>&1 | tail -2
sec "make workflow-check"; make workflow-check 2>&1 | tail -4
sec "make lint"; make lint 2>&1 | tail -40; echo "make lint exit: ${PIPESTATUS[0]}"
sec "make test-critical"; make test-critical 2>&1 | tail -3
