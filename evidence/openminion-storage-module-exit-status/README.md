# OpenMinion: `python -m openminion.modules.storage` exits 0 when storagectl's `main()` returns 2

First noted in the 0.1.13 verification comment on #412. The maintainer reproduced it there: `python -m openminion.modules.storage pool-stats` without a PostgreSQL URL prints `ok: false`, and the process exits 0. They asked for a separate focused report and PR ([comment](https://github.com/openminion/openminion/issues/412#issuecomment-6441295060)). This folder has the reproduction on the published 0.1.13 and on `dev` @ `691a1d54`, and the validation of the fix branch `fix/storage-module-exit-status` @ `614d88f9`.

`modules/storage/__main__.py:4-5` calls `main()` and drops its return value. `modules/storage/cli.py:900-901` ends with `raise SystemExit(main())`. The fix changes only `__main__.py`, to `raise SystemExit(main())`.

## Probe

`probes/probe_storage_exit.py` runs 10 storagectl cases. Each case runs three child processes with the same interpreter, arguments and environment, each started as `python -I -X utf8` from an empty directory:
- `-m openminion.modules.storage`;
- `-m openminion.modules.storage.cli`;
- a direct call of `openminion.modules.storage.cli.main(ARGS)`, which writes what `main()` returned or raised to a side file. Its stdout and stderr are what `main()` printed.

The child environment drops `OPENMINION_STORAGE_POSTGRES_URL` and sets `OPENMINION_HOME` and `OPENMINION_DATA_ROOT` to a work directory.

The probe prints the exit statuses and whether the outputs match. It also writes a JSON file with every run's stdout and stderr, and the sha256 and text of the installed `modules/storage/__main__.py` and the sha256 of `cli.py`. Paths are shortened to `<VENV>`, `<WORK>` and `<USER_HOME>`.

`probes/compare_runs.py` compares two result files case by case: the module's exit status, and its stdout and stderr with the package directory written as `<PKG>`. `probes/probe_linux.py` is the runner wrapper. It runs the probe, then a `pool-stats --postgres-url` success control against the job's PostgreSQL service, without printing the URL. `probes/test_storagectl_module_exit_status.py` is a copy of the PR's test file.

## Files

| sha256 | file |
|---|---|
| `941c2d08d1d9ca6645832ec883720d9c74d53b0c5719c6ae8a73034307b50366` | `probes/probe_storage_exit.py` |
| `9311243433d6d90d14d7c35d5b4c41cd04b7b5de0c6fa2edad87b4a02c1fa1ec` | `probes/compare_runs.py` |
| `bf5fb7dcc5c52f137256b2eedc54d4357fd7fb4da3de2ac7cf6efe67ee7a2811` | `probes/probe_linux.py` |
| `231d2fa9c445a86b7a0f9a812b1b404c7df22610f04f7cabc5d5f2d65252f235` | `probes/test_storagectl_module_exit_status.py` |

- `windows/0.1.13/`: `openminion==0.1.13` from PyPI, in a fresh `uv` venv. Files: `probe.txt`, `result.json`, `pip-list.txt`.
- `windows/dev-691a1d54/`: `dev` @ `691a1d543822522bc7208d45dd3be9e602b8e5ce`, installed (not editable) from `git -c core.autocrlf=false archive` of that commit.
  - `691a1d54` is `68d623d1` plus two dependabot merges that change only `.github/workflows`; `src/`, `tests/`, `pyproject.toml` and `CHANGELOG.md` are identical.
  - Its pip list equals 0.1.13's, since the package version is still 0.1.13.
  - `pytest-with-new-tests.txt` runs the PR's test file on these sources.
