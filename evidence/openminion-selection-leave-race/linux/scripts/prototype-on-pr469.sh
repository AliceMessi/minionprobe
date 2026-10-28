# The prototype fix commit cherry-picked onto PR #469 head c00bb452 (with the probe commit):
# the probe, three runs; #469's focused tests and the PostgreSQL file.
probe_runs prototype-on-pr469
sec "[prototype-on-pr469] #469's focused SQLite tests: tests/test_session_store.py -k 'active_agent or selection or leave or legacy or blank or join'"
pyt tests/test_session_store.py -k "active_agent or selection or leave or legacy or blank or join"
sec "[prototype-on-pr469] #469's PostgreSQL tests"
pyt -m postgres tests/integration/test_runtime_storage_postgres.py -k "room_active_agent_on_two_connections or room_join_activation_rules"
sec "[prototype-on-pr469] the PostgreSQL file"; pyt -m postgres tests/integration/test_runtime_storage_postgres.py
