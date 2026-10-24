"""Issue #368 live PostgreSQL probe: an SDK runtime whose memory store is
PostgreSQL runs one echo-provider turn and is closed twice. Reports how many
times the memory engine is disposed after each close, the call stack of each
disposal, and the server connections the engine holds before and after.

Needs OPENMINION_TEST_POSTGRES_URL and the postgres extra. Run from an
openminion checkout: PYTHONPATH=src:. python pg_close_chain_368.py
"""

import dataclasses
import json
import os
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import quote_plus

import sqlalchemy as sa
from sqlalchemy import event

from openminion import Agent, APIRuntime
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.memory.config import from_base_config
from openminion.services.runtime import memory as runtime_memory
from tests._csc_fixtures import _csc_install_default_agent


def openminion_frames():
    frames = []
    for frame in traceback.extract_stack()[:-2]:
        parts = Path(frame.filename).parts
        if "src" not in parts:
            continue
        relative = parts[len(parts) - parts[::-1].index("src") :]
        if relative[:1] == ("openminion",):
            frames.append(f"{'/'.join(relative)}:{frame.lineno} {frame.name}")
    return frames


def main():
    base_url = os.environ["OPENMINION_TEST_POSTGRES_URL"]
    token = uuid.uuid4().hex[:12]
    schema_name = f"probe368_{token}"
    application_name = f"probe368_{token}"
    options = quote_plus(f"-csearch_path={schema_name}")
    memory_url = f"{base_url}?options={options}&application_name={application_name}"
    admin = sa.create_engine(base_url, future=True)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema_name}"'))

    def server_connections():
        with admin.connect() as conn:
            return conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE application_name = :name"
                ),
                {"name": application_name},
            ).scalar_one()

    stores = []
    disposals = []
    resolve_backend = runtime_memory.resolve_memory_backend

    def record_backend(**kwargs):
        resolved = resolve_backend(**kwargs)
        stores.append(resolved.store)
        event.listen(
            resolved.store._engine,
            "engine_disposed",
            lambda engine: disposals.append(openminion_frames()),
        )
        return resolved

    runtime_memory.resolve_memory_backend = record_backend
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            config = OpenMinionConfig()
            _csc_install_default_agent(config, provider="echo")
            config.runtime.process_mode = "single-process"
            config.runtime.daemon_auto_start = False
            config.storage.path = str(root / ".openminion" / "state.db")
            config_path = root / "agents.json"
            save_config(config, str(config_path))
            manager = ConfigManager.load(
                str(config_path), home_root=root, data_root=root / ".openminion"
            )

            def postgres_memory_config(**kwargs):
                memory_config = from_base_config(**kwargs)
                store = dataclasses.replace(
                    memory_config.store, backend="postgres", postgres_url=memory_url
                )
                return dataclasses.replace(memory_config, store=store)

            manager.register("memory", postgres_memory_config)
            runtime = APIRuntime.from_manager(manager)
            with Agent(runtime=runtime) as agent:
                run_state = agent.run("hello").run_state
            before = server_connections()
            dispose_counts = []
            for _ in range(2):
                runtime.close()
                dispose_counts.append(len(disposals))
            deadline = time.monotonic() + 5.0
            after = server_connections()
            while after and time.monotonic() < deadline:
                time.sleep(0.05)
                after = server_connections()
            result = {
                "run_state": run_state,
                "memory_stores": [type(store).__name__ for store in stores],
                "server_connections_after_turn": before,
                "dispose_calls_after_each_runtime_close": dispose_counts,
                "server_connections_after_close": after,
                "dispose_stacks": list(disposals),
            }
            for store in stores:
                store._engine.dispose()
    finally:
        runtime_memory.resolve_memory_backend = resolve_backend
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin.dispose()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
