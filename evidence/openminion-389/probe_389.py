"""Bootstrap memory fallback: does its owner close what the assembly creates? (#389)

The memory store, audit sink and artifact store that the runtime memory
assembly behind the bootstrap fallback creates are counted, and each owner is
closed twice:
- a direct `build_agent_memory_service` caller, after one `record_turn`;
- a gateway service that `build_gateway_service` builds without
  `agent_memory`, after one turn through `GatewayService.run_once` (a stub
  agent replies, so no LLM is involved);
- borrowed control: a gateway service given the fallback memory as
  `agent_memory`; after its two closes the memory's own close (if any) runs;
- SDK control: an echo SDK runtime, one turn, then the runtime's memory
  gateway and gateway service are closed (whatever close they have), then
  `runtime.close()` twice.
Counts are listed as [store, audit sink, artifact store] after each close.
For the fallback it also reports where its artifact store's index lives
relative to the runtime roots passed in.

With OPENMINION_TEST_POSTGRES_URL set it repeats the two owners with a
PostgreSQL memory store and reports engine disposals after each close and the
server connections with the run's application_name after the turn and after
the closes (5 s settle).

Run from an openminion source checkout: PYTHONPATH="src;." python probe_389.py
(PYTHONPATH=src:. on Linux/macOS)
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from openminion import Agent, APIRuntime
from openminion.base.channel import ChannelRegistry
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.artifact.control import ArtifactCtl
from openminion.modules.memory.storage.audit import SQLiteMemoryAuditSink
from openminion.modules.storage.runtime.idempotency_store import IdempotencyStore
from openminion.modules.storage.runtime.migrations import migrate_database
from openminion.modules.storage.runtime.session_store import SessionStore
from openminion.modules.storage.runtime.sqlite import connect_database
from openminion.services.runtime import bootstrap
from openminion.services.runtime import memory as runtime_memory
from tests._csc_fixtures import _csc_install_default_agent

TURN = {
    "session_id": "s1",
    "run_id": "r1",
    "request_id": "q1",
    "channel": "console",
    "target": "probe",
    "user_message": "Remember: the probe colour is green.",
    "assistant_message": "Noted.",
}


class ReplyAgent:
    async def run_turn(self, message, history=None, **_kwargs):
        return SimpleNamespace(
            text="Noted.", channel=message.channel, target=message.target, metadata={}
        )


class Recorder:
    def __init__(self, pg=None):
        self.assemblies, self.stores, self.sinks, self.closed = [], [], [], []
        self.pg = pg
        self._undo = []

    def install(self):
        build_assembly = runtime_memory.build_memory_v2_runtime_assembly
        resolve_backend = runtime_memory.resolve_memory_backend
        create_sink = runtime_memory.SQLiteMemoryAuditSink

        def record_assembly(**kwargs):
            assembly = build_assembly(**kwargs)
            self.assemblies.append(assembly)
            return assembly

        def record_backend(**kwargs):
            resolved = resolve_backend(**kwargs)
            self._count(type(resolved.store))
            self.stores.append(resolved.store)
            if self.pg is not None:
                from sqlalchemy import event

                event.listen(
                    resolved.store._engine,
                    "engine_disposed",
                    lambda engine: self.pg["disposals"].append(1),
                )
            return resolved

        def record_sink(db_path):
            sink = create_sink(db_path)
            self.sinks.append(sink)
            return sink

        self._count(SQLiteMemoryAuditSink)
        self._count(ArtifactCtl)
        for name, value in (
            ("build_memory_v2_runtime_assembly", record_assembly),
            ("resolve_memory_backend", record_backend),
            ("SQLiteMemoryAuditSink", record_sink),
        ):
            self._undo.append((runtime_memory, name, getattr(runtime_memory, name)))
            setattr(runtime_memory, name, value)

    def _count(self, cls):
        if any(target is cls for target, _, _ in self._undo):
            return
        original = cls.close
        self._undo.append((cls, "close", original))

        def close(obj, *args, **kwargs):
            self.closed.append(id(obj))
            return original(obj, *args, **kwargs)

        cls.close = close

    def counts(self):
        [assembly] = self.assemblies
        items = [*self.stores, *self.sinks, assembly.artifactctl]
        return [self.closed.count(id(item)) for item in items]

    def uninstall(self):
        for target, name, value in reversed(self._undo):
            setattr(target, name, value)
        for assembly in self.assemblies:
            for item in (*self.sinks, assembly.artifactctl):
                if item is not None and id(item) not in self.closed:
                    item.close()
        if self.pg is not None:
            for store in self.stores:
                store._engine.dispose()


def make_config():
    config = OpenMinionConfig()
    _csc_install_default_agent(config, name="main", provider="echo")
    config.runtime.memory_enabled = True
    config.runtime.memory_provider = "memory_v2"
    return config


def make_manager(root, config, memory_config):
    config_path = root / "agents.json"
    save_config(config, str(config_path))
    manager = ConfigManager.load(
        str(config_path), home_root=root, data_root=root / "data"
    )
    if memory_config is not None:
        manager.register("memory", lambda **kwargs: memory_config)
    return manager


def fallback_kwargs(root, config, manager):
    return {
        "config": config,
        "memory_root": root / "memory",
        "home_root": root,
        "data_root": root / "data",
        "config_manager": manager,
        "logger": logging.getLogger("probe_389"),
    }


def build_memory(root, config, manager):
    return bootstrap.build_agent_memory_service(
        agent_id="main",
        storage_path=root / "state" / "openminion.db",
        **fallback_kwargs(root, config, manager),
    )


def build_gateway(root, config, manager, connection, agent_memory=None):
    return bootstrap.build_gateway_service(
        agent_service=ReplyAgent(),
        profile_name="main",
        channels=ChannelRegistry([]),
        sessions=SessionStore(connection),
        idempotency=IdempotencyStore(connection),
        security_policy=None,
        channel_authenticity_policy=None,
        config_path=root / "agents.json",
        storage_path=root / "state" / "openminion.db",
        agent_memory=agent_memory,
        **fallback_kwargs(root, config, manager),
    )


def gateway_turn(gateway):
    response = asyncio.run(
        gateway.run_once(
            channel="console",
            target="probe",
            message=TURN["user_message"],
            session_id="s1",
            deliver=False,
        )
    )
    return response.body


def close_twice(recorder, closer):
    close = getattr(closer, "close", None)
    if not callable(close):
        return f"{type(closer).__name__} has no close()"
    per_close = []
    for _ in range(2):
        close()
        entry = {"closed [store, sink, artifact]": recorder.counts()}
        if recorder.pg is not None:
            entry["engine_disposals"] = len(recorder.pg["disposals"])
        per_close.append(entry)
    return per_close


def artifact_write(recorder):
    try:
        recorder.assemblies[0].artifactctl.ingest_bytes(b"after close")
        return "accepted"
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def relative_to_probe_dir(path, root):
    resolved, base = Path(path).resolve(), root.resolve().parent
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return str(resolved)


def compact(text):
    """Print flat lists on one line."""
    import re

    return re.sub(
        r"\[\s+([^\[\]{}]*?)\s+\]",
        lambda m: "[" + ", ".join(v.strip() for v in m.group(1).split(",")) + "]",
        text,
    )


def run_owner(root, owner, memory_config=None, pg=None):
    root.mkdir(parents=True)
    config = make_config()
    manager = make_manager(root, config, memory_config)
    database = root / "state" / "openminion.db"
    migrate_database(database)
    connection = connect_database(database)
    recorder = Recorder(pg)
    recorder.install()
    row = {}
    try:
        if owner == "direct build_agent_memory_service caller":
            closer = build_memory(root, config, manager)
            closer.record_turn(**TURN)
        elif owner == "gateway service built without agent_memory":
            closer = build_gateway(root, config, manager, connection)
            row["turn_reply"] = gateway_turn(closer)
        else:
            memory = build_memory(root, config, manager)
            closer = build_gateway(root, config, manager, connection, memory)
            row["turn_reply"] = gateway_turn(closer)
        row["stores_created"] = [type(store).__name__ for store in recorder.stores]
        row["audit_sink_open_after_turn"] = [s._conn is not None for s in recorder.sinks]
        if pg is not None:
            row["server_connections_after_turn"] = pg["count"]()
        row["after_each_close"] = close_twice(recorder, closer)
        if owner.startswith("borrowed"):
            row["then_the_memory_own_close"] = close_twice(recorder, memory)
        row["audit_sink_open_after_close"] = [s._conn is not None for s in recorder.sinks]
        row["artifact_store_write_after_close"] = artifact_write(recorder)
        if pg is not None:
            row["server_connections_after_close"] = pg["settled"]()
        else:
            index = recorder.assemblies[0].artifactctl.config.index.sqlite_path
            row["runtime_roots"] = relative_to_probe_dir(root, root)
            row["fallback_artifact_index"] = relative_to_probe_dir(index, root)
        return row
    finally:
        recorder.uninstall()
        connection.close()


def run_sdk(root):
    root.mkdir(parents=True)
    config = make_config()
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(root / ".openminion" / "state.db")
    save_config(config, str(root / "agents.json"))
    recorder = Recorder()
    recorder.install()
    runtime = None
    try:
        runtime = APIRuntime.from_config_path(
            str(root / "agents.json"), home_root=root, data_root=root / ".openminion"
        )
        with Agent(runtime=runtime) as agent:
            run_state = agent.run("hello").run_state
        [assembly] = recorder.assemblies
        gateway = assembly.gateway
        row = {
            "turn_run_state": run_state,
            "runtime_gateway_service_uses_this_memory_gateway": (
                runtime.gateway._agent_memory is gateway
            ),
            "memory_gateway_close_owner": repr(getattr(gateway, "close_owner", None)),
            "memory_gateway_has_close": callable(getattr(gateway, "close", None)),
        }
        getattr(gateway, "close", lambda: None)()
        runtime.gateway.close()
        row["after_memory_gateway_and_gateway_service_close"] = recorder.counts()
        per_close = []
        for _ in range(2):
            runtime.close()
            per_close.append(recorder.counts())
        row["after_each_runtime_close"] = per_close
        return row
    finally:
        if runtime is not None:
            runtime.close()
        recorder.uninstall()


def postgres_rows(base, tmp):
    from urllib.parse import quote_plus

    import sqlalchemy as sa

    rows = {}
    admin = sa.create_engine(base, future=True)
    try:
        for owner in (
            "direct build_agent_memory_service caller",
            "gateway service built without agent_memory",
        ):
            token = uuid.uuid4().hex[:12]
            schema, app = f"probe_389_{token}", f"probe_389_{token}"
            url = (
                f"{base}?options={quote_plus(f'-csearch_path={schema}')}"
                f"&application_name={app}"
            )
            with admin.begin() as conn:
                conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

            def count(app=app):
                with admin.connect() as conn:
                    return conn.execute(
                        sa.text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE application_name = :n"
                        ),
                        {"n": app},
                    ).scalar_one()

            def settled(count=count):
                deadline = time.monotonic() + 5.0
                value = count()
                while value and time.monotonic() < deadline:
                    time.sleep(0.05)
                    value = count()
                return value

            pg = {"disposals": [], "count": count, "settled": settled}
            memory_config = {"store": {"backend": "postgres", "postgres": {"url": url}}}
            try:
                rows[f"{owner}, PostgreSQL memory store"] = run_owner(
                    tmp / f"pg-{token}", owner, memory_config, pg
                )
            finally:
                with admin.begin() as conn:
                    conn.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        admin.dispose()
    return rows


def main():
    logging.disable(logging.WARNING)
    rows = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        os.environ["OPENMINION_HOME"] = str(tmp / "env-home")
        os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "env-home" / "data")
        for index, owner in enumerate(
            (
                "direct build_agent_memory_service caller",
                "gateway service built without agent_memory",
                "borrowed: gateway service given the fallback memory",
            )
        ):
            rows[f"{owner}, SQLite memory store"] = run_owner(
                tmp / f"sqlite-{index}", owner
            )
        rows["SDK control: echo runtime"] = run_sdk(tmp / "sdk")
        base = os.environ.get("OPENMINION_TEST_POSTGRES_URL")
        if base:
            rows.update(postgres_rows(base, tmp))
    print(compact(json.dumps(rows, indent=2)))
    print(sys.version.split()[0], sys.platform)


if __name__ == "__main__":
    main()
