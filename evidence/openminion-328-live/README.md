# Live Gemini retests for openminion/openminion#328

This folder holds the scenario from [`../openminion-328`](../openminion-328), run live on `gemini-3.1-flash-lite` once the endpoint answered 200 again (22 Oct 2026, gated on a ping). One tool, `lookup_order`, returns `order A-104: ready for pickup, pickup code ZEBRA-<n>`. The agent loop answers with that code in every run. The question is whether the turn result keeps it.

| Files | Tree | Judge calls with a 200 | Tool output in judge context | Result keeps the code |
|---|---|---|---|---|
| `gemini-verdict-branch-run{3,4,5}-*` | #343 alone (`4a0492c9`) | 1 of 3 runs (run4) | no | run4: no (refusal) |
| `gemini-combined-run{0,1,2}-*` | #337 + #343 (local merge `29ed8ffb`) | 3 of 3 | yes, with the code, `verified: true` | 0 of 3 (refusals) |
| `gemini-dev-run{0,1,2}-*` | current `dev` @ `2395aa7b` (both merged) | 3 of 3 | yes, with the code, `verified: true` | 0 of 3 (one refusal, two invented statuses) |

Notes:

- `*-turn.json` is the full turn record. It has every HTTP call (host, path, status, request and response body), the agent-loop answers and the final result text.
- `*-judgeN-exchange.json` is one complete closure-judge request and response, next to the tool output and the actual result.
- `gemini-dev-run2` shows the second path. The judge returned `{"next_action":"close","satisfied":true}` without `final_answer`. The final-answer repair call (`judge1`) then returned `"Order A-104 is currently in transit and is expected to arrive by Friday, October 27th."`, which became the result.
- `gemini-verdict-branch-run3`: all three judge calls got 503. `run5` hit the 300 s turn timeout. Neither is a verdict retest.
- `runs-summary.json` lists every attempt with its parsed judge verdicts and outcome.

The files are redacted: no keys, org ids or auth headers (request headers were never recorded), and local paths are replaced with placeholders.
