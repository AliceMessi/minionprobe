# OpenMinion #456 follow-up: upgrade guidance for consolidation jobs from earlier packages, and the persisted-owner regression

Evidence for the follow-up PR that wireindevice asked for in the #463 [review](https://github.com/openminion/openminion/pull/463#issuecomment-6466117920) and [post-merge audit](https://github.com/openminion/openminion/pull/463#issuecomment-6470146303), and kept open on [#456](https://github.com/openminion/openminion/issues/456#issuecomment-6494434127). The PR's branch is `docs/456-consolidation-job-upgrade` on `AliceMessi/openminion`, head `973fb002`. It has two commits on `dev` @ `aec2df9e`:
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
