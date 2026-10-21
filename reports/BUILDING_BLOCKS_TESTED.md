# Building Blocks Tested

MinionProbe v0.1, runs of 2026-10-21. `openminion==0.1.8` (latest PyPI release), Windows 11 Pro, Python 3.12.11.

Columns:

- **local**: MinionProbe's scripted OpenAI-compatible fake provider. Offline and deterministic; used for fault injection and bug reproductions.
- **groq**: `openai/gpt-oss-120b`, free tier, OpenMinion defaults.
- **groq-lean**: Groq `openai/gpt-oss-20b` with `runtime.memory_enabled=false`, and `tools=[]` for probes that need no tools (so the ~6–7K-token default tool catalog is not sent). **Not** the default. It was used because the defaults exhausted the free tier (see `WHAT_WE_LEARNED.md` §3).
- **gemini**: `gemini-3.1-flash-lite`, free tier, OpenMinion defaults.

Statuses:

- PASS: every check held.
- FAIL: a check failed (details below and in the JSON).
- BLOCKED: the provider's rate limit or quota stopped the probe, so this is no verdict on OpenMinion.
- SKIP: not applicable.

Every check is deterministic: a nonce only the tool or skill knows, request counts on the wire, files on disk, or exception types.

## Matrix

| Block | Probe | local | groq | groq-lean | gemini | gemma |
|---|---|---|---|---|---|---|
| Agent execution | `basic_turn`: Plain Agent.run turn follows an exact instruction | - | **PASS** 8s | **PASS** 7s | **PASS** 44s | **PASS** 89s |
| Agent execution | `structured_output`: Structured output with output_type=PydanticModel | - | **PASS** 168s | **FAIL** 26s | **PASS** 16s | - |
| Models / providers | `provider_routing`: Requests reach the configured provider host and model | - | **PASS** 0s | **PASS** 0s | **PASS** 0s | - |
| Models / providers | `session_agent_binding`: Explicit agent_id wins over an existing session's agent | **FAIL** 6s | - | - | - | - |
| Tools | `decorated_tool`: @tool result reaches the model and the final answer | - | **BLOCKED** 134s | **PASS** 9s | **FAIL** 96s | **FAIL** 192s |
| Permissions | `dangerous_tool_approval`: dangerous=True tool asks approval_callback; deny blocks, allow runs | - | **BLOCKED** 254s | **PASS** 95s | **PASS** 107s | - |
| Sessions | `continuity_isolation`: Same session_id remembers; a different session_id is isolated | - | **BLOCKED** 155s | **FAIL** 115s | **FAIL** 113s | - |
| Sessions | `followup_not_replayed`: Follow-up Agent.run on the same session runs a new turn (no replay) | **FAIL** 0s | - | - | - | - |
| Memory | `cross_session_recall`: Durable fact captured in session A is recalled in session B | - | **BLOCKED** 193s | - | **FAIL** 72s | - |
| Skills | `ingested_skill_applied`: Ingested SKILL.md keeps its skill_id, is admitted and applied | - | **BLOCKED** 121s | **FAIL** 45s | **FAIL** 35s | - |
| Tasks / scheduling | `cron_schedules`: `openminion cron create` accepts cron/at schedules and timezones | **FAIL** 32s | - | - | - | - |
| Tasks / scheduling | `task_schedule_tool`: Model schedules a one-shot job through the task.schedule tool | - | **BLOCKED** 171s | **PASS** 133s | **PASS** 55s | - |
| Delegation | `handoff`: Handoff(target=...) transfers the turn to a peer agent | - | **BLOCKED** 184s | **BLOCKED** 185s | **FAIL** 22s | - |
| Delegation | `subagent_run`: subagent(parent, ...) runs a bounded child with its own instructions | - | **PASS** 63s | **PASS** 59s | **PASS** 22s | - |
| Diagnostics | `home_isolation`: Explicit home_root/data_root keep all state out of ~/.openminion | **FAIL** 7s | - | - | - | - |
| Diagnostics | `provider_unreachable`: Unreachable provider raises ProviderError (no fake success) | **PASS** 27s | - | - | - | - |
| Diagnostics | `rate_limit_after_tool`: 429 on the call after a tool runs raises ProviderError | **FAIL** 2s | - | - | - | - |
| Diagnostics | `run_stats`: result.stats agree with the HTTP traffic (calls and tokens) | - | **PASS** 0s | **FAIL** 0s | **FAIL** 0s | - |

### Totals

| Provider | PASS | FAIL | ERROR | BLOCKED | SKIP |
|---|---|---|---|---|---|
| local | 1 | 5 | 0 | 0 | 0 |
| groq | 5 | 0 | 0 | 7 | 0 |
| groq-lean | 6 | 4 | 0 | 1 | 0 |
| gemini | 6 | 6 | 0 | 0 | 0 |
| gemma | 1 | 1 | 0 | 0 | 0 |

The `groq` column's `run_stats` PASS comes from an earlier version of that probe, which only checked the bare ping turn (1 call vs 1 call). The current version compares every turn; it FAILs on `groq-lean` (stats total 10 vs 25 successful calls) and on `gemini` (14 vs 62).

Full per-probe evidence: `results/2026-10-21/MATRIX.json`. Triage (rules vs agent): `results/2026-10-21/triage/TRIAGE-MATRIX.md`.

