# dev 39da8ffb plus the probe commit: the prototype job's related-tests command, unchanged, as a control.
echo "src identical to 39da8ffb: $(git diff --quiet 39da8ffb HEAD -- src && echo yes || echo no)"
sec "[dev] related tests (the same command as in prototype.sh)"
pyt tests/test_session_store.py tests/test_room_cli.py tests/test_turn_router.py tests/cli/interactive/test_openminion_runtime.py tests/services/runtime/test_session_profile_binding.py tests/api/routes/test_session_continuations.py tests/session tests/storage --deselect tests/storage/test_startup_validation.py::TestValidatePostgresConnectionFailure::test_unreachable_db_raises_with_redacted_url
sec "[dev] the PostgreSQL file: -m postgres tests/integration/test_runtime_storage_postgres.py"; pyt -m postgres tests/integration/test_runtime_storage_postgres.py
