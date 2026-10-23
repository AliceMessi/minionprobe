# Release verification of openminion 0.1.10 (installed package)

This folder covers the installed-package checks the maintainer asked for on openminion/openminion #328, #342, #345, #347 and #349, after 0.1.10 was published on PyPI and GitHub on 23 Oct 2026. Everything below ran against `openminion==0.1.10` installed from PyPI into a fresh virtual environment, not against a source checkout. The offline probes are standalone scripts that use only the installed package; the wheel does not ship the repo's `tests/` fixtures. The #328 check is a live run against Gemini. Run on 23 Oct 2026.

## Package provenance

| | |
|---|---|
| Install | `uv pip install --python venv/Scripts/python.exe --index-url https://pypi.org/simple "openminion==0.1.10"` into a new `uv venv` (pytest was also installed but not used) |
| Wheel | `openminion-0.1.10-py3-none-any.whl`, uploaded 2026-10-23T06:59:18Z |
| sha256 (PyPI JSON) | `c8f093890f1715d20af5a674d6c5d925438492cca40f87a98a26e5140a154a09` |
| sha256 (downloaded wheel) | `c8f093890f1715d20af5a674d6c5d925438492cca40f87a98a26e5140a154a09` (match) |
| Installed files | All 2742 hashed RECORD entries of the wheel are in the installed RECORD and match the installed files' sha256: 0 mismatched, 0 missing |
| `pip show` (`uv pip show`) | `Name: openminion`, `Version: 0.1.10`, `Location: <VENV>\Lib\site-packages` |
| Imported module | Every probe prints `openminion.__file__` = `<VENV>\Lib\site-packages\openminion\__init__.py` and `__version__` = `0.1.10`. Probes run from empty directories outside any checkout, with `python -I` |
| Environment | CPython 3.12.11, native Windows 11 Pro 10.0.22631 |
| GitHub release | `v0.1.10`, published 2026-10-23T07:03:25Z. The annotated tag `14bc230d` points to commit `898bf2d1`. This was read from the GitHub API; I did not rebuild the wheel from that commit |

Details are in [`provenance.json`](provenance.json) and [`probes/provenance.py`](probes/provenance.py).

## Results

| Issue | Maintainer's acceptance check | Result | Evidence |
|---|---|---|---|
| #342 | Explicit SDK home/data roots govern all runtime artifact construction sites; no artifact index is written beneath the process cwd | **PASS** (15/15) | [`offline/probe_342.json`](offline/probe_342.json), [`offline/repro_342-issue-script.txt`](offline/repro_342-issue-script.txt) |
| #345 | Run status, invocation lookup and context slices preserve event insertion order under a reversed wall clock | **PASS** (25/25) | [`offline/probe_345.json`](offline/probe_345.json) |
| #347 | Gemini's OpenAI-compatible endpoint receives one ordered system message with the rendered context plus schema instructions; other endpoints keep their request shapes | **PASS** (25/25 offline; every live Gemini request below has exactly one system message) | [`offline/probe_347.json`](offline/probe_347.json), [`live/`](live) |
| #349 | The runtime-created or factory-supplied memory artifact controller closes exactly once after successful shutdown; repeated close is safe; `scheduler_still_running` leaves resources live | **PASS** (20/20). The factory and scheduler parts were checked through the installed builder, because `APIRuntime` cannot inject either | [`offline/probe_349.json`](offline/probe_349.json) |
| #328 | The same tool-backed SDK scenario on live Gemini with installed 0.1.10: actual judge request and final result, rendered tool evidence reaches the judge, the successful result keeps `ZEBRA-42` | **PASS**: 3 attempts, 3 with a judge HTTP 200, no 503s | [`live/runs-summary.json`](live/runs-summary.json) |

### #342 (`probes/probe_342.py`, `probes/repro_342.py`)

The probe runs two echo-provider runtimes on explicit roots, with `OPENMINION_*` unset and `HOME`/`USERPROFILE`/`Path.home` redirected. The process cwd and the redirected home each hold a populated default artifact store with the alias `shared-note`. Every `ArtifactCtl` built while the runtimes are alive is recorded with its index path and the code that built it.
- Each runtime builds 3 controllers: `services/runtime/memory.py::_runtime_artifactctl`, `modules/brain/adapters/factory/artifactctl.py::resolve_artifactctl` and `services/runtime/bootstrap.py::build_brain_runner_bundle`. All 6 open their own runtime's `<root>/.openminion/artifact/index.db`, and no other index is opened.
- Neither runtime resolves the unrelated alias. Each reads back only its own artifact, and neither lists the other runtime's artifacts or the unrelated stores'.
- The cwd and home stores are byte-for-byte unchanged, and no file is written under either.
- The issue's reproducer, unchanged, prints `completed`, `written under cwd: []` and `written under home: []`, and `proj/.openminion/artifact/index.db` exists.

