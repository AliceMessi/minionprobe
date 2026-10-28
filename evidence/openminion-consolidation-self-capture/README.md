# OpenMinion: a scheduled memory consolidation turn captures itself

The focused report requested on [openminion#449](https://github.com/openminion/openminion/issues/449#issuecomment-6459368215), split out of item 3 of that report. The cron payload builder marks a consolidation turn with `memory_consolidation_job: "true"`, and `capture_is_excluded` checks that key, but the brain never receives it. So the consolidation turn makes its own extraction request and commits its own capture bundle. The same metadata decides whether the brain attaches its memory consolidation context, and that context is not attached either. On a backend without atomic capture, each such turn adds a pending intent (the growth in [`openminion-unsupported-provider-extraction`](../openminion-unsupported-provider-extraction)).

## What ran

- Native Windows 11 Pro 10.0.22631, CPython 3.12.11, SQLite 3.50.4, 28 Oct 2026.
- One fresh `uv` venv with `openminion @ git+https://github.com/openminion/openminion@988ac4ec54ae803f4ac937a6fc2fc1c04bb5f590`, current `dev` ([`windows/dev-988ac4ec`](windows/dev-988ac4ec); `direct_url.json` records the commit). Its version string is 0.1.13.
- [`probes/probe_consolidation_self_capture.py`](probes/probe_consolidation_self_capture.py), run with `python -I -X utf8` from an empty directory outside any checkout. No repo test fixtures.
- [`probes/probe_420_lib.py`](probes/probe_420_lib.py) and [`probes/_common.py`](probes/_common.py) are byte-identical copies of the helpers in [`openminion-0.1.13-verification/probes`](../openminion-0.1.13-verification/probes) (see [`windows/probe-sha256.txt`](windows/probe-sha256.txt)).
- A real `APIRuntime` with the built-in memory store (`AuditedMemoryStore` over `SQLiteMemoryStore`) and one agent, `openminion`, whose `openai` provider points at a scripted OpenAI-compatible server on 127.0.0.1. The scripted extraction returns one item for the probe's capture message and none otherwise.
- Two turns:
  - **C (caller control):** an ordinary turn, "Please remember that I prefer tabs over spaces for indentation.", submitted with `runtime.submit_turn(payload={"message": ..., "session_id": "caller", "meta": {"memory_consolidation_job": "true"}})`.
  - **K (consolidation):** the installed `task.consolidate_memory` handler creates the job through the daemon's `TaskManager`, the daemon's own `attach_cron_scheduler(runtime=...)` runs it, and `TaskManager.run_task_now` queues one run.
- Nothing is injected. Call-through recorders, which return the original result, read the key `memory_consolidation_job` at three points:
  1. the manager request meta (`AgentRuntimeManager.submit_turn`);
  2. the ingress request's inbound metadata (`runtime_turn_request_from_manager_request`, as the daemon worker calls it);
  3. the brain message metadata that `capture_is_excluded` receives (`services/brain/post_execution/mixin.py`), and that the brain's resume hints receive (`_inject_resume_task_hints`, `services/brain/post_execution/context.py`).
  They also record whether `_attach_memory_consolidation_resume_context` is called, and each turn's capture intent, its bundle write and the provider's extraction requests.

## Results

10/10 checks pass.

| Point | K: consolidation turn | C: caller-supplied key |
|---|---|---|
| Manager request meta | `"true"` (with `cron_run_id`) | `"true"` |
| Ingress request inbound metadata | `"true"` | `"true"` |
| Brain message metadata (`capture_is_excluded` input) | missing (`cron_run_id` present) | missing |
| `capture_is_excluded` | `False` | `False` |
| Brain consolidation context (`_attach_memory_consolidation_resume_context`) | not attached (0 calls) | not attached |
| Extraction requests by the turn | 1 | 1 |
| Capture intent | `pending` (not `excluded`) | `pending` |
| Bundle write and `memory.capture.result` | 0 candidates, `succeeded_no_output` | 1 candidate, `succeeded` |

- The run finishes; recovery scans 0 (the built-in store has nothing pending).
- Direct calls: `capture_is_excluded({"memory_consolidation_job": "true"})` returns `True`, and `False` for `{}`. The resume hints' `_metadata_bool` also returns `True` for that key. The gateway's `_extract_ephemeral_prompt_metadata` keeps `cron_job_id`, `cron_run_id` and `scheduled_for` and drops `memory_consolidation_job`.
- Both turns run on channel `console`.
- So the key is lost in the gateway, for the scheduled turn and for a caller-supplied key alike. Today a caller cannot exclude its own turn from capture with this key (row C), and the cron executor cannot either (row K).

## Files

- `windows/dev-988ac4ec/probe_consolidation_self_capture.json`: the checks and, per turn, the key at each point, the `capture_is_excluded` and resume-hint inputs, whether the consolidation context was attached, the new intents with their result dispositions, the bundle writes and the provider requests by kind.
- `windows/dev-988ac4ec/probe_consolidation_self_capture.log`: the console output.
- `windows/dev-988ac4ec/pip-list.txt`, `pip-show.txt`, `direct_url.json`.
- Paths are sanitized (`<VENV>`, `<WORK>`).
