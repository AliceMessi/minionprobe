# OpenMinion #582: runtime regression-test commentary, equivalence and gates

Validation of the commentary-only PR for [openminion#582](https://github.com/openminion/openminion/issues/582) (invitation: [#581 comment 6607219420](https://github.com/openminion/openminion/issues/581#issuecomment-6607219420), claim and ETA: [#581 comment 6649543778](https://github.com/openminion/openminion/issues/581#issuecomment-6649543778), accepted in [#581 comment 6649696472](https://github.com/openminion/openminion/issues/581#issuecomment-6649696472)).

Branch `AliceMessi/openminion:fix/582-test-commentary`: one commit, `7f646e66` `test(runtime): trim regression-test harness narration`, on `dev` @ `a754ed20ee5a49eff4178ea4e920cc851c57f50f`. The 17 listed files are byte-identical between the audit commit `4476b647` and `a754ed20`. Only their module docstrings change.

Coordination: the maintainer noted that freshness files are diagnostic controls for open PR #625. Its review names `tests/api/test_freshness_continuation.py`, `tests/api/test_text_answer_freshness.py` and `tests/runtime/test_agent_explicit_thread.py`. At #625's head `c0061f3695f20fad8e37c674bbeceb5146fc3969` (checked 2026-11-05 after the run), #625 changes none of the 17 files, and no open PR touches them.

## Files

Every file is listed with its sha256 in [`SHA256SUMS.txt`](SHA256SUMS.txt). The folder's `.gitattributes` (`* -text`) keeps the published bytes equal to those hashes.

- `equivalence/ast_equivalence.py`: the same script as in `openminion-581`. For every changed `.py` file it compares `ast.dump` with every docstring removed, and lists each docstring that differs, with its old and new text.
- `equivalence/equivalence-windows.txt`: its output for `a754ed20` vs `7f646e66` (Windows 11, CPython 3.13.5). All 17 files are identical without docstrings. Exactly one docstring differs per file, the module docstring.
- `linux/runner-gates-582.sh`: the runner script, shipped as a base64 tarball together with `ast_equivalence.py`.
- `linux/gates-run-41968831477.txt`: [run 41968831477](https://github.com/AliceMessi/linux-runner/actions/runs/41968831477) on `7f646e66`, with `.head.txt`, `.result.txt` and `.setup.log`. The environment was Ubuntu 24.04.5, CPython 3.11.17, SQLite 3.45.1, ruff 0.15.22, the `postgres` extra and a PostgreSQL 16 service.

Runner paths are shortened to `<RUNNER_CHECKOUT>`, `<RUNNER_TEMP>` and `<RUNNER_TOOLS>`.

## Results (run 41968831477, head `7f646e66`)

| Check | Result |
|---|---|
| AST equivalence, docstrings removed, 17 files (CPython 3.11.17) | identical, 0 mismatches |
| `py_compile` of the 17 files | ok |
| `ruff check` / `ruff format --check` on the 17 files | all checks passed / 17 files already formatted |
| Collected test ids of the 24 focused files, `dev` tree vs head | 257 / 257, identical |
| Focused tests (the 14 listed test modules and the 10 test files that import a listed module), with `OPENMINION_TEST_POSTGRES_URL` set | 257 passed |
| Upstream `postgres-memory-retrieval` job, its 10 commands from `ci.yml` | all passed |
| `ruff check .` | all checks passed |
| `make format-check` | 5202 files already formatted |
| `make lint` | exit 0, mypy ratchet 4298 / 4298 |

Not run: the full `make test-ci` suite. The known unrelated timeout (`tests/storage/test_startup_validation.py::TestValidatePostgresConnectionFailure::test_unreachable_db_raises_with_redacted_url`) is not in these files and did not appear.
