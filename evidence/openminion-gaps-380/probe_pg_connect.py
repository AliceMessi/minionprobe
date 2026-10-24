"""Runtime memory session lifecycle with a PostgreSQL memory store.

An SDK runtime (echo provider, explicit roots) runs two turns in one session,
once with the default SQLite memory store (control) and once with a
PostgreSQL memory store selected through the `memory` config factory, as
tests/integration/test_runtime_memory_postgres.py does. The probe records each
call the session lifecycle makes to the four retention helpers it runs
(apply_confidence_decay, compress_old_summaries, enforce_scope_capacity,
evict_stale_insights), the `memory.session_lifecycle failed` warnings, and the
store type the helpers receive. On the PostgreSQL run it then calls
apply_confidence_decay directly on the store the lifecycle passes and on the
PostgresMemoryStore inside it.

The PostgreSQL run needs OPENMINION_TEST_POSTGRES_URL and the postgres extra.
Run from an openminion source checkout: PYTHONPATH="src;." python probe_pg_connect.py
(PYTHONPATH=src:. on Linux/macOS)
"""

import dataclasses
import json
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path

from openminion import Agent, APIRuntime
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.memory.config import from_base_config
from openminion.modules.memory.runtime import session_lifecycle
from openminion.modules.memory.runtime.gc import apply_confidence_decay
from tests._csc_fixtures import _csc_install_default_agent

HELPERS = (
    "apply_confidence_decay",
    "compress_old_summaries_gc",
    "enforce_scope_capacity",
    "evict_stale_insights",
)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []
        self.seen = set()

    def emit(self, record):
        if id(record) in self.seen:
            return
        self.seen.add(id(record))
        message = record.getMessage()
        if message.startswith("memory.session_lifecycle"):
            self.messages.append(message)


def run_case(root, postgres_url=None):
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    config_path = root / "agents.json"
    save_config(config, str(config_path))
    manager = ConfigManager.load(str(config_path), home_root=root, data_root=root / ".openminion")
    if postgres_url:
        def postgres_memory_config(**kwargs):
            memory_config = from_base_config(**kwargs)
            store = dataclasses.replace(memory_config.store, backend="postgres", postgres_url=postgres_url)
            return dataclasses.replace(memory_config, store=store)

        manager.register("memory", postgres_memory_config)

    calls = []
    originals = {name: getattr(session_lifecycle, name) for name in HELPERS}

    def recording(name, original):
        def helper(store, **kwargs):
            caller = sys._getframe(1).f_code.co_name
            entry = {"caller": caller, "helper": name, "store": type(store).__name__}
            try:
                result = original(store, **kwargs)
                entry["result"] = "ok"
                return result
            except Exception as exc:
                entry["result"] = f"raised {type(exc).__name__}: {exc}"
                raise
            finally:
                calls.append(entry)
        return helper

    for name, original in originals.items():
        setattr(session_lifecycle, name, recording(name, original))
    capture = Capture()
    loggers = (logging.getLogger(), logging.getLogger("openminion"))
    for logger in loggers:
        logger.addHandler(capture)
    try:
        runtime = APIRuntime.from_manager(manager)
        try:
            run_states = []
            with Agent(runtime=runtime) as agent:
                for _ in range(2):
                    run_states.append(agent.run("hello").run_state)
            [assembly] = list(runtime._memory_assemblies.values())
            store = assembly.service._store
            row = {
                "run_states": run_states,
                "store_passed_to_helpers": f"{type(store).__name__} wrapping {type(store._store).__name__}",
                "helper_calls": calls,
                "session_lifecycle_warnings": len(capture.messages),
                "distinct_session_ids_in_warnings": len(
                    {m.split(" session_id=", 1)[1].split(" ", 1)[0] for m in capture.messages}
                ),
                "first_warning": capture.messages[0].split(" agent_id=")[0]
                + " ... error=" + capture.messages[0].split(" error=", 1)[1]
                if capture.messages else None,
            }
            if postgres_url:
                direct = {}
                for label, target in (("audited store (what the lifecycle passes)", store),
                                      ("inner PostgresMemoryStore", store._store)):
                    try:
                        direct[label] = f"ok {apply_confidence_decay(target, interval_days=7, decay_rate=0.05, min_confidence=0.3)}"
                    except Exception as exc:
                        direct[label] = f"{type(exc).__name__}: {exc}"
                row["apply_confidence_decay_called_directly"] = direct
            return row
        finally:
            runtime.close()
    finally:
        for logger in loggers:
            logger.removeHandler(capture)
        for name, original in originals.items():
            setattr(session_lifecycle, name, original)


def main():
    logging.disable(logging.INFO)
    rows = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        os.environ["OPENMINION_HOME"] = str(tmp / "home")
        os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "home" / "data")
        rows["SQLite memory store (control)"] = run_case(tmp / "sqlite")
        base = os.environ.get("OPENMINION_TEST_POSTGRES_URL")
        if base:
            import sqlalchemy as sa
            from urllib.parse import quote_plus

            schema = f"probe_pgc_{uuid.uuid4().hex[:12]}"
            admin = sa.create_engine(base, future=True)
            with admin.begin() as conn:
                conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            try:
                url = f"{base}?options={quote_plus(f'-csearch_path={schema}')}"
                rows["PostgreSQL memory store"] = run_case(tmp / "pg", url)
            finally:
                with admin.begin() as conn:
                    conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                admin.dispose()
        else:
            rows["PostgreSQL memory store"] = "skipped: OPENMINION_TEST_POSTGRES_URL not set"
    print(json.dumps(rows, indent=2))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
