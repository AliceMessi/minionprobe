# Issue drafts (NOT filed)

These are draft GitHub issues for [openminion/openminion](https://github.com/openminion/openminion), written from MinionProbe runs on 2026-10-21. **Nothing here has been filed.** A human reviews each draft, re-runs the reproduction and decides whether to file it.

Each draft was reproduced by hand on Windows 11 with Python 3.12.11 against `openminion==0.1.8` (latest PyPI release, 2026-10-19) and the `dev` branch at `b642697` (2026-10-20). Logs are redacted (no keys, no account ids).

| # | Draft | Block | Repro needs | 0.1.8 | dev `b642697` |
|---|---|---|---|---|---|
| 01 | [identity.db is written to `~/.openminion` despite explicit home/data roots](01-identity-db-written-to-user-home.md) | diagnostics / isolation | echo provider (offline) | yes | yes |
| 02 | [A follow-up `Agent.run` on the same session replays the first reply](02-followup-run-replays-previous-reply.md) | sessions | echo provider (offline) | yes (also 0.1.4) | yes |
| 03 | [`Agent(agent_id=...)` is ignored for an existing session](03-agent-id-ignored-for-existing-session.md) | models / sessions | local fake provider (offline) | yes | yes |
| 04 | [A 429 after a tool call comes back as reply text with `run_state=completed`](04-429-after-tool-returned-as-completed-text.md) | diagnostics / errors | local fake provider (offline); also seen on Groq and Gemini | yes | yes |
| 05 | [Every `cron create --cron` fails on Windows: `unknown timezone: UTC`](05-cron-unknown-timezone-on-windows-tzdata.md) | tasks | CLI only (offline) | yes | yes |
| 06 | [The closure judge overwrites a correct tool-grounded answer](06-closure-judge-overwrites-grounded-tool-answer.md) | tools / agent execution | Gemini API (free tier) | yes, 3 of 3 runs | yes, 2 of 2 runs |
| 07 | [`skill ingest` ignores the declared `skill_id`: every `SKILL.md` becomes `skill`](07-skill-ingest-ignores-skill-id.md) | skills | CLI only (offline) | yes | yes |

Not drafted on purpose:

- Already filed upstream (cited, not re-filed): #248, #249 and #251 (fixed in 0.1.5), and #284 (handoff risk confirmation, still open). #248 was also found independently by another Hackathon #5 entrant (Cubiczan/cortensor-trial-watch).
- Provider or model behaviour, recorded in `reports/WHAT_WE_LEARNED.md`. For example, Groq rejects the internal `submit_output` schema with HTTP 400 `tool_use_failed`.