- `windows/head-614d88f9/`: the fix branch, from an editable install of the worktree. Files: `probe.txt`, `result.json`, `pip-list.txt`, `pytest.txt`. This Windows checkout has CRLF line endings, so the sha256 values of `__main__.py` and `cli.py` differ from the LF files in git. `git diff 691a1d54 614d88f9 -- src/openminion/modules/storage/cli.py` is empty.
- `windows/compare-0.1.13-vs-dev-691a1d54.txt` and `windows/compare-dev-691a1d54-vs-head-614d88f9.txt`: the `compare_runs.py` outputs.
- `linux/gates-run-40502647160.txt`: AliceMessi/linux-runner [run 40502647160](https://github.com/AliceMessi/linux-runner/actions/runs/40502647160), with the script `linux/runner-gates.sh`. The run used Ubuntu 24.04.5, CPython 3.11.17, SQLite 3.45.1, the `postgres` extra and a PostgreSQL service. On the branch head it ran:
  - the focused tests, `tests/storage` and `tests/modules`, and the probe;
  - the same steps again after `git checkout 691a1d54 -- src`;
  - back on the head: `ruff check .`, `make format-check`, `make workflow-check`, `make lint` and `make test-critical`.
- `linux/test-ci-run-40502646946.txt`: `make test-ci` on the branch head ([run 40502646946](https://github.com/AliceMessi/linux-runner/actions/runs/40502646946)), with the script `linux/runner-test-ci.sh`.

## Results

Windows 11 Pro 10.0.22631 and CPython 3.12.11 for 0.1.13 and `dev`. The Linux runner ran `dev` sources:

| Case | `main()` | module exit on 0.1.13 and `dev` | `storage.cli` exit | module exit on `614d88f9` |
|---|---|---|---|---|
| `plan --namespace session --sqlite <db>` | returns 0 | 0 | 0 | 0 |
| `pool-stats`, no URL | returns 2 | 0 | 2 | 2 |
| `a2a-archive-old --older-than-days 0` | returns 2 | 0 | 2 | 2 |
| `a2a-archive-old --older-than-days 7`, no URL | returns 2 | 0 | 2 | 2 |
| `export --backend postgres`, no URL | returns 2 | 0 | 2 | 2 |
| `import --backend postgres`, no URL | returns 2 | 0 | 2 | 2 |
| `dr-drill` on a file that is not SQLite | returns 2 | 0 | 2 | 2 |
| `plan --namespace unknown-module` | raises `ValueError` | 1 | 1 | 1 |
| `restore` from a missing snapshot | raises `BackupError` | 1 | 1 | 1 |
| `pool-stats --bogus` | argparse `SystemExit(2)` | 2 | 2 | 2 |

- In every case and build, `main()`, the module and `storage.cli` print the same stdout. The module and `storage.cli` print the same stderr, apart from their own traceback frames in the two raising cases.
- 0.1.13 against `dev`: the installed `__main__.py` and `cli.py` have the same sha256, and the module's stdout and stderr are byte-identical in all 10 cases.
- `dev` against `614d88f9`: the module's stdout is identical in all 10 cases. Its stderr is identical apart from the two raising cases, where the traceback frame for `__main__.py` shows `raise SystemExit(main())` instead of `main()`.
- Linux gives the same exit statuses on `dev` sources and on the head. `pool-stats --postgres-url <the job's PostgreSQL>` prints `"ok": true` with the pool stats and exits 0 from both modules, on both trees.
- Tests:
  - Windows, the PR's test file with `test_storagectl_cli.py` and `test_storagectl_restore.py`: 18 passed on the head. On `dev` sources, 2 failed (`test_module_exits_with_main_nonzero_status_and_same_output[pool-stats-without-postgres-url]` and `[a2a-archive-old-zero-days]`, `assert 0 == 2`) and 16 passed.
  - Linux focused set: 23 passed on the head; on `dev` sources, the same 2 failed and 21 passed.
  - Linux `make test-ci`: 1 failed, 20412 passed, 29 skipped. The failure is `test_unreachable_db_raises_with_redacted_url`, the known 60 s timeout to `192.0.2.1` when psycopg is installed.

## Reproduce

From an empty directory, with the interpreter of a venv that has `openminion` installed:

```
python -I -X utf8 probe_storage_exit.py --label LABEL --work <WORK> --out result.json
python -I -X utf8 compare_runs.py before.json after.json
```
