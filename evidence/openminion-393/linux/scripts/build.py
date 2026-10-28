"""Assemble a runner script from parts and encode it as a one-line command for run.py.

python build.py NAME head.sh [part.sh ...] [--var K=V ...] [--file /tmp/ev/x.py=local/path ...]
Variables come first, then head.sh, then the shipped files, then the other parts.
Writes out/NAME.sh and out/NAME.cmd.
"""
import base64, gzip, pathlib, sys

SP = pathlib.Path(__file__).resolve().parent
args = sys.argv[1:]
name, parts, vars_, files = args[0], [], [], []
i = 1
while i < len(args):
    if args[i] == "--var":
        vars_.append(args[i + 1]); i += 2
    elif args[i] == "--file":
        files.append(args[i + 1]); i += 2
    else:
        parts.append(args[i]); i += 1


def read(path):
    data = pathlib.Path(path).read_bytes().replace(b"\r\n", b"\n")
    return data if data.endswith(b"\n") else data + b"\n"


script = b"#!/bin/bash\nset -u\n"
for spec in vars_:
    k, v = spec.split("=", 1)
    script += f'{k}="{v}"\n'.encode()
script += read(SP / parts[0])
for spec in files:
    target, local = spec.split("=", 1)
    raw = pathlib.Path(local).read_bytes()  # byte for byte, so the sha256 matches the evidence copy
    script += f"cat > {target} <<'PYEOF_X'\n".encode() + raw + (b"" if raw.endswith(b"\n") else b"\n") + b"PYEOF_X\n"
for part in parts[1:]:
    script += read(SP / part)
(SP / "out").mkdir(exist_ok=True)
(SP / "out" / f"{name}.sh").write_bytes(script)
payload = base64.b64encode(gzip.compress(script, mtime=0)).decode("ascii")
cmd = f"echo {payload} | base64 -d | gunzip > /tmp/run.sh && bash /tmp/run.sh"
(SP / "out" / f"{name}.cmd").write_text(cmd, encoding="ascii")
print(name, len(script), len(cmd))
