# OpenMinion #393: the room's first active agent written under the session-row lock, the fix

Validation of the PR for [openminion#393](https://github.com/openminion/openminion/issues/393), in the scope proposed there ([comment](https://github.com/openminion/openminion/issues/393#issuecomment-6396647811)) and accepted ([comment](https://github.com/openminion/openminion/issues/393#issuecomment-6396896346)). Branch `fix/393-room-active-agent-race`: three commits (test, fix, docs and changelog) on `dev` @ `042e0a2c`, ending at `e7f42b5f`.

A room's first agent join read the session after its participant row had committed and, when the room had no active agent, wrote `active_agent_id` with an unconditional `UPDATE`, in separate statements, so another connection's join, selection or leave in between was replaced. With the fix the join re-reads the session and writes the active agent in its participant transaction, after #385's session-row lock, with the same empty/blank rule. The report and its probe are in [`openminion-race-active-agent`](../openminion-race-active-agent).

## Trees

| Name | Commit | What it is |
|---|---|---|
| `dev` | `openminion/openminion@042e0a2c` | `dev` the PR's commits are on |
| `head` | `AliceMessi/openminion@e7f42b5f` | the PR's three commits on it |

"dev sources" means the head's checkout after `git checkout 042e0a2c -- src`: `dev`'s `src` with the PR's tests.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `probes/probe_race_active_agent.py`: the report's probe, unchanged (sha256 `228f93d5...`, as in the report's logs). It pauses one join at the first session read after its participant write returns and drives the other connection; part 4 runs two first joins with no pause.
  - On `dev` that read is the one that decides the activation, so the pause sits between the read and the write.
  - On `head` the activation is read and written inside the participant transaction, so that read is the join's read-back of its participant, after the commit. The probe's "read active_agent_id=" lines then show the committed state, and its part 4 counts 0 by construction.
- `probes/probe_393_unpaused.py`: the PR probe for part 4. It needs no pause point: per room it counts the statements each join runs that write the active agent (`UPDATE sessions SET active_agent_id ...`, the activation write on both trees), and what each join's caller reads with `get_active_agent` right after its join returns, against the stored value after both joins. Same setup as the report's part 4 (two runtime storages on one SQLite file or one PostgreSQL schema, a human owner per room, a barrier, 200 rooms).
- `probes/probe_393_selection_vs_leave.py`: a case the PR does not change (its "Not included"). A selection of `beta` is held after `set_active_agent` checks that `beta` is a participant; `beta` leaves on another connection; the selection then stores `beta`.
- `linux/`: GitHub-hosted Ubuntu 24.04, CPython 3.11.17, SQLite 3.45.1, PostgreSQL 16 (`postgres:16` service) with the `postgres` extra, `make dev-install` on a fresh clone of the head. `linux/scripts/` has the parts each runner script was built from (`build.py` joins `head.sh`, the job's part and the shipped probe into one script).
  - `lint-run-<id>.txt` (`lint.sh`): `ruff check .`, `make format-check`, `make workflow-check`, `make lint`.
  - `gates-run-<id>.txt` (`gates.sh`): the upstream `postgres-memory-retrieval` job's commands, read from `.github/workflows/ci.yml`; the focused tests, the PR's PostgreSQL tests, the PostgreSQL file and a related slice on `head` and on dev sources; the PR's tests 20 times each on both backends; the lint gates and `make test-critical`.
  - `report-probe-run-<id>.txt` (`probes.sh`): the report's probe, three runs per tree.
  - `unpaused-probe-run-<id>.txt` (`unpaused.sh`): the PR probe, three runs per tree.
  - `test-ci-run-<id>.txt` (`testci.sh`): `make test-ci` on the head.
- `windows/`: native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4, a fresh `uv` venv with `uv pip install -e ".[dev]"` of the worktree (`windows-env-and-ruff.txt`).
  - `pytest-head.txt`, `pytest-dev.txt`: the focused tests and the two test files (PostgreSQL skipped, no server), on the worktree and after `git checkout 042e0a2c -- src`. Each prints the imported `openminion.__file__`.
  - `run-windows-{head,dev}.log`: the report's probe, three runs per tree (SQLite).
  - `run-windows-unpaused-{head,dev}.log`: the PR probe, three runs per tree (SQLite).
  - `run-windows-selection-vs-leave.log`: the "Not included" case on both trees.
  - `ablation-legacy-owner.txt`: a slice of session and room tests on the head, and again with `ensure_legacy_participant_record` passing `activate_agent=True`. Only the new legacy-owner control changes result. The other failures in that slice are the same on both runs; they are native-Windows failures in `tests/session` that the PR does not touch.

Paths are shortened to `<VENV>`, `<WORK>`, `<WORKTREE>`, `<LINUX_RUNNER_KIT>` (the local copy of [AliceMessi/linux-runner](https://github.com/AliceMessi/linux-runner)'s `run.py`), `<RUNNER_TEMP>` and `<RUNNER_CHECKOUT>`. Timestamped log lines from the package are left out of the runner logs. The runner logs print a count of the PostgreSQL storage's "has pending migrations" log warning on stderr instead of the lines.

## Results

### The report's probe, unchanged

Parts 1-3 give the same output in all three runs on each tree, on SQLite and PostgreSQL on Linux and on SQLite on Windows. On `dev` they are the report's results. On `head`, the probe's pause point is after the join's commit (see above), so A and B read the committed `'alpha'`:

| Case | `dev` | `head` |
|---|---|---|
| 1. two first joins | A and B read `None`; after A returns `'alpha'`; after B returns **`'beta'`** | A and B read `'alpha'`; after A returns `'alpha'`; after B returns **`'alpha'`** |
| 2. explicit selection during a first join | after the selection `'beta'`; after A returns **`'alpha'`** | after the selection `'beta'`; after A returns **`'beta'`** |
| 3. leave during a first join | A raises `RuntimeError`; agents `[]`, stored **`'alpha'`** | A raises `RuntimeError`; agents `[]`, stored **`None`** |
| `set_active_agent(alpha)` after case 3 | `ValueError` (not a participant) | the same |

Part 4, both joins started together, 200 rooms, three runs: rooms where both joins read no active agent and wrote one (of those, rooms where the stored agent is not the first listed). On `head` this count is 0 by construction, because the read the probe records comes after the commit; the PR probe below measures it without that read.

| | `dev` | `head` |
|---|---|---|
| PostgreSQL 16.15, Linux | 194 (180), 198 (187), 198 (188) | 0, 0, 0 |
| SQLite 3.45.1, Linux | 1 (0), 5 (0), 2 (0) | 0, 0, 0 |
| SQLite 3.50.4, native Windows | 43 (1), 39 (0), 27 (0) | 0, 0, 0 |

### The PR probe, two first joins with no pause

200 rooms, three runs: rooms where both joins ran the active-agent write (of those, rooms where a join's caller read an active agent right after its join that is not the one stored after both joins). Errors: 0 everywhere.

| | `dev` | `head` |
|---|---|---|
| PostgreSQL 16.15, Linux | 185 (9), 189 (5), 183 (7) | 0 (0), 0 (0), 0 (0) |
| SQLite 3.45.1, Linux | 0 (0), 1 (1), 3 (3) | 0 (0), 0 (0), 0 (0) |
| SQLite 3.50.4, native Windows | 8 (6), 15 (14), 20 (16) | 0 (0), 0 (0), 0 (0) |

### Tests

Linux runner ([gates run 40682185872](https://github.com/AliceMessi/linux-runner/actions/runs/40682185872), [test-ci run 40682268130](https://github.com/AliceMessi/linux-runner/actions/runs/40682268130), [lint run 40682141107](https://github.com/AliceMessi/linux-runner/actions/runs/40682141107)):

| | `head` | dev sources |
|---|---|---|
| upstream `postgres-memory-retrieval` job, its seven commands | 19, 1, 7, 6, 46, 7, 13 passed | - |
| focused SQLite tests (`tests/test_session_store.py -k ...`) | 17 passed | 3 failed (the three held cases), 14 passed |
| the PR's PostgreSQL tests | 6 passed | 3 failed (the three held cases), 3 passed |
| `-m postgres tests/integration/test_runtime_storage_postgres.py` | 46 passed | 3 failed, 43 passed |
| related slice, test-ci markers | 1 failed, 5282 passed, 4 skipped, 1 xfailed | 4 failed (the three held cases and the same timeout), 5279 passed |
| the PR's tests 20 times | SQLite 20 x 7 passed, PostgreSQL 20 x 6 passed | - |
| `ruff check .`, `make format-check`, `make workflow-check` | passed, 5132 files formatted, passed | - |
| `make lint` | passed, mypy ratchet 4309 / 4309 | - |
| `make test-critical` | 486 passed, 4 skipped | - |
| `make test-ci` | 1 failed, 20438 passed, 29 skipped, 1 xfailed | - |

The one failure on the head is `tests/storage/test_startup_validation.py::TestValidatePostgresConnectionFailure::test_unreachable_db_raises_with_redacted_url`: with the `postgres` extra installed, as on these runners, it hits the 60 s pytest timeout connecting to `192.0.2.1`, on `dev` too.

On dev sources, the three held cases fail with:
- case 1: `({'alpha': 'alpha', 'beta': 'beta'}, 'alpha')`, what each caller saw and the stored agent;
- case 2: `('beta', 'alpha')`, what the selection returned and the stored agent;
- case 3: `([], 'alpha')`, the agents listed and the stored agent.

Native Windows: the focused tests give 17 passed on `head`, and 3 failed (the same values) with 14 passed on dev sources. The two test files give 78 passed, 46 skipped (no PostgreSQL server) on `head`, and 3 failed, 75 passed, 46 skipped on dev sources.
