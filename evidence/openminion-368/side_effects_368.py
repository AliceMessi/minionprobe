"""Issue #368 side checks.

1. The brain memory factory (create_memory_adapter) already passes the store
   from resolve_memory_backend with owns_store=True. Does closing its adapter
   twice work for the mock and remote backends?
2. Can a parsed memory config (MemctlConfig) select the remote backend, or
   does resolve_memory_backend reject it before any store exists?

Run from an openminion checkout: PYTHONPATH=src python side_effects_368.py
"""

import dataclasses
import json
import tempfile
from pathlib import Path

from openminion.base.config import OpenMinionConfig
from openminion.modules.brain.adapters.factory.memory import create_memory_adapter
from openminion.modules.memory.config import from_base_config
from openminion.modules.memory.storage.factory import resolve_memory_backend


def outcome(action):
    try:
        action()
        return "ok"
    except Exception as exc:  # noqa: BLE001 - the probe reports it
        return f"{type(exc).__name__}: {exc}"


def main():
    result = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        for name, config in {
            "mock": {"store": {"backend": "mock"}},
            "remote": {
                "store": {
                    "backend": "remote",
                    "remote": {"endpoint": "http://127.0.0.1:9"},
                }
            },
        }.items():
            adapter = create_memory_adapter(
                mode="auto",
                db_path=root / "memory.db",
                config=config,
                artifactctl=object(),
            )
            result[f"brain factory {name}: first close"] = outcome(adapter.close)
            result[f"brain factory {name}: second close"] = outcome(adapter.close)
        base = from_base_config(
            base_config=OpenMinionConfig(), home_root=root, data_root=root
        )
        parsed = dataclasses.replace(
            base,
            store=dataclasses.replace(
                base.store, backend="remote", remote_endpoint="http://127.0.0.1:9"
            ),
        )
        result["parsed MemctlConfig with backend remote"] = outcome(
            lambda: resolve_memory_backend(config=parsed, db_path=root / "m.db")
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
