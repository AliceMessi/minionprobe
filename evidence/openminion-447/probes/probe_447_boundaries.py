"""Failed APIRuntime starts at each ownership boundary: what is still open? (#447)

    python -I -X utf8 probe_447_boundaries.py <case> <new empty dir> [<PostgreSQL URL of a new empty database>]

Runs one case in this process, on an installed openminion, with automatic garbage
collection off from before openminion is imported. It starts APIRuntime with one
offline echo agent and SQLite runtime storage (PostgreSQL with a URL), and prints
one JSON line:

- start: "ok", or the exception that reached the caller; original_exception: whether
  it is the injected instance (or the expected ConfigError / MigrationError);
- closes: how often each counted owner's close ran, and the most times one object
  was closed (max_per_object);
- at_exception: right after the exception (or after close() for the control), with
  no garbage collection: open files under the root, the databases among them, on
  Windows the files under the data root that cannot be renamed, the start threads
  still alive (runtime manager, pool-health emitter), and on PostgreSQL the other
  connections to the database;
- after_gc: the same after gc.collect().

Cases (the step that fails):
  control                  none: the start succeeds, then runtime.close()
  memory-provider          OPENMINION_MEMORY_PROVIDER=not-a-provider (ConfigError, not injected)
  sessions-table           the database already has a sessions table (MigrationError at v1, not injected)
  engine-emitter           the pool-health emitter start inside StorageEngine.from_config
  storage-migration        the runtime migration inside build_runtime_storage
  extensions               the extension runtime build
  ops                      the ops service, after the action policy exists
  support-authoring        tool authoring, after the memory assembly started
  after-support            the channel authenticity policy, after the support part
  gateway                  the gateway build, after the agent exists
  connection               the SQLite connection read in the runtime constructor's arguments (SQLite)
  post-init                APIRuntime.__post_init__ (initialize_runtime_components)
  runtime-manager          build_runtime_manager, after the runtime object exists
  bind                     the runtime handle bind, after the runtime manager started
  close-fails              like connection, and RuntimeStorageContext.close raises after closing
"""

import gc

gc.disable()

import json
import os
import sys
import threading
from collections import Counter
from pathlib import Path

import psutil

CASE = sys.argv[1]
root = Path(sys.argv[2]).resolve()
pg_url = sys.argv[3] if len(sys.argv) > 3 else None
root.mkdir(parents=True, exist_ok=True)
for key in ("OPENMINION_HOME", "OPENMINION_DATA_ROOT", "OPENMINION_GENERATED_ROOT",
            "OPENMINION_CONFIG_PATH", "OPENMINION_STORAGE_BACKEND",
            "OPENMINION_STORAGE_POSTGRES_URL", "OPENMINION_MEMORY_PROVIDER"):
    os.environ.pop(key, None)
data_root = root / ".openminion"
storage = {"path": str(data_root / "state.db")}
if pg_url:
    storage.update(backend="postgres", postgres_url=pg_url)
(root / "agents.json").write_text(
    json.dumps(
        {
            "default_agent": "first",
            "agents": {"first": {"provider": "echo", "default_channel": "console"}},
            "runtime": {"process_mode": "single-process", "daemon_auto_start": False},
            "storage": storage,
        }
    ),
    encoding="utf-8",
)
START_THREADS = ("runtimectl-", "storage-pool-health-emitter")
threads_before = set(threading.enumerate())


class Injected(RuntimeError):
    pass


INJECTED = Injected(f"injected failure: {CASE}")


def boom(*_args, **_kwargs):
    raise INJECTED


import openminion
import openminion.api.core.bootstrap as bootstrap
import openminion.api.core.infrastructure as infrastructure
import openminion.api.runtime as runtime_module
import openminion.modules.storage.engine as engine_module
import openminion.modules.storage.runtime.context as storage_context
from openminion import APIRuntime
from openminion.modules.llm.providers.factory import RuntimeLLMHandle
from openminion.modules.memory.runtime.assembly import RuntimeMemoryAssembly
from openminion.modules.policy.runtime.service import PolicyCtl
from openminion.modules.retrieve.runtime.retrieve import RetrieveCtl
from openminion.modules.storage.engine import StorageEngine
from openminion.modules.telemetry.service import TelemetryService
from openminion.modules.tool.authoring import ToolAuthoringService
from openminion.services.agent.service import AgentService
from openminion.services.gateway.service import GatewayService
from openminion.services.runtime.manager import AgentRuntimeManager
from openminion.tools.ops.service import OpsService

calls: list[tuple[str, object]] = []


def count(owner, name, label, *, fail=False):
    original = getattr(owner, name)

    def wrapper(self, *args, **kwargs):
        calls.append((label, self))
        result = original(self, *args, **kwargs)
        if fail:
            raise OSError(f"close of {label} failed (injected)")
        return result

    setattr(owner, name, wrapper)


