"""Remove one session-row lock call from participants.py (runner ablation only).

join:  the call in upsert_participant_record (allocation without the lock)
leave: the call in remove_participant (leave takes the participant row first)
"""

import pathlib
import sys

NL = chr(10)
path = pathlib.Path("src/openminion/modules/storage/runtime/session_store/participants.py")
text = path.read_text(encoding="utf-8")
call = "            self._lock_session_row(session_id)" + NL
if sys.argv[1] == "join":
    anchor = call + "            # A new or returning participant"
    replacement = "            # A new or returning participant"
else:
    anchor = call + "            updated = self._backend.execute_count("
    replacement = "            updated = self._backend.execute_count("
assert text.count(anchor) == 1, sys.argv[1]
path.write_text(text.replace(anchor, replacement), encoding="utf-8")
print("ablated:", sys.argv[1])
