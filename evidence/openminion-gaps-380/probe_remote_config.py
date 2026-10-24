"""Remote memory backend: can a parsed (typed) memory config select it?

A memctl YAML config selects `store.backend: remote` with `store.remote.endpoint`.
The probe parses it with `load_config` (the parser accepts it and fills
`StoreConfig.remote_endpoint`), then passes the parsed config to
`resolve_memory_backend` in the two typed shapes the code uses: the whole
`MemctlConfig`, and `cfg.store` as the memory CLI's `_get_service` does. It
also runs `memctl list` (in a subprocess) against that config file. The
control passes the same values as a dict config. Nothing contacts the
endpoint: resolving the backend only builds the transport.

Run from an openminion source checkout: PYTHONPATH="src;." python probe_remote_config.py
(PYTHONPATH=src:. on Linux/macOS)
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from openminion.modules.memory.config import load_config
from openminion.modules.memory.storage.factory import resolve_memory_backend

ENDPOINT = "http://127.0.0.1:9/memory"  # never contacted
REMOTE = {"endpoint": ENDPOINT, "timeout_seconds": 2.5, "max_retries": 2}


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


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        path = tmp / "memctl.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "version": 1,
                    "memctl": {
                        "store": {
                            "backend": "remote",
                            "remote": REMOTE,
                        },
                        "defaults": {"confidence": {"user_said": 0.7}},
                        "promotion": {"require_approval_for_global": True},
                        "retrieval": {"max_results": 25},
                        "retention": {"enable_soft_delete": True},
                    },
                }
            ),
            encoding="utf-8",
        )
        env = {"HOME": str(tmp), "USERPROFILE": str(tmp)}
        parsed = load_config(path, env=env)
        db_path = tmp / "unused.db"
        result = {
            "load_config(memctl.yaml)": {
                "store.backend": parsed.store.backend,
                "store.remote_endpoint": parsed.store.remote_endpoint,
                "store.remote_timeout_seconds": parsed.store.remote_timeout_seconds,
                "store.remote_max_retries": parsed.store.remote_max_retries,
            },
            "resolve_memory_backend(config=<MemctlConfig>)": outcome(
                lambda: resolve_memory_backend(config=parsed, db_path=db_path)
            ),
            "resolve_memory_backend(config=<MemctlConfig>.store)": outcome(
                lambda: resolve_memory_backend(config=parsed.store, db_path=db_path)
            ),
            "control: resolve_memory_backend(config=<dict with the same values>)": outcome(
                lambda: resolve_memory_backend(
                    config={"store": {"backend": "remote", "remote": REMOTE}},
                    db_path=db_path,
                )
            ),
        }
        cli_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OPENMINION_", "MEMCTL_"))
        }
        cli_env.update(env, MEMCTL_CONFIG=str(path))
        proc = subprocess.run(
            [sys.executable, "-m", "openminion.modules.memory", "list", "--scope", "agent:alpha", "--json"],
            capture_output=True, text=True, env=cli_env, cwd=os.getcwd(), timeout=300,
        )
        stderr = re.sub(r"\x1b\[[0-9;]*m", "", proc.stderr)
        errors = [line for line in stderr.splitlines() if "Error" in line]
        result["memctl list --scope agent:alpha --json (MEMCTL_CONFIG=memctl.yaml)"] = {
            "exit_code": proc.returncode,
            "last_error_line": errors[-1].strip() if errors else stderr.strip()[-300:],
        }
    print(json.dumps(result, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
