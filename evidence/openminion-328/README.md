# Evidence for openminion/openminion#328 (closure judge vs. grounded tool answer)

Captured on 2026-10-21 for the fix PR referenced in the issue. Scenario: the issue's `lookup_order` tool (`forced_tools=["lookup_order"]`, "Check order A-104"), Gemini `gemini-3.1-flash-lite` through the `gemini` preset, a fresh isolated OpenMinion home per run, and a distinct pickup code per run.

The full `/chat/completions` request and response bodies were recorded by wrapping `httpx.Client.send`; request headers were never read. The files are redacted: no keys, no org ids, no auth headers, and local temp paths are replaced with `<ISOLATED_TMP_HOME>`.

| File | What it shows |
|---|---|
| `runs-summary.json` | Every attempt, with outcome, HTTP statuses, tool executions and whether the code reached the judge. |
| `gemini-0.1.9-pypi-run5-judge-exchange.json` | The complete failing judge request and response on the PyPI 0.1.9 wheel. The request has no tool evidence, and Gemini's `final_answer` replaces the grounded answer. |
| `gemini-fix-branch-run{0,1,2}-judge-exchange.json` | The same scenario on the fix branch. `closure_action_outputs` now carries the `lookup_order` result in every judge request, yet Gemini still replaces the answer 3/3. |
| `*-turn.json` | The complete wire record of each turn. |
| `offline-*-run0-turn.json` | Deterministic offline captures with a scripted fake provider: the rendered judge request without (0.1.9) and with (fix branch) the `closure_action_outputs` line. |

In every completed Gemini judge call, the reply contains only `final_answer`. Because `ClosureJudgment` defaults to `satisfied=True` and `next_action="close"`, that partial reply is accepted as a satisfied close. This is the likely owner of the model-specific symptom, and it is kept out of the context fix on purpose.
