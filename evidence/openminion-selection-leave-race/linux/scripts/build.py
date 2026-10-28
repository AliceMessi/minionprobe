"""Assemble a runner script (variables, head.sh, a job part) and encode it as one command for run.py.

python build.py NAME PART.sh BASE=<sha>  ->  out/NAME.sh and out/NAME.cmd
"""
import base64, gzip, pathlib, sys

SP = pathlib.Path(__file__).resolve().parent
name, part, var = sys.argv[1], sys.argv[2], sys.argv[3]
k, v = var.split("=", 1)


def read(path):
    data = pathlib.Path(path).read_bytes().replace(b"\r\n", b"\n")
    return data if data.endswith(b"\n") else data + b"\n"


script = b"#!/bin/bash\nset -u\n" + f'{k}="{v}"\n'.encode() + read(SP / "head.sh") + read(SP / part)
out = SP.parent / "out"
out.mkdir(exist_ok=True)
(out / f"{name}.sh").write_bytes(script)
payload = base64.b64encode(gzip.compress(script, mtime=0)).decode("ascii")
cmd = f"echo {payload} | base64 -d | gunzip > /tmp/run.sh && bash /tmp/run.sh"
(out / f"{name}.cmd").write_text(cmd, encoding="ascii")
print(name, len(script), len(cmd))
