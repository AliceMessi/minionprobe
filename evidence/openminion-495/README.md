# OpenMinion #495: a room selection checked and written under the session-row lock, the fix

Validation of the PR for [openminion#495](https://github.com/openminion/openminion/issues/495), as proposed in the issue and approved ([comment](https://github.com/openminion/openminion/issues/495#issuecomment-6485460713)). Branch `fix/495-selection-leave-lock` on `AliceMessi/openminion`, three commits on `dev` @ `37fa75ff`:
- `f0764cbe` test: the two race orders and a legacy-rules control as shared scenarios, on SQLite and PostgreSQL; #469's one-after-another scenario also checks a refused selection of the agent that left;
- `6d9cfe3c` fix: `set_active_agent` ensures the legacy owner record first, then, in one transaction, takes `_lock_session_row`, reads the agent's membership, applies the legacy rules and writes the active agent;
- `c3734660` docs: the CHANGELOG entry under Unreleased.

`set_active_agent` checked the agent's membership and wrote `active_agent_id` in separate autocommit statements, outside the session-row lock that joins and leaves take, so a leave of the same agent on another connection could commit between them and the selection stored an agent that had left. The report and its evidence are in [`openminion-selection-leave-race`](../openminion-selection-leave-race).

## Trees

| Name | Commit | What it is |
|---|---|---|
| `head` | `AliceMessi/openminion@c3734660` | the PR head |
| `dev` sources | `openminion/openminion@37fa75ff` | the head's checkout after `git checkout 37fa75ff -- src`: `dev`'s source with the head's tests. Only `participants.py` differs, and the log prints `src identical to dev: yes`. The test commit `f0764cbe` is this same tree: it changes only the three test files |

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `probes/probe_selection_leave_race.py`: the report's probe, unchanged (sha256 `4897efb8...`, the same as in the report's evidence). It opens three runtime storages on one SQLite file and on one PostgreSQL schema and runs the two held cases and three controls.
- `linux/runner-gates.sh`: the runner script. `linux/build_dispatch.py` wraps it with a line that writes the probe to `/tmp/probe/` and encodes both as the one command passed to `scripts/hackathon-kit/linux-runner/run.py --preset openminion --postgres`.
- `linux/gates-run-40815025419.txt`: its output, [run 40815025419](https://github.com/AliceMessi/linux-runner/actions/runs/40815025419) on `c3734660`. `linux/setup-run-40815025419.txt` is the job's setup log (a fresh clone, `make dev-install` with `.deps/openminion-eval` from `dev`, the `postgres` extra); `linux/head-run-40815025419.txt` and `linux/result-run-40815025419.txt` are the checked-out head and the job's exit status.

Environment: GitHub-hosted Ubuntu 24.04.5, CPython 3.11.17, SQLite 3.45.1, PostgreSQL 16.15 (`postgres:16` service), psycopg 3.3.6, SQLAlchemy 2.1.6, pytest 8.4.2, ruff 0.15.22.

The steps, on the head and then on `dev` sources:
1. the report's probe, three runs;
2. the new tests on SQLite and on PostgreSQL (`-v`);
3. `tests/test_session_store.py -k "active_agent or selection or leave or legacy or blank or join"`, the selection #469's PR used for its room tests, which now also picks up the three new SQLite tests;
4. the whole PostgreSQL file, `-m postgres tests/integration/test_runtime_storage_postgres.py`;
5. `pytest -q -m "<test-ci markers>" <related>`.

On the head only: the new tests 10 times on each backend, the upstream `postgres-memory-retrieval` job's eight commands read from `.github/workflows/ci.yml`, `ruff check .`, `make format-check` and `make lint`.

`<related>` = `tests/test_session_store.py tests/test_room_cli.py tests/test_turn_router.py tests/cli/interactive/test_openminion_runtime.py tests/services/runtime/test_session_profile_binding.py tests/api/routes/test_session_continuations.py tests/session tests/storage` (the session store, the room CLI, the turn router, the other `set_active_agent` callers' tests, `tests/session`, `tests/storage`).
`<test-ci markers>` = `not benchmark and not e2e and not postgres and not memory_eval_benchmark and not mcp_live and not package_integration and not telegram_live and not slack_live and not slow`, as in `make test-ci`.

## Results

The report's probe, three runs per tree, SQLite and PostgreSQL. Each run gave the same output on each backend:

| Case | `dev` sources | head |
|---|---|---|
| 1. selection held before its write, leave on the other storage | the leave returned during the hold; A returned `'beta'`; agents `['alpha']`, stored `'beta'`, `get_active_agent` `'beta'`: FAIL | the leave did not return during the hold; A returned `'beta'`, then the leave ran its fallback: agents `['alpha']`, stored `'alpha'`: OK |
| 2. leave held before its fallback, selection on the other storage | A did not return during the hold but sent its write during it; after the leave returned, A returned `'beta'`; agents `['alpha']`, stored `'beta'`, `get_active_agent` `'beta'`: FAIL | A did not return during the hold and did not send its write during it; after the leave returned, A raised `ValueError: Agent 'beta' is not an active participant`; agents `['alpha']`, stored `'alpha'`: OK |
| 3-5. controls | OK | OK, the same values |
| probe exit status | 1 (`SQLite case 1`, `SQLite case 2`, `PostgreSQL case 1`, `PostgreSQL case 2`) | 0 |

Tests:

| | head | `dev` sources |
|---|---|---|
| new tests, SQLite (4: the two races, the legacy rules, #469's extended one-after-another) | 4 passed | 2 failed, 2 passed |
| new tests, PostgreSQL (the same 4) | 4 passed | 2 failed, 2 passed |
| `tests/test_session_store.py -k "active_agent or selection or leave or legacy or blank or join"` | 20 passed | 2 failed, 18 passed |
| the PostgreSQL file | 49 passed | 2 failed, 47 passed |
| `-m "<test-ci markers>" <related>` | 1 failed, 701 passed, 3 skipped | 3 failed, 699 passed, 3 skipped |
| new tests, 10 runs on each backend | 10 x 4 passed on SQLite, 10 x 4 passed on PostgreSQL | |

The 2 failures on `dev` sources are the two race tests, on each backend, each with `AssertionError: ('beta', ['alpha'], 'beta')`: what the selection returned, the agents listed and the stored active agent. Expected `('beta', ['alpha'], 'alpha')` when the leave comes during the selection, and `('refused', ['alpha'], 'alpha')` when the selection comes during the leave.

The one failure of the related slice on both trees is `tests/storage/test_startup_validation.py::TestValidatePostgresConnectionFailure::test_unreachable_db_raises_with_redacted_url`: with the `postgres` extra installed, as on this runner, it hits the 60 s pytest timeout connecting to `192.0.2.1`. It does the same on `dev`.

Repo gates on the head:
- the upstream `postgres-memory-retrieval` job's eight commands: 19, 1, 7, 6, 49, 8, 1 and 13 passed;
- `ruff check .`: all checks passed;
- `make format-check`: 5155 files already formatted;
- `make lint`: passed, mypy ratchet 4306 / 4306.

## Rerun

From a clone of `AliceMessi/openminion` at `fix/495-selection-leave-lock`, with `make dev-install`, the `postgres` extra and `OPENMINION_TEST_POSTGRES_URL` set:

```
.venv/bin/python -m pytest -q tests/test_session_store.py -k "selection_waits_and_then_applies or selection_during_a_leave or legacy_session_keeps_its_selection_rules or joins_selection_and_leaves_on_two_connections"
.venv/bin/python -m pytest -q -m postgres tests/integration/test_runtime_storage_postgres.py -k "leave-during-selection or selection-during-leave or one-after-another or legacy_session_keeps_its_selection_rules"
PYTHONPATH=src .venv/bin/python <this folder>/probes/probe_selection_leave_race.py
git checkout 37fa75ff -- src   # dev sources; the same commands then fail as above
git checkout HEAD -- src
```

Or the whole script on the runner: `python -I linux/build_dispatch.py <this folder> <out>`, then `run.py --repo AliceMessi/openminion --ref c3734660a9fa854df42c8050ad6fe86b6b870e45 --preset openminion --postgres -- "$(cat <out>/dispatched.cmd)"`.
