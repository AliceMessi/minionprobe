# MinionProbe matrix

- OpenMinion: `0.1.8` | Python 3.12.11 | Windows-11-10.0.22631-SP0
- Started: 2026-10-21T08:23:12+02:00 | Finished: 2026-10-21T09:11:32

| Provider | Model | Notes |
|---|---|---|
| local | `fake-model` | MinionProbe's scripted fake provider (offline, deterministic) |
| groq | `openai/gpt-oss-120b` | Groq free tier: 8K tokens/min, 1K requests/day per model |
| groq-lean | `openai/gpt-oss-20b` | Groq, NOT the default: runtime.memory_enabled=false and tools=[] for tool-less probes (fits the 8K TPM free tier) |
| gemini | `gemini-3.1-flash-lite` | Gemini API free tier via the OpenAI-compatible endpoint |
| gemma | `gemma-4-31b-it` | Gemma 4 served by Google AI Studio (not Cortensor Portal) |

PASS = all checks held; FAIL = a check failed; ERROR = probe crashed; BLOCKED = the provider (rate limit/quota) prevented a verdict; SKIP = not applicable. Times are wall-clock seconds, including rate-limit pacing.

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

## Totals

| Provider | PASS | FAIL | ERROR | BLOCKED | SKIP |
|---|---|---|---|---|---|
| local | 1 | 5 | 0 | 0 | 0 |
| groq | 5 | 0 | 0 | 7 | 0 |
| groq-lean | 6 | 4 | 0 | 1 | 0 |
| gemini | 6 | 6 | 0 | 0 | 0 |
| gemma | 1 | 1 | 0 | 0 | 0 |

## Details (non-PASS results)

### gemini / delegation.handoff: FAIL
- failed check: peer agent produced the answer (marker present): text='gemini: An HTTP 429 status code stands for "Too Many Requests." It is a client error response code indicating that the user has sent too many requests in a given amount of time ("rate limiting"). \n\nKe' transfer_called_on_wire=True peer_answer_on_wire=True
- observation `handoff_wire`: {'transfer_called': True, 'peer_answer_on_wire': True}

