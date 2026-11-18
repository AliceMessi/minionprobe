# OpenMinion 0.1.23: published-package verification of #745

#745 reported that the README's Python `Agent` snippet stopped at a policy confirmation. PR #747 (merge `443d5e33`, reviewed head `f2a3eb29`) fixed the README, the `tool()` docstring and `docs/runtime-surfaces.md`. This folder checks the published 0.1.23 against the maintainer's checklist: https://github.com/openminion/openminion/issues/745#issuecomment-6872339773

## Results

| Check | Windows 11, CPython 3.12.11 | Linux, Ubuntu 24.04.5, CPython 3.12 |
|---|---|---|
| `probe_745.py` on `openminion==0.1.23`, with the 0.1.23 PyPI README | 5/5 PASS (exit 0) | 5/5 PASS (exit 0) |
| Negative control on `openminion==0.1.21`, with the 0.1.21 PyPI README | 2/5 (exit 1): bare `@tool`, confirmation prompt, no docstring mapping | same |
| `release_source_745.py`: PyPI wheel, sdist and description vs tag `v0.1.23` | 10/10 PASS | n/a |

Linux run: https://github.com/AliceMessi/linux-runner/actions/runs/44235524924

## Layout

- `probes/`:
  - `probe_745.py` (sha256 `e649cd94...`);
  - `_common.py` and `probe_420_lib.py`, byte copies from `evidence/openminion-article-followups/probes/`;
  - `linux_setup.sh`, the Linux runner script (`steps_745.sh` is an unused placeholder required by the bundler);
  - `release_source_745.py`.
- `windows/745/`: 0.1.23 output and `probe-sha256.txt`. `control-0.1.21/` holds the negative control.
- `linux/745/`: runner output for both versions (`probe_745_<version>.json/.log/.exit`, pip lists, `os.txt`, `probes.sha256`, `readme-sha256.txt`, run id).
- `release-source/`:
  - `release_source.log`;
  - `published-v0.1.23/`: byte copies of `README.md`, `docs/runtime-surfaces.md` and `examples/sdk/tool_agent.py` at the tag.

Offline: every provider is a scripted OpenAI-compatible server on 127.0.0.1. Local paths are replaced with `<VENV>`, `<WORK>`, `<RUNNER_TEMP>` and similar tokens.
