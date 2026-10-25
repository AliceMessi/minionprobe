"""Consolidation cron run with pending capture intents: does recovery run?"""
import json, logging, os, sys, tempfile, traceback
from pathlib import Path
from types import SimpleNamespace
from openminion import Agent, APIRuntime
from openminion.base.config import ConfigManager, OpenMinionConfig, save_config
from openminion.modules.memory.backends.external import register_reference_sqlite_backend
from openminion.services.runtime.cron.executor import CronTurnExecutor
from tests._csc_fixtures import _csc_install_default_agent

logging.disable(logging.CRITICAL)
register_reference_sqlite_backend()
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    os.environ["OPENMINION_HOME"] = str(tmp / "home")
    os.environ["OPENMINION_DATA_ROOT"] = str(tmp / "home" / "data")
    config = OpenMinionConfig()
    _csc_install_default_agent(config, provider="echo")
    config.runtime.process_mode = "single-process"
    config.runtime.daemon_auto_start = False
    config.storage.path = str(tmp / ".openminion" / "state.db")
    save_config(config, str(tmp / "agents.json"))
    manager = ConfigManager.load(str(tmp / "agents.json"), home_root=tmp, data_root=tmp / ".openminion")
    mem = {"store": {"backend": "sqlite", "sqlite_path": str(tmp / "m.db")},
           "backend": {"provider": "external", "external_adapter": "reference-sqlite", "options": {"db_path": str(tmp / "e.sqlite3")}}}
    manager.register("memory", lambda **kw: mem)
    runtime = APIRuntime.from_manager(manager)
    try:
        with Agent(runtime=runtime) as agent:
            try:
                r = agent.run("hello", session_id="cap"); print("turn:", r.run_state)
            except Exception as exc:
                print("turn raised:", type(exc).__name__)
        print("runtime_manager:", type(getattr(runtime, "runtime_manager", None)).__name__)
        print("has list_registered_agents:", hasattr(runtime, "list_registered_agents"))
        agent_id = next(e.payload["agent_id"] for s in runtime.sessions.list_sessions(limit=10) for e in runtime.sessions.list_events(session_id=s.id, limit=100) if e.event_type == "turn.outcome")
        print("agent_id:", agent_id)
        executor = CronTurnExecutor(runtime=runtime, cron_store=SimpleNamespace(), request_builder=lambda payload, agent: None, timeout_s=30)
        job = {"id": "job-1", "agent_id": agent_id, "payload": {"kind": "agentTurn", "message": "consolidate", "_openminion_memory_consolidation": {"target_scope": f"agent:{agent_id}"}}}
        try:
            out = executor.execute(job, {"id": "run-1"})
            print("execute returned:", json.dumps(out, default=str)[:400])
        except Exception as exc:
            tb = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_tb(exc.__traceback__)][-5:]
            print("execute raised:", type(exc).__name__, str(exc)[:200]); print("  frames:", tb)
        # The same recovery call with the extractor's arguments passed by keyword.
        service = runtime.resolve_agent_service(agent_id)
        def by_keyword(session_api, session_id, root_turn_id, user_message):
            return service.extract_memory_capture_candidates(
                session_api=session_api, session_id=session_id,
                root_turn_id=root_turn_id, user_message=user_message)
        try:
            result = runtime.resolve_memory_assembly(agent_id).recover_pending_captures(
                sessions=runtime.sessions, agent_id=agent_id,
                extract_candidates=by_keyword, authorize=lambda *_: True)
            print("recovery with keyword arguments returned:", result)
        except Exception as exc:
            tb = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in traceback.extract_tb(exc.__traceback__)][-4:]
            print("recovery with keyword arguments raised:", type(exc).__name__, str(exc)[:200]); print("  frames:", tb)
    finally:
        runtime.close()
