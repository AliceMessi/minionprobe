# OpenMinion #449: capture recovery asks the store before extracting, the fix

Validation of the PR for [openminion#449](https://github.com/openminion/openminion/issues/449), as proposed there ([comment](https://github.com/openminion/openminion/issues/449#issuecomment-6460713210)) and approved ([comment](https://github.com/openminion/openminion/issues/449#issuecomment-6465005071)). Branch `fix/449-recovery-capability-query`:
- three commits (test, fix, docs and changelog) on `dev` @ `96e8e662`, ending at `7be94543`;
- then a merge of `dev` @ `042e0a2c`, which includes #463 (the fix for #456), giving the merged head `34ccf868`.

Pending-capture recovery called the extraction model for each pending intent, and only the bundle write then found that the store cannot commit an atomic capture bundle. With the fix, each owner on the bundle-write path answers `supports_capture_bundles()` with its existing check, without a write. After the identity check and the authorization, recovery asks the memory API. If the store cannot commit a bundle, recovery counts the intent as pending without reading its source, extracting, or writing. A memory API without the predicate keeps the old order.

The report itself is in [`openminion-unsupported-provider-extraction`](../openminion-unsupported-provider-extraction).

## Trees

| Name | Commit | What it is |
|---|---|---|
| `dev-96e8e662` | `openminion/openminion@96e8e662` | `dev` the PR's commits are on; #456 not fixed |
| `pr-7be94543` | `AliceMessi/openminion@7be94543` | the PR's three commits on it |
| `dev-042e0a2c` | `openminion/openminion@042e0a2c` | `dev` with #458 and #463; #456 fixed |
| `merged-34ccf868` | `AliceMessi/openminion@34ccf868` | the PR branch after merging `dev-042e0a2c` (CHANGELOG-only conflict) |

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `probes/probe_unsupported_extraction.py`, `probes/probe_420_lib.py` and `probes/_common.py` are the report's probe and helpers, unchanged (same sha256 as in the report's `windows/probe-sha256.txt`). The probe's checks describe the reported behavior, so fewer of them hold on the other trees; its tables are the comparison.
- `probes/probe_449_recovery.py` is the PR probe. It uses the same helpers and the same setup:
  - one unmodified SDK turn, then the consolidation job created by the installed handler with the package's default `batch_limit` (12);
  - the daemon's own cron scheduler runs the job 14 times on `reference-sqlite` (phase U);
  - then it closes the runtime, reopens the same runtime home with the built-in store and runs the same job 3 more times (phase R).

  Per run it records the recovery result, which intents the extractor was called for, the provider extraction requests and bundle writes inside recovery, the pending intents, the scheduled turn's own capture state, and the memory API's `supports_capture_bundles()` answer (`"absent"` where the package has no such method). Its 10 checks compare the recovery counts with the pending intents observed before each run, so they hold with and without #463.
- `probes/linux_setup.sh`: the runner script. It installs the four trees from git into fresh venvs (`uv venv --python 3.12`) and runs both probes with `python -I -X utf8` from an empty directory on each.
- `windows/run_probes.sh`: the same on native Windows 11 Pro 10.0.22631, with four fresh `uv` venvs (CPython 3.12.11, SQLite 3.50.4).
  - `windows/probes/<tree>/` holds the outputs, `pip-list.txt` and `direct_url.json` (the installed commit).
  - `windows/probe-sha256.txt` has the hashes of the probe files that ran.
- `windows/pytest-*.txt`: the focused tests and the PostgreSQL test file on native Windows.
  - On the worktree: `merged-34ccf868` and `head-7be94543`.
  - On dev sources: `git archive` of `042e0a2c` (with the merged head's four test files) and of `96e8e662` (with the PR's four test files), run with `PYTHONPATH=src`; each log prints the imported `openminion.__file__`.
  - `windows/windows-env-and-ruff.txt` has the versions and `ruff check` / `ruff format --check` on the merged head.