## Per block: what we used, what worked, what did not

| Block | What we used | Worked | Did not work / caveats | Draft |
|---|---|---|---|---|
| **Agent execution** | `Agent(instructions=..., output_type=IssueLabel)`, `run(timeout_seconds=)`, `AgentRunResult.stats/metadata` | Exact-instruction turn on all real providers; Pydantic `output_type` on Groq `gpt-oss-120b` and Gemini (`gpt-oss-20b` produced an out-of-enum label, which was correctly rejected) | `stats.llm_calls` counts loop steps, not provider calls (1 vs up to 8 HTTP calls). A bare turn without `tools=` sends ~7.7K input tokens | – |
| **Models / providers** | `openminion setup --provider groq/gemini/custom-openai-compatible --no-chat`, `Agent(agent_id=...)` | Routing to the configured host and model; setup makes no network call and stores only the key variable name | `agent_id` is ignored when the session was first used by another agent: the turn runs on the other provider | 03 |
| **Tools** | `@tool` with a `str` return, `tools=[fn]`, `forced_tools=` | Tool offered, executed, and its string result reaches the model (fixes for #248/#249 confirmed on the wire); Groq `gpt-oss-20b` quoted it correctly | Gemini: the closure judge replaced the grounded answer with a refusal (4/4 on 0.1.8, 2/2 on dev) | 06 |
| **Permissions** | `@tool(dangerous=True)`, `approval_callback` deny then allow | Deny blocked execution and allow ran it, with the callback receiving tool name and args (Gemini, groq-lean) | Groq defaults: BLOCKED by rate limits (passes on `groq-lean`) | – |
| **Sessions** | same vs different `session_id` | Isolation: a new session does not see the conversation | **A follow-up run on the same session replays the first reply**, with 0 provider calls (local, echo, Gemini, Groq; 0.1.4, 0.1.8 and dev) | 02 |
| **Memory** | durable capture plus recall in a new session | Capture reports `memory_capture_state=succeeded` (Gemini) | Recall in a new session answered UNKNOWN (Gemini). On Groq, memory capture's `submit_output` is rejected with 400 `tool_use_failed` and retried | – |
| **Skills** | `openminion skill ingest/admit/list`, skill use in a turn | After `ingest --name` + `admit` (expected hash `none`), Gemini and Groq applied the skill-only fact | Default ingest stores every `SKILL.md` as `skill_id "skill"` (collisions). Drafts are invisible to `skill list`/`show` | 07 |
| **Tasks / scheduling** | `openminion cron create --at/--cron/--tz`, model-facing `task.schedule` tool | `--at` jobs; Gemini and Groq scheduled a one-shot job through `task.schedule` without needing approval | On Windows every `--cron` schedule fails with `unknown timezone: UTC` (no `tzdata`) | 05 |
| **Delegation** | `subagent(parent, name=, instructions=, timeout_seconds=)`, `Handoff(target=)` + `forced_tools=["transfer_to_researcher"]` | `subagent` followed its own instructions on Groq and Gemini | Gemini: the handoff ran and the peer's answer is on the wire, but `result.text` was re-synthesized without it (draft 06 mechanism). `subagent(memory_posture="read_only_bounded")` raises "not bound in v1" (documented as internal) | – (#284 known) |
| **Diagnostics** | `ProviderError`, `TurnTimeoutError`, stats vs wire, isolated homes | A closed port raises `ProviderError` (the #251 fix works for the first call) | A 429 later in a turn returns as reply text with `run_state=completed`. `identity.db` is written to `~/.openminion` despite explicit roots | 04, 01 |

## Bugs found (all drafts in `issue-drafts/`, none filed)

| # | Title | Repro | 0.1.8 | dev `b642697` |
|---|---|---|---|---|
| 01 | `identity.db` written to the user home despite `home_root`/`data_root` | echo, offline | yes | yes |
| 02 | Follow-up `Agent.run` on the same session replays the first reply | echo, offline | yes (and 0.1.4) | yes |
| 03 | `agent_id` ignored for an existing session | fake provider, offline | yes | yes |
| 04 | 429 later in a turn returned as text with `run_state=completed` | fake provider, offline; Groq, Gemini | yes | yes |
| 05 | Windows: every `cron create --cron` fails (`tzdata` missing) | CLI, offline | yes | yes |
| 06 | Closure judge overwrites a correct tool-grounded answer | Gemini | yes | yes |
| 07 | `skill ingest` ignores the declared `skill_id`; every `SKILL.md` collides on `skill` | CLI, offline | yes | yes |

Known upstream issues, cited and not re-filed:

- #248, #249 and #251 are fixed in 0.1.5; the wire traces confirm #248 and #249, and #251 is confirmed for the first call only (see draft 04).
- #284 (handoff risk confirmation) did not show in our Gemini runs.
- #248 was also found independently by another Hackathon #5 entrant (Cubiczan/cortensor-trial-watch).

## Reproduce

```bash
python -m minionprobe run --providers local                      # offline, ~2 min: drafts 01–05 reproduce here
python -m minionprobe run --providers groq,gemini --blocks all   # needs GROQ_API_KEY / GEMINI_API_KEY
python -m minionprobe merge results/2026-10-21/run-*.json --out results/2026-10-21/MATRIX
```
