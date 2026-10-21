<!-- DRAFT, NOT FILED. Review and re-run before filing. -->

# [0.1.8] `skill ingest` ignores the declared `skill_id`: every `SKILL.md` becomes `skill`, and the result is invisible to `skill list`/`show`

## Summary

`openminion skill ingest --file <dir>/SKILL.md` derives the id from the file name. The `skill_id` declared in the file's Metadata section is ignored. Every skill that follows the documented `SKILL.md` naming is stored as `skill_id: "skill"`, `name: "SKILL"`, so two unrelated skills become two *versions of the same skill*. The ingested skill stays in `draft` (`admission.pending`): `skill list` (even `--status draft`) prints "No skills found." and `skill show skill` returns `NOT_FOUND`, although ingest reported `"ok": true`.

## Environment

- openminion 0.1.8 (PyPI) and `dev` at `b642697`
- Windows 11 Pro 10.0.22631, Python 3.12.11
- No model calls needed

## Steps to reproduce

```bash
OM="openminion --home-root ./proj --data-root ./proj/.openminion"
$OM config init --provider echo
$OM skill ingest --file examples/skills/hello/SKILL.md          # declares skill_id hello.greet
$OM skill ingest --file other-skill/SKILL.md                    # any second, unrelated SKILL.md
$OM skill list
$OM skill list --status draft
$OM skill show skill
```

## Expected

- The first skill is stored as `hello.greet` (as declared) and the second under its own id.
- `skill list` shows them, at least with their pending/draft status, so the next step (`skill admit`) is discoverable.

## Actual

```
{"ok": true, "skill_id": "skill", ..., "warnings": ["parse.warning:companion_metadata_unavailable",
 "parse.warning:agent_skills_name_nonconforming", "admission.pending"]}
{"ok": true, "skill_id": "skill", ...}                           # second file: same id
No skills found.                                                 # skill list
No skills found.                                                 # skill list --status draft
{"ok": false, "error": {"code": "NOT_FOUND", "message": "Skill not found", ...}}   # skill show skill
```

`proj/.openminion/skill/skills.db` afterwards: `skills = [('skill', 'SKILL', 'draft')]`, and `skill_versions` has **2 versions of `skill`**, one per unrelated file.

## What works (for the docs)

```bash
$OM skill ingest --file other-skill/SKILL.md --name minionprobe.release_codename   # -> skill_id minionprobe_release_codename
$OM skill admit --skill-id minionprobe_release_codename --version-hash <hash> \
    --expected-active-version-hash none --target-status verified --reason "..." \
    --verification-check manual --verification-result passed --verification-evidence-ref local
$OM skill list    # now listed as verified, and the runtime applies it (verified on Gemini)
```

`--expected-active-version-hash ""` or `null` fails with `SKILL_ACTIVE_VERSION_CONFLICT`. Only the literal `none` works for a first admission, and that is not documented in `--help`.

## Impact

- Following the documented layout (one `SKILL.md` per skill folder) silently merges unrelated skills into one id. The later file becomes a new version of the earlier one.
- New users see `ok: true` followed by an empty `skill list`. The skill never activates, and the agent answers without it. In our probe, Gemini made 16 calls, ran web search and invented an answer.

## Notes

Found by MinionProbe probe `skills.ingested_skill_applied`, which checks both the documented default flow and the `--name` + `admit` flow.
