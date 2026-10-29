"""Build the dispatched command: write the probe to /tmp/probe, then run runner-gates.sh.

python -I build_dispatch.py EVIDENCE_DIR OUT_DIR  (then: run.py ... -- "$(cat OUT_DIR/dispatched.cmd)")
"""
import base64, gzip, pathlib, sys

E = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
probe = (E / "probes" / "probe_selection_leave_race.py").read_bytes()
gates = (E / "linux" / "runner-gates.sh").read_bytes().replace(b"\r\n", b"\n")
blob = base64.b64encode(gzip.compress(probe, mtime=0)).decode("ascii")
pre = (
    "#!/bin/bash\nmkdir -p /tmp/probe\n"
    f"echo {blob} | base64 -d | gunzip > /tmp/probe/probe_selection_leave_race.py\n"
).encode()
script = pre + gates
(out / "dispatched.sh").write_bytes(script)
payload = base64.b64encode(gzip.compress(script, mtime=0)).decode("ascii")
cmd = f"echo {payload} | base64 -d | gunzip > /tmp/run.sh && bash /tmp/run.sh"
(out / "dispatched.cmd").write_text(cmd, encoding="ascii")
print(len(script), len(cmd))
