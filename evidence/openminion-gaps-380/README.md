# OpenMinion: five gaps left out of #380

The maintainer asked for separate minimal reports of the gaps that PR #380 (for #368) disclosed under "Left out". Each probe reproduces one gap on `dev` @ `81c134df` and prints JSON.

| Probe | Gap |
|---|---|
| `probe_external_close.py` | `external` memory provider: the service store (`BackendMemoryStoreAdapter`) has no `close()`, so the assembly close raises and the artifact store stays open; the created memory store is outside the close chain |
| `probe_fallback_retention.py` | `build_agent_memory_service` keeps only the gateway of the assembly it builds, so its store, audit sink and artifact store have no closer |
| `probe_construction_failure.py` | a build that fails after `resolve_memory_backend` (for example an unregistered external adapter) leaves the store and the artifact store open |
| `probe_remote_config.py` | a typed `MemctlConfig` with `store.backend: remote` fails in `resolve_memory_backend` (`remote.endpoint` vs `StoreConfig.remote_endpoint`) |
| `probe_pg_connect.py` | with the PostgreSQL memory store, the runtime session lifecycle retention helpers take the SQLite branch behind `AuditedMemoryStore` and raise on `_connect` |

Run from an OpenMinion source checkout with development dependencies: `PYTHONPATH="src;." python <probe>` on Windows, `PYTHONPATH=src:. python <probe>` on Linux. The PostgreSQL rows need `OPENMINION_TEST_POSTGRES_URL`; without it they are skipped.

Raw outputs, with the sha256 of each probe:
- `run-windows-81c134df.log`: native Windows 11, CPython 3.11.11
- `run-linux-81c134df.log`: Ubuntu 24.04 GitHub-hosted runner, CPython 3.11.17, PostgreSQL 16

The SQLite rows are identical on both platforms.
