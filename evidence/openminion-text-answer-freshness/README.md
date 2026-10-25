# OpenMinion: a text entry answer is not checked against the turn's freshness obligations

Reported separately at the maintainer's request on [#395](https://github.com/openminion/openminion/issues/395). `probe_text_answer_freshness.py` (sha256 `452ed05e549bc954ddb3f2506a610829abb401f1fb79f27ef5e8d999301f29ea`) runs an SDK `Agent` with `@tool lookup_order` against a scripted OpenAI-compatible fixture on 127.0.0.1 whose freshness classifier asks for live, sourced, exact-date evidence. Scenarios: T0, the first entry answers in text; T1, the continuation after the closure freshness guard answers in text; C, control, the continuation calls the tool again. Read-only observers record the obligations after each entry step, every `verify_freshness_answer` call (closure evaluator, `execution.closure.base`, verification policy) and each `brain.entry` decision.

Files:
- `run-linux.log`, from `run_text.sh`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17 (AliceMessi/linux-runner run 40207176181), on the #395 fix branch `fix/395-freshness-continuation-retention` @ `b3c7fb7e` (same `orchestration.py` behaviour as the final head `ad35d258`, which only compacts the new branch) and on `dev` sources (`b670bbb6`).
- `windows.log`: native Windows 11, CPython 3.11.11, the same two trees (#395 branch @ `ad35d258`).

On both platforms and both trees a text entry answer routes to `respond` (`entry_text_response`) and becomes `result.text` with no freshness check. With the #395 retention the continuation's obligations are still set at that point; on `dev` they have been reset to the empty-input default first.
