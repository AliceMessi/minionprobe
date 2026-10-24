"""Helpers shared by the #352 probes (imported, not run directly).

- Targets: "sqlite:<path>" or "postgres:<schema>". PostgreSQL uses the server in
  OPENMINION_TEST_POSTGRES_URL, one schema per database (search_path in the URL)
  and an application_name; the URL is never printed.
- Raw access goes through the database driver (sqlite3 / SQLAlchemy), not the
  store under test, so the digests of the stored rows do not depend on it.
- The oracle is a plain event-by-event reduction over all stored events, with
  the per-event normalization copied from openminion 0.1.10's
  `modules/task/run/status.py` (`_to_run_event`, `_normalize_run_state`,
  `_extract_event_error`, `_event_matches_thread`), not imported from 0.1.11.
- The history writer uses only APIs present in both 0.1.10 and 0.1.11, so the
  same history can be written by either version.
- The scenario checks at the end are adapted from the PR's shared scenario file
  (tests/_run_status_scenarios.py at v0.1.11), rewritten without pytest and
  against the oracle above; they need 0.1.11.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from unittest.mock import patch
from urllib.parse import quote_plus

EVENT_CLOCK = "openminion.modules.storage.runtime.session_store.lifecycle.utc_now_iso"
RUN_STATES = {"queued", "running", "waiting_tool", "responding", "completed", "failed", "cancelled"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}
NUL = chr(0)
NAN, INF = float("nan"), float("inf")
# Every character `str.strip()` removes; SQL TRIM without a set removes spaces only.
PADDING = "".join(chr(code) for code in range(0x3001) if chr(code).isspace())
SPECIAL_RUN_IDS = ('q"uote', "back\\slash", "café", "per%cent", "under_score")


# ------------------------------------------------------------------ targets
def parse_target(spec: str) -> tuple[str, str]:
    kind, _, value = spec.partition(":")
    if kind not in ("sqlite", "postgres") or not value:
        raise SystemExit(f"bad target {spec!r}")
    return kind, value


def pg_base_url() -> str:
    url = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "").strip()
    if not url:
        raise SystemExit("OPENMINION_TEST_POSTGRES_URL is not set")
    return url


def pg_schema_url(schema: str, app: str | None = None) -> str:
    base = pg_base_url()
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}options={quote_plus(f'-csearch_path={schema}')}"
    return f"{url}&application_name={app or schema}"


def pg_create_schema(schema: str) -> None:
    import sqlalchemy as sa

    engine = sa.create_engine(pg_base_url())
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    finally:
        engine.dispose()


def pg_drop_schema(schema: str) -> None:
    import sqlalchemy as sa

    engine = sa.create_engine(pg_base_url())
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        engine.dispose()


_ENGINES: dict[str, Any] = {}


def raw_query(target: str, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Run SQL with `?` placeholders through the driver; returns dict rows."""
    kind, value = parse_target(target)
    if kind == "sqlite":
        with closing(sqlite3.connect(value)) as conn:
            cur = conn.execute(sql, tuple(params))
            cols = [c[0] for c in cur.description] if cur.description else []
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    import sqlalchemy as sa

    engine = _ENGINES.get(value)
    if engine is None:
        engine = _ENGINES[value] = sa.create_engine(pg_schema_url(value, f"{value}_raw"))
    parts = sql.split("?")
    named = parts[0] + "".join(f":p{i}{part}" for i, part in enumerate(parts[1:]))
    with engine.connect() as conn:
        result = conn.execute(sa.text(named), {f"p{i}": v for i, v in enumerate(params)})
        return [dict(row) for row in result.mappings().all()]


def raw_execute(target: str, sql: str, params: Sequence[Any] = ()) -> None:
    kind, value = parse_target(target)
    if kind == "sqlite":
        with closing(sqlite3.connect(value)) as conn, conn:
            conn.execute(sql, tuple(params))
        return
    import sqlalchemy as sa

    engine = _ENGINES.get(value)
    if engine is None:
        engine = _ENGINES[value] = sa.create_engine(pg_schema_url(value, f"{value}_raw"))
    parts = sql.split("?")
    named = parts[0] + "".join(f":p{i}{part}" for i, part in enumerate(parts[1:]))
    with engine.begin() as conn:
        conn.execute(sa.text(named), {f"p{i}": v for i, v in enumerate(params)})


def dispose_engines() -> None:
    for engine in _ENGINES.values():
        engine.dispose()
    _ENGINES.clear()


def ledger(target: str) -> list[int]:
    try:
        return [int(r["version"]) for r in raw_query(target, "SELECT version FROM migrations ORDER BY version")]
    except Exception:  # noqa: BLE001 - no ledger yet
        return []


