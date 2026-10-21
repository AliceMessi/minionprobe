# Issue drafts (filed upstream as #323–#329)

These are draft GitHub issues for [openminion/openminion](https://github.com/openminion/openminion), written from MinionProbe runs on 2026-10-21. A human reviewed each draft and re-ran the reproductions; all seven were filed on 2026-10-21 (links in the table).

Each draft was reproduced by hand on Windows 11 with Python 3.12.11 against `openminion==0.1.8` (latest PyPI release, 2026-10-19) and the `dev` branch at `b642697` (2026-10-20). Logs are redacted (no keys, no account ids).

| # | Draft | Filed | Block | Repro needs | 0.1.8 | dev `b642697` |
|---|---|---|---|---|---|---|
| 01 | [identity.db is written to `~/.openminion` despite explicit home/data roots](01-identity-db-written-to-user-home.md) | [#323](https://github.com/openminion/openminion/issues/323) | diagnostics / isolation | echo provider (offline) | yes | yes |
| 02 | [A follow-up `Agent.run` on the same session replays the first reply](02-followup-run-replays-previous-reply.md) | [#324](https://github.com/openminion/openminion/issues/324) | sessions | echo provider (offline) | yes (also 0.1.4) | yes |
| 03 | [`Agent(agent_id=...)` is ignored for an existing session](03-agent-id-ignored-for-existing-session.md) | [#325](https://github.com/openminion/openminion/issues/325) | models / sessions | local fake provider (offline) | yes | yes |
| 04 | [A 429 after a tool call comes back as reply text with `run_state=completed`](04-429-after-tool-returned-as-completed-text.md) | [#326](https://github.com/openminion/openminion/issues/326) | diagnostics / errors | local fake provider (offline); also seen on Groq and Gemini | yes | yes |
| 05 | [Every `cron create --cron` fails on Windows: `unknown timezone: UTC`](05-cron-unknown-timezone-on-windows-tzdata.md) | [#327](https://github.com/openminion/openminion/issues/327) | tasks | CLI only (offline) | yes | yes |
| 06 | [The closure judge overwrites a correct tool-grounded answer](06-closure-judge-overwrites-grounded-tool-answer.md) | [#328](https://github.com/openminion/openminion/issues/328) | tools / agent execution | Gemini API (free tier) | yes, 4 of 4 runs | yes, 2 of 2 runs |
| 07 | [`skill ingest` ignores the declared `skill_id`: every `SKILL.md` becomes `skill`](07-skill-ingest-ignores-skill-id.md) | [#329](https://github.com/openminion/openminion/issues/329) | skills | CLI only (offline) | yes | yes |

Not drafted on purpose:

- Already filed upstream (cited, not re-filed): #248, #249 and #251 (fixed in 0.1.5), and #284 (handoff risk confirmation, still open). #248 was also found independently by another Hackathon #5 entrant (Cubiczan/cortensor-trial-watch).
- Provider or model behaviour, recorded in `reports/WHAT_WE_LEARNED.md`. For example, Groq rejects the internal `submit_output` schema with HTTP 400 `tool_use_failed`.
