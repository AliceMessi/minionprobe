"""#353: equal-timestamp messages keep append order (installed 0.1.11, fresh store).

  A. The issue's deterministic probe: one fixed clock tick, user then assistant
     appended with ids ffff... then 0000...; list_messages / list_recent_messages(1).
  B. Four tied appends whose ids sort in reverse of append order (fc80), across two
     conversations/threads: list order, append-time rowids, list limits, newest-message
     limits, latest_conversation_id, conversation and thread filters, stored ids and
     timestamps unchanged.
  C. Cursor pagination: list_messages_after_rowid pages, an append-time cursor after
     later tied appends, list_recent_messages(after_rowid=..., exclude_message_ids=...).
  D. Restart: append_seq is per session and continues after close + reopen.
  E. A failed append (duplicate id) rolls back the session write and its append_seq.
  F. Concurrent same-session appends from 4 storage handles in one clock tick.
  G. Compaction followed by more tied appends, through the installed
     SessionContextService (compact_session / build_history), then a restart.
  H. (SQLite only) Transcript message order: `openminion export transcript` (the
     installed console script) for tied messages, JSONL and Markdown, both id orders.

Backends: --backend sqlite (default) or --backend postgres (needs
OPENMINION_TEST_POSTGRES_URL; every case runs in its own schema).

Usage: python -I probe_353.py <work_dir> <out.json> [--backend sqlite|postgres]
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
BACKEND = sys.argv[sys.argv.index("--backend") + 1] if "--backend" in sys.argv else "sqlite"
WORK.mkdir(parents=True, exist_ok=True)
clear_override_env()
quiet()
info = banner("probe_353", backend=BACKEND)

from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config  # noqa: E402
from openminion.modules.storage.runtime.context import build_runtime_storage  # noqa: E402
from openminion.services.context.session import SessionContextService  # noqa: E402

MESSAGE_CLOCK = "openminion.modules.storage.runtime.session_store.messages.utc_now_iso"
MESSAGE_UUID = "openminion.modules.storage.runtime.session_store.messages.uuid4"
TIED_AT = "2026-10-22T01:00:00+00:00"
DESCENDING_IDS = tuple(ch * 32 for ch in "fc80")  # each id sorts before the previous one

c = Checks()
observed: dict[str, object] = {}
_schemas: list[str] = []
_admin = None


def _pg_admin():
    global _admin
    if _admin is None:
        import sqlalchemy as sa

        _admin = sa.create_engine(os.environ["OPENMINION_TEST_POSTGRES_URL"], future=True)
    return _admin


def new_store_spec(label: str) -> dict:
    """A fresh, isolated runtime store: a new SQLite file, or a new PostgreSQL schema."""
    if BACKEND == "sqlite":
        return {"path": WORK / label / "state" / "openminion.db"}
    import sqlalchemy as sa

    schema = f"p353_{label}_{uuid.uuid4().hex[:10]}"
    with _pg_admin().begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    _schemas.append(schema)
    base = os.environ["OPENMINION_TEST_POSTGRES_URL"]
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}options={quote_plus('-csearch_path=' + schema)}&application_name={schema}"
    return {"path": WORK / label / "state" / "pg.db", "url": url}


def open_storage(spec: dict, peer: str = ""):
    # SQLite peers share the one database file; on PostgreSQL the path is only a placeholder.
    path = spec["path"] if (not peer or "url" not in spec) else spec["path"].with_name(f"peer-{peer}.db")
    if "url" in spec:
        return build_runtime_storage(path, env={}, record_backend="record.postgres",
                                     record_backend_options={"url": spec["url"]})
    return build_runtime_storage(path)


def tied_appends(sessions, session_id: str, specs, ids) -> list:
    with patch(MESSAGE_CLOCK, return_value=TIED_AT), patch(
        MESSAGE_UUID, side_effect=[SimpleNamespace(hex=v) for v in ids]
    ):
        return [
            sessions.append_message(session_id=session_id, conversation_id=conv, thread_id=thr,
                                    role=role, body=body)
            for role, conv, thr, body in specs
        ]


def ids_of(items) -> list[str]:
    return [item.id for item in items]


def append_seq_rows(storage, session_id: str) -> dict[str, int]:
    rows = storage.record_store.query_dicts(
        "SELECT id, append_seq FROM messages WHERE session_id = ?", (session_id,)
    )
    return {str(r["id"]): int(r["append_seq"]) for r in rows}


# ---------------------------------------------------------------- A. issue probe
spec = new_store_spec("a")
st = open_storage(spec)
try:
    s = st.sessions
    sess = s.resolve_session(agent_id="main", channel="console", target="probe")
    with patch(MESSAGE_CLOCK, return_value="2026-10-22T01:00:00+00:00"), patch(
        MESSAGE_UUID, side_effect=[SimpleNamespace(hex=i) for i in ("f" * 32, "0" * 32)]
    ):
        s.append_message(session_id=sess.id, role="user", body="first")
        s.append_message(session_id=sess.id, role="assistant", body="second")
    read_roles = [m.role for m in s.list_messages(session_id=sess.id)]
    newest = [m.role for m in s.list_recent_messages(session_id=sess.id, limit=1)]
    observed["A_issue_probe"] = {"inserted_roles": ["user", "assistant"], "read_roles": read_roles,
                                 "newest_role": newest}
    c.check("A issue probe: list_messages returns user then assistant", read_roles == ["user", "assistant"], read_roles)
    c.check("A issue probe: list_recent_messages(limit=1) returns the newest append (assistant)", newest == ["assistant"], newest)
finally:
    st.close()

# ---------------------------------------------------------------- B/C. tied group of four
spec = new_store_spec("b")
st = open_storage(spec)
try:
    s = st.sessions
    sess = s.resolve_session(agent_id="main", channel="console", target="chat")
    specs = (
        ("inbound", "conv-a", "thread-a", "conv-a/inbound"),
        ("outbound", "conv-a", "thread-a", "conv-a/outbound"),
        ("inbound", "conv-b", "thread-b", "conv-b/inbound"),
        ("outbound", "conv-b", "thread-b", "conv-b/outbound"),
    )
    appended = tied_appends(s, sess.id, specs, DESCENDING_IDS)
    a_in, a_out, b_in, b_out = appended
    ids = ids_of(appended)
    listed = s.list_messages(session_id=sess.id)
    observed["B_tied_four"] = {
        "appended_ids": [i[:4] + "..." for i in ids],
        "listed_bodies": [m.body for m in listed],
        "listed_rowids": [m.rowid for m in listed],
        "append_time_rowids": [m.rowid for m in appended],
        "created_at": sorted({m.created_at for m in listed}),
    }
    c.check("B list_messages returns the 4 tied messages in append order", ids_of(listed) == ids, [m.body for m in listed])
    c.check("B rowids are 1..4 at append time and on read", [m.rowid for m in appended] == [1, 2, 3, 4] and [m.rowid for m in listed] == [1, 2, 3, 4],
            {"append": [m.rowid for m in appended], "read": [m.rowid for m in listed]})
    c.check("B public message ids are the generated ids (unchanged)", ids == list(DESCENDING_IDS))
    c.check("B stored created_at unchanged (one tick)", {m.created_at for m in listed} == {TIED_AT}, sorted({m.created_at for m in listed}))
    c.check("B list_messages(limit=2) returns the first two appends", ids_of(s.list_messages(session_id=sess.id, limit=2)) == ids[:2])
    r1 = ids_of(s.list_recent_messages(session_id=sess.id, limit=1))
    r3 = ids_of(s.list_recent_messages(session_id=sess.id, limit=3))
    c.check("B list_recent_messages(limit=1) returns the newest append", r1 == ids[3:], [i[:4] for i in r1])
    c.check("B list_recent_messages(limit=3) returns the last three appends in order", r3 == ids[1:], [i[:4] for i in r3])
    c.check("B latest_conversation_id is the newest append's conversation", s.latest_conversation_id(session_id=sess.id) == "conv-b",
            s.latest_conversation_id(session_id=sess.id))
    conv_a = ids_of(s.list_messages(session_id=sess.id, conversation_id="conv-a"))
    conv_a_recent = ids_of(s.list_recent_messages(session_id=sess.id, conversation_id="conv-a", limit=1))
    thread_b = s.list_recent_messages(session_id=sess.id, conversation_id="conv-b", thread_id="thread-b", limit=1)
    thread_b_all = ids_of(s.list_messages(session_id=sess.id, conversation_id="conv-b", thread_id="thread-b"))
    after_thread = ids_of(s.list_messages_after_rowid(session_id=sess.id, after_rowid=a_out.rowid, thread_id="thread-b"))
    c.check("B conversation filter conv-a: append order", conv_a == [a_in.id, a_out.id])
    c.check("B conversation filter conv-a, newest limit 1: the newer append", conv_a_recent == [a_out.id])
    c.check("B conversation+thread filter conv-b/thread-b: append order", thread_b_all == [b_in.id, b_out.id])
    c.check("B thread filter newest limit 1: newest append, session-wide rowid 4", ids_of(thread_b) == [b_out.id] and thread_b[0].rowid == 4,
            {"rowid": thread_b[0].rowid if thread_b else None})
    c.check("B thread filter after an append-time cursor: append order", after_thread == [b_in.id, b_out.id])

    pages, cursor = [], 0
    while True:
        page = s.list_messages_after_rowid(session_id=sess.id, after_rowid=cursor, limit=2)
        if not page:
            break
        pages.append(ids_of(page))
        cursor = page[-1].rowid
    observed["C_pages"] = [[i[:4] for i in p] for p in pages]
    c.check("C list_messages_after_rowid pages of 2 follow append order", pages == [ids[:2], ids[2:]], observed["C_pages"])
    after_first = ids_of(s.list_messages_after_rowid(session_id=sess.id, after_rowid=appended[0].rowid))
    c.check("C a cursor taken at the first append still points at it after 3 later tied appends", after_first == ids[1:])
    excl = ids_of(s.list_recent_messages(session_id=sess.id, limit=2, after_rowid=appended[1].rowid, exclude_message_ids=(ids[3],)))
    c.check("C list_recent_messages(after_rowid, exclude_message_ids) keeps append order", excl == [ids[2]])
finally:
    st.close()

# ---------------------------------------------------------------- D/E. restart, rollback
spec = new_store_spec("d")
st = open_storage(spec)
try:
    s = st.sessions
    first = s.resolve_session(agent_id="main", channel="console", target="first")
    second = s.resolve_session(agent_id="main", channel="console", target="second")
    with patch(MESSAGE_CLOCK, return_value=TIED_AT):
        first_ids = [s.append_message(session_id=first.id, role="inbound", body="f1").id,
                     s.append_message(session_id=first.id, role="outbound", body="f2").id]
        second_id = s.append_message(session_id=second.id, role="inbound", body="s1").id
finally:
    st.close()
st = open_storage(spec)
try:
    s = st.sessions
    with patch(MESSAGE_CLOCK, return_value=TIED_AT):
        first_ids.append(s.append_message(session_id=first.id, role="inbound", body="f3").id)
    seq_first = append_seq_rows(st, first.id)
    seq_second = append_seq_rows(st, second.id)
    c.check("D append_seq is per session and continues after close + reopen",
            seq_first == {first_ids[0]: 1, first_ids[1]: 2, first_ids[2]: 3} and seq_second == {second_id: 1},
            {"first": sorted(seq_first.values()), "second": sorted(seq_second.values())})
    c.check("D after restart the session reads in append order", ids_of(s.list_messages(session_id=first.id)) == first_ids,
            [m.body for m in s.list_messages(session_id=first.id)])

    sess = s.resolve_session(agent_id="main", channel="console", target="rollback")
    with patch(MESSAGE_CLOCK, return_value=TIED_AT):
        m1 = s.append_message(session_id=sess.id, role="inbound", body="first")
    before = s.get_session(sess.id)
    err = None
    with patch(MESSAGE_CLOCK, return_value="2026-10-22T01:00:01+00:00"), patch(MESSAGE_UUID, return_value=SimpleNamespace(hex=m1.id)):
        try:
            s.append_message(session_id=sess.id, role="outbound", body="duplicate id")
        except Exception as exc:  # noqa: BLE001
            err = type(exc).__name__
    after = s.get_session(sess.id)
    with patch(MESSAGE_CLOCK, return_value=TIED_AT):
        m2 = s.append_message(session_id=sess.id, role="outbound", body="second")
    c.check("E a failed append raises an integrity error", err is not None and "IntegrityError" in err, err)
    c.check("E the failed append left the session row unchanged",
            (after.updated_at, after.last_activity_at) == (before.updated_at, before.last_activity_at))
    c.check("E the failed append consumed no append_seq", append_seq_rows(st, sess.id) == {m1.id: 1, m2.id: 2},
            sorted(append_seq_rows(st, sess.id).values()))
    c.check("E reads stay in append order after the rollback", ids_of(s.list_messages(session_id=sess.id)) == [m1.id, m2.id])
finally:
    st.close()

# ---------------------------------------------------------------- F. concurrency
spec = new_store_spec("f")
st = open_storage(spec)
try:
    s = st.sessions
    sess = s.resolve_session(agent_id="main", channel="console", target="concurrent")
    peers = [open_storage(spec, peer=str(i)) for i in range(4)]
    barrier = threading.Barrier(4)

    def append_ten(index):
        barrier.wait()
        return [peers[index].sessions.append_message(session_id=sess.id, role="inbound", body=f"peer{index}-{k}") for k in range(10)]

    try:
        with patch(MESSAGE_CLOCK, return_value=TIED_AT):
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                per_peer = list(pool.map(append_ten, range(4)))
    finally:
        for p in peers:
            p.close()
    records = [m for items in per_peer for m in items]
    listed = s.list_messages(session_id=sess.id)
    seqs = sorted(append_seq_rows(st, sess.id).values())
    c.check("F 4 handles x 10 tied appends allocate append_seq 1..40 without gaps or duplicates", seqs == list(range(1, 41)), {"count": len(seqs)})
    c.check("F append-time rowids are 1..40 and increase per handle",
            sorted(m.rowid for m in records) == list(range(1, 41)) and all([m.rowid for m in it] == sorted(m.rowid for m in it) for it in per_peer))
    c.check("F list_messages equals the append-time order", ids_of(listed) == [m.id for m in sorted(records, key=lambda m: m.rowid)])
finally:
    st.close()

# ---------------------------------------------------------------- G. compaction + more tied appends
spec = new_store_spec("g")
st = open_storage(spec)
G: dict[str, object] = {}
try:
    s = st.sessions
    sess = s.resolve_session(agent_id="main", channel="console", target="compact")
    svc = SessionContextService(s, keep_recent_messages=2, archive_enabled=False)
    first6 = tied_appends(s, sess.id, [("inbound" if k % 2 == 0 else "outbound", None, None, f"t{k + 1}") for k in range(6)],
                          [ch * 32 for ch in "fedcba"])
    res1 = svc.compact_session(session_id=sess.id, force=True)
    ctx1 = s.get_session_context(session_id=sess.id)
    G["first_compaction"] = {"compacted_count": res1.compacted_count, "compacted_until_rowid": ctx1.compacted_until_rowid,
                             "anchor_body": next(m.body for m in first6 if m.id == ctx1.compacted_until_message_id),
                             "compacted_message_count": ctx1.compacted_message_count}
    c.check("G first compaction (keep 2) compacts t1..t4 and anchors at t4 (rowid 4)",
            res1.compacted_count == 4 and ctx1.compacted_until_rowid == 4 and ctx1.compacted_until_message_id == first6[3].id,
            G["first_compaction"])
    more = tied_appends(s, sess.id, [("inbound", None, None, f"t{k}") for k in (7, 8, 9)], [ch * 32 for ch in "987"])
    visible = [m.body for m in s.list_messages_after_rowid(session_id=sess.id, after_rowid=ctx1.compacted_until_rowid)]
    hist = svc.build_history(session_id=sess.id, channel="console", target="compact", recent_limit=50)
    hist_bodies = [m.body for m in hist if (m.metadata or {}).get("role") != "system"]
    anchor_now = s.message_by_id(first6[3].id) if hasattr(s, "message_by_id") else None
    G["after_tied_appends"] = {"visible_after_boundary": visible, "build_history_bodies": hist_bodies,
                               "anchor_rowid_now": getattr(anchor_now, "rowid", None)}
    c.check("G three more appends in the same tick are all visible after the boundary, in append order",
            visible == ["t5", "t6", "t7", "t8", "t9"], visible)
    c.check("G build_history shows the uncompacted tail t5..t9 and no compacted message", hist_bodies == ["t5", "t6", "t7", "t8", "t9"], hist_bodies)
    res2 = svc.compact_session(session_id=sess.id, force=True)
    ctx2 = s.get_session_context(session_id=sess.id)
    visible2 = [m.body for m in s.list_messages_after_rowid(session_id=sess.id, after_rowid=ctx2.compacted_until_rowid)]
    G["second_compaction"] = {"compacted_count": res2.compacted_count, "compacted_until_rowid": ctx2.compacted_until_rowid,
                              "compacted_message_count": ctx2.compacted_message_count, "visible": visible2}
    c.check("G second compaction compacts t5..t7, anchors at t7 (rowid 7) and leaves t8, t9",
            res2.compacted_count == 3 and ctx2.compacted_until_rowid == 7 and ctx2.compacted_until_message_id == more[0].id
            and ctx2.compacted_message_count == 7 and visible2 == ["t8", "t9"], G["second_compaction"])
finally:
    st.close()
st = open_storage(spec)
try:
    s = st.sessions
    svc = SessionContextService(s, keep_recent_messages=2, archive_enabled=False)
    ctx3 = s.get_session_context(session_id=sess.id)
    visible3 = [m.body for m in s.list_messages_after_rowid(session_id=sess.id, after_rowid=ctx3.compacted_until_rowid)]
    hist3 = [m.body for m in svc.build_history(session_id=sess.id, channel="console", target="compact", recent_limit=50)
             if (m.metadata or {}).get("role") != "system"]
    G["after_restart"] = {"visible": visible3, "build_history_bodies": hist3, "compacted_until_rowid": ctx3.compacted_until_rowid}
    c.check("G after restart the boundary and the visible tail are unchanged", visible3 == ["t8", "t9"] and hist3 == ["t8", "t9"]
            and ctx3.compacted_until_rowid == 7, G["after_restart"])
finally:
    st.close()
observed["G_compaction"] = G


# ---------------------------------------------------------------- H. transcript message order (SQLite)
def cli_export(config_path: Path, root: Path, session_id: str, fmt: str, out_file: Path) -> dict:
    exe_dir = Path(sys.executable).parent
    script = exe_dir / ("openminion.exe" if os.name == "nt" else "openminion")
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENMINION_") and k != "PYTHONPATH"}
    cmd = [str(script), "--home-root", str(root), "--data-root", str(root / ".openminion"), "--config", str(config_path),
           "--no-update-check", "export", "transcript", "--session-id", session_id, "--format", fmt, "--include-events",
           "--output", str(out_file)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env, cwd=os.getcwd(), timeout=300)
    return {"returncode": proc.returncode, "stdout": proc.stdout.strip()[-300:], "stderr": proc.stderr.strip()[-600:],
            "command": "openminion --home-root <root> --data-root <root>/.openminion --config <root>/config.json "
                       f"--no-update-check export transcript --session-id {session_id} --format {fmt} --include-events --output <file>"}


if BACKEND == "sqlite":
    H: dict[str, object] = {}
    for label, chars in (("ids_descending", "fc80"), ("ids_ascending", "08cf")):
        root = WORK / f"h-{label}"
        db = root / ".openminion" / "state.db"
        cfg = OpenMinionConfig()
        cfg.agents = {"openminion": AgentProfileConfig(name="openminion", provider="echo", default_channel="console")}
        cfg.default_agent = "openminion"
        cfg.storage.path = str(db)
        root.mkdir(parents=True, exist_ok=True)
        save_config(cfg, str(root / "config.json"))
        st = build_runtime_storage(db)
        try:
            s = st.sessions
            sess = s.resolve_session(agent_id="openminion", channel="console", target="focus", session_id="tied-session")
            tied_appends(s, sess.id, [("inbound" if k % 2 == 0 else "outbound", None, None, f"m{k}") for k in range(4)],
                         [ch * 32 for ch in chars])
            store_order = [m.body for m in s.list_messages(session_id=sess.id)]
        finally:
            st.close()
        jl = cli_export(root / "config.json", root, "tied-session", "jsonl", root / "out.jsonl")
        md = cli_export(root / "config.json", root, "tied-session", "md", root / "out.md")
        jsonl_order = [json.loads(line)["body"] for line in (root / "out.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        md_text = (root / "out.md").read_text(encoding="utf-8")
        md_order = [line for line in md_text.splitlines() if re.fullmatch(r"m\d", line)]
        H[label] = {"message_ids": [ch * 4 + "..." for ch in chars], "store_order": store_order, "jsonl_order": jsonl_order,
                    "markdown_order": md_order, "jsonl_cli": jl, "md_cli": md}
        expected = ["m0", "m1", "m2", "m3"]
        c.check(f"H export ({label}): CLI exit 0 for JSONL and Markdown", jl["returncode"] == 0 and md["returncode"] == 0,
                {"jsonl": jl["returncode"], "md": md["returncode"]})
        c.check(f"H export ({label}): store, JSONL and Markdown all list tied messages in append order",
                store_order == expected and jsonl_order == expected and md_order == expected,
                {"store": store_order, "jsonl": jsonl_order, "md": md_order})
    observed["H_transcript"] = H

# ---------------------------------------------------------------- cleanup + result
if BACKEND == "postgres" and _schemas:
    import sqlalchemy as sa

    with _pg_admin().begin() as conn:
        for schema in _schemas:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        observed["postgres_server_version"] = conn.execute(sa.text("SHOW server_version")).scalar_one()
    _pg_admin().dispose()

payload = {"banner": info, "backend": BACKEND, "summary": c.summary(), "all_pass": c.all_pass,
           "checks": c.items, "observed": observed}
write_result(OUT, payload)
print(json.dumps({"summary": c.summary(), "all_pass": c.all_pass}))
sys.exit(0 if c.all_pass else 1)
