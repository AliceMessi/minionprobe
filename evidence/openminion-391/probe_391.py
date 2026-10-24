"""#391: can a parsed (typed) memory config select the `remote` store backend?

Same setup as probe_remote_config.py from the #391 report
(evidence/openminion-gaps-380): a memctl YAML config with `store.backend: remote`
and `store.remote.{endpoint,timeout_seconds,max_retries}`, parsed by
`load_config`, then passed to `resolve_memory_backend` as the whole
`MemctlConfig` and as `cfg.store` (what the memory CLI passes), with the
equivalent dict config as control. A second config sets `max_retries: 0`.

The CLI row runs `memctl list` in a subprocess with MEMCTL_CONFIG pointing at
the config. The endpoint is a local HTTP server on 127.0.0.1 that this probe
starts: it records each request and answers with an empty record list. Nothing
else is contacted; resolving a backend only builds the transport.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_391.py
(PYTHONPATH=src:. on Linux/macOS)
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

import yaml

from openminion.modules.memory.config import load_config
from openminion.modules.memory.storage.factory import resolve_memory_backend

REQUESTS = []


class Recorder(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        REQUESTS.append({"path": self.path, "operation": json.loads(body)["operation"]})
        reply = json.dumps({"ok": True, "data": {"records": []}}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


def outcome(action):
    try:
        resolved = action()
        t = resolved.store._transport
        return (
            f"ok: {type(resolved.store).__name__}, transport endpoint={t._endpoint!r} "
            f"timeout_seconds={t._timeout_seconds} max_retries={t._max_retries}"
        )
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def write_config(path, remote):
    memctl = {
        "store": {"backend": "remote", "remote": remote},
        "defaults": {"confidence": {"user_said": 0.7}},
        "promotion": {},
        "retrieval": {},
        "retention": {},
    }
    path.write_text(yaml.safe_dump({"version": 1, "memctl": memctl}), encoding="utf-8")
    return path


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/memory"
    remote = {"endpoint": endpoint, "timeout_seconds": 2.5, "max_retries": 2}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        env = {"HOME": str(tmp), "USERPROFILE": str(tmp)}
        path = write_config(tmp / "memctl.yaml", remote)
        parsed = load_config(path, env=env)
        zero = load_config(
            write_config(tmp / "zero.yaml", {**remote, "max_retries": 0}), env=env
        )
        db_path = tmp / "unused.db"
        result = {
            "resolve_memory_backend(config=<MemctlConfig>)": outcome(
                lambda: resolve_memory_backend(config=parsed, db_path=db_path)
            ),
            "resolve_memory_backend(config=<MemctlConfig>.store)": outcome(
                lambda: resolve_memory_backend(config=parsed.store, db_path=db_path)
            ),
            "resolve_memory_backend(config=<MemctlConfig with max_retries: 0>.store)": outcome(
                lambda: resolve_memory_backend(config=zero.store, db_path=db_path)
            ),
            "control: resolve_memory_backend(config=<dict with the same values>)": outcome(
                lambda: resolve_memory_backend(
                    config={"store": {"backend": "remote", "remote": remote}},
                    db_path=db_path,
                )
            ),
        }
        cli_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OPENMINION_", "MEMCTL_"))
        }
        cli_env.update(env, MEMCTL_CONFIG=str(path), NO_PROXY="127.0.0.1,localhost")
        proc = subprocess.run(
            [sys.executable, "-m", "openminion.modules.memory", "list", "--scope", "agent:alpha", "--json"],
            capture_output=True, text=True, env=cli_env, cwd=os.getcwd(), timeout=300,
        )
        stderr = re.sub(r"\x1b\[[0-9;]*m", "", proc.stderr)
        errors = [line.strip() for line in stderr.splitlines() if "Error" in line]
        result["memctl list --scope agent:alpha --json (MEMCTL_CONFIG=memctl.yaml)"] = {
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "last_error_line": errors[-1] if errors else "",
            "requests_received": REQUESTS,
        }
    server.shutdown()
    print(json.dumps(result, indent=2).replace(endpoint, "http://127.0.0.1:<port>/memory"))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
