# Purpose

Classify a failed MinionProbe result so a human can decide what to do with it.

# Metadata

1. `skill_id`: `minionprobe.triage_rubric`
2. `risk_level`: `low`
3. `channels`: `console`
4. `required_tools`: `list_failures`, `get_failure`, `search_known_issues`
5. `approval_mode`: `auto`

# Inputs

1. `failures` (list): probe results whose status is FAIL, ERROR or BLOCKED.

# Recipe

1. Call `list_failures` to see every non-passing probe.
2. For each failure, call `get_failure` with its key when the summary is not enough.
3. Call `search_known_issues` with the probe id or a short symptom before calling anything a new bug.
4. Pick exactly one category per failure:
   - `known_issue`: matches an issue already filed upstream; put its number in `known_ref`.
   - `openminion_bug`: OpenMinion behaves differently from its documented contract, and the evidence (HTTP status, request counts, run_state, files on disk) shows it is not the model's choice.
   - `model_behavior`: the runtime did its job but the model ignored an instruction or answered badly.
   - `provider_limitation`: rate limit, quota, outage or a provider-side validation the runtime cannot control.
   - `probe_bug`: the probe itself looks wrong.
5. Prefer `provider_limitation` for BLOCKED results. Never classify as `openminion_bug` on model output wording alone.
6. Give a confidence between 0 and 1 and a one-sentence rationale that cites the evidence.

# Expected Output

1. JSON object `{"items": [{"key", "category", "known_ref", "confidence", "rationale"}]}` with one item per failure.

# Security Notes

1. Evidence is already redacted; never ask for or echo API keys.
2. Do not file, post or send anything. Drafts are reviewed by a human first.

# Test Cases

1. A BLOCKED result with a 429 message -> `provider_limitation`.
2. A handoff that needed a "Risk: financial" approval -> `known_issue` with `known_ref` `#284`.
3. A follow-up turn that made zero provider requests and repeated the previous answer -> `openminion_bug`.