count(storage_context.RuntimeStorageContext, "close", "runtime_storage", fail=CASE == "close-fails")
count(StorageEngine, "close", "storage_engine")
count(TelemetryService, "close_sync", "telemetry_service")
count(PolicyCtl, "close", "action_policy")
count(OpsService, "close", "ops_service")
count(RetrieveCtl, "close", "retrieve_ctl")
count(RuntimeMemoryAssembly, "close", "memory_assembly")
count(ToolAuthoringService, "close", "authored_tools")
count(RuntimeLLMHandle, "close", "llm_runtime")
count(AgentService, "close", "agent")
count(GatewayService, "close", "gateway")
count(APIRuntime, "close", "runtime")
count(AgentRuntimeManager, "shutdown", "runtime_manager")

expected_error = None
if CASE == "memory-provider":
    os.environ["OPENMINION_MEMORY_PROVIDER"] = "not-a-provider"
    expected_error = "ConfigError"
elif CASE == "sessions-table":
    expected_error = "MigrationError"
    if pg_url:
        import sqlalchemy as sa

        engine = sa.create_engine(pg_url)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)"))
        engine.dispose()
    else:
        import sqlite3

        data_root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(data_root / "state.db")
        conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
elif CASE == "engine-emitter":
    engine_module._start_pool_health_emitter = boom
elif CASE == "storage-migration":
    storage_context.migrate_database = boom
    storage_context.migrate_record_store = boom
elif CASE == "extensions":
    infrastructure.LifecycleService.build = boom
elif CASE == "ops":
    infrastructure.configured_ops_service = boom
elif CASE == "support-authoring":
    infrastructure.build_tool_authoring_service = boom
elif CASE == "after-support":
    infrastructure.build_channel_authenticity_policy = boom
elif CASE == "gateway":
    bootstrap.build_gateway_service = boom
elif CASE in ("connection", "close-fails"):
    storage_context.RuntimeStorageContext.connection = property(boom)
elif CASE == "post-init":
    runtime_module.initialize_runtime_components = boom
elif CASE == "runtime-manager":
    bootstrap.build_runtime_manager = boom
elif CASE == "bind":
    APIRuntime._bind_runtime_handle = staticmethod(boom)
elif CASE != "control":
    raise SystemExit(f"unknown case {CASE}")

result: dict[str, object] = {
    "case": CASE,
    "backend": "postgres" if pg_url else "sqlite",
    "openminion": getattr(openminion, "__version__", "?"),
}
runtime = None
try:
    runtime = APIRuntime.from_config_path(
        str(root / "agents.json"), home_root=root, data_root=data_root
    )
    result["start"] = "ok"
except BaseException as exc:  # noqa: BLE001
    result["start"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    if expected_error is not None:
        result["original_exception"] = type(exc).__name__ == expected_error
    else:
        result["original_exception"] = exc is INJECTED
if runtime is not None:
    runtime.close()
    result["closed_runtime"] = True


def snapshot() -> dict[str, object]:
    prefix = os.path.normcase(str(root))
    files = set()
    for item in psutil.Process().open_files():
        path = os.path.normcase(str(Path(item.path).resolve()))
        if path.startswith(prefix):
            files.add(Path(path).relative_to(prefix).as_posix())
    out: dict[str, object] = {
        "open_files": len(files),
        "open_databases": sorted(f for f in files if f.endswith((".db", ".sqlite", ".sqlite3"))),
        "start_threads": sorted(
            t.name for t in threading.enumerate()
            if t not in threads_before and t.is_alive() and t.name.startswith(START_THREADS)
        ),
    }
    if os.name == "nt":
        locked = []
        for path in sorted(p for p in data_root.rglob("*") if p.is_file()):
            moved = path.with_name(path.name + ".moved")
            try:
                os.replace(path, moved)
                os.replace(moved, path)
            except OSError:
                locked.append(path.relative_to(root).as_posix())
        out["not_renamable"] = locked
    if pg_url:
        import sqlalchemy as sa

        engine = sa.create_engine(pg_url)
        with engine.connect() as conn:
            out["pg_other_connections"] = conn.execute(
                sa.text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            ).scalar_one()
        engine.dispose()
    return out


per_object = Counter((label, id(target)) for label, target in calls)
result["closes"] = dict(sorted(Counter(label for label, _ in calls).items()))
result["max_per_object"] = dict(
    sorted(
        (label, max(n for (other, _), n in per_object.items() if other == label))
        for label in {label for label, _ in calls}
    )
)
result["at_exception"] = snapshot()
gc.collect()
result["after_gc"] = snapshot()
print(json.dumps(result, sort_keys=True))
