# MinionProbe matrix

- OpenMinion: `0.1.8` | Python 3.12.11 | Windows-11-10.0.22631-SP0
- Started: 2026-10-21T08:32:43+02:00 | Finished: 2026-10-21T08:33:10+02:00

| Provider | Model | Notes |
|---|---|---|
| gemini | `gemini-3.1-flash-lite` | Gemini API free tier via the OpenAI-compatible endpoint |

PASS = all checks held; FAIL = a check failed; ERROR = probe crashed; BLOCKED = the provider (rate limit/quota) prevented a verdict; SKIP = not applicable. Times are wall-clock seconds, including rate-limit pacing.

| Block | Probe | gemini |
|---|---|---|
| Skills | `ingested_skill_applied`: Ingested SKILL.md keeps its skill_id, is admitted and applied | **FAIL** 14s |

## Totals

| Provider | PASS | FAIL | ERROR | BLOCKED | SKIP |
|---|---|---|---|---|---|
| gemini | 0 | 1 | 0 | 0 | 0 |

## Details (non-PASS results)

### gemini / skills.ingested_skill_applied: FAIL
- failed check: default ingest keeps the skill_id declared in the file: skill_id='skill' warnings=['parse.warning:companion_metadata_unavailable', 'admission.pending']
- observation `skill_turn`: {'purposes': {'act': 0, 'decide': 0, 'entry': 1, 'follow_up': 0, 'judge': 0, 'plan': 0, 'reflect': 0, 'self_compaction': 0, 'skill_selection': 0, 'summarize': 0, 'tool_shortlist': 0}, 'http_requests': 2}

