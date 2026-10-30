# OpenMinion #456 follow-up: upgrade guidance for consolidation jobs from earlier packages, and the persisted-owner regression

Evidence for the follow-up PR that wireindevice asked for in the #463 [review](https://github.com/openminion/openminion/pull/463#issuecomment-6466117920) and [post-merge audit](https://github.com/openminion/openminion/pull/463#issuecomment-6470146303), and kept open on [#456](https://github.com/openminion/openminion/issues/456#issuecomment-6494434127). The PR's branch is `docs/456-consolidation-job-upgrade` on `AliceMessi/openminion`, head `973fb002` in round 1 (the final head after the 30 Oct review is `94a72e56`, see Rounds 2 and 3). It had two commits on `dev` @ `aec2df9e`:
- `e572f3c5`, `test(runtime)`;
- `973fb002`, `docs(runtime)` with the CHANGELOG entry.

There is no `src` change.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes. Paths are shortened to `<checkout>` (the runner's clone), `<RUNNER_TEMP>` and `<work>` (the transcript's temporary directory).

- `scripts/gates_456.sh` and `scripts/transcript_456.sh`: the two scripts. They were passed to the runner as base64 and run from the branch checkout. `linux/scripts.sha256` holds the hashes of the bytes that ran, which are the same as these files'.
- `linux/gates-run-40866237554.txt`: the job's output on the final head `973fb002`, from [run 40866237554](https://github.com/AliceMessi/linux-runner/actions/runs/40866237554).
  - `linux/head.txt` is the commit the runner checked out, and `linux/setup.log` is `make dev-install`.
  - `linux/transcript/transcript.txt` is the full transcript of the guidance's commands. `stderr.log` holds every command's stderr (INFO log lines), and the transcript shows only non-INFO stderr.
  - `pip-0.1.14.log` and `pip-freeze-0.1.14.txt` are the 0.1.14 venv.
