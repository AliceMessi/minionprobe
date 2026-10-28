# OpenMinion: a room selection racing a leave of the agent it selects

Split out of [openminion#469](https://github.com/openminion/openminion/pull/469) (fix for [#393](https://github.com/openminion/openminion/issues/393)) at the maintainer's request: "the pre-existing `set_active_agent` versus leave race", "with the two-connection reproduction and the current-dev control".

`set_active_agent` (`src/openminion/modules/storage/runtime/session_store/participants.py:208-238` on `dev` @ `39da8ffb`) checks that the agent is an active participant (`get_participant`, `:215-229`), then writes `active_agent_id` with an unconditional `UPDATE` (`:230-234`). The check and the write are separate statements, outside any transaction, and do not take `_lock_session_row`, the session-row lock that joins and leaves take (#385). A leave of the same agent on another connection can commit between them. The selection then stores an agent that is no longer a participant, and `get_active_agent` returns it (`:197-198` returns the stored id when it is set).

## Trees and packages

| Name | What it is | Where it ran |
|---|---|---|
| `dev` | `openminion/openminion@39da8ffb`, plus one commit `ad954d5b` that adds only `probe_tmp/probe_selection_leave_race.py` (branch `wip/selection-leave-race-dev-probe` on AliceMessi/openminion) | Linux runner, SQLite and PostgreSQL |
| `#469 head` | `c00bb452` (PR #469's head), plus the same probe commit as `f825de74` (`wip/selection-leave-race-pr469-probe`) | Linux runner, SQLite and PostgreSQL |
| `0.1.14` | `openminion==0.1.14` installed from PyPI | native Windows, SQLite |
| `prototype` | `dev` + the probe commit + `9acd6f92` (tests) + `aaf32f9a` (the proposed fix), `wip/selection-leave-race-prototype` | Linux runner, SQLite and PostgreSQL |
| `prototype on #469` | `#469 head` + the probe commit + the fix commit cherry-picked as `c9a4b7a5` (no conflict), `wip/selection-leave-race-prototype-on-pr469` | Linux runner, SQLite and PostgreSQL |

The `dev` and `#469 head` runs print `src identical to <sha>: yes`: the probe commit changes nothing under `src`. The prototype is not a PR; it was written only to check that the proposed change makes the reproduction pass.

## The probe

[`probes/probe_selection_leave_race.py`](probes/probe_selection_leave_race.py) (sha256 `4897efb8...`, the same bytes on every tree). It opens three runtime storages with `build_runtime_storage` on one SQLite file, and again on one PostgreSQL schema: A selects, B leaves, M creates the rooms and reads the results. Each case uses a new room: a human owner, then `alpha` and `beta` join, so `alpha` is the active agent.

How the calls are held (the held call itself is not changed; the wrapper only waits, then calls the real code):
- **Case 1, selection held before its write.** A's session-store backend `execute_count` is wrapped. When A's `set_active_agent(beta)` sends `UPDATE sessions SET active_agent_id = ...`, the wrapper waits before running it. On `dev` that point is after `get_participant` has returned `beta`'s row (the check) and before the write. B then runs `remove_participant(beta)`.
- **Case 2, leave held before its fallback.** B's participant owner `get_active_agent` is wrapped, as `check_leave_and_rejoin_wait_for_each_other` in `tests/_room_participant_scenarios.py` does. `remove_participant` calls it in its open transaction, after it has marked `beta` as left and before its fallback. A then runs `set_active_agent(beta)`. A non-holding wrapper on A's `execute_count` records whether A's write was sent during the hold.
- A hold ends when the other call returns, or after 1 s (below the 5 s SQLite busy timeout of the runtime storages), whichever comes first. The probe prints whether the other call returned during the hold.

Controls, one call after another on the two storages, no hold:
3. A selects `beta`.
4. `beta` is selected (on M), then B removes `beta`.
5. B removes `beta`, then A selects `beta`.

Each case prints OK when, after both calls, the stored active agent is an agent still listed as a participant (and, for the controls, the exact expected agents and stored agent), and FAIL otherwise. The probe exits 1 if any case fails.

The banner prints `openminion.__version__` and where it was imported from. At `39da8ffb` and `c00bb452` the source tree's version string is still `0.1.14`; the path shows the checkout's `src` was imported.

Rerun from a checkout: `PYTHONPATH=src python probe_selection_leave_race.py` (SQLite), with `OPENMINION_TEST_POSTGRES_URL=postgresql+psycopg://...` set for PostgreSQL too (needs the `postgres` extra). With an installed package it runs from any directory.

## Results

Every run on a tree gave the same output (three runs per tree; SQLite and PostgreSQL on Linux, SQLite on Windows for 0.1.14).

| Case | `dev` 39da8ffb, `#469 head` c00bb452, `0.1.14` | `prototype`, `prototype on #469` |
|---|---|---|
| 1. selection held before its write | B's leave returned during the hold (`True`); A returned `'beta'`; agents `['alpha']`, stored **`'beta'`**, `get_active_agent` **`'beta'`**: FAIL | B's leave did not return during the hold (`False`) and finished after the selection; A returned `'beta'`; agents `['alpha']`, stored `'alpha'` (the leave's fallback): OK |
| 2. leave held before its fallback | A's write was sent during the hold and waited for B's commit (A did not return during the hold); A returned `'beta'`; agents `['alpha']`, stored **`'beta'`**, `get_active_agent` **`'beta'`**: FAIL | A neither returned nor sent its write during the hold; after B's commit A raised `ValueError: Agent 'beta' is not an active participant`; agents `['alpha']`, stored `'alpha'`: OK |
| 3. control: selection, no leave | agents `['alpha', 'beta']`, stored `'beta'`: OK | the same: OK |
| 4. control: leave of the active agent | agents `['alpha']`, stored `'alpha'` (the fallback): OK | the same: OK |
| 5. control: selection after the leave | `ValueError: Agent 'beta' is not an active participant`; agents `['alpha']`, stored `'alpha'`: OK | the same: OK |
| probe exit status | 1 | 0 |

In case 1 on the prototype, A's return value is the session it reads back after its commit, while B's leave is still waiting for the lock or running; it was `'beta'` in every run. The stored value after both calls is the fallback, `'alpha'`.

### The prototype's tests

The prototype adds four shared scenarios to `tests/_room_participant_scenarios.py`, run on SQLite in `tests/test_session_store.py` (two `build_runtime_storage` connections on the test's file) and on PostgreSQL in `tests/integration/test_runtime_storage_postgres.py` (two runtime storages on one schema):
- `check_selection_held_before_its_write_applies_before_a_leave` (the probe's case 1, held at the same point);
- `check_selection_during_a_leave_is_refused` (case 2);
- `check_selection_and_leave_one_after_another` (the controls on two connections);
- `check_legacy_session_selection` (the legacy-session rules of `set_active_agent`).

| [run 40735052441](https://github.com/AliceMessi/linux-runner/actions/runs/40735052441) (`linux/prototype/`) | `prototype` | dev sources (the prototype checkout after `git checkout 39da8ffb -- src`) |
|---|---|---|
| new tests, SQLite | 4 passed | 2 failed, 2 passed |
| new tests, PostgreSQL | 4 passed | 2 failed, 2 passed |
| new tests, 20 runs per backend | 20 x 4 passed on each | - |
| `ruff check .` | All checks passed | - |
| `ruff format --check` on the 4 changed files | 4 files already formatted | - |
| `make lint` | exit 0, mypy ratchet 4309 / 4309 | - |

On dev sources the two held scenarios fail with:
- `test_selection_held_before_its_write_applies_before_a_leave` and `[selection-before-leave]`: `AssertionError: (['alpha'], 'beta')`, the agents listed and the stored agent;
- `test_selection_during_a_leave_is_refused` and `[leave-before-selection]`: `AssertionError: ('beta', ['alpha'], 'beta')`, what the selection returned, the agents listed and the stored agent.

Related tests, the same command on both trees (`tests/test_session_store.py tests/test_room_cli.py tests/test_turn_router.py tests/cli/interactive/test_openminion_runtime.py tests/services/runtime/test_session_profile_binding.py tests/api/routes/test_session_continuations.py tests/session tests/storage`, no marker filter, with `test_unreachable_db_raises_with_redacted_url` deselected because it hits the 60 s timeout when the `postgres` extra is installed, on `dev` too, see [openminion-393](../openminion-393)):

| | `prototype` | `dev` ([run 40736605493](https://github.com/AliceMessi/linux-runner/actions/runs/40736605493), `linux/dev-related/`) |
|---|---|---|
| related tests | 2 failed, 753 passed, 3 skipped | 2 failed, 749 passed, 3 skipped |
| `-m postgres tests/integration/test_runtime_storage_postgres.py` | 44 passed | 40 passed |

The 2 failures are the same on both trees: `tests/storage/benchmarks/test_benchmarks.py::test_baseline_file_shape` and `::test_storage_benchmarks_against_baseline`, `FileNotFoundError` for `docs/trackers/artifacts/storage-benchmarks/baseline.json` one directory above the checkout. The module is marked `postgres` (one of the two tests also `benchmark`), so `make test-ci` does not run it; the file is not in the repository at `39da8ffb`. The 4 more passes on the prototype are its new SQLite tests, and the 4 more in the PostgreSQL file its new PostgreSQL tests.

### The fix on top of #469

The fix commit cherry-picks onto `c00bb452` without conflict (`c9a4b7a5`, [run 40735052055](https://github.com/AliceMessi/linux-runner/actions/runs/40735052055), `linux/prototype-on-pr469/`):
- the probe gives the `prototype` column above in its three runs;
- #469's focused SQLite tests (`tests/test_session_store.py -k "active_agent or selection or leave or legacy or blank or join"`): 17 passed;
- #469's PostgreSQL tests (`-k "room_active_agent_on_two_connections or room_join_activation_rules"`): 6 passed;
- the PostgreSQL file: 46 passed.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `probes/probe_selection_leave_race.py`: the probe (see above).
- `probes/prototype-fix-and-tests.diff`: `git diff 39da8ffb aaf32f9a -- src tests`, the prototype's fix and tests.
- `linux/scripts/`: the runner scripts. `build.py` joins `BASE=<sha>`, `head.sh` and one job part (`dev.sh`, `pr469.sh`, `prototype.sh`, `prototype-on-pr469.sh`, `dev-related.sh`) into one script and encodes it as the command for `run.py` (the local copy of [AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner)'s client).
- `linux/<job>/`: `run.log` (the job's output), `head.txt`, `result.txt`, `setup.log` (`make dev-install` and the `postgres` extra) and `run-id.txt`. GitHub-hosted Ubuntu 24.04.5, CPython 3.11.17, SQLite 3.45.1, PostgreSQL 16.15 (`postgres:16` service), psycopg 3.3.6, SQLAlchemy 2.1.6, a fresh clone of the branch and `make dev-install` with `.deps/openminion-eval` from `dev`. The PostgreSQL storage writes a "has pending migrations" log warning on stderr when it opens; the logs print their count instead of the lines (42 per probe run, and no other stderr line).
  - `dev/`: [run 40735052390](https://github.com/AliceMessi/linux-runner/actions/runs/40735052390).
  - `pr469/`: [run 40735053622](https://github.com/AliceMessi/linux-runner/actions/runs/40735053622).
  - `prototype/`: [run 40735052441](https://github.com/AliceMessi/linux-runner/actions/runs/40735052441).
  - `prototype-on-pr469/`: [run 40735052055](https://github.com/AliceMessi/linux-runner/actions/runs/40735052055).
  - `dev-related/`: [run 40736605493](https://github.com/AliceMessi/linux-runner/actions/runs/40736605493), the related tests on `dev` as a control (`dev-related.sh`).
- `windows/probe-0.1.14-windows.log`: the probe, three runs, with the published `openminion==0.1.14` (native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4), from an empty directory with `python -I -X utf8`. PostgreSQL was not run on 0.1.14.
- `windows/package-0.1.14.txt`: the installed distribution's version and `INSTALLER` (no `direct_url.json`, so not a local or VCS install), and the sha256 of `participants.py`, `backend.py`, `record_store.py` and `backends/postgres.py` in the installed package and at `39da8ffb`: the same bytes.

Paths are shortened to `<VENV>` (the 0.1.14 venv). Runner paths (`/home/runner/...`) are left as they are.
