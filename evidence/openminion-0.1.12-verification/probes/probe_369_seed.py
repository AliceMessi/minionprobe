"""#369: write or check a pre-migration-15 runtime history with the published 0.1.11.

    python -I probe_369_seed.py seed   <sqlite|postgres> <work> <out.json> (--db <path> | --schema <name>) [--bad]
    python -I probe_369_seed.py verify <sqlite|postgres> <work> <out.json> (--db <path> | --schema <name> [--database <name>]) --expect <seed.json>

`seed` (run with openminion==0.1.11, migration head 13) writes 1,262 sessions
through the 0.1.11 SessionStore: 1,200 bulk sessions with string, non-string
and odd members (NaN, Infinity, an escaped NUL, a lone surrogate), the filter
table sessions, the odd metadata sessions, an ACP-style and a Focus-style
session, and `zz-last`. With --bad it also stores `zz-bad`, then overwrites its
metadata_json with truncated JSON (a stored document `json.loads` cannot
read). Its ids sort after more than 1,000 others, so the migration-15 backfill
reaches it in its second batch. It records the ledger, a sha256 of every
(id, metadata_json), and what the 0.1.11 filters return.

`verify` (run with 0.1.11 against the pre-upgrade BACKUP only, never against
the migrated database) opens the backup, checks that 0.1.11 applies nothing,
the ledger head is 13, there is no session_metadata_strings table, and the
stored metadata digest equals the seed's.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402
import probe_369_lib as L  # noqa: E402

MODE, BACKEND = sys.argv[1], sys.argv[2]
WORK = Path(sys.argv[3]).resolve()
OUT = Path(sys.argv[4])
ARGS = sys.argv[5:]


def arg(name: str, default: str | None = None) -> str | None:
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


PGURL = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
quiet()
clear_override_env()
WORK.mkdir(parents=True, exist_ok=True)
checks = Checks()
info = banner(f"probe_369_seed {MODE}", backend=BACKEND)


def pg_url_for(database: str | None, schema: str) -> str:
    base = PGURL
    if database:
        from sqlalchemy.engine import make_url

        base = make_url(PGURL).set(database=database).render_as_string(hide_password=False)
    return L.schema_url(base, schema)


def bulk_metadata(index: int) -> dict[Any, Any]:
    meta: dict[Any, Any] = {
        "surface": ("acp", "cli", "focus")[index % 3],
        "workspace_root": f"/work/{index % 7}",
        "title": f"legacy session {index}",
    }
    if index % 50 == 0:
        meta.update(score=L.NAN, note="n" + L.NUL + "ul", tag="s" + L.LONE_HIGH)
    if index % 97 == 0:
        meta.update(count=index, flag=True, none=None, nested={"surface": "acp"})
    if index % 211 == 0:
        meta.update(high=L.INF, low=-L.INF)
    return meta


LEGACY_FILTERS = L.LEGACY_FILTERS

if MODE == "seed":
    if BACKEND == "sqlite":
        db = Path(arg("--db")).resolve()
        storage = L.open_storage("sqlite", db_path=db)
    else:
        schema = arg("--schema")
        L.create_schema(PGURL, schema)
        info["postgres_server_version"] = L.pg_server_version(PGURL)
        storage = L.open_storage("postgres", url=pg_url_for(None, schema), work=WORK)
    try:
        s = storage.sessions
        for index in range(1200):
            s.resolve_session(agent_id="main", channel="console", target="legacy", session_id=f"legacy-{index:04d}",
                              metadata=bulk_metadata(index))
        L.store_sessions(s, L.FILTER_SESSIONS, "legacy-matrix")
        L.store_sessions(s, L.ODD_METADATA, "legacy-odd")
        s.resolve_session(agent_id="openminion", channel="console", target="local-client", session_id="legacy-acp-client",
                          metadata={"surface": "acp", "workspace_root": "/w/acp"})
        s.resolve_session(agent_id="alpha", channel="cli", target="focus", session_id="legacy-focus-client",
                          metadata={"working_dir": "/w/focus", "workspace_root": "/w/focus", "cwd": "/w/focus", "focus_mode": True})
        s.resolve_session(agent_id="main", channel="console", target="legacy-last", session_id="zz-last",
                          metadata={"surface": "acp", "position": "last"})
        if "--bad" in ARGS:
            s.resolve_session(agent_id="main", channel="console", target="legacy-last", session_id="zz-bad",
                              metadata={"surface": "acp"})
            s._backend.execute_count("UPDATE sessions SET metadata_json = ? WHERE id = ?", ('{"surface": "acp"', "zz-bad"))
        led = L.ledger(storage)
        meta = L.stored_metadata(storage)
        result = {
            "ledger": led,
            "sessions": len(meta),
            "metadata_sha256": L.metadata_digest(meta),
            "session_metadata_strings_exists": L.table_exists(storage, BACKEND, "session_metadata_strings"),
            "bad_session": meta.get("zz-bad"),
            "ids_before_zz": sum(1 for k in meta if k < "zz"),
        }
        obs: dict[str, Any] = {}
        for flt, _ in L.FILTER_EXPECTED:
            obs[f"matrix {L.show_filter(flt)}"] = L.outcome_ids(s, flt, "legacy-matrix")
        for flt, _ in L.POSTGRES_ODD_EXPECTED:
            obs[f"odd {L.show_filter(flt)}"] = L.outcome_ids(s, flt, "legacy-odd")
        for flt in LEGACY_FILTERS:
            obs[f"legacy {L.show_filter(flt)}"] = L.outcome_ids(s, flt, "legacy")
        result["observations"] = obs
        checks.check(f"seed: written by openminion {L.VERSION}; ledger head is {max(v for v, _ in led)}",
                     True, observed=[v for v, _ in led][-3:])
        checks.check("seed: no session_metadata_strings table before the upgrade", not result["session_metadata_strings_exists"])
        checks.check(f"seed: {len(meta)} sessions stored; more than 1,000 ids sort before zz-*", result["ids_before_zz"] > 1000,
                     observed=result["ids_before_zz"])
        if "--bad" in ARGS:
            checks.check("seed: zz-bad's stored metadata_json is truncated JSON", meta.get("zz-bad") == '{"surface": "acp"', observed=meta.get("zz-bad"))
        sample = obs[f"legacy {L.show_filter({'surface': 'acp'})}"]
        print("0.1.x filter {'surface': 'acp'} on the legacy target ->", sample if isinstance(sample, str) else f"{len(sample)} sessions")
    finally:
        storage.close()
    info.update(result)

elif MODE == "verify":
    expect = json.loads(Path(arg("--expect")).read_text(encoding="utf-8"))
    if BACKEND == "sqlite":
        storage = L.open_storage("sqlite", db_path=Path(arg("--db")).resolve())
    else:
        info["postgres_server_version"] = L.pg_server_version(PGURL)
        storage = L.open_storage("postgres", url=pg_url_for(arg("--database"), arg("--schema")), work=WORK)
    try:
        applied = tuple(storage.migration_result.applied_versions)
        led = L.ledger(storage)
        meta = L.stored_metadata(storage)
        checks.check(f"backup: openminion {L.VERSION} opens the restored pre-upgrade backup and applies nothing", applied == (), observed=applied)
        checks.check("backup: ledger head is 13 (no 14, no 15)", max(v for v, _ in led) == 13, observed=[v for v, _ in led][-3:])
        checks.check("backup: no session_metadata_strings table", not L.table_exists(storage, BACKEND, "session_metadata_strings"))
        checks.check("backup: stored metadata identical to the pre-upgrade seed (sha256 of every id+metadata_json)",
                     L.metadata_digest(meta) == expect["metadata_sha256"] and len(meta) == expect["sessions"],
                     observed=(len(meta), L.metadata_digest(meta)[:16]))
        if BACKEND == "sqlite":
            got = L.outcome_ids(storage.sessions, {"surface": "acp", "workspace_root": "/work/a"}, "legacy-matrix")
            checks.check("backup: 0.1.11 reads and filters the restored sessions", got == ["acp-a"], observed=got)
        else:
            got = storage.sessions.get_session("legacy-acp-client")
            checks.check("backup: 0.1.11 reads the restored sessions", got is not None and got.metadata.get("surface") == "acp")
    finally:
        storage.close()
else:
    raise SystemExit(f"unknown mode {MODE}")

summary = checks.summary()
write_result(OUT, {**info, "summary": summary, "checks": checks.items})
print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
sys.exit(0 if checks.all_pass else 1)
