# Live Gemini check of the system-message merge (openminion/openminion#347)

The #328 SDK scenario: one decorated tool, `lookup_order`, with `forced_tools`, the prompt "Check order A-104", the `gemini` preset with `gemini-3.1-flash-lite`, an isolated home per run, and the full request and response body of every `/chat/completions` call (headers were never recorded). The tool returns `order A-104: ready for pickup, pickup code ZEBRA-<n>`. The question is whether the turn result keeps that code. Runs were started only after a ping returned 200, at most 8 attempts per source. Runs on 22 Oct 2026.

- (a) `gemini-collapse-*`: `fix/347-gemini-collapse-system-messages` @ `1e0c0f54`, which is `dev` @ `2395aa7b` plus the fix, without #348.
- (b) `gemini-collapse348-*`: the same head with #348's commit `168e12b5` cherry-picked locally (`49c3e559`, not pushed). Its judge requests carry #348's contract wording; the (a) requests do not.

## (a) The fix on current `dev`, without #348

| Run | Code | 503s before the judge | Judge (HTTP) | Judge verdict | Repair | Result | Keeps the code |
|---|---|---|---|---|---|---|---|
| `gemini-collapse-run0` | ZEBRA-642 | 6 | 200 | close, `final_answer`: "Order A-104 is ready for pickup, and your pickup code is "ZEBRA-642"." | none | the judge's answer | yes |
| `gemini-collapse-run1` | ZEBRA-643 | 2 | 503, 503, 503 | none | none | a further loop answer: "Your order A-104 is ready for pickup with the code "ZEBRA-643"." | yes, but no judge verdict |
| `gemini-collapse-run2` | ZEBRA-644 | 10 | none | none | none | `ProviderError` (HTTP 503) before the judge | no result |
| `gemini-collapse-run3` | ZEBRA-645 | 16 | none | none | none | `ProviderError` (HTTP 503) before the judge | no result |
| `gemini-collapse-run4` | ZEBRA-646 | 7 | none | none | none | `ProviderError` (HTTP 503) before the judge | no result |
| `gemini-collapse-run5` | ZEBRA-647 | 11 | none | none | none | `ProviderError` (HTTP 503) before the judge | no result |
| `gemini-collapse-run6` | ZEBRA-648 | 4 | 200 | close, `final_answer`: "Order A-104 is ready for pickup, and your pickup code is "ZEBRA-648"." | none | the judge's answer | yes |
| `gemini-collapse-run7` | ZEBRA-649 | 0 | 200 | close, `final_answer`: "Order A-104 is ready for pickup, and your pickup code is "ZEBRA-649"." | none | the judge's answer | yes |

8 attempts, 3 with a judge 200. All three judge verdicts close with a `final_answer` that keeps the tool's code.

## (b) The fix with #348

| Run | Code | 503s before the judge | Judge (HTTP) | Judge verdict | Repair | Result | Keeps the code |
|---|---|---|---|---|---|---|---|
| `gemini-collapse348-run0` | ZEBRA-742 | 4 | 200 | close, `final_answer`: "Order A-104 is ready for pickup with your pickup code being "ZEBRA-742"." | none | the judge's answer | yes |
| `gemini-collapse348-run1` | ZEBRA-743 | 0 | 200 | close, `final_answer`: "Order A-104 is ready for pickup with the code "ZEBRA-743"." | none | the judge's answer | yes |
| `gemini-collapse348-run2` | ZEBRA-744 | 5 | none | none | none | `ProviderError` (HTTP 503) before the judge | no result |
| `gemini-collapse348-run3` | ZEBRA-745 | 10 | 200 | close, `final_answer`: "Order A-104 is ready for pickup with the code "ZEBRA-745"." | none | the judge's answer | yes |

4 attempts, 3 with a judge 200, all three keep the code.

## Request shape

Every captured request in both sets has exactly one system message (`runs-summary.json`, `calls[].message_roles`): `[system, user]` for the control calls and the judge, `[system, user, assistant, tool, user]` for the second agent-loop call. In each judge request the single system message holds `[JUDGE CONTEXT]` with the candidate and the tool output (with the code) ahead of the schema-only `Tool-calling contract:`.

Gemini's reported `prompt_tokens` per call, against the `dev` @ `2395aa7b` captures in [`../openminion-328-live`](../openminion-328-live) (`gemini-dev-run0..2`, two system messages per call):

| Call | `dev` @ `2395aa7b` | (a) this fix | (b) with #348 |
|---|---|---|---|
| reflect (`UserMessageCandidateReport`) | 442 | 1969 to 1992 | 1981 |
| first agent-loop call | 1938 to 1956 | 2046 to 2145 | 2047 to 2153 |
| `FreshnessContract` | 412 | 569 | 569 |
| closure judge | 974 | 3116 to 3123 | 3205 to 3225 |

On `dev` all three judged runs in those captures lost the code: the judge, or in one run its repair, answered with an invented status, a refusal and an invented delivery date.

## Files

- `*-turn.json`: the full turn record: every HTTP call (host, path, status, request and response body), the agent-loop answers and the final result text.
- `*-judge0-exchange.json`: one complete closure-judge request and its response.
- `runs-summary.json`: every attempt with per-call schema, status and message roles, the 503s before the judge, judge and repair statuses, prompt tokens, parsed verdicts, the result and the outcome.

The files are redacted: no keys, org ids or auth headers (request headers were never recorded), and local paths are replaced with placeholders.
