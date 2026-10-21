# What We Learned

MinionProbe v0.1, 2026-10-21. Target `openminion==0.1.8` (latest PyPI release on that date), Windows 11, Python 3.12.11. Real providers: Groq `openai/gpt-oss-120b` and Gemini `gemini-3.1-flash-lite`, both free tier. Everything below comes from recorded runs in `results/2026-10-21/` or from the hand reproductions in `issue-drafts/`. Where something is a hypothesis, it says so.

## 1. The headline: multi-turn SDK sessions are broken

`Agent.run(..., session_id=S)` works once. Every later run on `S` returns the **first** reply again, without calling the model: 0 HTTP requests, the same `run_id`, `thread_decision_action="replay"` (issue draft 02).

- It reproduces with the built-in echo provider, with the unmodified official `examples/sdk/sessions.py`, on Gemini and Groq, on 0.1.4, on 0.1.8 and on `dev`.
- The cause, from reading the source: SDK turns are sent with `deliver: False` and without the `caller_handles_delivery` flag that the CLI sets. The response is never marked delivered, so the thread stays `response_ready_undelivered` and is replayed forever.
- Why nobody noticed: the replay looks like a normal answer (`run_state=completed`), and it costs nothing, so token dashboards look *better*.

**Builder advice until fixed:** use a fresh `session_id` per turn and keep conversation state yourself. MinionProbe does this everywhere except the probes that test sessions.

## 2. Silent failures are the main pattern

Five of the seven bugs fail *silently*: `run_state=completed`, no exception, a plausible reply.

| Symptom | Where | Draft |
|---|---|---|
| Stale reply replayed | sessions | 02 |
| Turn runs on another agent's provider | models/sessions | 03 |
| Provider rate-limit text returned as the answer | diagnostics | 04 |
| Correct tool answer replaced by a refusal | tools/closure judge | 06 |
| Skill "ingested ok" but never usable | skills | 07 |

Product feedback: an SDK turn should either raise or carry a machine-readable flag (`result.metadata["replayed"]`, `["degraded_reason"]`) whenever it did not do what was asked. MinionProbe checks the wire (request counts, hosts, models) because `result` alone cannot be trusted for these cases.

## 3. Hidden calls and prompt size dominate cost, and break Groq's free tier

- **Prompt size.** A bare "ping" turn with no `tools=` argument sent **~7.7K input tokens**, because the full default tool catalog (100+ tools) goes into the system prompt. With an explicit `tools=[...]` list, a turn with a tool call was ~1.8–3.3K tokens.
  - Advice: always pass `tools=[...]`, or `tools=[]` since #269.
- **Hidden calls.** Besides the agent loop, OpenMinion makes forced `submit_output` "control" calls (memory reflect, freshness classifier, closure judge). Over the 14 Gemini turns of the first full run there were 64 HTTP calls, 30 of them control calls; a single handoff turn made 8.
- **`result.stats["llm_calls"]` reported 1 on most of those turns** (16 on the one skill turn), so it counts agent-loop steps, not provider calls.
  - In the final Gemini run, 13 completed turns summed to `stats.llm_calls = 14`, against **62 successful provider calls** on the wire.
  - Cost dashboards built on `stats` undercount by 2–8× per turn. The `run_stats` probe records this for every turn.
- **Groq free tier (8K tokens/minute):** with OpenMinion defaults, 7 of 12 Groq probes were **BLOCKED** by rate limits, even with header-based pacing and full-window waits. Six hit the per-minute limit; the last one hit the 200K/day cap.
  - The recorded final attempts alone made 136 HTTP calls, of which only 8 returned 200. There were 26 × 400 and 102 × 429.
  - Only 1 of 26 hidden control calls succeeded. 22 of the 400s were Groq rejecting the `submit_output` arguments; 2 more were "Tool choice is required, but model did not call a tool".
  - Two OpenMinion behaviours make it worse:
    - Groq validates tool arguments server-side. The memory-capture `submit_output` schema was rejected with `400 tool_use_failed` (`missing properties: 'kind'`, `additionalProperties 'bounded_key' not allowed`), and OpenMinion retried it 3 times. Each retry spends ~1.7K tokens of the minute's budget before the user's turn starts.
    - On 429, OpenMinion retried almost immediately. Our worst trace had 14 rate-limited requests in about 7 s, 0.2–1 s apart, while Groq's body said "Please try again in 7.9s". Then it gave up and, after a tool call, returned the error as the reply (draft 04).
  - **Daily cap:** by 08:48 the account had used 199,199 of Groq's 200,000 tokens/day for `openai/gpt-oss-120b`. That covered a few smoke turns plus one MinionProbe run of 11 turns. Most of it went to hidden calls, the 7.7K-token default tool catalog, and rejected retries. Even a bare "ping" turn now asks for ~6.4K tokens (`Requested 6424`).
- The `groq-lean` profile (`runtime.memory_enabled=false`, clearly non-default) was then run on `openai/gpt-oss-20b`, which has a separate daily quota, to get Groq verdicts on the other blocks. See `BUILDING_BLOCKS_TESTED.md`. Even with memory capture off, each turn still makes forced `submit_output` calls (freshness classifier, judge).

## 4. Provider-specific behaviour worth knowing

- **Gemini `gemini-3.1-flash-lite`:**
  - Typed output, approvals, subagents and the `task.schedule` tool worked first time.
  - The closure judge, steered by a freshness classifier that labelled "Check order A-104" as needing live web data, replaced the correct tool answer with "I do not have access ..." on every run (draft 06).
  - Occasional 503 "high demand" was retried transparently by OpenMinion.
