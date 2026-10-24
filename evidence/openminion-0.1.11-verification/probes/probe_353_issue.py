"""#353 reproduction, the same script on 0.1.10 (control) and on 0.1.11.

  1. The issue's deterministic probe (the final block of the maintainer probe in
     #352): one patched clock tick, user then assistant with ids ffff... and 0000....
  2. The native-Windows intermittent form, with nothing patched: N sessions, each
     gets 4 messages appended back to back with the real clock and real uuid4 ids.
     On Windows the clock often returns the same timestamp for consecutive appends,
     so some sessions hold tied messages. For each session the probe compares the
     append order with list_messages, list_recent_messages(limit=1) and the
     SessionContextService.build_history order. Whether ties occur depends on the
     platform clock; with no tied session the real-clock part is reported as not
     applicable on that platform (no check).

On 0.1.10 the checks are controls (the reversal is expected to reproduce); on
0.1.11 they assert append order.

Usage: python -I probe_353_issue.py <work_dir> <out.json> [sessions=300]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import Checks, banner, clear_override_env, quiet, write_result  # noqa: E402

WORK = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
N = int(sys.argv[3]) if len(sys.argv) > 3 else 300
WORK.mkdir(parents=True, exist_ok=True)
clear_override_env()
quiet()
info = banner("probe_353_issue", sessions=N)

import openminion  # noqa: E402
from openminion.modules.storage.runtime.migrations import migrate_database  # noqa: E402
from openminion.modules.storage.runtime.session_store import SessionStore  # noqa: E402
from openminion.modules.storage.runtime.sqlite import connect_database  # noqa: E402
from openminion.services.context.session import SessionContextService  # noqa: E402

VERSION = openminion.__version__
FIXED = VERSION != "0.1.10"
c = Checks()
observed: dict[str, object] = {"version": VERSION, "mode": "check" if FIXED else "control (pre-fix release)"}

# 1. the issue's deterministic probe, unchanged apart from the paths
db = WORK / "issue.db"
migrate_database(db)
conn = connect_database(db)
s = SessionStore(conn)
sess = s.resolve_session(agent_id="main", channel="console", target="probe")
ids = ["f" * 32, "0" * 32]
with patch("openminion.modules.storage.runtime.session_store.messages.utc_now_iso", return_value="2026-10-22T01:00:00+00:00"), \
        patch("openminion.modules.storage.runtime.session_store.messages.uuid4", side_effect=[SimpleNamespace(hex=i) for i in ids]):
    s.append_message(session_id=sess.id, role="user", body="first")
    s.append_message(session_id=sess.id, role="assistant", body="second")
probe_out = {"inserted_roles": ["user", "assistant"], "read_roles": [m.role for m in s.list_messages(session_id=sess.id)],
             "newest_role": [m.role for m in s.list_recent_messages(session_id=sess.id, limit=1)]}
print(json.dumps(probe_out))
observed["issue_probe"] = probe_out
conn.close()
if FIXED:
    c.check("issue probe: read_roles follow append order", probe_out["read_roles"] == ["user", "assistant"], probe_out["read_roles"])
    c.check("issue probe: newest_role is the newest append", probe_out["newest_role"] == ["assistant"], probe_out["newest_role"])
else:
    c.check("control on 0.1.10: issue probe reverses the tied pair", probe_out["read_roles"] == ["assistant", "user"], probe_out["read_roles"])
    c.check("control on 0.1.10: newest_role is the older append", probe_out["newest_role"] == ["user"], probe_out["newest_role"])

# 2. real clock, real ids, nothing patched
db2 = WORK / "realclock.db"
migrate_database(db2)
conn = connect_database(db2)
s = SessionStore(conn)
svc = SessionContextService(s, keep_recent_messages=50, archive_enabled=False)
tied_sessions = list_mismatch = newest_mismatch = history_mismatch = 0
examples: list[dict[str, object]] = []
for k in range(N):
    sess = s.resolve_session(agent_id="main", channel="console", target=f"t{k}")
    appended = [s.append_message(session_id=sess.id, role=("inbound" if j % 2 == 0 else "outbound"), body=f"m{j}") for j in range(4)]
    bodies = [m.body for m in appended]
    listed = [m.body for m in s.list_messages(session_id=sess.id)]
    newest = [m.body for m in s.list_recent_messages(session_id=sess.id, limit=1)]
    history = [m.body for m in svc.build_history(session_id=sess.id, channel="console", target=f"t{k}", recent_limit=10)
               if (m.metadata or {}).get("role") != "system"]
    stamps = [m.created_at for m in appended]
    tied = len(set(stamps)) < len(stamps)
    tied_sessions += tied
    bad = {"list": listed != bodies, "newest": newest != ["m3"], "history": history != bodies}
    list_mismatch += bad["list"]
    newest_mismatch += bad["newest"]
    history_mismatch += bad["history"]
    if any(bad.values()) and len(examples) < 5:
        examples.append({"session": k, "distinct_timestamps": len(set(stamps)), "listed": listed, "newest": newest, "history": history})
conn.close()
observed["real_clock"] = {"sessions": N, "messages_per_session": 4, "sessions_with_tied_timestamps": tied_sessions,
                          "list_messages_out_of_append_order": list_mismatch, "newest_not_last_append": newest_mismatch,
                          "build_history_out_of_append_order": history_mismatch, "examples": examples}
print(json.dumps(observed["real_clock"]))
# A precondition, not a result: the real-clock form needs consecutive appends that get
# one timestamp. Coarse clocks (native Windows) produce them; a microsecond clock may not.
observed["real_clock"]["platform_clock_produced_ties"] = tied_sessions > 0
if tied_sessions == 0:
    observed["real_clock"]["note"] = ("no two consecutive appends got the same timestamp on this platform's clock, so the "
                                      "real-clock form cannot show the defect here; the patched-clock probe above covers it")
    print(observed["real_clock"]["note"])
elif FIXED:
    c.check("real clock: 0 sessions read out of append order (list_messages, newest, build_history)",
            list_mismatch == newest_mismatch == history_mismatch == 0,
            {"list": list_mismatch, "newest": newest_mismatch, "history": history_mismatch})
else:
    c.check("control on 0.1.10: some tied sessions read out of append order", list_mismatch > 0,
            {"list": list_mismatch, "newest": newest_mismatch, "history": history_mismatch})

payload = {"banner": info, "summary": c.summary(), "all_pass": c.all_pass, "checks": c.items, "observed": observed}
write_result(OUT, payload)
print(json.dumps({"summary": c.summary(), "all_pass": c.all_pass}))
sys.exit(0 if c.all_pass else 1)
