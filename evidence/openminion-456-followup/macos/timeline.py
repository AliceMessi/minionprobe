"""Print the resize and PTY-read timeline of a focus_dump JSON (batch 2 plugin)."""
import glob
import json
import sys

f = sys.argv[1]
j = json.load(open(f, encoding="utf-8"))
raw = open(f.replace(".json", ".raw.txt"), encoding="utf-8", newline="").read()
acts = sorted([a for a in j["actions"] if a["op"] in ("resize", "chunk")], key=lambda a: a["t"])
t0 = min(a["t"] for a in acts if a["op"] == "resize")
for a in acts:
    if a["op"] == "resize":
        print(f"t={a['t'] - t0:7.3f} RESIZE {a['rows']}x{a['cols']} raw_len={a['raw_len']} after_0.1s_read={a['raw_len_after_0.1s_read']}")
    else:
        s = a["start"]
        print(f"t={a['t'] - t0:7.3f} read at_char={s} bytes={a['bytes']} begins={ascii(raw[s:s + 24])}")
print("end of recorded output:", len(raw), "| exception:", (j.get("exception") or "none")[:60])