### gemini / diagnostics.run_stats: FAIL
- failed check: stats.llm_calls equals successful provider calls on every turn: 0/13 turns match; e.g. basic: stats=1 wire=2; structured: stats=1 wire=3; handoff: stats=1 wire=12; subagent: stats=1 wire=4; capture: stats=1 wire=3; recall: stats=1 wire=5; deny: stats=1 wire=6; allow: stats=1 wire=6; set: stats=1 wire=3; recall-isolated: stats=1 wire=3; skill: stats=1 wire=2; task.schedule: stats=2 wir
- observation `call_accounting`: {'turns': [{'turn': 'basic', 'stats_llm_calls': 1, 'http_ok_calls': 2, 'stats_input_tokens': 7683, 'wire_prompt_tokens': 8067}, {'turn': 'structured', 'stats_llm_calls': 1, 'http_ok_calls': 3, 'stats_input_tokens': 7703, 'wire_prompt_tokens': 8560}, {'turn': 'handoff', 'stats_llm_calls': 1, 'http_ok

### gemini / memory.cross_session_recall: FAIL
- failed check: new session recalls the fact: text='gemini: UNKNOWN'
- observation `memory_capture`: {'state': 'succeeded', 'control_call_errors': [], 'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'follow_up': 0, 'judge': 0, 'plan': 0, 'reflect': 0, 'self_compaction': 0, 'skill_selection': 0, 'summarize': 0, 'tool_shortlist': 0}}

### gemini / sessions.continuity_isolation: FAIL
- failed check: same session recalls the word: text='gemini: noted.'
- observation `followup_replayed`: {'label': 'recall-same', 'text': 'gemini: noted.'}

### gemini / skills.ingested_skill_applied: FAIL
- failed check: default ingest keeps the skill_id declared in the file: skill_id='skill' warnings=['parse.warning:companion_metadata_unavailable', 'admission.pending']
- observation `skill_turn`: {'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'follow_up': 0, 'judge': 0, 'plan': 0, 'reflect': 0, 'self_compaction': 0, 'skill_selection': 0, 'summarize': 0, 'tool_shortlist': 0}, 'http_requests': 2}

### gemini / tools.decorated_tool: FAIL
- failed check: final answer quotes the tool result: text='gemini: Order A-104 is currently in processing. It is scheduled to be shipped by the end of the week.'
- observation `grounded_answer_replaced`: {'detail': 'the agent-loop response contained the tool result, but result.text does not', 'final_text': 'gemini: Order A-104 is currently in processing. It is scheduled to be shipped by the end of the week.', 'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'follow_up': 0, 'judge': 0, 'plan': 0, 'ref

### gemma / tools.decorated_tool: FAIL
- failed check: turn completed without exception: TurnTimeoutError: Turn execution timed out after 180.00 seconds.

### groq / delegation.handoff: BLOCKED
- error: `provider rate limit persisted after 3 attempts: groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 5977, ...`
- observation `rate_limited_retry`: {'label': 'handoff', 'attempt': 1, 'wait_s': 30.0}
- observation `rate_limited_retry`: {'label': 'handoff', 'attempt': 2, 'wait_s': 30.0}
- observation `provider_error_returned_as_text`: {'label': 'handoff', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 5977, Requested 2130. Please 

### groq / memory.cross_session_recall: BLOCKED
- error: `provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The configured model provider could not continue this turn because it reported a quota, billing, or rate-limit block (groq rate limited: Rate limit reached for model `op...`
- observation `rate_limited_retry`: {'label': 'capture', 'attempt': 1, 'wait_s': 30.0}
- observation `rate_limited_retry`: {'label': 'capture', 'attempt': 2, 'wait_s': 30.0}

### groq / permissions.dangerous_tool_approval: BLOCKED
- error: `provider rate limit persisted after 3 attempts: groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7335, ...`
- observation `provider_error_returned_as_text`: {'label': 'allow', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7682, Requested 1697. Please tr
- observation `rate_limited_retry`: {'label': 'allow', 'attempt': 1, 'wait_s': 12.3}
- observation `provider_error_returned_as_text`: {'label': 'allow', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7508, Requested 1701. Please tr
- observation `rate_limited_retry`: {'label': 'allow', 'attempt': 2, 'wait_s': 11.1}

### groq / sessions.continuity_isolation: BLOCKED
- error: `provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The configured model provider could not continue this turn because it reported a quota, billing, or rate-limit block (groq rate limited: Rate limit reached for model `op...`
- observation `rate_limited_retry`: {'label': 'set', 'attempt': 1, 'wait_s': 30.0}
- observation `rate_limited_retry`: {'label': 'set', 'attempt': 2, 'wait_s': 30.0}

### groq / skills.ingested_skill_applied: BLOCKED
- failed check: skill appears in `openminion skill list`: No skills found.
- error: `provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The configured model provider could not continue this turn because it reported a quota, billing, or rate-limit block (groq rate limited: Rate limit reached for model `op...`
- observation `rate_limited_retry`: {'label': 'skill', 'attempt': 1, 'wait_s': 30.0}
- observation `rate_limited_retry`: {'label': 'skill', 'attempt': 2, 'wait_s': 30.0}

### groq / tasks.task_schedule_tool: BLOCKED
- error: `provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The configured model provider could not continue this turn because it reported a quota, billing, or rate-limit block (groq rate limited: Rate limit reached for model `op...`
- observation `rate_limited_retry`: {'label': 'task.schedule', 'attempt': 1, 'wait_s': 30.0}
- observation `provider_error_returned_as_text`: {'label': 'task.schedule', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7326, Requested 2745. P
- observation `rate_limited_retry`: {'label': 'task.schedule', 'attempt': 2, 'wait_s': 17.5}

### groq / tools.decorated_tool: BLOCKED
- error: `provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The configured model provider could not continue this turn because it reported a quota, billing, or rate-limit block (groq rate limited: Rate limit reached for model `op...`
- observation `rate_limited_retry`: {'label': 'tool', 'attempt': 1, 'wait_s': 30.0}
- observation `rate_limited_retry`: {'label': 'tool', 'attempt': 2, 'wait_s': 30.0}

### groq-lean / agent_execution.structured_output: FAIL
- failed check: turn completed without exception: AgentOutputValidationError: Reply did not validate against IssueLabel: 1 validation error for IssueLabel
  Invalid JSON: expected value at line 1 column 1 [type=json_invalid, input_value='groq-lean: {"label":"crash","confidence":0.9}', input_type=str]
    For further information visit https://errors.pydantic.dev/2.14/v/json_invalid

### groq-lean / delegation.handoff: BLOCKED
- error: `provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The configured model provider could not continue this turn because it reported a quota, billing, or rate-limit block (groq rate limited: Rate limit reached for model `op...`
- observation `provider_error_returned_as_text`: {'label': 'handoff', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq-lean: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-20b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7388, Requested 2266. Ple
- observation `rate_limited_retry`: {'label': 'handoff', 'attempt': 1, 'wait_s': 14.4}
- observation `provider_error_returned_as_text`: {'label': 'handoff', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq-lean: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-20b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7326, Requested 2140. Ple
- observation `rate_limited_retry`: {'label': 'handoff', 'attempt': 2, 'wait_s': 13.0}

### groq-lean / diagnostics.run_stats: FAIL
- failed check: stats.llm_calls equals successful provider calls on every turn: 0/9 turns match; e.g. basic: stats=1 wire=3; subagent: stats=1 wire=3; deny: stats=1 wire=2; allow: stats=2 wire=5; set: stats=1 wire=3; recall-isolated: stats=1 wire=2; skill: stats=1 wire=2; task.schedule: stats=1 wire=2; tool: stats=1 wire=3
- observation `call_accounting`: {'turns': [{'turn': 'basic', 'stats_llm_calls': 1, 'http_ok_calls': 3, 'stats_input_tokens': 1574, 'wire_prompt_tokens': 2595}, {'turn': 'subagent', 'stats_llm_calls': 1, 'http_ok_calls': 3, 'stats_input_tokens': 1584, 'wire_prompt_tokens': 2627}, {'turn': 'deny', 'stats_llm_calls': 1, 'http_ok_call

### groq-lean / sessions.continuity_isolation: FAIL
- failed check: same session recalls the word: text='groq-lean: noted.'
- observation `followup_replayed`: {'label': 'recall-same', 'text': 'groq-lean: noted.'}

### groq-lean / skills.ingested_skill_applied: FAIL
- failed check: default ingest keeps the skill_id declared in the file: skill_id='skill' warnings=['parse.warning:companion_metadata_unavailable', 'admission.pending']
- observation `skill_turn`: {'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'follow_up': 0, 'judge': 0, 'plan': 0, 'reflect': 0, 'self_compaction': 0, 'skill_selection': 0, 'summarize': 0, 'tool_shortlist': 0}, 'http_requests': 5}

### local / diagnostics.home_isolation: FAIL
- failed check: nothing written under the user's home: [".openminion\\identity\\identity.db"]
- observation `files_written_to_home`: {'files': ['.openminion\\identity\\identity.db']}

### local / diagnostics.rate_limit_after_tool: FAIL
- failed check: ProviderError(RATE_LIMITED) raised: run_state=completed text='local: RATE_LIMITED: openai rate limited: Rate limit reached for model fake-model. Please try again in 1s.'
- observation `provider_error_returned_as_text`: {'label': '429-after-tool', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'local: RATE_LIMITED: openai rate limited: Rate limit reached for model fake-model. Please try again in 1s.'}

### local / models.session_agent_binding: FAIL
- failed check: turn with agent_id=fake-b used model-b: models requested=['model-a'] metadata.agent=fake-a
- failed check: result metadata reports agent fake-b: metadata.agent=fake-a

### local / sessions.followup_not_replayed: FAIL
- failed check: follow-up 2 reached the provider: provider requests=0
- failed check: follow-up 2 answers the new message: text='local: FAKE-ANSWER(fake-model): first message ALPHA'
- failed check: follow-up 3 reached the provider: provider requests=0
- failed check: follow-up 3 answers the new message: text='local: FAKE-ANSWER(fake-model): first message ALPHA'

### local / tasks.cron_schedules: FAIL
- failed check: cron create --cron without --tz (default UTC): Invalid schedule: unknown timezone: UTC
- failed check: cron create --cron --tz UTC: Invalid schedule: unknown timezone: UTC
- failed check: cron create --cron --tz Europe/Rome: Invalid schedule: unknown timezone: Europe/Rome
- observation `tzdata_installed`: {'value': False}
- observation `cron_status`: {'output': 'Scheduler Status\n================\nState: unreachable\nReason: daemon_unreachable\nCheck: openminion service status cron'}

## Notable observations

- gemini / `agent_execution.basic_turn` `hidden_llm_calls`: {'http_requests': 2, 'control_calls': 1, 'stats_llm_calls': 1, 'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'follow_up': 0, 'judge': 0, 'plan': 0, 'reflect': 0, 'self_compaction': 0, 'skill_selection': 0, 'summarize': 0, 'tool_shortlist': 0}}
- gemini / `tools.decorated_tool` `grounded_answer_replaced`: {'detail': 'the agent-loop response contained the tool result, but result.text does not', 'final_text': 'gemini: Order A-104 is currently in processing. It is scheduled to be shipped by the end of the week.', 'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'fo
- groq / `agent_execution.basic_turn` `hidden_llm_calls`: {'http_requests': 1, 'control_calls': 0, 'stats_llm_calls': 1, 'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'follow_up': 0, 'judge': 0, 'plan': 0, 'reflect': 0, 'self_compaction': 0, 'skill_selection': 0, 'summarize': 0, 'tool_shortlist': 0}}
- groq / `agent_execution.structured_output` `rate_limited_retry`: {'label': 'structured', 'attempt': 1, 'wait_s': 30.0}
- groq / `agent_execution.structured_output` `rate_limited_retry`: {'label': 'structured', 'attempt': 2, 'wait_s': 30.0}
- groq / `delegation.handoff` `rate_limited_retry`: {'label': 'handoff', 'attempt': 1, 'wait_s': 30.0}
- groq / `delegation.handoff` `rate_limited_retry`: {'label': 'handoff', 'attempt': 2, 'wait_s': 30.0}
- groq / `delegation.handoff` `provider_error_returned_as_text`: {'label': 'handoff', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 
- groq / `memory.cross_session_recall` `rate_limited_retry`: {'label': 'capture', 'attempt': 1, 'wait_s': 30.0}
- groq / `memory.cross_session_recall` `rate_limited_retry`: {'label': 'capture', 'attempt': 2, 'wait_s': 30.0}
- groq / `permissions.dangerous_tool_approval` `provider_error_returned_as_text`: {'label': 'allow', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 80
- groq / `permissions.dangerous_tool_approval` `rate_limited_retry`: {'label': 'allow', 'attempt': 1, 'wait_s': 12.3}
- groq / `permissions.dangerous_tool_approval` `provider_error_returned_as_text`: {'label': 'allow', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 80
- groq / `permissions.dangerous_tool_approval` `rate_limited_retry`: {'label': 'allow', 'attempt': 2, 'wait_s': 11.1}
- groq / `permissions.dangerous_tool_approval` `provider_error_returned_as_text`: {'label': 'allow', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Limit 80
- groq / `sessions.continuity_isolation` `rate_limited_retry`: {'label': 'set', 'attempt': 1, 'wait_s': 30.0}
- groq / `sessions.continuity_isolation` `rate_limited_retry`: {'label': 'set', 'attempt': 2, 'wait_s': 30.0}
- groq / `skills.ingested_skill_applied` `rate_limited_retry`: {'label': 'skill', 'attempt': 1, 'wait_s': 30.0}
- groq / `skills.ingested_skill_applied` `rate_limited_retry`: {'label': 'skill', 'attempt': 2, 'wait_s': 30.0}
- groq / `tasks.task_schedule_tool` `rate_limited_retry`: {'label': 'task.schedule', 'attempt': 1, 'wait_s': 30.0}
- groq / `tasks.task_schedule_tool` `provider_error_returned_as_text`: {'label': 'task.schedule', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-120b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): 
- groq / `tasks.task_schedule_tool` `rate_limited_retry`: {'label': 'task.schedule', 'attempt': 2, 'wait_s': 17.5}
- groq / `tools.decorated_tool` `rate_limited_retry`: {'label': 'tool', 'attempt': 1, 'wait_s': 30.0}
- groq / `tools.decorated_tool` `rate_limited_retry`: {'label': 'tool', 'attempt': 2, 'wait_s': 30.0}
- local / `diagnostics.rate_limit_after_tool` `provider_error_returned_as_text`: {'label': '429-after-tool', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'local: RATE_LIMITED: openai rate limited: Rate limit reached for model fake-model. Please try again in 1s.'}
- gemma / `agent_execution.basic_turn` `hidden_llm_calls`: {'http_requests': 4, 'control_calls': 3, 'stats_llm_calls': 1, 'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'follow_up': 0, 'judge': 0, 'plan': 0, 'reflect': 0, 'self_compaction': 0, 'skill_selection': 0, 'summarize': 0, 'tool_shortlist': 0}}
- groq-lean / `agent_execution.basic_turn` `hidden_llm_calls`: {'http_requests': 3, 'control_calls': 2, 'stats_llm_calls': 1, 'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'follow_up': 0, 'judge': 0, 'plan': 0, 'reflect': 0, 'self_compaction': 0, 'skill_selection': 0, 'summarize': 0, 'tool_shortlist': 0}}
- groq-lean / `delegation.handoff` `provider_error_returned_as_text`: {'label': 'handoff', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq-lean: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-20b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Li
- groq-lean / `delegation.handoff` `rate_limited_retry`: {'label': 'handoff', 'attempt': 1, 'wait_s': 14.4}
- groq-lean / `delegation.handoff` `provider_error_returned_as_text`: {'label': 'handoff', 'code': 'RATE_LIMITED', 'run_state': 'completed', 'text': 'groq-lean: RATE_LIMITED: groq rate limited: Rate limit reached for model `openai/gpt-oss-20b` in organization `org_REDACTED` service tier `on_demand` on tokens per minute (TPM): Li
- groq-lean / `delegation.handoff` `rate_limited_retry`: {'label': 'handoff', 'attempt': 2, 'wait_s': 13.0}
- groq-lean / `tasks.task_schedule_tool` `rate_limited_retry`: {'label': 'task.schedule', 'attempt': 1, 'wait_s': 62.0}
