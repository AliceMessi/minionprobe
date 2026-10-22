# Live Gemini retest of the closure grounding contract (openminion/openminion#328)

This folder holds the same scenario as [`../openminion-328-live`](../openminion-328-live), run on the branch `fix/328-closure-grounding-contract` @ `168e12b5` (current `dev` @ `2395aa7b` plus the contract change) with `gemini-3.1-flash-lite` on 22 Oct 2026. Runs were gated on a ping, so they only started while the endpoint answered 200. One tool, `lookup_order`, returns `order A-104: ready for pickup, pickup code ZEBRA-<n>`. In every run the agent loop answers with that code. What we check is whether the turn result keeps it.

| Run | Code | Judge calls (HTTP) | Judge verdict | Repair calls (HTTP) | Result | Keeps the code |
|---|---|---|---|---|---|---|
| `gemini-grounding-run0` | ZEBRA-542 | 503, 200 | close, `final_answer`: "I do not have access to an order database ..." | none | the refusal | no |
| `gemini-grounding-run1` | ZEBRA-543 | 200 | close with no `final_answer` (the refusal is in `reason`) | 503, 503, 503 | a further loop answer | yes, but only because the repair call failed |
| `gemini-grounding-run2` | ZEBRA-544 | 503, 503, 200 | close, `final_answer`: "Please provide more information, such as the platform ..." | none | the request for more information | no |

None of the three judge verdicts uses the tool result, so the change does not improve the live result. In run1 the judge closed without a `final_answer`, and all three repair attempts got 503. The existing unsuccessful-repair path then continued (`closure_missing_final_answer`). One more loop call answered with the code, and that answer became the result without another judge call.

## Why the contract does not reach Gemini

- The rendered judge request contains the new contract, the candidate (`action_summary`) and the tool output with the code. All of it sits in the first system message, about 7.8k characters. The provider layer then appends the schema-only `Tool-calling contract:` as a second system message.
- Gemini reports `prompt_tokens: 974` for every judge call here, exactly as in the `dev` captures, even though the first system message grew by about 470 characters. Every repair call in the earlier captures reports 162.
- `gemini-system-order-probe.json` shows three direct calls with one fact in different system-message positions. When the fact is in the first of two system messages, the model answers `UNKNOWN` (31 prompt tokens). When it is in the last system message, or in a single merged one, the model answers correctly.
- `gemini-system-position-probe-2runs.json` is a second, independent position probe (22 Oct 2026, two runs per case) with the fact "HERON-7731". In the first of two system messages it gives `UNKNOWN` both times (24 prompt tokens). In the last system message (36), or in one merged message (40), it gives the code both times. Each record holds the request body, the HTTP attempts (including 503 retries) and the full response. These are the numbers in the openminion/openminion#347 table.
- `gemini-merged-system-replay.json` resends the six captured 200 judge requests unchanged except that their system messages are merged into one. Three are from this branch and three from `dev`. All six return a close whose `final_answer` keeps the pickup code (3.1k to 3.2k prompt tokens). With the context visible, the old and the new contract behave the same in this scenario. The replay is a diagnostic and does not run through any OpenMinion code path.

## Files

- `*-turn.json`: the full turn record. It has every HTTP call (host, path, status, request and response body), the agent-loop answers and the final result text.
- `*-judgeN-exchange.json` / `*-repairN-exchange.json`: one complete closure-judge or final-answer-repair request and its response.
- `offline-grounding-run0-*`: a deterministic capture with a scripted OpenAI-compatible provider. It shows the new judge and repair contracts on the wire.
- `runs-summary.json`: every attempt, with judge and repair statuses, judge prompt tokens, the parsed verdicts and the outcome.

The files are redacted: no keys, org ids or auth headers (request headers were never recorded), and local paths are replaced with placeholders.
