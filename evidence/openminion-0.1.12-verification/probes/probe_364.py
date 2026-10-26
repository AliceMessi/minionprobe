"""#364 on the installed package: room participants that join on one clock tick.

    python -I probe_364.py <work_dir> <out.json> [--backend sqlite|postgres]
        [--peers N] [--joins N] [--rounds N]

Fresh runtime databases (SQLite file, or a new PostgreSQL schema when
--backend postgres; OPENMINION_TEST_POSTGRES_URL must be set). Checks:

1. storage API: the issue's tied-clock probe (row ids starting "f", "c", "0", so
   each id sorts before the previous join's), the active-agent fallback;
2. active upsert keeps row/joined_at/place; leave and rejoin renews the place;
   explicit active-agent selection; the legacy owner record; joins write no
   session value; a join after the wall clock moved back still lists by its
   joined_at;
3. reopening the database (a new storage context) keeps all of it;
4. the room CLI (`openminion room create` / `room invite` through the
   installed CLI main) and the routing consumer (`_routed_agents`, which the
   runtime ingress uses to pick the agents of a room turn);
5. concurrency: peers on their own connections join one room on one tick;
   a rejoin that starts inside the leave's transaction; a leave/rejoin storm.

Runs unchanged on 0.1.11 as the negative control (there `join_seq` does not
exist, so those reads fail and are reported as such).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402
from probe_364_lib import (  # noqa: E402
    EARLIER_AT,
    LATER_AT,
    PARTICIPANT_CLOCK,
    PARTICIPANT_UUID,
    TIED_AT,
    id_feed,
    rid,
    join_agents,
    join_seq_by_participant,
    open_storage,
    participant_ids,
    pg_base_url,
    pg_schema,
    pg_server_version,
    remove_agent,
    row_ids,
)


def safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the probe reports it
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"


def scenario(checks: Checks, name: str):
    """Run a scenario; an exception that escapes it is a failed check."""

    def wrap(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            checks.check(f"{name}: completes without an exception", False, observed=f"{type(exc).__name__}: {exc}"[:400])
            traceback.print_exc()
        return fn

    return wrap


# ------------------------------------------------------------------ storage API
def storage_api(checks: Checks, sessions, label: str, keep: dict) -> None:
    @scenario(checks, f"{label} tied join order")
    def _tied():
        room = sessions.create_room(channel="cli", target="review")
        keep["tied_room"] = room.id
        joined = join_agents(sessions, room.id, ("first", "second", "third"), ids="fc0")
        checks.check(
            f"{label} tied: the three joins share one joined_at and their row ids sort in reverse join order",
            [(p.id, p.joined_at) for p in joined] == [(rid(room.id, c), TIED_AT) for c in "fc0"]
            and sorted(p.id for p in joined) == [p.id for p in reversed(joined)],
            observed=[(p.participant_id, p.id[:3], p.joined_at) for p in joined],
        )
        listed = participant_ids(sessions, room.id)
        checks.check(f"{label} tied: list_participants is in join order", listed == ["first", "second", "third"], observed=listed, expected=["first", "second", "third"])
        active = sessions.get_active_agent(room.id)
        checks.check(f"{label} tied: the first agent is active", active == "first", observed=active)
        seq = safe(join_seq_by_participant, sessions, room.id)
        checks.check(f"{label} tied: join_seq 1, 2, 3 in join order", seq == {"first": 1, "second": 2, "third": 3}, observed=seq)
        remove_agent(sessions, room.id, "first")
        active = sessions.get_active_agent(room.id)
        listed = participant_ids(sessions, room.id)
        checks.check(f"{label} tied: removing the active first agent selects the second", active == "second", observed=active, expected="second")
        checks.check(f"{label} tied: remaining participants list in join order", listed == ["second", "third"], observed=listed)

    @scenario(checks, f"{label} upsert and rejoin")
    def _upsert():
        room = sessions.create_room(channel="cli", target="rejoin")
        keep["rejoin_room"] = room.id
        join_agents(sessions, room.id, ("first", "second", "third"), ids="fc0")
        with patch(PARTICIPANT_CLOCK, return_value=LATER_AT), patch(PARTICIPANT_UUID, side_effect=row_ids(room.id, "9")):
            again = sessions.add_participant(
                session_id=room.id, participant_type="agent", participant_id="first",
                channel="cli", role="owner", display_name="First again",
            )
        checks.check(
            f"{label} upsert: an active participant added again keeps its row id and joined_at, takes the new channel/role/display name",
            (again.id, again.joined_at, again.channel, again.role, again.display_name)
            == (rid(room.id, "f"), TIED_AT, "cli", "owner", "First again"),
            observed=(again.id[:3], again.joined_at, again.channel, again.role, again.display_name),
        )
        listed = participant_ids(sessions, room.id)
        checks.check(f"{label} upsert: it keeps its place", listed == ["first", "second", "third"], observed=listed)
        seq = safe(join_seq_by_participant, sessions, room.id)
        checks.check(f"{label} upsert: join_seq unchanged (1, 2, 3)", seq == {"first": 1, "second": 2, "third": 3}, observed=seq)

        remove_agent(sessions, room.id, "first")
        rejoined = join_agents(sessions, room.id, ("first",), ids="8")[0]
        listed = participant_ids(sessions, room.id)
        checks.check(
            f"{label} rejoin same tick: same row, new place after every earlier join",
            (rejoined.id, rejoined.joined_at) == (rid(room.id, "f"), TIED_AT) and listed == ["second", "third", "first"],
            observed={"row": rejoined.id[:3], "joined_at": rejoined.joined_at, "listed": listed},
            expected=["second", "third", "first"],
        )
        seq = safe(join_seq_by_participant, sessions, room.id)
        checks.check(f"{label} rejoin same tick: join_seq first=4", seq == {"first": 4, "second": 2, "third": 3}, observed=seq)

        remove_agent(sessions, room.id, "second", at=LATER_AT)
        rejoined = join_agents(sessions, room.id, ("second",), ids="7", at=LATER_AT)[0]
        listed = participant_ids(sessions, room.id)
        checks.check(
            f"{label} rejoin later: the rejoin's joined_at and a new place",
            (rejoined.id, rejoined.joined_at) == (rid(room.id, "c"), LATER_AT) and listed == ["third", "first", "second"],
            observed={"row": rejoined.id[:3], "joined_at": rejoined.joined_at, "listed": listed},
        )
        seq = safe(join_seq_by_participant, sessions, room.id)
        checks.check(f"{label} rejoin later: join_seq second=5", seq == {"first": 4, "second": 5, "third": 3}, observed=seq)
        keep["rejoin_expected"] = {"listed": listed, "seq": seq, "active": sessions.get_active_agent(room.id)}

    @scenario(checks, f"{label} explicit active agent")
    def _explicit():
        room = sessions.create_room(channel="cli", target="explicit")
        join_agents(sessions, room.id, ("first", "second", "third"), ids="fc8")
        sessions.set_active_agent(session_id=room.id, agent_id="third")
        join_agents(sessions, room.id, ("fourth", "first"), ids="05")
        a1 = sessions.get_active_agent(room.id)
        remove_agent(sessions, room.id, "second")
        a2 = sessions.get_active_agent(room.id)
        checks.check(f"{label} explicit: a later join, a re-add and a non-active leave keep the selected agent", (a1, a2) == ("third", "third"), observed=(a1, a2))
        remove_agent(sessions, room.id, "third")
        listed = participant_ids(sessions, room.id)
        active = sessions.get_active_agent(room.id)
        checks.check(
            f"{label} explicit: the selected agent leaving selects the earliest remaining join",
            listed == ["first", "fourth"] and active == "first",
            observed={"listed": listed, "active": active},
        )
        sessions.set_active_agent(session_id=room.id, agent_id="fourth")
        active = sessions.get_active_agent(room.id)
        err = safe(sessions.set_active_agent, session_id=room.id, agent_id="second")
        checks.check(
            f"{label} explicit: a later join can be selected; an agent that left cannot",
            active == "fourth" and isinstance(err, str) and "not an active participant" in err and sessions.get_active_agent(room.id) == "fourth",
            observed={"active": active, "select_left": err},
        )
        keep["explicit_room"] = room.id

    @scenario(checks, f"{label} legacy owner")
    def _legacy():
        # Read on a separate legacy session: get_active_agent creates the owner
        # record (with the real clock) when it is missing.
        other = sessions.resolve_session(agent_id="legacy-agent", channel="console", target="legacy-read")
        active_before = sessions.get_active_agent(other.id)
        session = sessions.resolve_session(agent_id="legacy-agent", channel="console", target="legacy-tie")
        join_agents(sessions, session.id, ("guest-agent",), ids="f0")
        listed = participant_ids(sessions, session.id)
        checks.check(f"{label} legacy: the owner record lists before the first invited agent on one tick", listed == ["legacy-agent", "guest-agent"], observed=listed)
        seq = safe(join_seq_by_participant, sessions, session.id)
        checks.check(f"{label} legacy: join_seq owner=1, guest=2", seq == {"legacy-agent": 1, "guest-agent": 2}, observed=seq)
        active_after_invite = sessions.get_active_agent(session.id)
        err = safe(sessions.set_active_agent, session_id=session.id, agent_id="other-agent")
        sessions.set_active_agent(session_id=session.id, agent_id="legacy-agent")
        reselected = sessions.get_active_agent(session.id)
        checks.check(
            f"{label} legacy: the owner agent from the session key is active before any invite; an agent outside the session cannot be selected (bound to the owner); the owner can be selected again",
            active_before == "legacy-agent" and isinstance(err, str) and "bound to agent 'legacy-agent'" in err and reselected == "legacy-agent",
            # active_after_invite: add_participant makes the first invited agent
            # active when the session row has none (unchanged code, compared
            # with the 0.1.11 control run).
            observed={"active_before_invite": active_before, "active_after_invite": active_after_invite, "select_other": err, "reselect_owner": reselected},
        )

    @scenario(checks, f"{label} joins write no session value")
    def _no_session_write():
        room = sessions.create_room(channel="cli", target="session-row")
        before = sessions.get_session(room.id)
        with patch(PARTICIPANT_CLOCK, return_value=LATER_AT):
            sessions.add_participant(session_id=room.id, participant_type="human", participant_id="alice")
        same = sessions.get_session(room.id) == before
        join_agents(sessions, room.id, ("first",), ids="f")
        selected = sessions.get_session(room.id)
        join_agents(sessions, room.id, ("second", "first"), ids="c0", at=LATER_AT)
        checks.check(
            f"{label} session row: a human join changes nothing; the first agent becomes active; later joins and the active agent's re-add change nothing",
            same and selected is not None and selected.active_agent_id == "first" and sessions.get_session(room.id) == selected,
            observed={"human_join_unchanged": same, "active": getattr(selected, "active_agent_id", None)},
        )

    @scenario(checks, f"{label} wall clock moved back")
    def _clock_back():
        room = sessions.create_room(channel="cli", target="clock-back")
        join_agents(sessions, room.id, ("late-clock",), ids="1", at=LATER_AT)
        join_agents(sessions, room.id, ("early-clock",), ids="2", at=EARLIER_AT)
        listed = participant_ids(sessions, room.id)
        checks.check(
            f"{label} clock moved back: a later join with an earlier joined_at lists first (joined_at still orders first)",
            listed == ["early-clock", "late-clock"],
            observed=listed,
        )
        seq = safe(join_seq_by_participant, sessions, room.id)
        checks.check(f"{label} clock moved back: join_seq records the join order (late-clock=1, early-clock=2)", seq == {"late-clock": 1, "early-clock": 2}, observed=seq)


# ------------------------------------------------------------------ concurrency
def concurrency(checks: Checks, sessions, peers: list, label: str, joins: int, rounds: int) -> None:
    @scenario(checks, f"{label} concurrent joins")
    def _joins():
        room = sessions.create_room(channel="cli", target="crowd")
        start = threading.Barrier(len(peers))
        errors: list[str] = []

        def join(index: int):
            start.wait()
            out = []
            for step in range(joins):
                try:
                    out.append(peers[index].add_participant(session_id=room.id, participant_type="agent", participant_id=f"peer-{index}-{step}"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{type(exc).__name__}: {exc}"[:300])
            return out

        t0 = time.perf_counter()
        with patch(PARTICIPANT_CLOCK, return_value=TIED_AT):
            with ThreadPoolExecutor(max_workers=len(peers)) as pool:
                per_peer = list(pool.map(join, range(len(peers))))
        elapsed = round(time.perf_counter() - t0, 2)
        listed = participant_ids(sessions, room.id)
        own_order = all(
            [n for n in listed if n in [p.participant_id for p in joined]] == [p.participant_id for p in joined]
            for joined in per_peer
        )
        seq = safe(join_seq_by_participant, sessions, room.id)
        distinct = isinstance(seq, dict) and sorted(seq.values()) == list(range(1, len(peers) * joins + 1))
        checks.check(
            f"{label} concurrent joins: {len(peers)} peers x {joins} joins on one tick, no errors, every join has its own place 1..{len(peers) * joins}",
            not errors and distinct and len(listed) == len(peers) * joins,
            observed={"errors": errors[:5], "joined": len(listed), "distinct_join_seq": distinct, "seconds": elapsed},
        )
        checks.check(
            f"{label} concurrent joins: each peer's joins list in the order it made them, the listing follows join_seq",
            own_order and isinstance(seq, dict) and listed == sorted(seq, key=seq.get),
            observed={"own_order": own_order},
        )
        checks.check(f"{label} concurrent joins: the active agent is one of the joined agents (#393 not certified)", sessions.get_active_agent(room.id) in listed, observed=sessions.get_active_agent(room.id))

    @scenario(checks, f"{label} rejoin inside the leave's transaction")
    def _overlap():
        leaver, joiner = peers[0], peers[1]
        room = sessions.create_room(channel="cli", target="leave-rejoin")
        join_agents(sessions, room.id, ("first", "second"), ids="fc")
        marked_left = threading.Event()
        rejoining = threading.Event()
        owner = leaver._participants
        get_active_agent = owner.get_active_agent

        def get_active_agent_once_rejoin_started(session_id):
            if not marked_left.is_set():
                marked_left.set()
                rejoining.wait(timeout=10)
                time.sleep(0.5)  # let the rejoin reach its first lock
            return get_active_agent(session_id)

        def leave():
            with patch.object(owner, "get_active_agent", get_active_agent_once_rejoin_started):
                return leaver.remove_participant(session_id=room.id, participant_type="agent", participant_id="first")

        def rejoin():
            marked_left.wait(timeout=10)
            rejoining.set()
            return joiner.add_participant(session_id=room.id, participant_type="agent", participant_id="first")

        t0 = time.perf_counter()
        with patch(PARTICIPANT_CLOCK, return_value=TIED_AT):
            with ThreadPoolExecutor(max_workers=2) as pool:
                left = pool.submit(leave)
                rejoined = pool.submit(rejoin)
                left_result = safe(left.result, timeout=60)
                rejoin_result = safe(rejoined.result, timeout=60)
        elapsed = round(time.perf_counter() - t0, 2)
        listed = participant_ids(sessions, room.id)
        seq = safe(join_seq_by_participant, sessions, room.id)
        active = sessions.get_active_agent(room.id)
        checks.check(
            f"{label} overlapping leave/rejoin: both complete (no unique-index error, no deadlock)",
            left_result is True and not isinstance(rejoin_result, str) and getattr(rejoin_result, "id", "") == rid(room.id, "f"),
            observed={"leave": left_result, "rejoin": rejoin_result if isinstance(rejoin_result, str) else rejoin_result.id[:3], "seconds": elapsed},
        )
        checks.check(
            f"{label} overlapping leave/rejoin: the rejoin waits for the leave, takes a new place, and the fallback selects the earliest remaining join",
            listed == ["second", "first"] and seq == {"first": 3, "second": 2} and active == "second",
            observed={"listed": listed, "join_seq": seq, "active": active},
        )

    @scenario(checks, f"{label} leave/rejoin storm")
    def _storm():
        room = sessions.create_room(channel="cli", target="storm")
        agents = [f"agent-{i}" for i in range(len(peers))]
        join_agents(sessions, room.id, agents, ids="".join(f"{15 - i:x}" for i in range(len(peers))))
        errors: list[str] = []
        start = threading.Barrier(len(peers))
        ops = [0]
        lock = threading.Lock()

        def churn(index: int):
            start.wait()
            for _ in range(rounds):
                for action in ("leave", "join"):
                    try:
                        if action == "leave":
                            peers[index].remove_participant(session_id=room.id, participant_type="agent", participant_id=agents[index])
                        else:
                            peers[index].add_participant(session_id=room.id, participant_type="agent", participant_id=agents[index])
                        with lock:
                            ops[0] += 1
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{action}: {type(exc).__name__}: {exc}"[:300])

        t0 = time.perf_counter()
        with patch(PARTICIPANT_CLOCK, return_value=TIED_AT):
            with ThreadPoolExecutor(max_workers=len(peers)) as pool:
                list(pool.map(churn, range(len(peers))))
        elapsed = round(time.perf_counter() - t0, 2)
        listed = participant_ids(sessions, room.id)
        seq = safe(join_seq_by_participant, sessions, room.id)
        active = sessions.get_active_agent(room.id)
        ok_seq = isinstance(seq, dict) and len(set(seq.values())) == len(seq) == len(agents)
        checks.check(
            f"{label} storm: {len(peers)} peers each leave and rejoin {rounds} times on one tick, including the active agent: every operation completes",
            not errors and ops[0] == len(peers) * rounds * 2,
            observed={"operations": ops[0], "errors": errors[:5], "error_count": len(errors), "seconds": elapsed},
        )
        checks.check(
            f"{label} storm: afterwards every agent is listed once, places are distinct, the listing follows join_seq and the active agent is a participant",
            sorted(listed) == sorted(agents) and ok_seq and listed == sorted(seq, key=seq.get) and active in listed,
            observed={"listed": listed, "join_seq": seq, "active": active},
        )
        fallbacks = []
        for _ in range(2):
            before = participant_ids(sessions, room.id)
            current = sessions.get_active_agent(room.id)
            remove_agent(sessions, room.id, current)
            expected = next(p for p in before if p != current)
            fallbacks.append({"left": current, "selected": sessions.get_active_agent(room.id), "expected": expected})
        checks.check(
            f"{label} storm: then the active agent leaving selects the earliest remaining join (twice)",
            all(f["selected"] == f["expected"] for f in fallbacks),
            observed=fallbacks,
        )


# ------------------------------------------------------------------ CLI and routing
def write_config(root: Path, url: str) -> Path:
    from openminion.base.config import AgentProfileConfig, OpenMinionConfig, save_config

    config = OpenMinionConfig()
    config.agents = {
        name: AgentProfileConfig(name=name, provider="echo", default_channel="console")
        for name in ("first", "second", "third", "fourth")
    }
    config.default_agent = "first"
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    if url:
        config.storage.backend = "postgres"
        config.storage.postgres_url = url
    root.mkdir(parents=True, exist_ok=True)
    path = root / "agents.json"
    save_config(config, str(path))
    return path


NOT_RUN: list[dict] = []
AGENTS = ("first", "second", "third", "fourth")


class StorageRuntime:
    """What the room command handlers and `_routed_agents` read from a runtime
    (`sessions`, `config.agents`, `close()`), over a PostgreSQL runtime storage.
    Used only on PostgreSQL, where APIRuntime does not start (see NOT_RUN)."""

    def __init__(self, storage) -> None:
        from types import SimpleNamespace

        self.sessions = storage.sessions
        self.config = SimpleNamespace(agents={name: object() for name in AGENTS})

    def close(self) -> None:  # the storage is closed by the probe
        return None


def cli(argv: list[str], ids: list[str], at: str = TIED_AT, runtime=None) -> tuple[int, str, str]:
    from openminion.cli.main import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(PARTICIPANT_CLOCK, return_value=at))
        stack.enter_context(patch(PARTICIPANT_UUID, side_effect=id_feed(ids)))
        if runtime is not None:
            stack.enter_context(patch("openminion.cli.commands.room._open_room_runtime", return_value=runtime))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = main(argv)
            except SystemExit as exc:
                rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def cli_routing(checks: Checks, root: Path, url: str, label: str) -> None:
    from openminion import APIRuntime
    from openminion.services.runtime.ingress.execution import _routed_agents
    from openminion.services.runtime.ingress.types import RuntimeTurnRequest

    cfg = write_config(root, url)
    base = ["--home-root", str(root), "--data-root", str(root / ".openminion"), "--config", str(cfg), "room"]
    shim = storage = None
    how = "the installed CLI (`openminion.cli.main.main`)"
    if url:
        # APIRuntime with storage.backend=postgres: record why the real room
        # command cannot run, then drive the installed parser and room
        # handlers with only the runtime construction replaced.
        try:
            APIRuntime.from_config_path(str(cfg), home_root=root, data_root=root / ".openminion").close()
            started = "started"
        except Exception as exc:  # noqa: BLE001
            started = f"{type(exc).__name__}: {exc}"
        NOT_RUN.append({
            "item": "room CLI against a PostgreSQL runtime database through APIRuntime",
            "reason": "APIRuntime.from_config_path with storage.backend=postgres does not start",
            "observed": started,
        })
        storage = open_storage(root / "pg-cli" / "state.db", url)
        shim = StorageRuntime(storage)
        how = "the installed CLI parser and room handlers, with `_open_room_runtime` returning the PostgreSQL session store"
    rooms: dict[str, str] = {}
    for mode in ("broadcast", "sequential", "addressed"):
        rc, out, err = cli(base + ["create", "--name", f"tie-{mode}", "--agent", "first", "--agent", "second", "--agent", "third", "--routing-mode", mode], [rid(mode, c) for c in "fc0"], runtime=shim)
        lines = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        rooms[mode] = lines.get("room", "")
        checks.check(
            f"{label} CLI `openminion room create --routing-mode {mode} --agent first --agent second --agent third` (one tick, row ids reverse) via {how}: exit 0, 3 participants, active first",
            rc == 0 and lines.get("participants") == "3" and lines.get("active_agent") == "first",
            observed={"rc": rc, "stdout": out.strip().splitlines(), "stderr_tail": err.strip().splitlines()[-2:]},
        )
    rc, out, err = cli(base + ["invite", rooms["broadcast"], "--agent", "fourth"], [rid("broadcast", "1")], runtime=shim)
    checks.check(
        f"{label} CLI `openminion room invite <room> --agent fourth` on the same tick (row id sorts before two earlier joins): exit 0, 4 participants",
        rc == 0 and "participants=4" in out,
        observed={"rc": rc, "stdout": out.strip().splitlines(), "stderr_tail": err.strip().splitlines()[-2:]},
    )

    runtime = shim if shim is not None else APIRuntime.from_config_path(str(cfg), home_root=root, data_root=root / ".openminion")
    reader = "a new runtime" if shim is None else "the session store"
    try:
        def routed(room_id: str, message: str = "hello room") -> list:
            request = RuntimeTurnRequest(
                agent_id="first", profile_agent_id="first", message=message, channel="cli",
                target="room", timeout_seconds=30.0, session_id=room_id,
            )
            agents, mode = _routed_agents(runtime=runtime, request=request)
            return [list(agents), mode]

        b = rooms["broadcast"]
        listed = participant_ids(runtime.sessions, b)
        checks.check(f"{label} CLI room (read back by {reader}): list_participants in join order", listed == ["first", "second", "third", "fourth"], observed=listed)
        r = safe(routed, b)
        checks.check(f"{label} routing (broadcast): the room turn goes to the agents in join order", r == [["first", "second", "third", "fourth"], "broadcast"], observed=r)
        r = safe(routed, rooms["sequential"])
        checks.check(f"{label} routing (sequential, no explicit order): join order", r == [["first", "second", "third"], "sequential"], observed=r)
        remove_agent(runtime.sessions, b, "first")
        active = runtime.sessions.get_active_agent(b)
        r = safe(routed, b)
        checks.check(
            f"{label} routing (broadcast): after the active first agent leaves, the second is active and the turn goes to second, third, fourth",
            active == "second" and r == [["second", "third", "fourth"], "broadcast"],
            observed={"active": active, "routed": r},
        )
        a = rooms["addressed"]
        r0 = safe(routed, a)
        remove_agent(runtime.sessions, a, "first")
        active = runtime.sessions.get_active_agent(a)
        r1 = safe(routed, a)
        r2 = safe(routed, a, "@third please")
        checks.check(
            f"{label} routing (addressed): the active agent answers; after it leaves the fallback (second) answers; a mention still selects its agent",
            r0 == [["first"], "addressed"] and active == "second" and r1 == [["second"], "addressed"] and r2 == [["third"], "addressed"],
            observed={"before": r0, "active_after_leave": active, "after": r1, "mention_third": r2},
        )
    finally:
        runtime.close()
        if storage is not None:
            storage.close()


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("work")
    ap.add_argument("out")
    ap.add_argument("--backend", default="sqlite", choices=("sqlite", "postgres"))
    ap.add_argument("--peers", type=int, default=4)
    ap.add_argument("--joins", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=10)
    args = ap.parse_args()
    quiet()
    clear_override_env()
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    extra = {"backend": args.backend, "peers": args.peers, "joins": args.joins, "rounds": args.rounds}
    checks = Checks()
    label = args.backend

    stack = contextlib.ExitStack()
    url = cli_url = ""
    if args.backend == "postgres":
        if not pg_base_url():
            print("OPENMINION_TEST_POSTGRES_URL is not set: PostgreSQL not run")
            return 2
        url = stack.enter_context(pg_schema("probe364"))
        cli_url = stack.enter_context(pg_schema("probe364cli"))
        extra["postgres_server"] = pg_server_version(url)
    info = banner("probe_364", **extra)
    db = work / "fresh" / "state" / "openminion.db"
    keep: dict = {}
    with stack:
        from openminion.modules.storage.runtime.migrations import DEFAULT_MIGRATIONS

        head = max(m.version for m in DEFAULT_MIGRATIONS)
        storage = open_storage(db, url)
        checks.check(
            f"{label} fresh database migrated to the package head ({head})",
            storage.migration_result.current_version == head
            and list(storage.migration_result.applied_versions) == sorted(m.version for m in DEFAULT_MIGRATIONS),
            observed={"applied": list(storage.migration_result.applied_versions), "current": storage.migration_result.current_version},
        )
        peers = [open_storage(db, url) for _ in range(max(2, args.peers))]
        try:
            storage_api(checks, storage.sessions, label, keep)
            concurrency(checks, storage.sessions, [p.sessions for p in peers], label, args.joins, args.rounds)
        finally:
            for p in peers:
                p.close()
            storage.close()

        # Reopen: a new storage context on the same database.
        reopened = open_storage(db, url)
        try:
            s = reopened.sessions
            checks.check(f"{label} reopen: no migration is applied again", list(reopened.migration_result.applied_versions) == [], observed=list(reopened.migration_result.applied_versions))
            if "tied_room" in keep:
                listed = participant_ids(s, keep["tied_room"])
                checks.check(f"{label} reopen: the tied room still lists second, third with second active", listed == ["second", "third"] and s.get_active_agent(keep["tied_room"]) == "second", observed={"listed": listed, "active": s.get_active_agent(keep["tied_room"])})
            if "rejoin_room" in keep and "rejoin_expected" in keep:
                now = {"listed": participant_ids(s, keep["rejoin_room"]), "seq": safe(join_seq_by_participant, s, keep["rejoin_room"]), "active": s.get_active_agent(keep["rejoin_room"])}
                checks.check(f"{label} reopen: the upsert/rejoin room keeps its listing, join_seq and active agent", now == keep["rejoin_expected"], observed=now, expected=keep["rejoin_expected"])
            if "explicit_room" in keep:
                checks.check(f"{label} reopen: the explicitly selected agent is still active", s.get_active_agent(keep["explicit_room"]) == "fourth", observed=s.get_active_agent(keep["explicit_room"]))
        finally:
            reopened.close()

        @scenario(checks, f"{label} CLI and routing")
        def _cli():
            cli_routing(checks, work / "cli-root", cli_url, label)

    summary = checks.summary()
    write_result(Path(args.out), {"banner": info, "checks": checks.items, "not_run": NOT_RUN, "summary": summary})
    for item in NOT_RUN:
        print(f"NOT RUN {item['item']}: {item['reason']} | observed={item['observed'][:300]!r}")
    print(f"SUMMARY passed={summary['passed']} failed={summary['failed']} total={summary['total']} not_run={len(NOT_RUN)}")
    return 0 if checks.all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