def has_table(target: str, name: str) -> bool:
    kind, _ = parse_target(target)
    if kind == "sqlite":
        rows = raw_query(target, "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return bool(rows)
    rows = raw_query(target, f"SELECT to_regclass('{name}') IS NOT NULL AS present")
    return bool(rows[0]["present"])


def stored_event_rows(target: str) -> list[dict[str, Any]]:
    rows = raw_query(target, "SELECT * FROM events ORDER BY id")
    return [{k: (v if isinstance(v, (int, str)) or v is None else str(v)) for k, v in r.items()} for r in rows]


def digest(rows: list[dict[str, Any]]) -> str:
    canon = json.dumps([sorted(r.items()) for r in rows], ensure_ascii=True, default=str)
    return hashlib.sha256(canon.encode("ascii")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def open_storage(target: str, work: Path):
    """The normal runtime open: build_runtime_storage (runs migrations to head)."""
    from openminion.modules.storage.runtime.context import build_runtime_storage

    kind, value = parse_target(target)
    if kind == "sqlite":
        return build_runtime_storage(Path(value), env={})
    work.mkdir(parents=True, exist_ok=True)
    return build_runtime_storage(
        work / "unused-sqlite-path.db",
        env={},
        record_backend="record.postgres",
        record_backend_options={"url": pg_schema_url(value)},
    )


# ------------------------------------------------------------------ clock
class OrdinalClock:
    """created_at as a function of the event's ordinal only (not of how many
    times the store reads the clock), so 0.1.10 and 0.1.11 writers produce the
    same stamps. mode "forward": +1 s per event; "stepping": +1 s per event,
    7 s back every 5th (non-monotonic)."""

    BASE = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)

    def __init__(self) -> None:
        self.n = 0
        self.mode = "forward"

    def __call__(self) -> str:
        offset = self.n if self.mode == "forward" else self.n - 8 * (self.n // 5)
        return (self.BASE + timedelta(seconds=offset, microseconds=1)).isoformat()

    @contextmanager
    def patched(self) -> Iterator["OrdinalClock"]:
        with patch(EVENT_CLOCK, new=self):
            yield self


# ------------------------------------------------------------------ history
def write_history(sessions, append_run_state_event) -> dict[str, Any]:
    """Two sessions, written with the APIs both versions have.

    - "window": the maintainer's probe from the issue, unchanged: 2,000 events of
      run "old", then queued/failed of run "new" (events 1..2002, forward clock).
    - "mixed": a long run "long" that starts at the first event and is still
      running at the end, a long run "long2" that starts early and completes at
      the end, interleaved short runs on thread-b and runs on conv2/thread-c,
      tool/response/attach events, and every 50 turns padded run ids and states,
      JSON-token text, and NaN/Infinity/NUL in run fields and unrelated fields
      (stepping, non-monotonic clock).
    """
    clock = OrdinalClock()
    counts: dict[str, int] = {}
    with clock.patched():
        window = sessions.resolve_session(agent_id="main", channel="console", target="window")
        for i in range(2002):
            append_run_state_event(
                sessions,
                session_id=window.id,
                run_id="old" if i < 2000 else "new",
                state=("queued" if i % 2 == 0 else ("completed" if i < 2000 else "failed")),
                current_step="step",
                conversation_id="conv",
                thread_id="thread",
                payload={"invocation_id": "old-inv" if i < 2000 else "new-inv"},
            )
            clock.n += 1
        counts["window"] = 2002

        clock.mode = "stepping"
        mixed = sessions.resolve_session(agent_id="main", channel="console", target="mixed")
        start = clock.n

        def ev(event_type: str, **payload: Any) -> None:
            sessions.append_event(session_id=mixed.id, event_type=event_type, payload=payload)
            clock.n += 1

        A = {"conversation_id": "conv", "thread_id": "thread-a"}
        Bt = {"conversation_id": "conv", "thread_id": "thread-b"}
        C = {"conversation_id": "conv2", "thread_id": "thread-c"}
        ev("run.queued", run_id="long", state="queued", step="accepted", invocation_id="inv-long", **A)
        ev("client.attach", attach_id="writer-a", attach_role="writer", **A)
        ev("run.queued", run_id="long2", state="queued", invocation_id="inv-long2", **C)
        for turn in range(720):
            ev("run.running", run_id="long", state="running", step=f"step-{turn}" if turn % 9 else "", **A)
            s = f"s-{turn}"
            ev("run.queued", run_id=s, state="queued", step="accepted", invocation_id=f"inv-{s}", **Bt)
            if turn % 4 == 0:
                ev("tool.call", run_id=s, tool="lookup", **Bt)
            if turn % 7 == 3:
                ev("run.failed", run_id=s, state="failed", step="turn.failed", error=f"boom-{turn}", **Bt)
            else:
                ev("run.completed", run_id=s, state="completed", step="", **Bt)
            if turn % 5 == 0:
                ev("response.persisted", run_id=s, **Bt)
            if turn % 10 == 0:
                ev("response.delivered", run_id=s, **Bt)
            if turn % 3 == 0:
                c = f"c-{turn}"
                ev("run.queued", run_id=c, state="queued", invocation_id=f"inv-{c}", **C)
                ev("run.running", run_id="long2", state="running", step=f"c-step-{turn}", **C)
            if turn % 50 == 25:
                ev("run.running", run_id=f"{PADDING}{s}{PADDING}", state=" RUNNING\t", step=PADDING, error=PADDING, **Bt)
                ev("run.queued", run_id="trace: NaN", state="queued", step="[Infinity]",
                   error="loss: NaN, retry", conversation_id="trace: 1e999", thread_id="[NaN]")
                ev("run.queued", run_id="trace: 1e999", state="queued", conversation_id="trace: 1e999", thread_id="[NaN]")
                ev("tool.result", run_id=s, output=f"bin{NUL}ary", metrics=[NAN, INF, -INF], **Bt)
                ev("run.running", run_id="long", error=NAN, latency=-INF, **A)
                ev("run.running", run_id="long", step=INF, **A)
                ev("run.waiting_tool", run_id=f"nul{NUL}{turn % 3}", error=f"bad{NUL}byte", **C)
                ev("client.attach", attach_id=f"o{NUL}{turn}", attach_role="observer", **A)
                ev("RUN.failed", run_id=s, state="bogus", **Bt)
                ev("run.running", run_id=SPECIAL_RUN_IDS[(turn // 50) % len(SPECIAL_RUN_IDS)], step="special", **C)
            if turn % 97 == 0:
                ev("client.attach", attach_id=f"writer-b{turn}", attach_role=" Writer ", **Bt)
        ev("run.completed", run_id="long2", state="completed", error="", **C)
        ev("run.cancel_requested", run_id="long", **A)
        ev("run.running", run_id="long", state="running", step="final-step", **A)
        counts["mixed"] = clock.n - start
    return {"sessions": {"window": window.id, "mixed": mixed.id}, "events_written": counts}


# ------------------------------------------------------------------ oracle (0.1.10 copy)
def o_state(state: Any, event_type: str) -> str:
    candidate = str(state or "").strip().lower()
    if candidate in RUN_STATES:
        return candidate
    if event_type.startswith("run."):
        suffix = event_type.split(".", 1)[1].strip().lower()
        if suffix in RUN_STATES:
            return suffix
    return "running"


def o_error(payload: dict[str, Any]) -> str:
    raw = payload.get("error")
    return "" if raw is None else str(raw).strip()


def o_run_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    p = ev["payload"]
    run_id = str(p.get("run_id", "")).strip()
    if not run_id:
        return None
    return {
        "id": ev["id"], "run_id": run_id, "state": o_state(p.get("state"), ev["event_type"]),
        "step": str(p.get("step", "")).strip(), "payload": p, "created_at": ev["created_at"],
    }


def o_matches_thread(ev: dict[str, Any], conversation_id: str, thread_id: str, session_id: str) -> bool:
    p = ev["payload"] or {}
    ec = str(p.get("conversation_id", "") or "").strip()
    et = str(p.get("thread_id", "") or "").strip()
    if conversation_id:
        if ec:
            if ec != conversation_id:
                return False
        elif conversation_id != session_id:
            return False
    if thread_id and et and et != thread_id:
        return False
    return True


def _parse(row: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(str(row["payload_json"]))
    return {"id": int(row["id"]), "event_type": str(row["event_type"]),
            "payload": payload if isinstance(payload, dict) else {}, "created_at": str(row["created_at"])}


class Oracle:
    """Reads a session's stored events once (driver SQL) and reduces them."""

    def __init__(self, target: str, session_id: str) -> None:
        self.session_id = session_id
        cols = "id, event_type, payload_json, created_at"
        self.all = [_parse(r) for r in raw_query(
            target, f"SELECT {cols} FROM events WHERE session_id = ? ORDER BY id", (session_id,))]
        # The readers select run events with `event_type LIKE 'run.%'` (as 0.1.10 did).
        self.run_rows = [_parse(r) for r in raw_query(
            target, f"SELECT {cols} FROM events WHERE session_id = ? AND event_type LIKE ? ORDER BY id",
            (session_id, "run.%"))]
        self.items = [i for i in map(o_run_event, self.run_rows) if i is not None]

    def runs(self, *, limit: int, scan_limit: int) -> list[dict[str, Any]]:
        safe = max(1, min(int(limit), 500))
        window = self.run_rows[-max(1, safe, int(scan_limit)):]
        listed = {i["run_id"] for i in map(o_run_event, window) if i is not None}
        runs: dict[str, dict[str, Any]] = {}
        for item in self.items:
            if item["run_id"] not in listed:
                continue
            run = runs.setdefault(item["run_id"], {
                "run_id": item["run_id"], "started_at": item["created_at"], "ended_at": "",
                "current_step": "", "error": "", "event_count": 0})
            run["event_count"] += 1
            run["state"] = item["state"]
            if item["step"]:
                run["current_step"] = item["step"]
            err = o_error(item["payload"])
            if err:
                run["error"] = err
            if item["state"] in TERMINAL_STATES:
                run["ended_at"] = item["created_at"]
        ordered = sorted(runs.values(), key=lambda r: (r["started_at"], r["run_id"]), reverse=True)
        return ordered[:safe]

    def run_ids(self) -> list[str]:
        return sorted({i["run_id"] for i in self.items})

    def run_event_ids(self, run_id: str, limit: int) -> list[int]:
        rid = str(run_id).strip()
        own = [i["id"] for i in self.items if i["run_id"] == rid]
        return own[-max(1, min(int(limit), 1000)):]

    def thread(self, conversation_id: str, thread_id: str) -> dict[str, Any]:
        conv = str(conversation_id or "").strip()
        thread = str(thread_id or "").strip() or conv or self.session_id  # no messages stored
        events = [e for e in self.all if o_matches_thread(e, conv, thread, self.session_id)]
        runs = [i for i in (o_run_event(e) for e in events if e["event_type"].startswith("run.")) if i]

        def text(e: dict[str, Any], key: str) -> str:
            return str(e["payload"].get(key, "")).strip()

        starts = [e for e in events if e["event_type"] == "run.queued" and text(e, "invocation_id")]
        writers = [text(e, "attach_id") for e in events if e["event_type"] == "client.attach"
                   and text(e, "attach_id") and text(e, "attach_role").lower() in {"", "writer"}]
        types = {e["event_type"] for e in events}
        delivery = "none"
        for event_type, state in (("response.persisted", "persisted"), ("response.delivered", "delivered"),
                                  ("response.acked", "acked")):
            if event_type in types:
                delivery = state
        return {
            "latest_run_id": runs[-1]["run_id"] if runs else "",
            "latest_run_state": runs[-1]["state"] if runs else "",
            "invocation_id": text(starts[-1], "invocation_id") if starts else "",
            "invocation_source_event_id": starts[-1]["id"] if starts else 0,
            "latest_event_id": events[-1]["id"] if events else 0,
            "updated_at": events[-1]["created_at"] if events else "",
            "delivery_state": delivery,
            "writer_attach_id": writers[-1] if writers else "",
        }


def expected_facts(payload: dict[str, Any], event_type: str) -> dict[str, Any]:
    """The facts as the 0.1.10 readers' expressions read the payload (decoded)."""
    run_id = str(payload.get("run_id", "")).strip()
    error = payload.get("error")
    return {
        "run_id": run_id,
        "run_state": o_state(payload.get("state"), event_type) if run_id else "",
        "has_step": int(bool(run_id and str(payload.get("step", "")).strip())),
        "has_error": int(bool(run_id and error is not None and str(error).strip())),
        "conversation_id": str(payload.get("conversation_id", "") or "").strip(),
        "thread_id": str(payload.get("thread_id", "") or "").strip(),
        "has_invocation": int(bool(str(payload.get("invocation_id", "")).strip())),
        "has_attach": int(bool(str(payload.get("attach_id", "")).strip())),
        "attach_role": str(payload.get("attach_role", "")).strip().lower(),
    }


def decoded_fact_row(row: dict[str, Any]) -> dict[str, Any]:
    def dec(col: str) -> str:
        return json.loads(row[col]) if row[col] else ""

    return {
        "run_id": dec("run_key"), "run_state": row["run_state"], "has_step": int(row["has_step"]),
        "has_error": int(row["has_error"]), "conversation_id": dec("conversation_key"),
        "thread_id": dec("thread_key"), "has_invocation": int(row["has_invocation"]),
        "has_attach": int(row["has_attach"]), "attach_role": dec("attach_role"),
    }


def check_facts_table(target: str) -> dict[str, Any]:
    """Every stored event has exactly one facts row, equal both to 0.1.11's
    event_run_facts() of the stored row and to the 0.1.10-expression oracle;
    stored keys are ASCII without NUL; the digest column matches."""
    from openminion.modules.storage.runtime.session_store.rows import row_to_event
    from openminion.modules.storage.runtime.session_store.run_facts import event_run_facts, key_digest

    events = raw_query(target, "SELECT id, session_id, event_type, payload_json, created_at, canonical_event_id FROM events ORDER BY id")
    facts = raw_query(target, "SELECT * FROM event_run_facts ORDER BY event_id")
    mismatch_pkg, mismatch_oracle, bad_keys = [], [], []
    by_id = {int(f["event_id"]): f for f in facts}
    for row in events:
        f = by_id.get(int(row["id"]))
        if f is None:
            mismatch_pkg.append(int(row["id"]))
            continue
        norm = {k: (int(v) if k in ("event_id", "has_step", "has_error", "has_invocation", "has_attach") else v) for k, v in f.items()}
        if norm != event_run_facts(row_to_event(row)):
            mismatch_pkg.append(int(row["id"]))
        payload = json.loads(row["payload_json"])
        payload = payload if isinstance(payload, dict) else {}
        if decoded_fact_row(f) != expected_facts(payload, row["event_type"]) or f["session_id"] != row["session_id"]:
            mismatch_oracle.append(int(row["id"]))
        if f["run_key_digest"] != key_digest(f["run_key"]):
            bad_keys.append(int(row["id"]))
        for col in ("run_key", "conversation_key", "thread_key", "attach_role"):
            if not (f[col].isascii() and NUL not in f[col]):
                bad_keys.append(int(row["id"]))
    return {
        "events": len(events), "facts": len(facts),
        "facts_without_event": sorted(set(by_id) - {int(r["id"]) for r in events})[:10],
        "mismatch_vs_event_run_facts": mismatch_pkg[:10], "mismatch_count_vs_event_run_facts": len(mismatch_pkg),
        "mismatch_vs_0110_expressions": mismatch_oracle[:10], "mismatch_count_vs_0110_expressions": len(mismatch_oracle),
        "bad_keys": bad_keys[:10],
        "ok": len(events) == len(facts) and not mismatch_pkg and not mismatch_oracle and not bad_keys,
    }


# ------------------------------------------------------------------ reader comparison
def compare_readers(sessions, target: str, session_id: str, name: str, *, thread_requests,
                    record: Callable[..., bool], scan_limits=None, limits=(1, 3, 20, 500),
                    event_limits=(1, 2, 200, 1000), padded=True) -> dict[str, Any]:
    """Readers vs oracle for one session; returns a session-id-free dump."""
    from openminion.modules.task.run import list_session_run_events, list_session_runs, resolve_thread_lifecycle

    oracle = Oracle(target, session_id)
    n_run = len(oracle.run_rows)
    dump: dict[str, Any] = {"runs": {}, "run_events": {}, "threads": {}}
    scans = scan_limits or sorted({1, 2, 5, 13, 40, 1999, 2000, 2001, max(1, n_run - 1), n_run, n_run + 5})
    bad = []
    for scan in scans:
        for limit in limits:
            exp = oracle.runs(limit=limit, scan_limit=scan)
            got = [{k: getattr(r, k) for k in ("run_id", "started_at", "ended_at", "current_step", "error", "event_count", "state")}
                   for r in list_session_runs(sessions, session_id=session_id, limit=limit, scan_limit=scan)]
            if got != exp:
                bad.append({"scan_limit": scan, "limit": limit, "got": got[:3], "expected": exp[:3]})
            dump["runs"][f"{scan}/{limit}"] = got
    default_runs = [{k: getattr(r, k) for k in ("run_id", "started_at", "ended_at", "current_step", "error", "event_count", "state")}
                    for r in list_session_runs(sessions, session_id=session_id)]
    default_ok = default_runs == oracle.runs(limit=20, scan_limit=2000)
    dump["runs"]["default"] = default_runs
    record(f"{name}: list_session_runs equals the reduction for {len(scans)} scan limits x {len(limits)} limits, and with defaults",
           not bad and default_ok,
           {"run_events_in_session": n_run, "events_in_session": len(oracle.all), "combinations": len(scans) * len(limits) + 1,
            "mismatches": bad[:3], "default_listing_first": default_runs[:2]})

    run_ids = oracle.run_ids()
    ev_bad = []
    calls = 0
    for rid in run_ids:
        for limit in event_limits:
            requests = (rid, f" {rid}\t") if padded else (rid,)
            for req in requests:
                got = [e.id for e in list_session_run_events(sessions, session_id=session_id, run_id=req, limit=limit)]
                calls += 1
                if got != oracle.run_event_ids(rid, limit):
                    ev_bad.append({"run_id": rid, "limit": limit, "request_padded": req != rid, "got_tail": got[-3:],
                                   "expected_tail": oracle.run_event_ids(rid, limit)[-3:]})
                if limit == 200 and req == rid:
                    dump["run_events"][rid] = got
    record(f"{name}: list_session_run_events returns each run's newest events (all {len(run_ids)} runs, limits {list(event_limits)}, exact and padded requests)",
           not ev_bad, {"runs": len(run_ids), "calls": calls, "mismatches": ev_bad[:3]})

    th_bad = []
    for conv, thread in thread_requests:
        conv_v = session_id if conv == "<session>" else conv
        p = resolve_thread_lifecycle(sessions, session_id=session_id, conversation_id=conv_v, thread_id=thread)
        exp = oracle.thread(conv_v, thread)
        got = {k: getattr(p, k) for k in exp}
        if got != exp:
            th_bad.append({"request": [conv, thread], "got": got, "expected": exp})
        full = p.to_dict()
        full["conversation_id"] = "<session>" if full["conversation_id"] == session_id else full["conversation_id"]
        full["thread_id"] = "<session>" if full["thread_id"] == session_id else full["thread_id"]
        dump["threads"][f"{conv}|{thread}"] = full
    record(f"{name}: resolve_thread_lifecycle equals the reduction for {len(thread_requests)} conversation/thread requests (latest run/state, invocation, latest event id = thread high water, updated_at, delivery, writer attach)",
           not th_bad, {"requests": [list(r) for r in thread_requests], "mismatches": th_bad[:3],
                        "projections": {k: {kk: v[kk] for kk in ("latest_run_id", "latest_run_state", "invocation_id", "latest_event_id", "thread_state")}
                                        for k, v in list(dump["threads"].items())[:6]}})
    return dump


def readers_digest(dump: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dump, sort_keys=True, ensure_ascii=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------ scenario checks (0.1.11)
# Adapted from tests/_run_status_scenarios.py at v0.1.11 (PR #372), without pytest,
# with the oracle above instead of the package's own reader helpers.
def expect_raises(exc_types, fn: Callable[[], Any], match: str | None = None) -> str:
    import re

    try:
        fn()
    except exc_types as exc:  # type: ignore[misc]
        text = f"{type(exc).__name__}: {exc}"
        if match is not None and not re.search(match, str(exc), re.S):
            raise AssertionError(f"raised but did not match {match!r}: {text}") from exc
        return text
    raise AssertionError("did not raise")


@contextmanager
def stepping_clock() -> Iterator[list[str]]:
    """Patch the event clock: 1 s forward per append, 7 s back every 5th."""
    base = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
    stamps: list[str] = []

    def tick(counter=itertools.count()) -> str:
        index = next(counter)
        offset = index - 8 * (index // 5)
        stamps.append((base + timedelta(seconds=offset, microseconds=1)).isoformat())
        return stamps[-1]

    with patch(EVENT_CLOCK, side_effect=tick):
        yield stamps


def append_runs(sessions, session_id: str) -> None:
    def run(event_type: str, run_id: str, **payload: Any) -> None:
        sessions.append_event(session_id=session_id, event_type=event_type, payload={"run_id": run_id, **payload})

    specials = itertools.cycle(SPECIAL_RUN_IDS)
    for index in range(6):
        long_run = "long"
        short = f"short-{index}"
        special = next(specials)
        run("run.queued", short, state="queued", step="turn.accepted")
        run("run.running", long_run, state="running", step=f"step-{index}")
        run("run.queued", special, state="queued", step="turn.accepted")
        run("run.waiting_tool", short, state="waiting_tool", step="", error=None)
        run("run.running", f"nested-{index}", parent={"run_id": short})
        run("run.running", short.upper(), state="running")
        run("run.failed", special, state="failed", step="turn.failed", error=503)
        run("run.queued", short, state="completed", step="  ", error="  ")
        run("run.completed", f"suffix-{index}", state="bogus")
        run("run.failed", f"padded-{index}", state=" RUNNING ")
        run("run.cancel_requested", long_run)
        if index % 2:
            run("run.completed", long_run, state="completed", error="boom")
        sessions.append_event(session_id=session_id, event_type="tool.call", payload={"run_id": short})


def append_payload_variants(sessions, session_id: str, append_run_state_event) -> None:
    def run(event_state: str, run_id: Any, /, **payload: Any) -> None:
        append_run_state_event(sessions, session_id=session_id, run_id="replaced", state=event_state,
                               current_step="", payload={"run_id": run_id, **payload})

    run("queued", "r", step="first", error="earlier error")
    run("running", f"{PADDING}r{PADDING}", step=PADDING, error=PADDING)
    run("failed", "r", state=f"{PADDING}RUNNING{PADDING}")
    run("queued", "​r")
    run("queued", 7, step=0, error=0)
    run("running", "7", step=False, error=False)
    run("completed", f"{PADDING}7", step=None, error=None)
    run("queued", None)
    run("queued", True, state=None)
    run("failed", {}, state=5)
    run("queued", [], error={})
    run("queued", "", error="no run")
    run("queued", PADDING, error="no run")
    run("queued", "trace: NaN", step="[Infinity]", error="trace, -Infinity")
    run("running", "trace: 1e999", error="loss: NaN, retry")
    run("queued", "[NaN]")
    sessions.append_event(session_id=session_id, event_type="RUN.failed", payload={"run_id": "r", "state": "bogus"})


def append_threads(sessions, session_id: str) -> None:
    def event(event_type: str, thread_id: str | None, **payload: Any) -> None:
        if thread_id is not None:
            payload = {"conversation_id": "conv", "thread_id": thread_id, **payload}
        sessions.append_event(session_id=session_id, event_type=event_type, payload=payload)

    event("run.queued", "quiet", run_id="q1", state="queued", invocation_id="inv-q1")
    event("client.attach", "quiet", attach_id="writer-q", attach_role="writer")
    event("client.attach", "quiet", attach_id="observer-q", attach_role="observer")
    event("run.completed", "quiet", run_id="q1", state="completed")
    event("response.persisted", "quiet", run_id="q1")
    for index in range(12):
        run_id = f"b{index}"
        event("run.queued", "busy", run_id=run_id, invocation_id=f"inv-b{index}")
        event("run.queued", "busy", run_id=f"{run_id}-retry")
        event("client.attach", "busy", attach_id=f"attach-{index}", attach_role="")
        event("run.running", "busy", run_id=run_id, state="running")
        if index % 4 == 0:
            event("session.note", None, note=index)
        event("run.completed", "busy", run_id=run_id, state="completed")
    event("run.cancel_requested", "busy", run_id="b11")
    event("client.detached", "busy", attach_id="attach-11")
    event("response.delivered", "quiet", run_id="q1")


def append_thread_payload_variants(sessions, session_id: str) -> None:
    def event(event_type: str, **payload: Any) -> None:
        sessions.append_event(session_id=session_id, event_type=event_type, payload=payload)

    padded = {"conversation_id": f"{PADDING}conv{PADDING}", "thread_id": "\nt\t"}
    event("run.queued", **padded, run_id="t1", invocation_id=f"{PADDING}inv-1")
    event("client.attach", **padded, attach_id=f"{PADDING}w1", attach_role=" WRITER\t")
    event("client.attach", conversation_id="conv", thread_id="t", attach_id=None)
    event("client.attach", conversation_id="conv", thread_id="t", attach_id="o1", attach_role=None)
    event("run.queued", conversation_id="conv", thread_id="t", run_id="t2", invocation_id=None)
    event("RUN.running", conversation_id="conv", thread_id="t", run_id="t3")
    event("run.running", conversation_id=False, thread_id=0, run_id="t4", state="running")
    event("run.waiting_tool", conversation_id=None, thread_id=None, run_id="t5")
    event("run.queued", conversation_id="trace: NaN", thread_id="[NaN]", run_id="trace, Infinity", invocation_id="trace: 1e999")
    event("run.running", conversation_id="trace: 1e999", thread_id="[1e999]", run_id="x")
    event("run.queued", conversation_id="conv", thread_id="​t", run_id="zw")
    event("run.failed", **padded, run_id=f"{PADDING}t1{PADDING}", state="\tfailed\n")


def append_nonstandard_values(sessions, session_id: str) -> None:
    def event(event_type: str, **payload: Any) -> None:
        sessions.append_event(session_id=session_id, event_type=event_type,
                              payload={"conversation_id": "conv", "thread_id": "t", **payload})

    event("run.queued", run_id="r", error="earlier", step="s", invocation_id="inv")
    event("run.running", run_id="r", error=NAN)
    event("run.running", run_id="r", step=INF, latency=-INF)
    event("tool.result", run_id="r", output=f"bin{NUL}ary", metrics=[NAN, INF])
    event("client.attach", attach_id=f"w{NUL}1", attach_role="writer")
    event("client.attach", attach_id="o1", attach_role=NAN)
    event("run.failed", run_id="r", error=f"bad{NUL}byte", state=NAN)
    event("run.queued", run_id=f"nul{NUL}id", invocation_id=INF)
    event("run.queued", run_id=NAN, state="queued")
    event("run.completed", run_id=-INF)
    nul_thread = {"conversation_id": f"c{NUL}", "thread_id": f"t{NUL}"}
    event("response.persisted", **nul_thread, run_id="x", note=NAN)
    event("run.queued", **nul_thread, run_id="x", error=-INF)


FACT_KEYS = ("run_id", "state", "step", "error", "conversation_id", "thread_id", "invocation_id", "attach_id", "attach_role")
FACT_VALUES: tuple[Any, ...] = (
    f"{PADDING}x{PADDING}", chr(0x200B) + "x", "trace: NaN", "a" + chr(92) + "u0000b", f"nul{NUL}id", NUL,
    PADDING, "", None, True, False, 0, 7, 1e16, -0.0, "7", " RUNNING ", "Cancelled", " WRITER ", "observer",
    {}, [], {"run_id": "r"}, ["r", 1], NAN, INF, -INF,
)


def legacy_event_rows(session_ids: Sequence[str], count: int) -> list[dict[str, Any]]:
    """Events as the store wrote them before migration 13 (payload_json = metadata_json)."""
    from openminion.modules.storage.runtime.session_store.rows import metadata_json

    base = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)
    templates: tuple[tuple[str, dict[str, Any]], ...] = (
        ("run.queued", {"state": "queued", "step": "accepted", "invocation_id": "i"}),
        ("run.running", {"step": PADDING, "error": NAN}),
        ("tool.result", {"output": f"bin{NUL}ary", "metrics": [NAN, -INF]}),
        ("client.attach", {"attach_id": "w", "attach_role": " Writer "}),
        ("run.waiting_tool", {"step": INF, "error": f"bad{NUL}byte"}),
        ("RUN.failed", {"state": "bogus"}),
        ("run.completed", {"state": f"{PADDING}completed", "error": "trace: NaN"}),
        ("response.delivered", {}),
    )
    rows = []
    for index in range(count):
        event_type, extra = templates[index % len(templates)]
        turn = index // len(templates)
        payload = {"run_id": f"run-{turn % 7}" if index % 11 else f"nul{NUL}{turn % 3}",
                   "conversation_id": "conv", "thread_id": f"t{turn % 3}", **extra}
        stamp = base + timedelta(seconds=index - 8 * (index // 5))
        rows.append({"session_id": session_ids[index % len(session_ids)], "event_type": event_type,
                     "payload_json": metadata_json(payload), "created_at": stamp.isoformat()})
    return rows


def recorded_backfill_migrations(batches: list[int]):
    from openminion.modules.storage.runtime.migrations import DEFAULT_MIGRATIONS
    from openminion.modules.storage.runtime.session_store.run_facts import backfill_event_run_facts

    class Recorder:
        def __init__(self, rows: Any) -> None:
            self._rows = rows

        def query_dicts(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
            found = self._rows.query_dicts(sql, params)
            batches.append(len(found))
            return found

        def insert_many(self, table: str, rows: list[dict[str, Any]]) -> int:
            return self._rows.insert_many(table, rows)

    def backfill(rows: Any) -> None:
        backfill_event_run_facts(Recorder(rows))

    return tuple(dataclasses.replace(m, backfill=backfill) if m.version == 13 else m for m in DEFAULT_MIGRATIONS)


def migrations_upto(version: int):
    from openminion.modules.storage.runtime.migrations import DEFAULT_MIGRATIONS

    return tuple(m for m in DEFAULT_MIGRATIONS if m.version <= version)


def migrate_target(target: str, migrations) -> Any:
    """Apply the given runtime migrations with the package's own runners."""
    from openminion.modules.storage.runtime import migrations as mig

    kind, value = parse_target(target)
    if kind == "sqlite":
        return mig.migrate_database(value, migrations=migrations)
    from openminion.modules.storage.backends.postgres import RecordStorePostgres

    store = RecordStorePostgres(pg_schema_url(value, f"{value}_mig"))
    try:
        return mig.migrate_record_store(store, backend_type="postgres", migrations=migrations)
    finally:
        store.close()


def insert_legacy_history(target: str, count: int) -> list[str]:
    """Two sessions and `count` legacy events inserted through the driver."""
    session_ids = ["legacy-a", "legacy-b"]
    for sid in session_ids:
        raw_execute(target, "INSERT INTO sessions(id, session_key, channel, target) VALUES (?, ?, 'console', ?)",
                    (sid, f"key-{sid}", sid))
    rows = legacy_event_rows(session_ids, count)
    kind, value = parse_target(target)
    if kind == "sqlite":
        with closing(sqlite3.connect(value)) as conn, conn:
            conn.executemany("INSERT INTO events(session_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                             [(r["session_id"], r["event_type"], r["payload_json"], r["created_at"]) for r in rows])
    else:
        import sqlalchemy as sa

        engine = _ENGINES.get(value) or sa.create_engine(pg_schema_url(value, f"{value}_raw"))
        _ENGINES[value] = engine
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO events(session_id, event_type, payload_json, created_at) "
                                 "VALUES (:session_id, :event_type, :payload_json, :created_at)"), rows)
    return session_ids
