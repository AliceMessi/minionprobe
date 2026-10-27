"""Runner wrapper: run probe_storage_exit.py in isolated mode from an empty
directory, then a pool-stats success control against the job's PostgreSQL
service (when OPENMINION_TEST_POSTGRES_URL is set). The URL is never printed."""

import json
import os
import subprocess
import sys
import tempfile

base = tempfile.mkdtemp(prefix="om-exit-")
cwd = tempfile.mkdtemp(prefix="om-exit-cwd-")
probe = "/tmp/ev/probe_storage_exit.py"
code = subprocess.call(
    [sys.executable, "-I", "-X", "utf8", probe, "--label", "runner",
     "--work", base + "/w", "--out", base + "/result.json"],
    cwd=cwd,
)
print(f"probe exit: {code}")

url = os.environ.get("OPENMINION_TEST_POSTGRES_URL", "")
if not url:
    print("pool-stats PostgreSQL control: OPENMINION_TEST_POSTGRES_URL not set, skipped")
    sys.exit(code)
env = dict(os.environ)
env.pop("OPENMINION_STORAGE_POSTGRES_URL", None)
env["OPENMINION_HOME"] = base + "/pg-home"
env["OPENMINION_DATA_ROOT"] = base + "/pg-home/.openminion"
print()
print("[pg] pool-stats --postgres-url <OPENMINION_TEST_POSTGRES_URL> (success control)")
for module in ("openminion.modules.storage", "openminion.modules.storage.cli"):
    done = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-m", module, "pool-stats", "--postgres-url", url],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=300,
    )
    out = done.stdout.replace(url, "<PG_URL>")
    try:
        payload = json.loads(out)
        summary = f"ok={payload.get('ok')!r} pool keys={sorted((payload.get('pool') or {}).keys())}"
    except ValueError:
        summary = "stdout is not JSON: " + out[:200]
    err = done.stderr.replace(url, "<PG_URL>").strip().splitlines()
    print(f"    python -m {module:<31} -> exit {done.returncode}; {summary}; stderr lines={len(err)}")
    if err:
        print("    stderr last line: " + err[-1][:300])
sys.exit(code)
