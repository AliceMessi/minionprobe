# MinionProbe

**A probe suite for the [OpenMinion](https://github.com/openminion/openminion) Python SDK.** It has one small probe for each building block (agent execution, models, tools, permissions, sessions, memory, skills, tasks, delegation, diagnostics) and runs them against real model providers. The output is a **provider × building-block matrix** plus evidence you can act on: redacted HTTP traces, failed checks, and **drafted GitHub issues that a human approves before anything is filed**.

Built for [Cortensor Hackathon #5](https://dorahacks.io/hackathon/2418/detail). MinionProbe is a QA helper for builders who want to know which OpenMinion features work on *their* provider before they build on them.

> Status: v0.1 (2026-10-21). Tested on Windows 11, Python 3.12, `openminion==0.1.8` (latest PyPI release) and the `dev` branch at `b642697`.

## What it found (v0.1)

Seven reproducible OpenMinion bugs, each with a ready-to-review draft in [`issue-drafts/`](issue-drafts/README.md). Six of them reproduce offline in seconds with the built-in echo provider, MinionProbe's local fake provider, or the CLI alone (the closure-judge one needs Gemini):

1. The SDK writes `~/.openminion/identity/identity.db` even with explicit `home_root`/`data_root`.
2. **A follow-up `Agent.run` on the same `session_id` replays the first reply** without calling the model. This also affects the official `examples/sdk/sessions.py` (0.1.4, 0.1.8 and dev).
3. `Agent(agent_id="b")` silently runs on agent "a"'s provider when the session was first used by "a".
4. A 429 later in a turn comes back as reply text with `run_state=completed` (the #251 fix is incomplete).
5. On Windows, every `cron create --cron` fails with `unknown timezone: UTC`, because `tzdata` is not a dependency.
6. On Gemini, the hidden closure judge replaces a correct tool-grounded answer with "I do not have access ...".
7. `skill ingest` ignores the declared `skill_id`: every `SKILL.md` becomes `skill`, and unrelated skills collide.

The matrix and the full notes are in [`reports/BUILDING_BLOCKS_TESTED.md`](reports/BUILDING_BLOCKS_TESTED.md) and [`reports/WHAT_WE_LEARNED.md`](reports/WHAT_WE_LEARNED.md). Raw runs are in [`results/`](results/).

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
- MinionProbe never files issues, opens PRs or posts anything. Drafts carry a "NOT FILED" banner.
- The fake provider binds to `127.0.0.1` only and receives a dummy key.

## Dependencies and disclosure

- Runtime dependency: [`openminion==0.1.8`](https://pypi.org/project/openminion/) (Apache-2.0) and its transitive dependencies (pydantic, httpx and others). Tests use `pytest`.
- Model providers used for the published runs: Groq (`openai/gpt-oss-120b`) and Google Gemini API (`gemini-3.1-flash-lite`), both free tier. MinionProbe has no affiliation with OpenMinion, Groq or Google.
- **AI-assisted development:** this project was built with an AI coding assistant (Claude Code), directed and reviewed by the author. Every finding in `issue-drafts/` was reproduced on real runs; the commit history shows how the work progressed.

## License

Apache-2.0, see [LICENSE](LICENSE).
