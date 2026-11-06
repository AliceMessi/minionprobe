# OpenMinion 0.1.19: published-source verification of #581 and #582

[openminion#581](https://github.com/openminion/openminion/issues/581) (PR #627, storage commentary) and [openminion#582](https://github.com/openminion/openminion/issues/582) (PR #628, runtime regression-test commentary) were kept open for verification on the first released source carrying the merges. The maintainer's checklists are [comment 6650647826](https://github.com/openminion/openminion/issues/581#issuecomment-6650647826) and [comment 6651071192](https://github.com/openminion/openminion/issues/582#issuecomment-6651071192).

## Release identity

| | |
|---|---|
| Tag | `v0.1.19`, annotated tag object `23115611b7bd1e4f8bf405ba0598b5858542ff35`, commit `394478bdcdab2fdda592ce546f9da3e1eb8dc561` (on `main`) |
| GitHub release | 2026-11-06T16:08:01Z |
| PyPI | `openminion-0.1.19-py3-none-any.whl` sha256 `186d310f1ef753b058dd6e48dfc1282bcbea0e52ba7c78314f063fafc8b27436`, `openminion-0.1.19.tar.gz` sha256 `28fe6edf0c1a92fccd2129c2c63b12dfa79b4e6934daf187e1635ce264c60c33`, both uploaded 2026-11-06T16:00Z |
| #627 | merge `42475bbdc993914310fa80027f093b0b5f512428`, reviewed head `31a1f1b82dad3ae26ada8f456fa71a49aea20b5f` |
| #628 | merge `b7e6fb302b4984a2c67b61c2cfade63cdbc417d1`, reviewed head `4fa56dcacd1cfdf457b7f351ca1342861e124848` |

Both merges and both reviewed heads are ancestors of `v0.1.19` and not of `v0.1.18`, so 0.1.19 is the first release carrying them.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `release-source/`:
  - `release_source.py`: a byte copy of the 0.1.17 round's script (`evidence/openminion-0.1.17-verification/probes/release_source.py`). It reads both archives without importing them and compares every file with the tag tree.
  - `prs.json`: its input (PR, merge, head and touched paths per issue).
  - `release_source.json` and `.log`: its output.
  - `pypi.txt`: the PyPI file names, sha256 digests, upload times and URLs.
- `581-582/tools/release_equivalence.py`: for every path a PR touched, it compares three revisions:
  - `pre`: the merge commit's first parent, i.e. the accepted pre-cleanup source;
  - `merged`: the merge commit;
  - `tag`: the release.

  It records:
  - the bytes of `tag` and `merged`;
  - the executable AST (`ast.dump` with every docstring removed; comments are not in the AST) of `pre`, `merged` and `tag`;
  - whether every docstring and comment of `merged` is retained at the tag (tokenize);
  - test names, `@pytest.fixture` functions, `assert` statements and `pytest.raises`/`pytest.warns` calls, compared between `pre` and `tag`;
  - for a path changed after the merge, the commits that changed it and the top-level statements that differ.
- `581-582/tools/runner-release-581-582.sh`: the runner script. It was shipped to the runner as a base64 tarball with `release_equivalence.py` (same sha256 as here).
- `581-582/windows/equivalence-windows.txt`: `release_equivalence.py` on Windows 11, CPython 3.13.5, against our clone of openminion/openminion.
- `581-582/linux/`: [run 42252532970](https://github.com/AliceMessi/linux-runner/actions/runs/42252532970), with `.run.log`, `.setup.log`, `.head.txt` and `.result.txt`. It ran on a checkout of openminion/openminion at `v0.1.19`, with:
  - `make dev-install` plus the `postgres` extra;
  - Ubuntu 24.04.5, CPython 3.11.18, SQLite 3.45.1;
  - a PostgreSQL 16.15 service.

Paths in the logs are shortened (`<RUNNER_CHECKOUT>`, `<RUNNER_TEMP>`, `<WORK>`).

## Results

| Check | #581 (10 paths) | #582 (17 paths) |
|---|---|---|
| Wheel and sdist vs the tag tree (all files) | wheel 2768/2768 identical; sdist 2773/2773 identical (plus the 8 generated `PKG-INFO`, `setup.cfg` and `egg-info` files) | same |
| Touched files in the archives | the 3 `src/` modules are in the wheel and the sdist, byte-identical to the tag; `docs/` and `tests/` are not packaged, so the tag source is used for them | `tests/` only: not packaged, tag source used |
| Tag bytes == merged (reviewed patch) | 10/10 | 14/17. The other 3 were changed after the merge by #625 and #664 (see below) |
| Executable AST, pre-cleanup == merged | 9/9 `.py` | 17/17 |
| Executable AST, pre-cleanup == tag | 9/9 | 14/17. The 3 later-changed files differ only by #625/#664's fixture change |
| Docstrings and comments of the merged patch retained at the tag | 9/9 files | 17/17 files |
| Test names, fixtures, `assert` and `pytest.raises`/`warns` counts, pre-cleanup == tag | 9/9 | 17/17 |
| `py_compile`, `ruff check`, `ruff format --check` (26 `.py` files, at the tag) | ok | ok |
| Collected test ids of the focused files, pre-cleanup tree vs tag | 281 / 281, identical (sha256 `70d350ee255b5b41...` both) | 257 / 257, identical (sha256 `5732c975630b81fc...` both) |
| Focused tests at the tag, `OPENMINION_TEST_POSTGRES_URL` set | 281 passed | 257 passed |
| Upstream `postgres-memory-retrieval` job at the tag, its 10 commands from `ci.yml` | all passed (`test_postgres_module_schemas.py`: 32 passed) | (same run) |

The focused files are those of the PR validations: the touched test modules, plus every test file that imports a touched module. The import list was re-derived at the tag (11 files for #581, 24 for #582).

**#582's three later-changed files.** `tests/api/test_freshness_continuation.py` and `tests/runtime/test_turn_capture_authorization.py` were changed by #625 (`f04fddbd`, `8d414228`), and `tests/api/test_text_answer_freshness.py` by #664 (`30079b09`).
- The changes are confined to the scripted provider fixture:
  - the `replace_entry_task` import and its use in the stub server's `do_POST`;
  - the order kept on `Script`;
  - `max_calls_per_run` on `_scripted_agent`.
- No docstring or comment changed, and no test name, fixture, assertion or `pytest.raises` count changed.
- #628's module docstrings are byte-identical at the tag, including the two docstrings corrected in review (`4fa56dca`):
  - the denied-call statement in `test_turn_denied_call_outcome.py`;
  - the 127.0.0.1 scripted provider and recovery in `test_turn_capture_authorization.py`.

**Runner note.** Collecting on the pre-cleanup trees used `git checkout <pre> -- src tests`. Restoring the tag with `git checkout HEAD -- src tests` left one untracked file, `tests/a2a/test_idempotency_stale_reclaim.py`: it exists before the cleanup and was removed later. It is not among the focused files, which are passed to pytest by path, and no touched module imports it.

Not run: the full `make test-ci` suite. #627 and #628 change comments and docstrings only, and the focused sets above are the PR validations' own.
