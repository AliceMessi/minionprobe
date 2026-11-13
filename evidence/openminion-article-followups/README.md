# OpenMinion: follow-ups to the HackerNoon Go-Live Gate article

The article "Building a Multi-Agent Website Launch Validator with OpenMinion" (https://hackernoon.com/building-a-multi-agent-website-launch-validator-with-openminion, code pinned to `openminion==0.1.14`) reports four problems. These offline probes check each one on `openminion==0.1.21` from PyPI.

| Probe | Article report | Result on 0.1.21 |
|---|---|---|
| `probe_tool_scope.py` | read-only tools still ask for approval | Intended (#464). The README `@tool` snippet still stops at the policy prompt. |
| `probe_fact_extraction.py` | the first request of every turn is not the agent's | Extraction runs first, carries the agent's instructions, and the agent-level switch is ignored. |
| `probe_retry_after.py` | the 300 ms retry vs a 60 s rate window | Confirms #717: two nested retry owners and 9 requests in about 3 s. |
| `probe_output_type.py` | `output_type` is not enforced | Intended: validated after the turn, with `AgentOutputValidationError` on failure. |

The `probe_output_type.py` checks marked FAIL come from probe-side wording, not from a defect: `raw_text` keeps the `openminion: ` prefix (#567), and `tool_choice` is `auto`.

Run: `linux/run.log` (GitHub ubuntu-24.04, `probes/linux_setup.sh`). Per-probe JSON and logs are in `linux/`.
