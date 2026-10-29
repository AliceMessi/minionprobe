"""#364 on the installed package: the real room CLI on PostgreSQL runtime storage
(participant join order, migration 14 `join_seq`), now that APIRuntime starts there (#437).

    python -I -X utf8 probe_364.py <work_dir> <out.json> [--backend sqlite|postgres]

Every room command is a real subprocess; nothing replaces `_open_room_runtime`,
which starts APIRuntime.from_config_path with `storage.backend: postgres`.
  A  the `openminion` console script, real clock and ids: `room create` (three
     agents) and `room invite` (a fourth); rows read back independently.
  B  the issue's tied-clock technique in a subprocess (probe_364_cli.py: only the
     participant clock and row-id generator are fixed, then the installed
     openminion.cli.main.main runs): three agents join on one tick with row ids
     that sort in reverse join order, in broadcast, sequential and addressed
     rooms; `room invite --agent fourth` on the same tick. Read back through a
     new APIRuntime on the same database and independently.
  C  the routing consumer through the real CLI: `openminion run --agent <a
     participant> --session <room>` (single-process, in-process runtime) on each
     room; the active first agent then leaves (runtime.sessions.remove_participant
     on a new APIRuntime, same tick; the room CLI has no leave command), and
     `_routed_agents` is read with that runtime; `run` again, including an
     addressed `@third` turn. The replies are the outbound rows each run wrote to
     the messages table (their `agent` metadata). The B rooms are created with
     `--channel console --target cli-user`, the channel/target `openminion run`
     resolves sessions with.
PostgreSQL: a new database whose URL names its own application_name. Each CLI
process's server connections are counted while it runs and after it exits; no
runtime SQLite database may appear at storage.path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, quiet, write_result  # noqa: E402
from probe_437_lib import (  # noqa: E402
    PgDatabase,
    Reader,
    brief,
    clean_env,
    cli_argv,
    describe_exc,
    install_masking,
    kv,
    mask,
    pg_base,
    pg_versions,
    run_cli,
    run_process,
    sqlite_files,
    storage_path,
    strip_env,
    write_config,
)

PARTICIPANTS = "openminion.modules.storage.runtime.session_store.participants"
TIED_AT = "2026-10-29T01:00:00+00:00"
AGENTS = ("first", "second", "third", "fourth")
CASES: dict[str, Any] = {}


def rid(scope: str, char: str) -> str:
    """A row id starting with `char`; the rest is fixed per room scope."""
    return char + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:31]


def scenario(checks: Checks, name: str):
    def wrap(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            checks.check(f"{name}: completes without an exception", False, observed=mask(describe_exc(exc))[:500])
            traceback.print_exc()
        return fn

    return wrap


class Env:
    def __init__(self, work: Path, backend: str) -> None:
        self.backend = backend
        self.db = PgDatabase("om364") if backend == "postgres" else None
        self.root = work / f"rooms-{backend}"
        storage = {"backend": "postgres", "postgres_url": self.db.url(scheme="postgresql+psycopg")} if self.db else {}
        self.cfg = write_config(self.root, agents=AGENTS, storage=storage)
        self.reader = Reader(db=self.db) if self.db else Reader(sqlite_path=storage_path(self.root))
        self.processes: list[dict[str, Any]] = []

    def cli(self, argv: list[str]) -> dict[str, Any]:
        result = run_cli(self.root, self.cfg, argv, watch=self.db)
        self.processes.append({"argv": argv, "exit_code": result["exit_code"],
                               "connections_max_while_running": result.get("connections_max_while_running"),
                               "connections_after_exit": result.get("connections_after_exit")})
        return result

    def tied_cli(self, argv: list[str], ids: list[str]) -> dict[str, Any]:
        launcher = str(Path(__file__).resolve().parent / "probe_364_cli.py")
        cmd = [sys.executable, "-I", "-X", "utf8", launcher, TIED_AT, ",".join(ids), "--",
               *cli_argv(self.root, self.cfg, argv)]
        result = run_process(cmd, cwd=self.root, env=clean_env(), watch=self.db)
        self.processes.append({"argv": ["(tied)", *argv], "exit_code": result["exit_code"],
                               "connections_max_while_running": result.get("connections_max_while_running"),
                               "connections_after_exit": result.get("connections_after_exit")})
        return result

    def runtime(self):
        from openminion import APIRuntime

        return APIRuntime.from_config_path(str(self.cfg), home_root=self.root, data_root=self.root / ".openminion")

    def participants(self, room: str) -> list[dict[str, Any]]:
        return self.reader.rows(
            "SELECT id, participant_type, participant_id, role, joined_at, left_at, join_seq FROM room_participants "
            "WHERE session_id = ? ORDER BY join_seq", (room,))

    def outbound(self, room: str) -> list[dict[str, Any]]:
        rows = self.reader.rows("SELECT id, role, body, metadata_json, append_seq FROM messages "
                                "WHERE session_id = ? ORDER BY append_seq", (room,))
        out = []
        for r in rows:
            if r["role"] != "outbound":
                continue
            meta = json.loads(r["metadata_json"] or "{}")
            out.append({"id": r["id"], "agent": meta.get("agent"), "body": r["body"], "append_seq": r["append_seq"]})
        return out


def run_room_turn(env: Env, room: str, message: str, requester: str, *, new_thread: bool = False
                  ) -> tuple[dict[str, Any], list[str]]:
    """`openminion run --agent <requester> --session <room>`: the requester is a room participant other
    than the agent the routing should pick, so the replies show the routing, not the requester.

    A single-agent `openminion run` leaves its reply undelivered (deliver=False), and the next `run` on
    the same thread replays it (thread decision `replay`, reason `undelivered_response_pending`; the same
    on 0.1.14). The later addressed turns therefore pass --reset-session (a new thread in the same room
    session). Only replies written to the messages table by this run count."""
    before = {m["id"] for m in env.outbound(room)}
    argv = ["run", "--agent", requester, "--session", room, "--json"] + (["--reset-session"] if new_thread else [])
    result = env.cli(argv + [message])
    new = [m for m in env.outbound(room) if m["id"] not in before]
    return result, [m["agent"] for m in new]


def turn_meta(result: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(result["stdout"])
    except ValueError:
        return {"stdout_tail": result["stdout"].strip().splitlines()[-3:], "stderr_tail": result["stderr_tail"]}
    turn = payload.get("turn") or {}
    meta = turn.get("metadata") or {}
    return {"ok": payload.get("ok"), "runtime_source": payload.get("runtime_source"),
            "room_routing_mode": meta.get("room_routing_mode"), "room_routed_agents": meta.get("room_routed_agents"),
            "agent_id": turn.get("agent_id"), "body": turn.get("body"), "run_id": turn.get("run_id"),
            "thread_decision_action": meta.get("thread_decision_action"),
            "thread_decision_reason": meta.get("thread_decision_reason"), "replayed_response": meta.get("replayed_response")}


def section_a(checks: Checks, env: Env, label: str) -> None:
    created = env.cli(["room", "create", "--name", "order", "--agent", "first", "--agent", "second", "--agent", "third",
                       "--routing-mode", "broadcast"])
    out = kv(created["stdout"])
    room = out.get("room", "")
    invited = env.cli(["room", "invite", room, "--agent", "fourth"]) if room else {"exit_code": None, "stdout": ""}
    rows = env.participants(room) if room else []
    CASES["A"] = {"create": brief(created), "invite": brief(invited), "rows": rows}
    checks.check(
        f"{label} A: `openminion room create --agent first --agent second --agent third --routing-mode broadcast` "
        "(console script) exits 0 with participants=3, active_agent=first; `room invite <room> --agent fourth` exits 0 "
        "with participants=4",
        created["exit_code"] == 0 and out.get("participants") == "3" and out.get("active_agent") == "first"
        and invited["exit_code"] == 0 and "participants=4" in invited["stdout"],
        observed={"create": brief(created), "invite": brief(invited)},
    )
    checks.check(
        f"{label} A: the participant rows (independent read) are first, second, third, fourth with join_seq 1..4",
        [(r["participant_id"], r["join_seq"]) for r in rows] == [(a, i + 1) for i, a in enumerate(AGENTS)],
        observed=[(r["participant_id"], r["join_seq"], r["role"]) for r in rows],
    )


def section_b(checks: Checks, env: Env, label: str) -> dict[str, str]:
    rooms: dict[str, str] = {}
    for mode in ("broadcast", "sequential", "addressed"):
        # channel/target as `openminion run` uses them (console, cli-user), so section C can drive these rooms.
        res = env.tied_cli(["room", "create", "--name", f"tie-{mode}", "--agent", "first", "--agent", "second",
                            "--agent", "third", "--routing-mode", mode, "--channel", "console", "--target", "cli-user"],
                           [rid(mode, c) for c in "fc0"])
        out = kv(res["stdout"])
        rooms[mode] = out.get("room", "")
        CASES[f"B create {mode}"] = brief(res)
        checks.check(
            f"{label} B: `room create --routing-mode {mode} --agent first --agent second --agent third` on one tick, "
            "row ids sorting in reverse join order: exit 0, participants=3, active_agent=first",
            res["exit_code"] == 0 and out.get("participants") == "3" and out.get("active_agent") == "first",
            observed=brief(res),
        )
    # When the create failed (the 0.1.14 control), invite a room id that does not exist, so the
    # command still reaches its runtime start instead of stopping at "missing session id".
    b = rooms["broadcast"] or "room-not-created"
    res = env.tied_cli(["room", "invite", b, "--agent", "fourth"], [rid("broadcast", "1")])
    CASES["B invite"] = brief(res)
    checks.check(
        f"{label} B: `room invite <broadcast room> --agent fourth` on the same tick (row id sorts before two earlier "
        "joins): exit 0, participants=4",
        res["exit_code"] == 0 and "participants=4" in res["stdout"], observed=brief(res),
    )
    rows = env.participants(b)
    ids = [r["id"] for r in rows]
    CASES["B rows"] = rows
    checks.check(
        f"{label} B: the stored rows share one joined_at and their ids sort in reverse join order "
        "(first 'f..', second 'c..', third '0..', fourth '1..')",
        {r["joined_at"] for r in rows} == {TIED_AT} and len(rows) == 4
        and ids == [rid("broadcast", c) for c in "fc01"],
        observed=[(r["participant_id"], r["id"][:3], r["joined_at"], r["join_seq"]) for r in rows],
    )
    checks.check(
        f"{label} B: join_seq is 1, 2, 3, 4 in join order (independent read)",
        [(r["participant_id"], r["join_seq"]) for r in rows] == [(a, i + 1) for i, a in enumerate(AGENTS)],
        observed=[(r["participant_id"], r["join_seq"]) for r in rows],
    )
    rt = env.runtime()
    try:
        listed = [p.participant_id for p in rt.sessions.list_participants(b)]
        active = rt.sessions.get_active_agent(b)
        others = {m: [p.participant_id for p in rt.sessions.list_participants(rooms[m])] for m in ("sequential", "addressed")}
        record = type(rt.runtime_storage.record_store).__name__
    finally:
        rt.close()
    CASES["B read back"] = {"broadcast": listed, "active": active, **others, "record_store": record}
    checks.check(
        f"{label} B: a new APIRuntime ({record}) lists first, second, third, fourth with first active; the sequential "
        "and addressed rooms list first, second, third",
        listed == list(AGENTS) and active == "first"
        and all(v == ["first", "second", "third"] for v in others.values()),
        observed=CASES["B read back"],
    )
    return rooms


def section_c(checks: Checks, env: Env, label: str, rooms: dict[str, str]) -> None:
    from openminion.services.runtime.ingress.execution import _routed_agents
    from openminion.services.runtime.ingress.types import RuntimeTurnRequest

    b, s, a = rooms["broadcast"], rooms["sequential"], rooms["addressed"]
    res, agents = run_room_turn(env, b, "hello room", "fourth")
    CASES["C broadcast"] = {"cli": brief(res), "meta": turn_meta(res), "replies": agents}
    checks.check(
        f"{label} C: `openminion run --agent fourth --session <broadcast room>`: exit 0; the turn goes to first, "
        "second, third, fourth in join order (outbound rows in the messages table)",
        res["exit_code"] == 0 and agents == list(AGENTS), observed={"replies": agents, "meta": turn_meta(res)},
    )
    res, agents = run_room_turn(env, s, "hello room", "third")
    CASES["C sequential"] = {"cli": brief(res), "meta": turn_meta(res), "replies": agents}
    checks.check(f"{label} C: `run --agent third` on the sequential room (no explicit order): first, second, third",
                 res["exit_code"] == 0 and agents == ["first", "second", "third"],
                 observed={"replies": agents, "meta": turn_meta(res)})
    res, agents = run_room_turn(env, a, "hello room", "third")
    CASES["C addressed"] = {"cli": brief(res), "meta": turn_meta(res), "replies": agents}
    checks.check(f"{label} C: `run --agent third` on the addressed room: the active agent first answers",
                 res["exit_code"] == 0 and agents == ["first"], observed={"replies": agents, "meta": turn_meta(res)})
    # Observation only (not a check, outside #364): a second single-agent `openminion run` on the same
    # thread, without --resume or --reset-session.
    again, again_agents = run_room_turn(env, a, "a different prompt on the same thread", "third")
    CASES["note: second run on the same thread"] = {"exit_code": again["exit_code"], "meta": turn_meta(again),
                                                     "new outbound rows": again_agents,
                                                     "first run": turn_meta(res)}
    print("NOTE second `openminion run` on the same thread:", json.dumps(CASES["note: second run on the same thread"]))

    rt = env.runtime()
    try:
        with patch(f"{PARTICIPANTS}.utc_now_iso", return_value=TIED_AT):
            left_b = rt.sessions.remove_participant(session_id=b, participant_type="agent", participant_id="first")
            left_a = rt.sessions.remove_participant(session_id=a, participant_type="agent", participant_id="first")
        active_b, active_a = rt.sessions.get_active_agent(b), rt.sessions.get_active_agent(a)
        listed = [p.participant_id for p in rt.sessions.list_participants(b)]

        def routed(room: str, message: str = "hello room") -> list:
            request = RuntimeTurnRequest(agent_id="first", profile_agent_id="first", message=message, channel="cli",
                                         target="room", timeout_seconds=30.0, session_id=room)
            agents_, mode = _routed_agents(runtime=rt, request=request)
            return [list(agents_), mode]

        consumer = {"broadcast": routed(b), "addressed": routed(a), "addressed @third": routed(a, "@third please")}
    finally:
        rt.close()
    CASES["C leave"] = {"left": [left_b, left_a], "active": [active_b, active_a], "listed": listed, "routed": consumer}
    checks.check(
        f"{label} C: on a new APIRuntime the active first agent leaves both rooms on the same tick: second becomes "
        "active; the broadcast room lists second, third, fourth",
        left_b is True and left_a is True and active_b == "second" and active_a == "second"
        and listed == ["second", "third", "fourth"],
        observed=CASES["C leave"],
    )
    checks.check(
        f"{label} C: `_routed_agents` with that real runtime: broadcast second, third, fourth; addressed second; "
        "`@third` selects third",
        consumer == {"broadcast": [["second", "third", "fourth"], "broadcast"], "addressed": [["second"], "addressed"],
                     "addressed @third": [["third"], "addressed"]},
        observed=consumer,
    )
    res, agents = run_room_turn(env, b, "hello again", "fourth")
    CASES["C broadcast after leave"] = {"cli": brief(res), "meta": turn_meta(res), "replies": agents}
    checks.check(f"{label} C: `run --agent fourth` on the broadcast room after the leave: second, third, fourth",
                 res["exit_code"] == 0 and agents == ["second", "third", "fourth"],
                 observed={"replies": agents, "meta": turn_meta(res)})
    res, agents = run_room_turn(env, a, "hello again", "third", new_thread=True)
    res3, agents3 = run_room_turn(env, a, "@third please", "second", new_thread=True)
    CASES["C addressed after leave"] = {"cli": [brief(res), brief(res3)], "replies": [agents, agents3]}
    checks.check(f"{label} C: `run --agent third --reset-session` on the addressed room after the leave: the "
                 "fallback second answers; `run --agent second --reset-session \"@third please\"` is answered by third",
                 res["exit_code"] == 0 and agents == ["second"] and res3["exit_code"] == 0 and agents3 == ["third"],
                 observed={"plain": agents, "@third": agents3})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--backend", default="sqlite", choices=("sqlite", "postgres"))
    args = ap.parse_args()
    install_masking()
    strip_env()
    quiet()
    if args.backend == "postgres" and not pg_base():
        print("PGURL is not set: PostgreSQL not run")
        return 2
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {"backend": args.backend}
    if args.backend == "postgres":
        extra["postgres"] = pg_versions()
    info = banner("probe_364", **extra)
    checks = Checks()
    label = args.backend
    env = Env(work, args.backend)
    if env.db is not None:
        CASES["database"] = {"name": env.db.name, "application_name": env.db.app}

    @scenario(checks, f"{label} A")
    def _a():
        section_a(checks, env, label)

    rooms: dict[str, str] = {}

    @scenario(checks, f"{label} B")
    def _b():
        rooms.update(section_b(checks, env, label))

    if rooms and all(rooms.values()):
        @scenario(checks, f"{label} C")
        def _c():
            section_c(checks, env, label, rooms)
    else:
        # No room was created (the 0.1.14 control): record what `openminion run` does with this config.
        res = env.cli(["run", "--agent", "first", "--session", "room-not-created", "--json", "hello room"])
        CASES["C run without a room"] = brief(res)
        checks.check(f"{label} C: `openminion run --session <room>` starts its runtime and exits 0",
                     res["exit_code"] == 0, observed=brief(res))

    CASES["processes"] = env.processes
    if env.db is not None:
        conns = [(p["argv"][:3], p["connections_max_while_running"], p["connections_after_exit"]) for p in env.processes]
        checks.check(
            f"{label}: every CLI process connected to PostgreSQL under the database's application_name while running "
            "and left no connection after it exited",
            bool(env.processes) and all(p["connections_max_while_running"] > 0 and p["connections_after_exit"]["count"] == 0
                                        for p in env.processes),
            observed=conns,
        )
        files = sqlite_files(env.root)
        CASES["sqlite_files_under_root"] = files
        checks.check(f"{label}: no runtime SQLite database created at storage.path",
                     not storage_path(env.root).exists() and ".openminion/state.db" not in files,
                     observed={"storage.path exists": storage_path(env.root).exists(), "sqlite files": sorted(files)})
        CASES["postgres_ledger"] = env.db.rows("SELECT version, name FROM migrations WHERE version >= 14 ORDER BY version")
        CASES["postgres_join_seq_index"] = env.db.rows(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'room_participants' ORDER BY indexname")
    summary = checks.summary()
    write_result(Path(args.out), json.loads(mask(json.dumps({"banner": info, "checks": checks.items, "cases": CASES,
                                                              "summary": summary}, default=str))))
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