- `linux/earlier-1f812832/`: the same gates on an earlier head `1f812832` ([run 40863601063](https://github.com/AliceMessi/linux-runner/actions/runs/40863601063)).
  - One new assertion failed there: the forged-metadata control expected the result disposition `succeeded`, but with the echo provider it is `succeeded_no_output`.
  - `973fb002` changes that assertion and the docs section, which now covers which agent the commands act for. The transcript script was extended with homes C and D.
- `linux/explore-e0552eca/`: the first exploration ([run 40862395566](https://github.com/AliceMessi/linux-runner/actions/runs/40862395566)), on a wip head with a draft of the test. It was used to learn the CLI's output before writing the guidance.

## Gates on the final head

[Run 40866237554](https://github.com/AliceMessi/linux-runner/actions/runs/40866237554): GitHub-hosted Ubuntu 24.04.5, CPython 3.11.17, SQLite 3.45.1, a fresh clone of `973fb002` with `make dev-install` (`.deps/openminion-eval` from `dev`). The branch does not change `src`, so these runs use `dev`'s sources.

| Gate | Result |
|---|---|
| `pytest -q <focused>` (14 files, see the script) | 253 passed |
| the three new tests, 5 runs | 3 passed each time |
| M1: `metadata={"kind": CONSOLIDATION_TASK_KIND}` removed from `task.consolidate_memory`'s handler | new tests: 3 failed; whole file: 3 failed, 5 passed |
| M2: `task_manager=None` in the daemon's `_cron_turn_executor_for_runtime` | new tests: 3 failed; whole file: 3 failed, 5 passed |
| M3: the executor's kind check replaced by `if False:` | new tests: 1 failed (the pre-upgrade test: the run `finished` instead of `failed`), 2 passed; whole file: 1 failed, 7 passed |
| `python -m scripts.validate.cli_layout` (CLI documentation validator, 86 current docs) | ok, exit 0 |
| `pytest -q -m "<test-ci markers>" tests/runtime tests/task tests/tools/task tests/cron tests/api tests/services tests/memory tests/cli tests/test_daemon_resilience.py` | 5731 passed, 2 skipped, 1 xfailed |
| `ruff check .` | all checks passed |
| `make format-check` | 5161 files already formatted |
| `make lint` | passed (mypy ratchet 4306 / 4306, layout validators) |

The mutations are made in the runner checkout one at a time with an exact-string replacement, shown with `git diff -- src`, and undone with `git checkout -- src` before the next one. None is committed.

M1 and M2 fail the two tests of jobs with the kind: the daemon pauses the new job on its first run, so the second `run_task_now` raises `task is not active`. They also fail the pre-upgrade test:
- with M1, its replacement has no kind;
- with M2, the pre-upgrade task stays `active`, because the executor without a task manager only disables the cron row.

Not run: the upstream `postgres-memory-retrieval` job (the PR touches nothing it runs), Windows, `make test-ci` and `make test-critical`.

## The guidance's commands on real runtime homes

The commands come from `transcript_456.sh`, in the same run. The setup, marked `[setup]` in the transcript:
- a separate venv with `openminion==0.1.14` from PyPI, whose `task.consolidate_memory` stores no task kind;
- homes with an echo agent, written with 0.1.14's config API;
- each pre-upgrade job created by 0.1.14's own CLI, `openminion tools run task.consolidate_memory`;
- for a scheduled run without waiting for the interval, the job's `cron_jobs.next_due_at` set to now.

Lines marked `[evidence]` read `task/task.db` directly, because the CLI does not show the task kind. Every other command is the checkout's own `openminion` CLI.

- **Home A: the job had not run on the new package.**
  - `tasks list`, `tasks show` and `tools run task.list` show the 0.1.14 job `active`, with `enabled: true` and `consolidation.batch_limit` 8. Its record has `metadata.kind=None`.
  - `tasks pause` leaves it `paused` with `enabled: false`.
  - With the daemon started, `tools run task.consolidate_memory` (`runtime_source: daemon`) creates the replacement, with `metadata.kind='memory_consolidation'`.
  - `tasks list` shows only the replacement enabled. Its scheduled run under the daemon `finished`.
- **Home B: the job ran on the new package.**
  - The daemon's run of the 0.1.14 job `failed`, with `last_run.error` `{"code": "cron_failed", "message": "memory consolidation job is missing trusted task provenance; ..."}`. The job is `paused`, with `enabled: false`, and no consolidation task is enabled.
  - `tasks resume` makes it `active` again. Its next run `failed` and paused it again.
  - `tasks cancel` on the paused job leaves it `cancelled`.
  - `tools run task.consolidate_memory` (in process) creates the replacement. `tasks list` shows only the replacement enabled.
  - Raw cron API, through `openminion api run`:
    - `POST /v1/cron/jobs` with the reserved payload answers HTTP 400 `{"ok":false,"error":{"code":"invalid_request","message":"memory consolidation jobs must be created through task.consolidate_memory",...}}`.
    - `GET /v1/cron/jobs` lists the same three jobs as before: the replacement, `system-cron-cleanup` and the cancelled one.
- **Home C: the default agent is `main`.**
  - The 0.1.14 job created with `OPENMINION_AGENT_ID=main` belongs to `main`. `tasks list` shows it, and `OPENMINION_AGENT_ID=main openminion tools run task.list --runtime-source inproc` shows its settings.
  - Without the variable, `tools run task.consolidate_memory` creates the job for `openminion`; `tasks list --agent-id openminion` shows it, and `tasks cancel ... --agent-id openminion` cancels it.
  - With the daemon running, the variable without `--runtime-source inproc` still gives a job for `openminion` (`runtime_source: daemon`). With `--runtime-source inproc` the job is for `main`.
  - After `tasks cancel` of the old job, `tasks list` shows only the replacement enabled for `main`. Its scheduled run `finished`.
- **Home D: the replacement was created while the old job was enabled.**
  - Both jobs are enabled until the old job's run `failed` and paused it.
  - Then only the replacement is enabled.

Pending capture intents do not appear in the transcript. The three new tests cover them, and so does the 0.1.15 verification on the published package ([`openminion-0.1.15-verification`](../openminion-0.1.15-verification), `probe_456_upgrade.py`), which was not rerun.

## Round 2: head `f7f27c92` (after the #535 review of 30 Oct)

The review asked to reconcile with `dev` after the 0.1.16 release with the entry under `## Unreleased`, to scope the "treat every consolidation task as a pre-upgrade job" instruction to jobs created by packages before 0.1.15, and to get a green macOS check. Head `f7f27c92` on `docs/456-consolidation-job-upgrade`, on top of `21f276ea`:
- `94133a06`: "Merge branch 'dev' into docs/456-consolidation-job-upgrade", `dev` @ `f7b6100a`. Git merged `CHANGELOG.md` cleanly but put the entry under `Current package line - 2026-10-29`; the merge commit moves it to `## Unreleased`. `git diff f7b6100a -- CHANGELOG.md` only adds the entry.
- `f7f27c92`, `docs(runtime)`: the scope in `docs/runtime-database-upgrades.md` and the changelog entry. No test or source change.

Files in `linux/round2-f7f27c92/` ([run 41025256705](https://github.com/AliceMessi/linux-runner/actions/runs/41025256705), `postgres` extra and PostgreSQL 16):
- `gates_456.sh`: the round-1 gates script plus the upstream `postgres-memory-retrieval` commands read from `ci.yml`. `transcript_456r2.sh`: home E below. `scripts.sha256` has the bytes that ran (the same as these files).
- `gates-run-41025256705.txt` (runner paths shortened to `<checkout>`), `head.txt`, `result.txt`, `run-id.txt`; `transcript/` with the transcript, the stderr log and both PyPI venvs' pip logs.

| Gate on `f7f27c92` | Result |
|---|---|
| `pytest -q <focused>` | 253 passed |
| upstream `postgres-memory-retrieval`, 9 commands | 19, 1, 7, 6, 54, 8, 2, 13, 13 passed |
| the three new tests, 5 runs | 3 passed each time |
| M1 / M2 / M3 (as above) | new tests 3 / 3 / 1 failed; whole file 3 failed 5 passed / 3 failed 5 passed / 1 failed 7 passed |
| `python -m scripts.validate.cli_layout` | ok, exit 0 |
| `pytest -q -m "<test-ci markers>" <related>` | 5731 passed, 2 skipped, 1 xfailed |
| `ruff check .`, `make format-check`, `make lint` | passed; 5162 files already formatted; mypy 4306 / 4306 |

**Home E: upgrade from 0.1.16** (`transcript_456r2.sh`). Two PyPI venvs, `openminion==0.1.14` and `openminion==0.1.16`. One home: job X created by 0.1.14's CLI, then job Y created by 0.1.16's CLI (so the home was opened by 0.1.16 too), then this branch's CLI and daemon:
- before any run: both `active` and enabled; the task database has `metadata.kind=None` for X and `memory_consolidation` for Y;
- the daemon's runs: Y `finished`, X `failed` with `memory consolidation job is missing trusted task provenance; it was paused and must be recreated with task.consolidate_memory`; X is `paused` with `enabled: false`, Y stays `active` and enabled;
- Y's next scheduled run `finished` too;
- `tasks cancel` X and `tools run task.consolidate_memory` create X's replacement with `memory_consolidation`; the enabled consolidation tasks are the replacement and Y, and Y's kind is unchanged.

## The macOS `focus-pty-macos` check (`macos/`)

The upstream check failed on `21f276ea` (job 123883135760) with `test_focus_pty_keeps_top_down_transcript_with_inline_input_across_resize`, `assert 13 == (8 - 1)`. Files:
- `upstream-ci/ci_history.py` and `ci_history.tsv`: `focus-pty-macos` in the last 200 `ci.yml` runs of openminion/openminion (every attempt). 184 jobs: 170 success, 6 failure, 8 cancelled. `failed-jobs-excerpts.txt`: the runner image and the failure lines of the 6 failed jobs; all 6 are this assertion. Four are `dev` pushes (`e9689539`, `1043a5b3`, `68d623d1`, `949e3192`); for `1043a5b3` and `949e3192` attempt 2 of the same run passed.
- `focus-pty-macos-repeat.yml` and `focus_dump.py`: the workflow and the diagnostics-only pytest plugin on branch [`macos-focus-pty`](https://github.com/AliceMessi/linux-runner/tree/macos-focus-pty) of AliceMessi/linux-runner (commit `87c634a`). The workflow uses `macos-latest`, `ci.yml`'s actions, "Prepare isolated runtime roots", "Bootstrap repo venv", `PYTHONPATH` and the same four tests, run N times per job with `-p focus_dump`. The plugin records sends, resizes and (from `87c634a`) every PTY read after a resize, and saves the raw PTY output, the actions and the final emulator screen of the resize test and of any failing session; it changes no test or assertion.
- `repeat-41026401853/`: [run 41026401853](https://github.com/AliceMessi/linux-runner/actions/runs/41026401853) (workflow and plugin at `fc46306`, kept here). 3 jobs x 8 iterations each for `f7f27c92` (this PR), `02f25bfe` (#534's earlier head, same Focus code) and `dev` @ `f7b6100a`. `results.txt`: every iteration's result line. One failure: `pr534-s1-i6`, at the 42-row check. `pr534-s1/` has its dump and pytest output and those of the passing `i5`.
- `repeat-41028097498/`: [run 41028097498](https://github.com/AliceMessi/linux-runner/actions/runs/41028097498) (`87c634a`). 5 jobs x 10 iterations for `dev`, plus one job x 10 for each PR head (from the matrix's include entries). One failure: `dev-s4-i2`, at the 18-row check. `dev-s4/` has its dump and pytest output and those of the passing `i1`; `timeline-dev-s4-i{1,2}.txt` are the PTY read timelines (`timeline.py`).
- `replay.py`: replays a dump's raw PTY output through pyte 0.8.2 (the version the job installs) with the recorded resize points and prints the occupied rows and cursor; `replay-*.txt` are its outputs. The `*.raw.txt` dumps are the PTY output as read, so they keep the terminal's CR bytes; the replay offsets count them.

Totals: `dev` 1 failure in 74 iterations, `f7f27c92` 0 in 34, #534's head 1 in 34; both failures are `assert 13 == (8 - 1)`. The final head `94a72e56` differs from `f7f27c92` only in docs (Round 3) and was not repeated on macOS.

What the dumps show:
- Every resize is followed by several erase and redraw pairs from prompt_toolkit. The erase (`ESC[2D ESC[J ...`, 16 to 24 bytes) and the redraw (328 or 399 bytes) are separate PTY reads. One pair arrives about 0.1 to 0.45 s after the resize, later than the immediate ones (`timeline-*.txt`). prompt_toolkit's `_on_resize` erases and flushes before it redraws, and it also polls the terminal size every 0.5 s (code reading, `prompt_toolkit` 3.0.53 `application/application.py:586-596`, `:1212-1232`; `renderer.py:735-752`).
- `dev-s4-i2`: the delayed erase was read 0.441 s after the 18x72 resize and its redraw 1 ms later. The replay gives a consistent layout before that erase (14 occupied rows, cursor row 13), exactly the failing state right after it (8 occupied rows, the stale composer copy on rows 7-8, cursor row 13), and a consistent layout again after its redraw.
- `pr534-s1-i6`: up to the 42x120 resize's read the output equals the passing `i5`'s (apart from the session id); then one more erase and redraw of the same frame follows (this run had no read timeline). The replay gives the failing state at the end of that erase (offset 8487) and a consistent layout before and after.
- `assert_flow_layout` and `_assert_inline_composer` (`tests/e2e/cli/focus/test_local.py:599-615`, `:24-29`) read the PTY again in each property after the wait loop, so their rows and cursor can straddle an erase and its redraw.

No test or assertion was changed.

## Round 3: final head `94a72e56` (step 4 of the replacement procedure)

After round 2, step 4 still told operators to check that the replacement is the only enabled consolidation task. After an upgrade from 0.1.15 or later that is false when a never-replaced job from before 0.1.15 sits next to a valid job created by 0.1.15 or later (home E: X and Y), and it would point the operator at Y. `94a72e56`, `docs(runtime)` on top of `f7f27c92`, changes only `docs/runtime-database-upgrades.md` (step 4, lines 217-223) and the Unreleased changelog entry:
- step 4 checks that the old job is not enabled (`cancelled`, or `paused` with `enabled: false`), and that the replacement has `enabled: true` and the old job's `agent_id`;
- the enabled consolidation tasks are then the replacements plus any job created by 0.1.15 or later, which stays as it is;
- "the replacements are the only enabled consolidation tasks" is kept only for upgrades from a package before 0.1.15.

`94a72e56` differs from `f7f27c92` only in those two docs files. The macOS repeats above ran on `f7f27c92`.

Files in `linux/round3-94a72e56/` ([run 41030950593](https://github.com/AliceMessi/linux-runner/actions/runs/41030950593), `postgres` extra and PostgreSQL 16): `gates-run-41030950593.txt` (runner paths shortened to `<checkout>`), `head.txt`, `result.txt`, `run-id.txt`, `scripts.sha256` and `transcript/`. The scripts are the round-2 files in `linux/round2-f7f27c92/` (the same sha256).

| Gate on `94a72e56` | Result |
|---|---|
| `pytest -q <focused>` | 253 passed |
| upstream `postgres-memory-retrieval`, 9 commands | 19, 1, 7, 6, 54, 8, 2, 13, 13 passed |
| the three new tests, 5 runs | 3 passed each time |
| M1 / M2 / M3 | new tests 3 / 3 / 1 failed; whole file 3 failed 5 passed / 3 failed 5 passed / 1 failed 7 passed |
| `python -m scripts.validate.cli_layout` | ok, exit 0 |
| `pytest -q -m "<test-ci markers>" <related>` | 5731 passed, 2 skipped, 1 xfailed |
| `ruff check .`, `make format-check`, `make lint` | passed; 5162 files already formatted; mypy 4306 / 4306 |

Home E on `94a72e56` (X `3f92392b…` from 0.1.14, Y `2c194ad8…` from 0.1.16): Y's runs `finished` twice and Y stays enabled with `memory_consolidation`; X's run `failed` with the provenance message and X was paused; after `tasks cancel` X and the replacement (`9cb2f8ed…`, `memory_consolidation`), `tasks list` shows X `cancelled` with `enabled: false`, the replacement enabled for `openminion`, and the enabled consolidation tasks are the replacement and Y, as step 4 now says.
