# Evidence for the #328 verdict-contract follow-up

These captures go with the PR that requires a model-authored closure verdict (`satisfied` and `next_action` with no defaults), filed under openminion/openminion#328. The scenario is the same as in [`../openminion-328`](../openminion-328).

| File | What it shows |
|---|---|
| `offline-0.1.9-pypi-partial-judge-*` | PyPI 0.1.9 with a scripted judge that returns only `final_answer`. The partial reply is accepted as a satisfied close, and its refusal becomes the result. |
| `offline-verdict-branch-partial-judge-*` | The same script on the fix branch. The judge request carries `required: ["satisfied","next_action"]`, the partial reply is rejected, and the turn keeps the grounded `ZEBRA-42` answer. |
| `gemini-verdict-branch-run{0,1,2}-turn.json` | Live Gemini attempts on the fix branch. All ended in HTTP 503 ("high demand") before reaching the judge, so they are not a live verdict retest. |
| `runs-summary.json` | Every attempt with its outcome. |

The files are redacted: no keys, org ids or auth headers, and local paths are replaced with a placeholder.
