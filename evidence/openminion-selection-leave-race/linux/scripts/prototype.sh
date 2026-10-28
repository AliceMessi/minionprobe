# Prototype (dev 39da8ffb + probe + tests + fix): the probe, the new tests, related tests, lint;
# then the new tests on dev sources (git checkout 39da8ffb -- src).
NEW_SQLITE=(tests/test_session_store.py -k "selection_held_before_its_write or selection_during_a_leave or selection_and_leave_one_after_another or legacy_session_selection")
NEW_PG=(-m postgres tests/integration/test_runtime_storage_postgres.py -k "selection_and_leave_on_two_connections or legacy_session_selection")
probe_runs prototype
sec "[prototype] new tests, SQLite"; pyt -v "${NEW_SQLITE[@]}"
sec "[prototype] new tests, PostgreSQL"; pyt -v "${NEW_PG[@]}"
sec "[prototype] new tests 20 times on each backend (last pytest line per run, counted)"
for i in $(seq 1 20); do $P -m pytest -q -p no:cacheprovider "${NEW_SQLITE[@]}" 2>&1 | tail -1 | sed 's/ in [0-9.]*s//'; done | sort | uniq -c | sed 's/^/SQLite: /'
for i in $(seq 1 20); do $P -m pytest -q -p no:cacheprovider "${NEW_PG[@]}" 2>&1 | tail -1 | sed 's/ in [0-9.]*s//'; done | sort | uniq -c | sed 's/^/PostgreSQL: /'
sec "[prototype] related tests: the session store, room CLI, turn router, set_active_agent callers, tests/session, tests/storage (one test deselected: it times out with the postgres extra installed, on dev too)"
pyt tests/test_session_store.py tests/test_room_cli.py tests/test_turn_router.py tests/cli/interactive/test_openminion_runtime.py tests/services/runtime/test_session_profile_binding.py tests/api/routes/test_session_continuations.py tests/session tests/storage --deselect tests/storage/test_startup_validation.py::TestValidatePostgresConnectionFailure::test_unreachable_db_raises_with_redacted_url
sec "[prototype] the PostgreSQL file: -m postgres tests/integration/test_runtime_storage_postgres.py"; pyt -m postgres tests/integration/test_runtime_storage_postgres.py
sec "[prototype] ruff check ."; $P -m ruff check . 2>&1 | tail -5
sec "[prototype] ruff format --check on the changed files"; $P -m ruff format --check src/openminion/modules/storage/runtime/session_store/participants.py tests/_room_participant_scenarios.py tests/test_session_store.py tests/integration/test_runtime_storage_postgres.py 2>&1 | tail -5
sec "[prototype] make lint"; make lint 2>&1 | tail -12; echo "make lint exit=${PIPESTATUS[0]}"
git checkout -q 39da8ffb -- src
sec "dev sources from here on: git checkout 39da8ffb -- src ($(git status --short -- src | wc -l) paths differ from the prototype)"
sec "[dev sources] new tests, SQLite"; pyt "${NEW_SQLITE[@]}"
sec "[dev sources] new tests, PostgreSQL"; pyt "${NEW_PG[@]}"
git checkout -q HEAD -- src
sec "back to the prototype: $(git status --short -- src | wc -l) paths differ"
