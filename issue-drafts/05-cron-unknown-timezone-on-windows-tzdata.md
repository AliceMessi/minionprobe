<!-- Filed upstream on 2026-10-21 as https://github.com/openminion/openminion/issues/327 -->

# [0.1.8, Windows] Every `openminion cron create --cron ...` fails with `unknown timezone: UTC` (missing `tzdata` dependency)

## Summary

On Windows, any recurring cron schedule is rejected, including the default timezone:

```
"error": "Invalid schedule: unknown timezone: UTC"
```

Windows ships no IANA time zone database, so Python's `zoneinfo` needs the `tzdata` package. `openminion` does not declare it. One-shot `--at` jobs work because they do not resolve a zone.

## Environment

- openminion 0.1.8 (PyPI) and `dev` at `b642697`, fresh `uv venv -p 3.12` + `uv pip install openminion==0.1.8`
- Windows 11 Pro 10.0.22631, Python 3.12.11
- `python -c "import tzdata"` gives `ModuleNotFoundError`

## Steps to reproduce

```powershell
uv venv -p 3.12 .venv; uv pip install --python .venv\Scripts\python.exe openminion==0.1.8
& .venv\Scripts\openminion.exe --home-root .\proj --data-root .\proj\.openminion config init --provider echo
& .venv\Scripts\openminion.exe --home-root .\proj --data-root .\proj\.openminion cron create --instruction "say hi" --cron "0 9 * * 1-5" --json
& .venv\Scripts\openminion.exe --home-root .\proj --data-root .\proj\.openminion cron create --instruction "say hi" --cron "0 9 * * 1-5" --tz Europe/Rome --json
& .venv\Scripts\openminion.exe --home-root .\proj --data-root .\proj\.openminion cron create --instruction "say hi" --at 2026-12-31T09:00:00+00:00 --json
```

## Expected

All three jobs are created (`"ok": true`).

## Actual

| Command | Result |
|---|---|
| `--cron "0 9 * * 1-5"` (no `--tz`) | `ok: false`, `Invalid schedule: unknown timezone: UTC` |
| `--cron ... --tz UTC` | `ok: false`, `Invalid schedule: unknown timezone: UTC` |
| `--cron ... --tz Europe/Rome` | `ok: false`, `Invalid schedule: unknown timezone: Europe/Rome` |
| `--at 2026-12-31T09:00:00+00:00` | `ok: true` |

After `pip install tzdata` in the same venv, `--cron ... --tz Europe/Rome` returns `ok: true` with a task id.

## Impact

- Scheduled tasks (`cron create`, and the model-facing `task.schedule` with `{kind: "cron"}`) are unusable on Windows out of the box, with an error that points at the user's input rather than a missing dependency.
- Users following the docs' `--tz` examples hit it immediately.

## Suggested fix

Add `tzdata; sys_platform == "win32"` (or unconditionally) to `project.dependencies`, or catch `ZoneInfoNotFoundError` and say "install tzdata".

## Notes

Found by MinionProbe probe `tasks.cron_schedules` (CLI only, no model calls).