### #345 (`probes/probe_345.py`)

The store's clock (`session_store.lifecycle.utc_now_iso`) is patched to step back, as in the issue and the PR tests, with no sleeps.
- **Owner:** ascending and descending order, limits from both ends, the `run.` prefix filter, agreement with `list_events_after_id` / `list_events_before_id` / `event_high_water`, and stored `created_at` returned unchanged.
- **Run status:** the issue's reproducer reads `failed` / `turn.failed`. Its events are listed as ids `[1, 2, 3]`, `limit=1` returns `turn.failed`, and `ended_at` keeps the stored stamp, which is earlier than `started_at`.
- **Invocation lookup:** `resolve_thread_lifecycle` reports the second invocation (`run-2`, `running`, `invocation-run-2` from event 3), with the last inserted event as `latest_event_id` / `updated_at`.
- **Context slice:** `last_event_id` is the high water, and tool events are in insertion order.
- **Real SDK turns,** with every event append reading a clock 1 ms earlier than the last:
  - An echo turn reads `completed`, with its events in id order.
  - A turn against a local scripted server that answers 401 reads `failed` / `turn.failed`, with `queued` → `running` → `failed`.
  - In both, the stored timestamps descend.

### #347 (`probes/probe_347.py`)

Offline. `urllib.request.urlopen` and `httpx.Client.send` are replaced in-process, so the captured bodies are the serialized bytes the installed adapter would send. Non-loopback sockets are blocked (none were attempted), and credentials are fixture values.
- **Profile:** the Google endpoint identity resolves to `gemini_openai_compat`, which differs from the default only in `collapse_system_messages=True`. A Gemini model behind a gateway, a lookalike host, an OpenRouter Gemini model and no identity all keep `openai_default`.
- **Schema-only request** (the judge/repair/reflect shape):
  - The `gemini` preset, a custom-named connection to the Google endpoint, and a config whose identity is derived from the base URL each POST to `generativelanguage.googleapis.com` with roles `[system, user]`. The single system message equals the gateway request's system messages joined in order (`Primary system context.` then `Tool-calling contract:`), and all other fields match the gateway request.
  - The gateway and OpenAI keep `openai_default` `[system, system, user]`, MiniMax keeps `minimax_openai_compat` `[system, user]`, and Cortensor Portal keeps `cortensor_portal` `[system, system, user]`.
