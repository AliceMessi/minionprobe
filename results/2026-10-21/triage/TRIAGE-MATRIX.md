# Triage of `MATRIX.json`

- Failures triaged: 24
- Triage agent: gemini (gemini-3.1-flash-lite)
- Nothing here has been filed upstream. Every draft needs human review first.

| Failure | Rule triage | Agent triage | Agent rationale |
|---|---|---|---|
| `gemini/delegation.handoff` | model_behavior: peer answered on the wire but the final text was re-synthesized (same mechanism as draft 06); needs more runs | model_behavior (0.8) | Model encountered rate limiting (429) during delegation, resulting in a failure to complete the task. |
| `gemini/memory.cross_session_recall` | model_behavior: capture succeeded, recall failed; may need memory_dynamic_retrieval_enabled; needs investigation | model_behavior (0.9) | The model failed to recall a fact across sessions, indicating a persistence or retrieval issue. |
| `gemini/sessions.continuity_isolation` | openminion_bug: matches verified draft issue-drafts/02-followup-run-replays-previous-reply.md | model_behavior (0.9) | Model failed to maintain proper session isolation boundaries. |
| `gemini/skills.ingested_skill_applied` | openminion_bug: matches verified draft issue-drafts/07-skill-ingest-ignores-skill-id.md | probe_bug (0.7) | Warning indicates metadata unavailability during skill ingestion, causing a probe failure. |
| `gemini/tools.decorated_tool` | openminion_bug: matches verified draft issue-drafts/06-closure-judge-overwrites-grounded-tool-answer.md | model_behavior (0.8) | Model failed to correctly incorporate tool output into its final response. |
| `gemini/diagnostics.run_stats` | openminion_bug: stats.llm_calls counts loop steps, not provider calls; product feedback, not drafted | probe_bug (0.7) | Mismatch between reported stats and actual network calls suggests potential instrumentation inaccuracy in the probe. |
| `groq/delegation.handoff` | provider_limitation: provider blocked the probe: provider rate limit persisted after 3 attempts: groq: RATE_LIMITED: groq rate limi | provider_limitation (1.0) | Provider rate limit (429) reached, blocking probe execution. |
| `groq/memory.cross_session_recall` | provider_limitation: provider blocked the probe: provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The co | provider_limitation (1.0) | Provider rate limit reached. |
| `groq/permissions.dangerous_tool_approval` | provider_limitation: provider blocked the probe: provider rate limit persisted after 3 attempts: groq: RATE_LIMITED: groq rate limi | provider_limitation (1.0) | Provider rate limit reached. |
| `groq/sessions.continuity_isolation` | provider_limitation: provider blocked the probe: provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The co | provider_limitation (1.0) | Provider rate limit reached. |
| `groq/skills.ingested_skill_applied` | provider_limitation: provider blocked the probe: provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The co | provider_limitation (1.0) | Provider rate limit reached. |
| `groq/tasks.task_schedule_tool` | provider_limitation: provider blocked the probe: provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The co | provider_limitation (1.0) | Provider rate limit reached. |
| `groq/tools.decorated_tool` | provider_limitation: provider blocked the probe: provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The co | provider_limitation (1.0) | Provider rate limit reached. |
| `local/diagnostics.rate_limit_after_tool` | openminion_bug: matches verified draft issue-drafts/04-429-after-tool-returned-as-completed-text.md | model_behavior (0.9) | The system correctly identified a rate limit error raised after a tool call. |
| `local/models.session_agent_binding` | openminion_bug: matches verified draft issue-drafts/03-agent-id-ignored-for-existing-session.md | model_behavior (0.8) | Model failed to correctly bind the agent_id specified in the session. |
| `local/sessions.followup_not_replayed` | openminion_bug: matches verified draft issue-drafts/02-followup-run-replays-previous-reply.md | model_behavior (0.8) | Incorrect handling of follow-up messages resulted in repeating previous answers. |
| `local/diagnostics.home_isolation` | openminion_bug: matches verified draft issue-drafts/01-identity-db-written-to-user-home.md | probe_bug (0.7) | Probe failure suggests an issue with environmental isolation verification. |
| `local/tasks.cron_schedules` | openminion_bug: matches verified draft issue-drafts/05-cron-unknown-timezone-on-windows-tzdata.md | openminion_bug (0.9) | System failed to parse valid timezones, indicating a bug in the task scheduling module. |
| `gemma/tools.decorated_tool` | provider_limitation: turn exceeded timeout_seconds (slow model/provider); no verdict on the feature itself | model_behavior (0.8) | Turn execution timed out, suggesting model latency or processing issues with tools. |
| `groq-lean/agent_execution.structured_output` | model_behavior: the model's JSON did not validate against output_type | model_behavior (0.8) | Model failed to produce valid structured JSON output as required. |
| `groq-lean/delegation.handoff` | provider_limitation: provider blocked the probe: provider rate limit persisted after 3 attempts: ProviderError RATE_LIMITED: The co | provider_limitation (1.0) | Provider rate limit reached. |
| `groq-lean/sessions.continuity_isolation` | openminion_bug: matches verified draft issue-drafts/02-followup-run-replays-previous-reply.md | model_behavior (0.8) | Failed to maintain session isolation, repeating content. |
| `groq-lean/skills.ingested_skill_applied` | openminion_bug: matches verified draft issue-drafts/07-skill-ingest-ignores-skill-id.md | probe_bug (0.7) | Warning indicates metadata issues during skill ingestion. |
| `groq-lean/diagnostics.run_stats` | openminion_bug: stats.llm_calls counts loop steps, not provider calls; product feedback, not drafted | probe_bug (0.7) | Mismatch between recorded stats and actual calls. |

## Agent-drafted issues (unreviewed)

- `results\2026-10-21\triage\drafts\local__tasks.cron_schedules.md`

## Verified drafts (human-reviewed, in `issue-drafts/`)

- `diagnostics.home_isolation` -> `issue-drafts/01-identity-db-written-to-user-home.md`
- `sessions.continuity_isolation` -> `issue-drafts/02-followup-run-replays-previous-reply.md`
- `sessions.followup_not_replayed` -> `issue-drafts/02-followup-run-replays-previous-reply.md`
- `models.session_agent_binding` -> `issue-drafts/03-agent-id-ignored-for-existing-session.md`
- `diagnostics.rate_limit_after_tool` -> `issue-drafts/04-429-after-tool-returned-as-completed-text.md`
- `tasks.cron_schedules` -> `issue-drafts/05-cron-unknown-timezone-on-windows-tzdata.md`
- `tools.decorated_tool` -> `issue-drafts/06-closure-judge-overwrites-grounded-tool-answer.md`
- `skills.ingested_skill_applied` -> `issue-drafts/07-skill-ingest-ignores-skill-id.md`

## Notes from the triage run

- rubric skill minionprobe_triage_rubric: ingested=True admitted=True
- attempt 1: triage agent classified 24/24 failures; stats={'input_tokens': 8067, 'output_tokens': 1478, 'cache_read_tokens': 0, 'llm_calls': 2, 'tool_calls': 1, 'tool_errors': 0, 'duration_ms': 16822}
