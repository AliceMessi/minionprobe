"""#353: upgrade from the prior published schema (0.1.10, runtime head 11) to 0.1.11.

Modes (each run in its own process):
  --mode seed           with openminion==0.1.10 as the writer: tied messages, a
                        compaction context whose anchor sits inside a tied group of
                        6, messages after it, and a control session whose anchor is
                        alone at its timestamp. Records what 0.1.10 reads.
  --mode upgrade        with openminion==0.1.11, after the 0.1.10 process exited
                        (writers stopped) and a pre-upgrade backup was taken: open
                        (migrations 12 and 13 apply), check, restart, check, append
                        more tied messages, compact, restart, check.
  --mode verify-backup  (SQLite) with 0.1.10: the pre-upgrade backup opens at head 11
                        with the seeded history (rollback needs this matching backup).

History written by the 0.1.10 public owners: SessionStore.append_message (clock and
uuid4 patched only to force ties and fixed ids) and
SessionContextService.compact_session.

Usage:
  python -I probe_353_upgrade.py <work_dir> <out.json> --mode seed|upgrade|verify-backup
         [--backend sqlite|postgres] [--pg-schema NAME] [--backup PATH] [--seed-json PATH]
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402


def arg(name: str, default: str = "") -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
MODE = arg("--mode")
BACKEND = arg("--backend", "sqlite")
SCHEMA = arg("--pg-schema", "")
BACKUP = arg("--backup", "")
SEED_JSON = arg("--seed-json", "")
WORK.mkdir(parents=True, exist_ok=True)
clear_override_env()
quiet()
info = banner("probe_353_upgrade", mode=MODE, backend=BACKEND)

import openminion  # noqa: E402
from openminion.modules.storage.runtime.context import build_runtime_storage  # noqa: E402
from openminion.services.context.session import SessionContextService  # noqa: E402

MESSAGE_CLOCK = "openminion.modules.storage.runtime.session_store.messages.utc_now_iso"
MESSAGE_UUID = "openminion.modules.storage.runtime.session_store.messages.uuid4"
T0, T1, T2, T3 = ("2026-10-22T00:59:00+00:00", "2026-10-22T01:00:00+00:00",
                  "2026-10-22T01:01:00+00:00", "2026-10-22T01:02:00+00:00")
DB = WORK / "state" / "openminion.db"
c = Checks()
observed: dict[str, object] = {"version": openminion.__version__}


def pg_url() -> str:
    base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options={quote_plus('-csearch_path=' + SCHEMA)}&application_name={SCHEMA}"


def admin_exec(sql: str) -> None:
    import sqlalchemy as sa

    eng = sa.create_engine(os.environ["OPENMINION_TEST_POSTGRES_URL"], future=True)
    try:
        with eng.begin() as conn:
            conn.execute(sa.text(sql))
    finally:
        eng.dispose()


def open_storage(path: Path = DB):
    if BACKEND == "postgres":
        return build_runtime_storage(path, env={}, record_backend="record.postgres",
                                     record_backend_options={"url": pg_url()})
    return build_runtime_storage(path)


def append(sessions, session_id: str, at: str, rows) -> list:
    """rows: (body, id) pairs appended in order at one timestamp."""
    with patch(MESSAGE_CLOCK, return_value=at), patch(MESSAGE_UUID, side_effect=[SimpleNamespace(hex=i) for _, i in rows]):
        return [sessions.append_message(session_id=session_id, role="inbound", body=b) for b, _ in rows]


def context_dict(ctx) -> dict:
    return {k: getattr(ctx, k) for k in ("compacted_until_rowid", "compacted_until_created_at", "compacted_until_message_id",
                                         "compacted_message_count", "rolling_summary", "version", "updated_at")}


def visible(sessions, session_id: str) -> list[str]:
    ctx = sessions.get_session_context(session_id=session_id)
    after = ctx.compacted_until_rowid if ctx else 0
    return [m.body for m in sessions.list_messages_after_rowid(session_id=session_id, after_rowid=after, limit=1000)]


def history(svc, session_id: str, target: str) -> list[str]:
    return [m.body for m in svc.build_history(session_id=session_id, channel="console", target=target, recent_limit=100)
            if (m.metadata or {}).get("role") != "system"]


def message_rows(storage, session_id: str) -> list[dict]:
    rows = storage.record_store.query_dicts(
        "SELECT id, body, role, created_at FROM messages WHERE session_id = ? ORDER BY id", (session_id,))
    return [{"id": r["id"], "body": r["body"], "role": r["role"], "created_at": str(r["created_at"])} for r in rows]


def ledger(storage) -> list[int]:
    return [int(r["version"]) for r in storage.record_store.query_dicts("SELECT version FROM migrations ORDER BY version")]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ===================================================================== seed (0.1.10)
if MODE == "seed":
    if BACKEND == "postgres":
        admin_exec(f'CREATE SCHEMA "{SCHEMA}"')
    st = open_storage()
    try:
        s = st.sessions
        sess = s.resolve_session(agent_id="main", channel="console", target="s", session_id="upgrade-s")
        solo = s.resolve_session(agent_id="main", channel="console", target="solo", session_id="upgrade-solo")
        svc = SessionContextService(s, keep_recent_messages=2, archive_enabled=False)
        # session s: append order e0, g1..g4; compaction; g5, g6 in the same tick; l1.
        # Inside T1 every id sorts before the id of the message appended before it.
        append(s, sess.id, T0, [("e0", "5" * 32)])
        append(s, sess.id, T1, [("g1", "f" * 32), ("g2", "d" * 32), ("g3", "b" * 32), ("g4", "9" * 32)])
        res = svc.compact_session(session_id=sess.id, force=True)
        ctx_after_compaction = s.get_session_context(session_id=sess.id)
        compacted_bodies = [m.body for m in s.list_messages(session_id=sess.id, limit=ctx_after_compaction.compacted_until_rowid)]
        append(s, sess.id, T1, [("g5", "7" * 32), ("g6", "3" * 32)])
        append(s, sess.id, T2, [("l1", "4" * 32)])
        # control session: anchor alone at its timestamp
        append(s, solo.id, T0, [("a1", "a" * 32)])
        append(s, solo.id, T1, [("a2", "c" * 32)])
        append(s, solo.id, T2, [("a3", "e" * 32)])
        append(s, solo.id, T3, [("a4", "8" * 32)])
        svc.compact_session(session_id=solo.id, force=True)
        seed = {
            "append_order_s": ["e0", "g1", "g2", "g3", "g4", "g5", "g6", "l1"],
            "first_compaction": {"compacted_count": res.compacted_count, "compacted_bodies": compacted_bodies,
                                 "context": context_dict(ctx_after_compaction)},
            "context_s": context_dict(s.get_session_context(session_id=sess.id)),
            "context_solo": context_dict(s.get_session_context(session_id=solo.id)),
            "read_order_s_0110": [m.body for m in s.list_messages(session_id=sess.id)],
            "visible_after_boundary_s_0110": visible(s, sess.id),
            "build_history_s_0110": history(svc, sess.id, "s"),
            "visible_solo_0110": visible(s, solo.id),
            "never_compacted_s": [b for b in ["e0", "g1", "g2", "g3", "g4", "g5", "g6", "l1"] if b not in compacted_bodies],
            "messages_s": message_rows(st, sess.id),
            "messages_solo": message_rows(st, solo.id),
            "ledger": ledger(st),
        }
    finally:
        st.close()  # the 0.1.10 writer stops here; the process exits next
    hidden = [b for b in seed["never_compacted_s"] if b not in seed["visible_after_boundary_s_0110"]]
    replayed = [b for b in seed["first_compaction"]["compacted_bodies"] if b in seed["visible_after_boundary_s_0110"]]
    seed["0110_hidden_uncompacted"] = hidden
    seed["0110_compacted_shown_again"] = replayed
    observed["seed"] = seed
    c.check("seed written by 0.1.10 at runtime head 11", seed["ledger"] == list(range(1, 12)) and openminion.__version__ == "0.1.10", seed["ledger"])
    c.check("seed: 0.1.10 compaction anchored inside the tied T1 group", seed["context_s"]["compacted_until_created_at"] == T1,
            seed["first_compaction"])
    # Recorded, not asserted as pass/fail of 0.1.11: what the pre-fix release reads.
    print(json.dumps({"0110_read_order": seed["read_order_s_0110"], "0110_visible": seed["visible_after_boundary_s_0110"],
                      "0110_hidden_uncompacted": hidden, "0110_compacted_shown_again": replayed}))

# ===================================================================== upgrade (0.1.11)
elif MODE == "upgrade":
    seed = json.loads(Path(SEED_JSON).read_text(encoding="utf-8"))["observed"]["seed"]
    order_sqlite = ["e0", "g1", "g2", "g3", "g4", "g5", "g6", "l1"]  # migration-time rowid = append order
    order_pg = ["e0", "g6", "g5", "g4", "g3", "g2", "g1", "l1"]  # pre-upgrade (created_at, id) read order
    expected_order = order_sqlite if BACKEND == "sqlite" else order_pg

    # Coordinated upgrade: the 0.1.10 writer process has exited; keep a pre-upgrade backup.
    backup: dict[str, object] = {"writers_stopped": "the 0.1.10 seed process closed its storage and exited before this step"}
    if BACKEND == "sqlite":
        bpath = WORK / "backup" / "openminion.pre-0.1.11.db"
        bpath.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(DB))
        dst = sqlite3.connect(str(bpath))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        b = sqlite3.connect(str(bpath))
        try:
            backup.update({"method": "sqlite3 online backup API", "path": str(bpath), "sha256": sha256(bpath),
                           "ledger_max": b.execute("SELECT MAX(version) FROM migrations").fetchone()[0],
                           "messages": b.execute("SELECT COUNT(*) FROM messages").fetchone()[0]})
        finally:
            b.close()
    else:
        bpath = Path(BACKUP)
        text = bpath.read_text(encoding="utf-8", errors="replace")
        backup.update({"method": "pg_dump -n <schema> (taken by steps_353.sh before this step)", "sha256": sha256(bpath),
                       "bytes": bpath.stat().st_size, "has_messages_table": "CREATE TABLE" in text and ".messages" in text})
    observed["backup"] = backup
    c.check("pre-upgrade backup taken after the writer stopped",
            (backup.get("ledger_max") == 11 and backup.get("messages") == 12) if BACKEND == "sqlite" else bool(backup.get("has_messages_table")),
            {k: v for k, v in backup.items() if k != "path"})

    def check_state(label: str, st, svc) -> None:
        s = st.sessions
        rows = message_rows(st, "upgrade-s")
        seq = st.record_store.query_dicts("SELECT body, append_seq FROM messages WHERE session_id = ? ORDER BY append_seq",
                                          ("upgrade-s",))
        seq_order = [r["body"] for r in seq if r["body"] in expected_order]
        listed = [m.body for m in s.list_messages(session_id="upgrade-s")][: len(expected_order)]
        ctx = context_dict(s.get_session_context(session_id="upgrade-s"))
        vis = visible(s, "upgrade-s")
        hist = history(svc, "upgrade-s", "s")
        vis_solo = visible(s, "upgrade-solo")
        ctx_solo = context_dict(s.get_session_context(session_id="upgrade-solo"))
        hidden = [b for b in seed["never_compacted_s"] if b not in vis]
        replay = [b for b in seed["first_compaction"]["compacted_bodies"] if b in vis and b != "e0"]
        observed[label] = {"append_seq_order": seq_order, "list_messages": listed, "context_s": ctx, "visible_s": vis,
                           "build_history_s": hist, "context_solo": ctx_solo, "visible_solo": vis_solo,
                           "hidden_uncompacted": hidden, "compacted_shown_again_once": replay}
        seed_ids = {r["id"] for r in seed["messages_s"]}
        kept = [r for r in rows if r["id"] in seed_ids]
        c.check(f"{label}: public ids, bodies, roles and created_at unchanged", kept == seed["messages_s"],
                {"seeded": len(seed["messages_s"]), "matching": sum(r in seed["messages_s"] for r in kept)})
        c.check(f"{label}: append_seq order is the {'migration-time rowid (append) order' if BACKEND == 'sqlite' else 'pre-upgrade (created_at, id) order'}",
                seq_order == expected_order, seq_order)
        c.check(f"{label}: list_messages reads in that order", listed == expected_order, listed)
        c.check(f"{label}: tied-group boundary moved back to e0 (rowid 1, count 1), summary kept",
                (ctx["compacted_until_rowid"], ctx["compacted_until_created_at"], ctx["compacted_until_message_id"],
                 ctx["compacted_message_count"], ctx["rolling_summary"]) ==
                (1, T0, "5" * 32, 1, seed["context_s"]["rolling_summary"]), ctx)
        c.check(f"{label}: no never-compacted message is hidden", hidden == [], {"never_compacted": seed["never_compacted_s"], "visible": vis})
        c.check(f"{label}: visible history = every message after e0 in the stored order", vis == expected_order[1:], vis)
        c.check(f"{label}: build_history shows the same messages", hist == expected_order[1:], hist)
        c.check(f"{label}: control session (anchor alone at its timestamp) keeps its boundary",
                (ctx_solo["compacted_until_rowid"], ctx_solo["compacted_message_count"], ctx_solo["compacted_until_message_id"]) ==
                (seed["context_solo"]["compacted_until_rowid"], seed["context_solo"]["compacted_message_count"],
                 seed["context_solo"]["compacted_until_message_id"]) and vis_solo == ["a3", "a4"], {"context": ctx_solo, "visible": vis_solo})

    st = open_storage()
    try:
        applied = list(st.migration_result.applied_versions)
        observed["upgrade_migration_result"] = {"applied_versions": applied, "current_version": st.migration_result.current_version}
        c.check("upgrade applies migrations 12 and 13", applied == [12, 13] and st.migration_result.current_version == 13, applied)
        c.check("ledger after upgrade is 1..13", ledger(st) == list(range(1, 14)), ledger(st))
        check_state("after upgrade", st, SessionContextService(st.sessions, keep_recent_messages=2, archive_enabled=False))
    finally:
        st.close()

    st = open_storage()  # restart
    try:
        c.check("restart: no migration re-applied", list(st.migration_result.applied_versions) == [], list(st.migration_result.applied_versions))
        svc = SessionContextService(st.sessions, keep_recent_messages=2, archive_enabled=False)
        check_state("after restart", st, svc)
        s = st.sessions
        new = append(s, "upgrade-s", T1, [("g7", "1" * 32), ("g8", "0" * 32)])
        vis_new = visible(s, "upgrade-s")
        exp_new = expected_order[1:7] + ["g7", "g8", "l1"]
        observed["after post-upgrade tied appends"] = {"visible_s": vis_new, "append_time_rowids": [m.rowid for m in new]}
        c.check("post-upgrade appends in the same tick land after the tied group and are visible", vis_new == exp_new, vis_new)
        res = svc.compact_session(session_id="upgrade-s", force=True)
        ctx = context_dict(s.get_session_context(session_id="upgrade-s"))
        vis_c = visible(s, "upgrade-s")
        observed["after post-upgrade compaction"] = {"compacted_count": res.compacted_count, "context_s": ctx, "visible_s": vis_c}
        c.check("compaction after the upgrade keeps the last 2 in stored order and hides nothing else",
                res.compacted_count == 7 and ctx["compacted_until_rowid"] == 8 and vis_c == ["g8", "l1"], {"count": res.compacted_count, "visible": vis_c})
    finally:
        st.close()

    st = open_storage()  # second restart
    try:
        svc = SessionContextService(st.sessions, keep_recent_messages=2, archive_enabled=False)
        vis_r = visible(st.sessions, "upgrade-s")
        hist_r = history(svc, "upgrade-s", "s")
        observed["after second restart"] = {"visible_s": vis_r, "build_history_s": hist_r}
        c.check("second restart: boundary and visible tail unchanged", vis_r == ["g8", "l1"] and hist_r == ["g8", "l1"], {"visible": vis_r, "history": hist_r})
    finally:
        st.close()
    if BACKEND == "postgres":
        import sqlalchemy as sa

        eng = sa.create_engine(os.environ["OPENMINION_TEST_POSTGRES_URL"], future=True)
        with eng.begin() as conn:
            observed["postgres_server_version"] = conn.execute(sa.text("SHOW server_version")).scalar_one()
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE'))
        eng.dispose()

# ===================================================================== verify-backup (0.1.10, SQLite)
elif MODE == "verify-backup":
    bpath = Path(BACKUP)
    copy = WORK / "restore" / "openminion.db"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(bpath.read_bytes())
    st = build_runtime_storage(copy)
    try:
        bodies = [m.body for m in st.sessions.list_messages(session_id="upgrade-s")]
        observed["restored_backup"] = {"applied_on_open": list(st.migration_result.applied_versions), "ledger": ledger(st),
                                       "session_s_message_count": len(bodies)}
        c.check("0.1.10 opens the pre-upgrade backup at head 11 without migrating", list(st.migration_result.applied_versions) == []
                and ledger(st) == list(range(1, 12)) and len(bodies) == 8 and openminion.__version__ == "0.1.10", observed["restored_backup"])
    finally:
        st.close()
else:
    raise SystemExit(f"unknown --mode {MODE!r}")

payload = {"banner": info, "mode": MODE, "backend": BACKEND, "summary": c.summary(), "all_pass": c.all_pass,
           "checks": c.items, "observed": observed}
write_result(OUT, payload)
print(json.dumps({"summary": c.summary(), "all_pass": c.all_pass}))
sys.exit(0 if c.all_pass else 1)