- **Ordinary tool-call transcript,** complete and stream: Gemini sends one leading system message (the gateway's three system messages joined in order), and the user, assistant `tool_calls` and tool turns, `tools`, `tool_choice` and the stream fields are unchanged. The gateway keeps `[system, system, user, assistant, tool, system]`.
- **SDK turns** (the #328 scenario through `APIRuntime`/`Agent`, answered by MinionProbe's scripted fake logic):
  - On the `gemini` preset, all 8 requests have exactly one system message, and the closure judge's system message holds `[JUDGE CONTEXT]` with the tool output ahead of the `Tool-calling contract:`.
  - The gateway and OpenAI keep `[system, system, user]` for the control and judge calls, and MiniMax keeps its single-system shape.

### #349 (`probes/probe_349.py`)

- **SDK path:** this is the maintainer's probe, rebuilt without `tests._csc_fixtures`: `memory_v2`, single-process echo, explicit roots. One turn builds 3 controllers, and the memory assembly's is the first. The close counts are `[1, 1, 1]` after `runtime.close()` twice and stay `[1, 1, 1]` after a third call. After shutdown, `ingest_bytes` on the memory controller raises `sqlite3.ProgrammingError: Cannot operate on a closed database.`
- **Factory-supplied and default controllers:** through `build_memory_v2_runtime_assembly`, each closes once across two `close()` calls, and the second call returns the same result.
- **`scheduler_still_running`:** a stub scheduler stays alive after `stop()`. `close()` returns `closed=False` twice, the controller and the memory service have 0 closes, and the controller still accepts a write. After the scheduler stops, the service closes once and the controller once, and a repeat call is a no-op. This was checked for both the factory and the default controller.
- **No controller obtained:** a factory that returns `None` or raises, and the disabled, smoke and backend-none assemblies, leave the assembly owning nothing, and the default factory is never called.
- **Not reachable through the public SDK:** the factory, scheduler and no-controller cases call the installed `openminion.services.runtime.memory` builder directly, because `APIRuntime` cannot inject a factory or scheduler. The gateway fallback, the cron executor and the audit sink (#361) are out of scope and were not checked.

### #328 live (`probes/capture_328_v0110.py`, `probes/gated_v0110.py`)

The scenario from the original report: a decorated `lookup_order` tool with `forced_tools`, the prompt "Check order A-104", the `gemini` preset with `gemini-3.1-flash-lite`, and an isolated home per run. The first run used the original pickup code `ZEBRA-42`, and later runs used 43 and 44. Each attempt started only after a ping to the endpoint returned 200. The limit was 8 attempts, with a target of 3 attempts with a judge HTTP 200. Every `/chat/completions` request and response body was recorded; headers were not.

| Attempt | Code | HTTP statuses | 503 / non-200 before the judge | Judge (HTTP) | Judge request | Judge verdict | Result (`result.text`) | Keeps the code |
|---|---|---|---|---|---|---|---|---|
| `gemini-v0110-run0` | ZEBRA-42 | 200 ×5 | none | 200 | `[system, user]`; the one system message has `[JUDGE CONTEXT]` and `closure_action_outputs` with `order A-104: ready for pickup, pickup code ZEBRA-42` ahead of the `Tool-calling contract:` (3104 prompt tokens) | close, satisfied, `final_answer`: "Order A-104 is ready for pickup, and your pickup code is "ZEBRA-42"." | `gemini: Order A-104 is ready for pickup, and your pickup code is "ZEBRA-42".` (the judge's answer) | yes |
| `gemini-v0110-run1` | ZEBRA-43 | 200 ×6 | none | 200 | same shape, with the tool output for ZEBRA-43 (3194 prompt tokens) | close, satisfied, `final_answer`: "Order A-104 is ready for pickup, and your pickup code is "ZEBRA-43"." | `gemini: Your order A-104 is ready for pickup with the pickup code "ZEBRA-43".` (from one more agent-loop call after the judge, see below) | yes |
| `gemini-v0110-run2` | ZEBRA-44 | 200 ×5 | none | 200 | same shape, with the tool output for ZEBRA-44 (3109 prompt tokens) | close, satisfied, `final_answer`: "Order A-104 is ready for pickup with the pickup code "ZEBRA-44"." | `gemini: Order A-104 is ready for pickup with the pickup code "ZEBRA-44".` (the judge's answer) | yes |

That makes 3 attempts and 3 judged runs. No attempt got a 503 or any other non-200 status, so none had to be set aside as provider-unavailable. All 16 captured requests (reflect, `FreshnessContract`, agent loop, judge) have exactly one system message. No final-answer repair call was made. The gate log is in [`live/gate.log`](live/gate.log).

## Observations outside the acceptance checks

- **Empty browser directories under the cwd.** Constructing an SDK runtime creates empty directories under the process cwd: `.openminion/browser-playwright/{artifacts/{pdf,screenshots,traces},downloads}`. This happens in every probe, with or without `OPENMINION_*` set. The Playwright browser provider creates them when it registers at tool bootstrap (`tools/browser/providers/playwright/artifacts.py::ensure_dirs`), because its artifact and download directories default to the workspace root, which falls back to `os.getcwd()` (`tools/browser/providers/playwright/config.py`). They hold no files and no artifact index, and they do not come from `ArtifactCtl`, so #342's check is not affected.
- **Extra agent-loop call in `gemini-v0110-run1`.** In this #328 run (ZEBRA-43), one more agent-loop request followed the judge's close (seq 5). It has a different system prompt with `[IDENTITY]` / `[TOOL RESULT FORMAT]` sections and the generic continuation user turn ("Continue the active task using the existing conversation and tool results..."). Its answer, which also contains the code, became `result.text` instead of the judge's `final_answer`. This request also has one system message. Runs 0 and 2, and the earlier #351 runs in [`../openminion-347`](../openminion-347), had no request after a judge 200. I did not trace which component issues it.

## Files

- `provenance.json`: PyPI metadata, wheel hash comparison and the RECORD check.
- `offline/probe_*.json` / `.log`: every check with observed values, and the console output.
- `offline/repro_342-issue-script.txt`: the #342 issue's reproducer output on 0.1.10.
- `live/gemini-v0110-run*-turn.json`: the full turn record, with every HTTP call (host, path, status, request and response body), the per-judge request shape, the agent-loop answers and the final result.
- `live/gemini-v0110-run*-judge0-exchange.json`: the complete closure-judge request and response.
- `live/runs-summary.json`: one row per attempt. `live/gate.log`: pings and attempts in order.
- `probes/`: the scripts as run. The only change is that `PROBE_REPO` resolves to this repository. They expect a venv with `openminion==0.1.10`; `gated_v0110.py` expects it at `../venv` relative to `probes/`.

The files are redacted with MinionProbe's redactor: no keys, org ids or auth headers, and request headers were never recorded. Local paths are replaced with `<VENV>`, `<VERIFY_DIR>`, `<WORK>`, `<ISOLATED_TMP_HOME>` and `<USER_HOME>`. Every file was then scanned for key shapes, the literal key value and local paths: no hits.
