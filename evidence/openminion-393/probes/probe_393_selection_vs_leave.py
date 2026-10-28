"""Not changed by #393: a selection racing a leave of the agent it selects.

`set_active_agent` checks that the agent is an active participant, then writes
`active_agent_id` with an unconditional UPDATE, in separate statements and not
under the session-row lock. This probe holds a selection of `beta` on storage A
right after that check, makes `beta` leave on storage B, then lets the selection
go on. SQLite only (one file, two runtime storages).

PYTHONPATH=src python probe_393_selection_vs_leave.py
"""

import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

from openminion.modules.storage.runtime.context import build_runtime_storage


def main():
    print(f"python {sys.version.split()[0]} {sys.platform} | SQLite {sqlite3.sqlite_version}")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = Path(tmp) / "state" / "openminion.db"
        main_storage, a, b = (build_runtime_storage(path, env={}) for _ in range(3))
        try:
            sessions = main_storage.sessions
            room = sessions.create_room(channel="cli", target="selection-vs-leave")
            for agent in ("alpha", "beta"):
                sessions.add_participant(
                    session_id=room.id, participant_type="agent", participant_id=agent
                )
            print(f"before: active {sessions.get_active_agent(room.id)!r}")

            owner = a.sessions._participants
            get_participant = owner.get_participant
            checked, go = threading.Event(), threading.Event()

            def held_check(*args, **kwargs):
                result = get_participant(*args, **kwargs)
                checked.set()
                go.wait(20)
                return result

            owner.get_participant = held_check
            box = {}

            def select():
                try:
                    box["result"] = a.sessions.set_active_agent(
                        session_id=room.id, agent_id="beta"
                    ).active_agent_id
                except Exception as exc:  # reported
                    box["result"] = f"{type(exc).__name__}: {exc}"

            thread = threading.Thread(target=select, daemon=True)
            thread.start()
            checked.wait(20)
            print("A: set_active_agent(beta) checked beta is a participant, held")
            left = b.sessions.remove_participant(
                session_id=room.id, participant_type="agent", participant_id="beta"
            )
            print(f"B: remove_participant(beta) -> {left}")
            go.set()
            thread.join(20)
            agents = [
                item.participant_id
                for item in sessions.list_participants(room.id)
                if item.participant_type == "agent"
            ]
            print(f"A: set_active_agent(beta) -> {box.get('result')!r}")
            print(
                f"after: agents listed {agents} | stored active_agent_id "
                f"{sessions.get_session(room.id).active_agent_id!r} | "
                f"get_active_agent {sessions.get_active_agent(room.id)!r}"
            )
        finally:
            for storage in (main_storage, a, b):
                storage.close()


if __name__ == "__main__":
    main()
