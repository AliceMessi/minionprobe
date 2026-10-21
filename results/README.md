# Results

One folder per day. Each `python -m minionprobe run` writes:

- `run-<HHMMSS>-<runid>.json`: everything recorded. Per probe: status, checks, observations, known issues. Per turn: latency, `run_state`, reply (shortened), `result.stats`, `llm_call_counts_by_purpose`, and every chat-completions call seen on the wire (host, model, tools offered, `tool_choice`, status, redacted error body, rate-limit headers, usage tokens). All strings are redacted.
- `run-<HHMMSS>-<runid>.md`: the provider × building-block matrix for that run.

`MATRIX.md` / `MATRIX.json` merge every run of the day; the latest result per provider and probe wins (`python -m minionprobe merge ... --out .../MATRIX`). `triage/` holds the triage report and any agent-drafted (unreviewed) issues.

Only the final attempt of a rate-limited turn is stored, so HTTP counts for BLOCKED probes are lower bounds.

## 2026-10-21

All runs used `openminion==0.1.8`, Windows 11, Python 3.12.11. `MATRIX.md` is the merged view used in `reports/`.

| Run | Provider column | Model | What it is |
|---|---|---|---|
| `run-082312-0e0812` | gemini | `gemini-3.1-flash-lite` | First full Gemini run (older skills probe) |
| `run-082312-e2d96e` | groq | `openai/gpt-oss-120b` | Full Groq run, OpenMinion defaults; hit the 8K TPM limit and then the 200K/day cap |
| `run-083243-cfee7d` | gemini | `gemini-3.1-flash-lite` | Skills probe re-run with the ingest + admit flow |
| `run-083839-3b8f09` | gemma | `gemma-4-31b-it` | Exploratory: 2 probes on Gemma 4 via Google AI Studio (slow) |
| `run-084753-29b841` | groq-lean | `openai/gpt-oss-120b` | Aborted: Groq's daily token cap (TPD) was exhausted |
| `run-084611-e23e18` | gemini | `gemini-3.1-flash-lite` | **Final full Gemini run** |
| `run-085127-1f319b` | local | fake provider | **Final offline run** (deterministic bug reproductions) |
| `run-085559-1bc384` | groq-lean | `openai/gpt-oss-20b` | **Lean Groq run** (`memory_enabled=false`, `tools=[]` for tool-less probes) |
| `run-085719-55769e` | gemini | `gemini-3.1-flash-lite` | Handoff re-run with wire checks (transfer called, peer answered) |
| `run-091100-f5a4f4` | groq-lean | `openai/gpt-oss-20b` | `decorated_tool` re-run after Unicode-normalised checks (first verdict was a false FAIL) |

Earlier local runs in this folder (`run-082316-*`, `run-084938-*`) are superseded by `run-085127-1f319b` and kept for history.
