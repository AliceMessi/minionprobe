# OpenMinion #419, PR #430 round 3: persisted state compatibility

The review of head `1d4d4d15` confirmed the round-2 fix. It asked to ship the compatibility contract of the two new `WorkingState` fields (`turn_evidence`, `turn_evidence_trace_id`) outside the PR description, and to test saving and loading them. Every working state that the new version saves contains both fields, and a runtime from before them rejects it with `extra_forbidden`.

Round 3 is head `8d71ac50`: two commits on `1d4d4d15`, then a merge of `dev` @ `a213dcd2` (after #429 and #431; no conflicts, every Unreleased entry kept).
- `docs/runtime-database-upgrades.md` has a new section, "Brain working state fields". It says where the working state is saved and refers to the guide's coordinated upgrade: stop the processes that share the runtime state, keep a pre-upgrade backup, upgrade them together, never run an older process against the newer state, and roll back with the matching backup. The #419 CHANGELOG entry says the same in short.
- `tests/brain/test_turn_evidence_persistence.py` goes through `save_state` / `load_or_init_state` on the runtime's SQLite session store and on the local session store (4 tests on 2 stores):
  - a state saved before the fields loads with both unset;
  - a saved `turn_evidence` keeps its trace id and is the evidence only for that trace id;
  - every saved state writes both fields;
  - a field the reader does not define is still rejected.

Files:
- `run-linux-gates.log`, from `run419c.sh`: Ubuntu 24.04 GitHub-hosted runner, [run 40358572576](https://github.com/AliceMessi/linux-runner/actions/runs/40358572576). On the head and on `dev` sources (`git checkout a213dcd2 -- src`):
  - `<focused>`: 23 passed on the head; 6 failed and 17 passed on `dev` sources;
  - `<persistence>`: 8 passed on the head; on `dev` sources the file does not import (`_same_turn_evidence` does not exist);
  - `<related>`: 577 passed on both;
  - `tests/brain tests/api tests/modules/brain` with the `make test-ci` markers: 4924 passed on the head; on `dev` sources, the same 6 failed and 4910 passed, and the persistence file does not import (run with `--continue-on-collection-errors`);
  - the issue's probe on both trees;
  - `ruff check .`, `make format-check`, `make workflow-check`, `make lint`, `make test-critical`.
- `run-linux-testci.log`, from `testci.sh`: the full `make test-ci` on the head, [run 40358597770](https://github.com/AliceMessi/linux-runner/actions/runs/40358597770). Result: 1 failed, 20385 passed, 29 skipped, 1361 deselected. The failure is the known `test_unreachable_db_raises_with_redacted_url` timeout. It happens only when the `postgres` extra is installed, and on `dev` too.
- `windows-persistence.log`, from `windows419c.sh`: native Windows 11, on clean `git archive` copies of the head:
  - `<focused> <persistence>` on the head: 31 passed;
  - a `dev` tree with the head's two test files: the same 6 failed, the persistence file does not import;
  - ablation C, both fields `exclude=True`: the 3 tests that read the saved state fail on both stores;
  - ablation D, `WorkingState` with `extra="ignore"`: only the extra-field test fails, on both stores;
  - `ruff check` / `ruff format --check` on the new test file.
- `windows-ablations-ab.log`, from `windows419b.sh` (unchanged from round 2), on the same head: the round-2 ablations A (trace-id check removed: the two-turn test fails) and B (the two assignments removed: the three continuation tests fail).

| file | sha256 |
|---|---|
| `run419c.sh` | `67b613e293079c52700f7524f1a1d1e7b00c0b4800d81695552e66f73b656b04` |
| `testci.sh` | `38e2a1e9d4eb4e63cd0d5e2e510fd8cc43a7d37bb8aeb2a191aeadddb70d9951` |
| `windows419b.sh` | `3c37864cbf5b7b6646e58340f27d0186573617998580e07bfa683bdc69058948` |
| `windows419c.sh` | `e3be9144bc98be11d394e23fb040ceddf1b1f7a86fc655bb7f441b0c1c8e9df8` |