- `linux/probes/`: [run 40675417029](https://github.com/AliceMessi/linux-runner/actions/runs/40675417029), both probes on the four trees (GitHub-hosted Ubuntu 24.04, CPython 3.12.15, SQLite 3.45.1). `spec_*.txt` are the installed specs, and `probes.sha256` the hashes of the shipped probes, the same as on Windows.
- `linux/gates-run-40675378906.txt`: [run 40675378906](https://github.com/AliceMessi/linux-runner/actions/runs/40675378906) on the merged head, with the script `linux/runner-gates-merged.sh`. CPython 3.11.17, SQLite 3.45.1, the `postgres` extra and a PostgreSQL 16 service. The steps:
  - on the head, the upstream `postgres-memory-retrieval` job's command list, read from `.github/workflows/ci.yml`;
  - the focused tests, the PostgreSQL test file and a related slice, on the head and again after `git checkout 042e0a2c -- src`;
  - back on the head, `ruff check .`, `make format-check`, `make workflow-check`, `make lint` and `make test-critical`.
- `linux/test-ci-run-40675378952.txt`: `make test-ci` on the merged head ([run 40675378952](https://github.com/AliceMessi/linux-runner/actions/runs/40675378952)), with `linux/runner-test-ci-merged.sh`.
- `linux/gates-run-40672524248.txt` and `linux/test-ci-run-40672524144.txt`: the same on `7be94543` before the merge, against `96e8e662` sources (`linux/runner-gates.sh`, `linux/runner-test-ci.sh`).
- `linux/earlier-baea9376/`: an earlier head, `baea9376`, on another fork branch. Its gates run ([run 40670388785](https://github.com/AliceMessi/linux-runner/actions/runs/40670388785)) failed `make lint` (mypy ratchet 4311 / 4309). `runner-mypy-diff.sh` ([run 40671965597](https://github.com/AliceMessi/linux-runner/actions/runs/40671965597)) runs the ratchet's mypy on that head and on `dev` sources and prints the two new errors:
  - `KnowledgeBackend` has no attribute `apply_capture_bundle`, because that head's adapter called the method as an attribute;
  - `Returning Any` in `AuditedMemoryStore.supports_capture_bundles`.

  `7be94543` keeps `apply_capture_bundle` as on `dev` and wraps that return in `bool(...)`.

Paths are shortened to `<VENV>`, `<WORK>`, `<WORKTREE>`, `<DEV_TREE>`, `<RUNNER_TEMP>` and `<RUNNER_CHECKOUT>`. Timestamped log lines from the package are left out of the pytest, gates and test-ci logs.

## Results

Both probes give the same numbers on Windows and on Linux.

`reference-sqlite`, one user turn, 14 runs (both probes, phase U):

| | `dev-96e8e662` | `pr-7be94543` | `dev-042e0a2c` | `merged-34ccf868` |
|---|---|---|---|---|
| Extraction requests inside recovery | **102** (1, 2, ..., 12, 12, 12) | **0** | **14** (1 per run) | **0** |
| Recovery per run `(scanned, recovered, pending)` | `(min(k, 12), 0, min(k, 12))` | the same | `(1, 0, 1)` | `(1, 0, 1)` |
| Pending after run 14 | 15 | 15 | 1 | 1 |
| The scheduled turns' own captures (#456) | `pending`, 14 extraction requests | the same | `excluded`, none | the same |
| Provider requests in total (built-in control) | 162 (60) | 60 (60) | 72 (58) | 58 (58) |
| `supports_capture_bundles()` | no such method | `False` | no such method | `False` |
| The user turn's intent | pending, payload unchanged, no result; extracted again in each run | pending, payload unchanged, no result; not extracted again | as `dev-96e8e662` | as `pr-7be94543` |
| Report probe checks | 10/10 | 7/10 | 4/10 | 3/10 |

The same runtime home reopened with the built-in store, 3 more runs (PR probe, phase R):

| | `dev-96e8e662` | `pr-7be94543` | `dev-042e0a2c` | `merged-34ccf868` |
|---|---|---|---|---|
| `supports_capture_bundles()` | no such method | `True` | no such method | `True` |
| Recovery per reopened run | `(12, 12, 0)`, `(3, 3, 0)`, `(0, 0, 0)` | the same | `(1, 1, 0)`, `(0, 0, 0)`, `(0, 0, 0)` | the same |
| Extraction requests inside recovery | 15, one per recovered intent | the same | 1 | 1 |
| Extractor calls for the user turn's intent, all 17 runs | 15 | 1 (first reopened run) | 15 | 1 (first reopened run) |
| `memory.capture.result` for the user turn's intent | 1 | 1 | 1 | 1 |
| PR probe checks | 5/10 | 10/10 | 5/10 | 10/10 |

On the `dev` trees, the PR probe's 5 failing checks are the ones about the new behavior: the predicate (twice), no recovery extraction, the user intent not extracted again, and recovered exactly once. The report probe's checks that no longer hold:
- on `pr-7be94543`, the 3 checks about repeated extraction;
- on the `042e0a2c` trees, also the checks that rely on the scheduled turns' own pending intents (the pending growth, the per-run counts and the bound, the scheduled turns' own capture, their request metadata, and the built-in control's scheduled bundle), because of #463.

Tests, gates run 40675378906 on `merged-34ccf868`:

- the upstream `postgres-memory-retrieval` job's seven commands: 19, 1, 7, 6, 40, 8 (with the new PostgreSQL control) and 13 passed;
- `pytest -q <focused>` -> 31 passed. On `042e0a2c` sources: 10 failed (the new tests and changed assertions), 21 passed;
- `pytest -q -m postgres tests/integration/test_runtime_memory_postgres.py` -> 8 passed. On `042e0a2c` sources the new control fails (`'MemctlAdapter' object has no attribute 'supports_capture_bundles'`), 7 passed;
- `pytest -q -m "<test-ci markers>" <related>` -> 6677 passed, 4 skipped. On `042e0a2c` sources: 10 failed (the same tests), 6667 passed, 4 skipped;
- `ruff check .`, `make format-check` (5132 files), `make workflow-check` and `make lint` (mypy ratchet 4309 / 4309, `services` 356 / 356) pass. `make test-critical` -> 486 passed, 4 skipped;
- `make test-ci` ([run 40675378952](https://github.com/AliceMessi/linux-runner/actions/runs/40675378952)) -> 1 failed, 20442 passed, 29 skipped, 1 xfailed. The one failure is the PostgreSQL connection timeout described below.

On `7be94543`, gates run 40672524248 gives the same results against `96e8e662` sources: 6673 related passed on the head, and 10 failed with 6663 passed on dev sources. Its `make test-ci` (run 40672524144) -> 1 failed, 20428 passed, 29 skipped, 1 xfailed. The failure is `test_unreachable_db_raises_with_redacted_url`, a 60 s timeout connecting to `192.0.2.1` when the `postgres` extra is installed. It also times out on `dev` with that extra.

Windows:
- `pytest -q <focused> tests/integration/test_runtime_memory_postgres.py` -> 31 passed, 8 skipped (no PostgreSQL server), on `merged-34ccf868` and on `pr-7be94543`.
- On `042e0a2c` and `96e8e662` sources: 10 failed, 21 passed, 8 skipped. The 10 are the new tests and the changed assertions; the controls pass on both trees.
- `ruff check .` and `ruff format --check` on the changed files pass.

`<focused>` = `tests/memory/runtime/test_capture_recovery.py tests/memory/test_external_backend_capture.py tests/runtime/test_cron_capture_recovery.py`; `<related>` = `tests/memory tests/runtime tests/brain tests/services/runtime tests/services/agent tests/integration tests/tools/memory tests/modules/tool`.
