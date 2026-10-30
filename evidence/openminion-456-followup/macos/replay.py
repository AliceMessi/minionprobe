"""Replay a focus_dump raw PTY transcript through pyte with the recorded resizes.

Usage: python -I -X utf8 replay.py <dump.json> <dump.raw.txt> [extra offsets...]
Prints the occupied rows and the cursor after each resize's read and at each
extra offset, the way the harness's _occupied_rows/_assert_inline_composer see them.
"""
import json
import sys

import pyte

meta = json.load(open(sys.argv[1], encoding="utf-8"))
raw = open(sys.argv[2], encoding="utf-8", newline="").read()
extra = sorted(int(x) for x in sys.argv[3:])
screen = pyte.Screen(100, 24)
stream = pyte.Stream(screen)
pos = 0


def state(label):
    rows = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        rows.append("".join(line[x].data for x in range(screen.columns) if line[x].data).rstrip())
    last = max((i for i, r in enumerate(rows) if r), default=-1)
    occ = rows[: last + 1]
    cur = screen.cursor.y + 1
    ok = len(occ) >= 2 and occ[-1].startswith("◆ ") and occ[-2].startswith("❯ ") and cur == len(occ) - 1
    print(f"{label}: offset={pos} size={screen.lines}x{screen.columns} occupied={len(occ)} cursor_row={cur} flow_layout_ok={ok}")
    for i, r in enumerate(occ):
        print(f"   {i + 1:02d}|{r[:100]}")


def feed_to(offset):
    global pos
    stream.feed(raw[pos:offset])
    pos = offset


resizes = [a for a in meta["actions"] if a["op"] == "resize"]
# The actions list mixes sessions of the whole test module run only when several sessions
# share one id; the resize test has one session, so take its resize entries in order.
points = []
for a in resizes:
    points.append(("resize", a))
    points.append(("read", a))
for kind, a in points:
    if kind == "resize":
        feed_to(a["raw_len"])
        state(f"before resize to {a['rows']}x{a['cols']}")
        screen.resize(lines=a["rows"], columns=a["cols"])
    else:
        feed_to(a["raw_len_after_0.1s_read"])
        state(f"after resize to {a['rows']}x{a['cols']} and its 0.1 s read")
for off in extra:
    feed_to(off)
    state("extra offset")
feed_to(len(raw))
state("end of recorded output")
