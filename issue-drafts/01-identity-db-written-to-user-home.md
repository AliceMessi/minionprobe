<!-- DRAFT, NOT FILED. Review and re-run before filing. -->

# [0.1.8] SDK writes `~/.openminion/identity/identity.db` even with explicit `home_root`/`data_root`

## Summary

`APIRuntime.from_config_path(cfg, home_root=..., data_root=...)` keeps every store under the given roots except one. The first turn of a new session opens `<user home>/.openminion/identity/identity.db`. If the user's global identity database holds a profile with the same agent id, a project-scoped runtime may also read that global profile.

## Environment

- openminion 0.1.8 (PyPI) and `dev` at `b642697` (2026-10-20); same result on both
- Windows 11 Pro 10.0.22631, Python 3.12.11 (uv venv)
- Provider: built-in `echo` (no credentials, no network)

## Steps to reproduce

```bash
# 1. a throwaway project and a throwaway "user home"
mkdir proj fakehome
openminion --home-root ./proj --data-root ./proj/.openminion config init --provider echo
```

```python
# 2. repro.py: run with USERPROFILE/HOME pointing at ./fakehome and no OPENMINION_* env vars
import logging, sys, uuid
from pathlib import Path
logging.disable(logging.CRITICAL)
from openminion import Agent, APIRuntime

proj = Path(sys.argv[1]).resolve()
rt = APIRuntime.from_config_path(str(proj / ".openminion" / "agents.json"),
                                 home_root=proj, data_root=proj / ".openminion")
with Agent(runtime=rt, session_id="leak-" + uuid.uuid4().hex[:8]) as agent:
    print(agent.run("hello").run_state)
rt.close()
print("leaked:", sorted(str(p) for p in Path.home().rglob("*") if p.is_file()))
```

```bash
USERPROFILE=$PWD/fakehome HOME=$PWD/fakehome python repro.py ./proj
```

## Expected

Nothing is created under the user's home. Identity state lives at `proj/.openminion/identity/identity.db`, like every other store.

## Actual

```
completed
leaked: ['.../fakehome/.openminion/identity/identity.db']
```

Stack at the time of the write (abridged):

```
modules/context/service.py            _render_identity
modules/brain/adapters/context/bridges/identity.py  _resolve_identityctl -> _build_identity_ctl
    identity_db = load_config().storage.db_path      # no home_root / data_root passed
modules/storage/record_store.py       sqlite3.connect(...)
```

`BridgeIdentityClient._build_identity_ctl` calls `openminion.modules.identity.config.load_config()` without the runtime's `home_root`/`data_root`. The path is then resolved from environment variables, falling back to `Path.home()`.

## Impact

- State isolation per project (the documented `--home-root`/`--data-root` pattern) silently breaks. CI runners, multi-tenant hosts and test suites all write to the real user profile.
- The identity bridge reads profiles from the wrong database. With `identity.bridge_fallback reason=profile_absent` in the logs it falls back today, but a global profile with a matching agent id would be applied to a project-scoped agent.

## Workaround

Set `OPENMINION_HOME` and `OPENMINION_DATA_ROOT` in the process environment as well as passing `home_root`/`data_root`. MinionProbe does this (`minionprobe/env.py::isolation_env`); with both set, nothing is written outside the project.

## Notes

- Found by MinionProbe's `diagnostics.home_isolation` probe, which runs this exact check in a subprocess with a temporary user profile.
- Possibly related to the config-path re-prefixing fixes in #236, but this is a different code path (identity bridge).
