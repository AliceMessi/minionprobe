# MinionProbe

[![ci](https://github.com/AliceMessi/minionprobe/actions/workflows/ci.yml/badge.svg)](https://github.com/AliceMessi/minionprobe/actions/workflows/ci.yml)

**A probe suite for the [OpenMinion](https://github.com/openminion/openminion) Python SDK.** It has one small probe for each building block (agent execution, models, tools, permissions, sessions, memory, skills, tasks, delegation, diagnostics) and runs them against real model providers. The output is a **provider × building-block matrix** plus evidence you can act on: redacted HTTP traces, failed checks, and **drafted GitHub issues that a human approves before anything is filed**.

Built for [Cortensor Hackathon #5](https://dorahacks.io/hackathon/2418/detail). MinionProbe is a QA helper for builders who want to know which OpenMinion features work on *their* provider before they build on them.

**Impact so far (25 Oct 2026):**
- **28 bugs filed upstream** from MinionProbe runs and follow-up probes (the latest, [#412](https://github.com/openminion/openminion/issues/412) and [#413](https://github.com/openminion/openminion/issues/413), on 25 Oct: production cases found while fixing #408 and #402, which the maintainer asked us to report on their own).
- **32 fix PRs by us merged** into OpenMinion `dev` (table in [Fix PRs](#fix-prs-bug-fix-bonus)). The fixes that shipped were re-verified on the released packages: `openminion==0.1.10` ([evidence](evidence/openminion-0.1.10-verification)) and `openminion==0.1.11`, which ships seven more of our fixes ([evidence](evidence/openminion-0.1.11-verification); every check passed for #352, #353, #361, #365, #368, #373, #378).
- **8 more issues** opened by the maintainer while reviewing our PRs were taken on: all 8 merged (including #372, the run-status facts table with migration 13, #380 for #368, #385 for #364 with migration 14, and #406 for #369 with migration 15).

▶ **Demo video (2:10):** https://youtu.be/5aBN0y6YQeA. It shows an offline run, the matrix, bug #324 reproduced live, the triage agent and the filed issues. Terminal scenes are replays of real captured runs.

> Status: v0.1 (2026-10-21). The probes are pinned to `openminion==0.1.8`, so every reproduction keeps working; tested on Windows 11 and Linux, Python 3.12, plus the `dev` branch at `b642697`. The upstream fixes were verified on `openminion==0.1.10` ([evidence](evidence/openminion-0.1.10-verification)).

## What it found (v0.1)

Seven reproducible OpenMinion bugs, each with a reviewed draft in [`issue-drafts/`](issue-drafts/README.md), filed upstream after human review as [#323](https://github.com/openminion/openminion/issues/323), [#324](https://github.com/openminion/openminion/issues/324), [#325](https://github.com/openminion/openminion/issues/325), [#326](https://github.com/openminion/openminion/issues/326), [#327](https://github.com/openminion/openminion/issues/327), [#328](https://github.com/openminion/openminion/issues/328), [#329](https://github.com/openminion/openminion/issues/329). Six of them reproduce offline in seconds with the built-in echo provider, MinionProbe's local fake provider, or the CLI alone (the closure-judge one needs Gemini):

1. The SDK writes `~/.openminion/identity/identity.db` even with explicit `home_root`/`data_root`.
2. **A follow-up `Agent.run` on the same `session_id` replays the first reply** without calling the model. This also affects the official `examples/sdk/sessions.py` (0.1.4, 0.1.8 and dev).
3. `Agent(agent_id="b")` silently runs on agent "a"'s provider when the session was first used by "a".
4. A 429 later in a turn comes back as reply text with `run_state=completed` (the #251 fix is incomplete).
5. On Windows, every `cron create --cron` fails with `unknown timezone: UTC`, because `tzdata` is not a dependency.
6. On Gemini, the hidden closure judge replaces a correct tool-grounded answer with "I do not have access ...".
7. `skill ingest` ignores the declared `skill_id`: every `SKILL.md` becomes `skill`, and unrelated skills collide.

The matrix and the full notes are in [`reports/BUILDING_BLOCKS_TESTED.md`](reports/BUILDING_BLOCKS_TESTED.md) and [`reports/WHAT_WE_LEARNED.md`](reports/WHAT_WE_LEARNED.md). Raw runs are in [`results/`](results/).

## Fix PRs (bug-fix bonus)

The maintainers confirmed all seven reports, and every fix PR below was reviewed and **merged**. Follow-up reports found while fixing (#342, #345, #347, #349) were also confirmed, assigned to us and fixed. **OpenMinion 0.1.10 (23 Oct 2026) ships these fixes.** We ran the installed-package release verification ([`openminion-0.1.10-verification`](evidence/openminion-0.1.10-verification)), including a live Gemini retest. On that basis the maintainers closed #328, #342, #345, #347 and #349 as *fixed and verified in 0.1.10*:

| Issue | Fix PR |
|---|---|
| [#323](https://github.com/openminion/openminion/issues/323) identity.db outside runtime roots | [#335](https://github.com/openminion/openminion/pull/335) |
| [#324](https://github.com/openminion/openminion/issues/324) same-session follow-up replays the first reply | [#334](https://github.com/openminion/openminion/pull/334) |
| [#325](https://github.com/openminion/openminion/issues/325) explicit agent ignored on an existing session | [#336](https://github.com/openminion/openminion/pull/336) |
| [#326](https://github.com/openminion/openminion/issues/326) post-tool 429 returned as completed text | [#338](https://github.com/openminion/openminion/pull/338) |
| [#327](https://github.com/openminion/openminion/issues/327) Windows cron without tzdata | [#341](https://github.com/openminion/openminion/pull/341) |
| [#328](https://github.com/openminion/openminion/issues/328) closure judge vs. grounded answer | [#337](https://github.com/openminion/openminion/pull/337) context fix + [#343](https://github.com/openminion/openminion/pull/343) required verdict fields + [#351](https://github.com/openminion/openminion/pull/351) (the root cause on Gemini, #347). Evidence: [`openminion-328`](evidence/openminion-328), [`openminion-328b`](evidence/openminion-328b), [`openminion-328-live`](evidence/openminion-328-live), [`openminion-347`](evidence/openminion-347) |
| [#329](https://github.com/openminion/openminion/issues/329) skill ingest identity and pending versions | [#339](https://github.com/openminion/openminion/pull/339) |
| [#342](https://github.com/openminion/openminion/issues/342) SDK artifact store outside runtime roots (found while fixing) | [#346](https://github.com/openminion/openminion/pull/346) |
| [#345](https://github.com/openminion/openminion/issues/345) run state read by wall-clock event order (found while fixing #326) | [#350](https://github.com/openminion/openminion/pull/350) |
| [#347](https://github.com/openminion/openminion/issues/347) Gemini's OpenAI-compatible endpoint reads only the last system message (root cause of #328 on Gemini) | [#351](https://github.com/openminion/openminion/pull/351), with live evidence in [`openminion-347`](evidence/openminion-347) |
| [#349](https://github.com/openminion/openminion/issues/349) runtime memory artifact store never closed on shutdown (found in review of #346) | [#356](https://github.com/openminion/openminion/pull/356) |
| [#353](https://github.com/openminion/openminion/issues/353) messages with identical timestamps read in reverse append order (found in review of #350) | [#363](https://github.com/openminion/openminion/pull/363) |
| [#361](https://github.com/openminion/openminion/issues/361) runtime memory audit database left open after shutdown (found while fixing #349) | [#366](https://github.com/openminion/openminion/pull/366) |
| [#365](https://github.com/openminion/openminion/issues/365) transcript export orders tied events by string id (disclosed in review of #363) | [#370](https://github.com/openminion/openminion/pull/370) |
| [#352](https://github.com/openminion/openminion/issues/352) run status read only the oldest 2,000 run events of a session (disclosed in review) | [#372](https://github.com/openminion/openminion/pull/372): per-event run facts, runtime migration 13 with an in-transaction backfill, differential fuzz identical to `dev` on SQLite and Postgres. Evidence [`openminion-372`](evidence/openminion-372) |
| [#373](https://github.com/openminion/openminion/issues/373) migration runner leaves SQLite connections open until GC, blocking database cleanup on Windows (found while isolating #366) | [#377](https://github.com/openminion/openminion/pull/377), evidence [`openminion-373`](evidence/openminion-373). The OMX import case was split out as [#378](https://github.com/openminion/openminion/issues/378) at the maintainer's request |
| [#378](https://github.com/openminion/openminion/issues/378) OMX import leaves the target SQLite connection open until GC, blocking file cleanup on Windows (split out of #373) | [#379](https://github.com/openminion/openminion/pull/379), evidence [`openminion-omx-close`](evidence/openminion-omx-close) |
| [#368](https://github.com/openminion/openminion/issues/368) runtime memory assembly never closes the store it creates; with PostgreSQL the engine stays open (disclosed in review of #366) | [#380](https://github.com/openminion/openminion/pull/380), live PostgreSQL close-chain evidence in [`openminion-368`](evidence/openminion-368) |
| [#364](https://github.com/openminion/openminion/issues/364) tied participant join timestamps reorder listings and the active-agent fallback (opened by the maintainer) | [#385](https://github.com/openminion/openminion/pull/385): runtime migration 14 (`join_seq`) and a session-row lock for joins and leaves, evidence [`openminion-364`](evidence/openminion-364). The first-active-agent race was split out as [#393](https://github.com/openminion/openminion/issues/393), and the tests' own Windows cleanup problem as [#408](https://github.com/openminion/openminion/issues/408) (evidence [`openminion-windows-test-cleanup`](evidence/openminion-windows-test-cleanup)), at the maintainer's request |
| [#400](https://github.com/openminion/openminion/issues/400) PostgreSQL retention and purge fail on any record or candidate that has evidence | [#415](https://github.com/openminion/openminion/pull/415): the shared evidence decoder keeps the list that PostgreSQL returns for JSONB, with SQLite controls, live PostgreSQL tests and a check composed with #399. Evidence [`openminion-400`](evidence/openminion-400) |
| [#369](https://github.com/openminion/openminion/issues/369) session metadata filters (ACP `session/list`, Focus directory sessions) fail on PostgreSQL with `json_extract` (opened by the maintainer) | [#406](https://github.com/openminion/openminion/pull/406): runtime migration 15 adds `session_metadata_strings`, the top-level string members of each session's metadata, written with every session write and backfilled from `json.loads`, so stored NaN, Infinity, NUL and lone surrogates are covered; the PostgreSQL filter is a bounded, literal `EXISTS` over it, SQLite keeps its reader, and the SQL dialect comes from the record store type. Evidence [`openminion-369`](evidence/openminion-369) |

Fix PRs opened on 25 Oct, in review:

| Issue | Fix PR |
|---|---|
| [#408](https://github.com/openminion/openminion/issues/408) storage tests leave their own SQLite connections open, so they fail on native Windows | [#414](https://github.com/openminion/openminion/pull/414), test-only; evidence [`openminion-408`](evidence/openminion-408). The production `storagectl restore` case is reported as [#412](https://github.com/openminion/openminion/issues/412) (evidence [`openminion-storagectl-restore-target`](evidence/openminion-storagectl-restore-target)) |
| [#402](https://github.com/openminion/openminion/issues/402) `memctl inspect` never shows the history summary behind the audited store | [#416](https://github.com/openminion/openminion/pull/416); evidence [`openminion-402`](evidence/openminion-402). `memctl inspect` without `--scope` is reported as [#413](https://github.com/openminion/openminion/issues/413) (evidence [`openminion-memctl-inspect-scope`](evidence/openminion-memctl-inspect-scope)) |
| [#396](https://github.com/openminion/openminion/issues/396) run readers match `RUN.`/`Run.` event types on SQLite only | [#417](https://github.com/openminion/openminion/pull/417): the two SQL run readers use the case-sensitive `SUBSTR` prefix equality the thread projection already uses; the SQLite plans are unchanged, and a PostgreSQL plan change of the recent-runs window is up for the maintainer's decision. Evidence [`openminion-396`](evidence/openminion-396) |
| [#403](https://github.com/openminion/openminion/issues/403) `memctl` leaves the audit database open until garbage collection (Windows cleanup) | [#418](https://github.com/openminion/openminion/pull/418): the service factory owns the store and sink it creates, and each command closes it through its Click context. Evidence [`openminion-403`](evidence/openminion-403) |
| [#397](https://github.com/openminion/openminion/issues/397) every SDK turn fails with an external memory backend that has no atomic capture | [#421](https://github.com/openminion/openminion/pull/421): the missing method is reported at the adapter as the existing unsupported-capture error, with a clean merge onto #398. Evidence [`openminion-397`](evidence/openminion-397). The consolidation cron recovery failure found while testing is reported as [#420](https://github.com/openminion/openminion/issues/420) (evidence [`openminion-cron-capture-recovery`](evidence/openminion-cron-capture-recovery)) |
| [#395](https://github.com/openminion/openminion/issues/395) freshness obligation reset on a same-turn continuation step | [#422](https://github.com/openminion/openminion/pull/422): the turn keeps its freshness contract when a step has no new input and no typed contract, with SDK regressions for the #328 trace scenarios. Evidence [`openminion-395`](evidence/openminion-395). The plain-text answer that still skips the freshness check is reported separately as [#419](https://github.com/openminion/openminion/issues/419) (evidence [`openminion-text-answer-freshness`](evidence/openminion-text-answer-freshness)) |

## Quick start

Requirements: Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/) (plain `pip` works too).

```bash
git clone https://github.com/AliceMessi/minionprobe.git && cd minionprobe
uv venv -p 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[test]"     # Linux/macOS: .venv/bin/python

# 1. offline: no keys, no network (local fake provider + CLI probes)
.venv/Scripts/python.exe -m minionprobe run --providers local

# 2. real providers: keys are read from the environment only
export GROQ_API_KEY=...      # PowerShell: $env:GROQ_API_KEY = "..."
export GEMINI_API_KEY=...
.venv/Scripts/python.exe -m minionprobe run --providers local,groq,gemini --blocks all

# 3. triage the failures with an OpenMinion agent (drafts only, nothing is filed)
.venv/Scripts/python.exe -m minionprobe triage results/<date>/run-<id>.json --provider gemini

# tests (offline)
.venv/Scripts/python.exe -m pytest -q
```

Other commands:

- `python -m minionprobe list` lists every probe.
- `--blocks sessions,tools` and `--only decorated_tool` select a subset.
- `python -m minionprobe merge results/<date>/run-*.json --out results/<date>/MATRIX` merges several runs into one matrix.
- `--providers gemma` adds Gemma 4 through Google AI Studio.
- `--providers lmstudio` uses a local LM Studio server (`MINIONPROBE_LMSTUDIO_URL`, `MINIONPROBE_LMSTUDIO_MODEL`).
- Models can be overridden with `MINIONPROBE_GROQ_MODEL`, `MINIONPROBE_GEMINI_MODEL` and `MINIONPROBE_GEMMA_MODEL`.

Each run writes `results/<date>/run-<time>-<id>.json` (full redacted evidence) and a `.md` matrix.

### Cost and rate limits

A full real-provider run makes roughly 15 user turns per provider. Each OpenMinion turn makes 1–6 HTTP calls, because of the hidden memory, freshness and judge calls. Everything was run on free tiers. On Groq (8K tokens/minute) MinionProbe paces itself from the `x-ratelimit-*` headers and waits a full window after a 429. A probe the provider still blocks is reported as **BLOCKED**, never as a pass or fail.

## How it uses the OpenMinion SDK

```
          python -m minionprobe run
                     │
        ┌────────────┴──────────────┐
        │ for each provider         │   openminion --home-root .probe-home/<run>/<provider> setup --provider groq ... --no-chat
        │  isolated OpenMinion home │   (+ skill ingest/admit for the skills probe)
        └────────────┬──────────────┘
                     │ APIRuntime.from_config_path(cfg, home_root=..., data_root=...)
        ┌────────────┴──────────────┐
        │ probes (one per block)    │   Agent(runtime=..., agent_id=..., tools=[@tool ...], forced_tools=..., output_type=...)
        │  ctx.turn(agent, msg)     │   agent.run(msg, session_id=..., approval_callback=..., timeout_seconds=...)
        └────────────┬──────────────┘   subagent(parent, ...), Handoff(target=...), ProviderError, AgentOutputValidationError
                     │ httpx recorder (host, model, tools offered, status, redacted errors, rate-limit headers)
        ┌────────────┴──────────────┐
        │ results/<date>/run-*.json │ ──► report.py: provider × block matrix (.md)
        └────────────┬──────────────┘
                     │ python -m minionprobe triage
        ┌────────────┴──────────────┐
        │ triage Agent              │   @tool list_failures / get_failure / search_known_issues (read-only)
        │  + SKILL.md rubric        │   output_type=TriageReport, skill ingested into its own home
        │  + issue-writer subagent  │   subagent(...) drafts GitHub issue bodies → results/<date>/triage/drafts/
        └───────────────────────────┘   (NOT filed: a human reviews and files by hand)
```

The OpenMinion surfaces each building block exercises:

| Block | Probe(s) | OpenMinion surface |
|---|---|---|
| Agent execution | `basic_turn`, `structured_output` | `Agent.run`, `AgentRunResult.stats/metadata`, `output_type=` + `AgentOutputValidationError` |
| Models | `provider_routing`, `session_agent_binding` | `openminion setup --provider`, `Agent(agent_id=...)` per profile |
| Tools | `decorated_tool` | `@tool`, `tools=[...]`, `forced_tools=` |
| Permissions | `dangerous_tool_approval` | `@tool(dangerous=True)`, `approval_callback` (deny then allow) |
| Sessions | `continuity_isolation`, `followup_not_replayed` | `session_id` continuity and isolation |
| Memory | `cross_session_recall` | durable memory capture (`memory_capture_state`) and recall |
| Skills | `ingested_skill_applied` | `openminion skill ingest` / `skill admit` / `skill list`, skill use in a turn |
| Tasks | `cron_schedules`, `task_schedule_tool` | `openminion cron create` (`--cron`, `--at`, `--tz`), the `task.schedule` tool |
| Delegation | `subagent_run`, `handoff` | `subagent(parent, ...)`, `Handoff(target=...)` |
| Diagnostics | `run_stats`, `provider_unreachable`, `rate_limit_after_tool`, `home_isolation` | `ProviderError`, run stats vs wire traffic, state isolation |

Probe design rules:

- Every probe checks something deterministic: a nonce the model can only know from the tool or skill, a request count on the wire, a file on disk, or an exception type. Model wording alone is never the pass criterion.
- `local` probes use [`minionprobe/fakeprovider.py`](minionprobe/fakeprovider.py), a scripted OpenAI-compatible server that OpenMinion reaches through its `custom-openai-compatible` preset. It makes fault injection exact (e.g. a 429 only on the call after a tool ran) and runs offline in CI.
- Each provider gets its own OpenMinion home under `.probe-home/`, and `OPENMINION_HOME`/`OPENMINION_DATA_ROOT` are set too: that is the workaround for issue draft 01.

## Safety and privacy

- API keys are read from environment variables only. OpenMinion's `setup` stores the variable *name*, never the value.
- Every string written to `results/` and `issue-drafts/` goes through [`minionprobe/redact.py`](minionprobe/redact.py), which removes key-shaped strings, literal key values from the environment, and Groq organization ids. Request headers are never read.
- MinionProbe never files issues, opens PRs or posts anything. Drafts carry a "NOT FILED" banner until a human files them by hand.
- The fake provider binds to `127.0.0.1` only and receives a dummy key.

## Dependencies and disclosure

- Runtime dependency: [`openminion==0.1.8`](https://pypi.org/project/openminion/) (Apache-2.0) and its transitive dependencies (pydantic, httpx and others). Tests use `pytest`.
- Model providers used for the published runs: Groq (`openai/gpt-oss-120b`) and Google Gemini API (`gemini-3.1-flash-lite`), both free tier. MinionProbe has no affiliation with OpenMinion, Groq or Google.
- **AI-assisted development:** this project was built with an AI coding assistant (Claude Code), directed and reviewed by the author. Every finding in `issue-drafts/` was reproduced on real runs; the commit history shows how the work progressed.

## License

Apache-2.0, see [LICENSE](LICENSE).