- **Gemini 429:** also surfaced as reply text with `run_state=completed` in a turn without any tool (same family as draft 04).
- **Gemma 4 31B (Google AI Studio, not Cortensor Portal):** `basic_turn` passed but took 89 s; the tool turn exceeded a 180 s `timeout_seconds`. This matches other entrants' reports that Gemma 4 on Portal needs a much longer turn timeout than the 90 s default.
- **Handoff on Gemini:**
  - The wire trace shows `transfer_to_researcher` **was** called, and the peer's answer (with its required marker) came back.
  - The final `result.text` was then re-synthesized without the marker: 12 HTTP calls for one turn, 8 of them hidden control calls.
  - This is the same "final answer rewritten after the work" mechanism as draft 06; benign here, but the caller never sees the peer's actual output.
  - We did not see the #284 "Risk: financial" confirmation.
- **Memory on Gemini:** capture reported `memory_capture_state=succeeded`, yet a new session answered "UNKNOWN" to the recall question. We did not dig into whether recall needs `memory_dynamic_retrieval_enabled` (off by default); this needs investigation, not filing.

- **`result.text` starts with the agent id** (`"gemini: PONG-…"`, `"groq: …"`, `"openminion: hello"` for echo). Apps that show `result.text` to users print the agent name too. `output_type` parsing survives it only because the SDK falls back to extracting the first JSON object.

## 5. LLM triage needs a human (by design)

MinionProbe's triage agent works end to end on Gemini: tools called, rubric skill ingested and admitted, `TriageReport` validated on the first attempt once the prompt embedded the JSON schema. But `gemini-3.1-flash-lite` is not a reliable judge:

- On the first, single-provider run it disagreed with the verified evidence on 4 of 6 failures. For example, it blamed the closure-judge rewrite on #248.
- On the final merged matrix (24 failures) it agreed with the evidence-based rules on 12 of 24. Every disagreement downgraded a verified runtime bug to "model behaviour" or "probe bug"; the session replay, for instance, became "model failed to maintain isolation".
- The `subagent` issue-writer, by contrast, produced grounded, readable drafts (see `results/2026-10-21/triage/drafts/`). That is why MinionProbe shows the rule-based and agent classifications side by side, files nothing itself, and keeps human-verified drafts in `issue-drafts/`.

## 6. Isolation and Windows

- `--home-root`/`--data-root` (CLI) and `home_root`/`data_root` (SDK) are not enough: the identity bridge still opens `~/.openminion/identity/identity.db` (draft 01). Set `OPENMINION_HOME` and `OPENMINION_DATA_ROOT` as well.
- Windows has no IANA tz database. Without `tzdata`, every recurring cron job fails with `unknown timezone: UTC` (draft 05).
- `openminion setup` for a second agent rewrites the shared `providers.openai` block and `default_agent`. A dummy-key `custom-openai-compatible` profile needs its `api_key_env` changed in three places (agent override, `providers.openai`, `system.providers.openai`). Editing only one is shadowed, as other entrants reported too.
- OpenMinion's own WARNING log line for a failed turn prints the provider error verbatim, including Groq's organization id. MinionProbe's results are redacted, but builders shipping logs should scrub them.

## 7. What worked well

- `@tool` decorated functions are offered to the model and their **string** results reach it intact on 0.1.8 (#248/#249 fixed), confirmed on the wire on Gemini and Groq.
- `@tool(dangerous=True)` plus `approval_callback`: deny blocked the tool and allow ran it, on Gemini and Groq (`groq-lean`). The callback received the tool name and args.
- The model-facing `task.schedule` tool created a real one-shot job on Gemini and Groq. Unlike `cron create --cron`, it uses `kind: at`, so it is not affected by draft 05.
- `output_type=PydanticModel` parsed cleanly on Gemini and Groq `gpt-oss-120b`. On `gpt-oss-20b` the model answered `{"label":"crash"}`, outside the enum, and the SDK correctly raised `AgentOutputValidationError`.
- `subagent(parent, ...)` followed its own instructions on both providers.
- Unreachable provider: `ProviderError` is raised, not a fake success (the #251 fix works for the first call).
- `openminion setup --no-chat` never makes a network call and stores only the *name* of the key variable. That makes isolated, reproducible homes easy.
- Skills work once admitted: after `skill ingest --name ...` and `skill admit ... --expected-active-version-hash none`, Gemini and Groq both applied a skill-only fact on the first try.
- On Groq `gpt-oss-20b` (lean profile) the decorated tool's result was quoted correctly, so the closure-judge rewrite (draft 06) is model-dependent. Watch out for typography: that model wrote the nonce with a non-breaking hyphen (U+2011). Exact string checks on model output need Unicode normalisation; MinionProbe's first Groq verdict was a false FAIL until we added it.

## 8. Actionable feedback for the OpenMinion team (short list)

1. Mark SDK responses delivered (or set `caller_handles_delivery`) so sessions work (draft 02). Add an SDK test that runs two turns on one session.
2. Raise `ProviderError` from every turn path, including post-tool continuation (draft 04). Honour `Retry-After` / "try again in" before retrying.
3. Pass `home_root`/`data_root` into the identity bridge (draft 01).
4. Declare `tzdata` on Windows (draft 05).
5. Respect the declared `skill_id`. Show drafts in `skill list`. Document the `none` sentinel for the first `skill admit` (draft 07).
6. Do not let the closure judge discard a completed, tool-grounded answer (draft 06). Count tool results as "live data".
7. Expose provider-call counts and tokens in `result.stats` (or document that `llm_calls` means loop steps). Offer a "lean" mode for small TPM budgets. Fall back from strict tool schemas when a provider rejects them (Groq).
